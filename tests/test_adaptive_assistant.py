"""Tests für die adaptive Sekretär-Schicht (Check-in / Kapazität / Vorschläge / Fortschritt)."""

import os
import unittest
from datetime import date, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-adaptive.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, engine, get_db
import backend.tenant  # noqa: F401  (registriert Tenant-Scoping fuer Stamping und Filter)
from backend import cross_app
from backend.config import settings
from backend.daily_energy import compute_day_capacity, energy_fits, upsert_checkin
from backend.models import Calendar, Habit, HabitSession, Todo
from backend.scheduler import BERLIN, _week_iso_str, auto_plan_todos
from backend.suggestions import build_progress, build_suggestions

TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"

# Zukunftstage ohne Feiertage/Wochenende → day_type 'frei'
FREI_DI = date(2027, 3, 9)   # Dienstag


class AdaptiveBaseTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        # Cross-App im Test aus → keine echten Netz-Calls, deterministisch.
        self._cross_prev = settings.CROSS_APP_ENABLED
        settings.CROSS_APP_ENABLED = False
        cross_app.clear_cache()
        self.db = next(get_db())
        self.db.add(Calendar(id=TERMINE_CAL_ID, name="Termine", is_system=True))
        self.db.commit()

    def tearDown(self):
        settings.CROSS_APP_ENABLED = self._cross_prev
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _todo(self, title, **kw):
        t = Todo(title=title, scheduling_mode="pool", **kw)
        self.db.add(t)
        self.db.commit()
        self.db.refresh(t)
        return t

    def _habit(self, **kw):
        d = dict(name="Gitarre", target_hours_per_week=3.0, session_duration_minutes=60,
                 category="sonstige", active=True)
        d.update(kw)
        h = Habit(**d)
        self.db.add(h)
        self.db.commit()
        self.db.refresh(h)
        return h


class CapacityTest(AdaptiveBaseTest):
    def test_no_checkin_is_normal_and_backward_compatible(self):
        cap = compute_day_capacity(self.db, FREI_DI)
        self.assertFalse(cap["has_checkin"])
        self.assertEqual(cap["factor"], 1.0)
        self.assertEqual(cap["max_task_energy"], "hoch")
        self.assertTrue(cap["allow_physical"])

    def test_low_energy_checkin_dampens(self):
        upsert_checkin(self.db, FREI_DI, energy="niedrig", sleep_quality="schlecht")
        cap = compute_day_capacity(self.db, FREI_DI)
        self.assertTrue(cap["has_checkin"])
        self.assertIn(cap["level"], ("geschont", "erschöpft"))
        self.assertLess(cap["factor"], 1.0)
        self.assertFalse(cap["allow_physical"])
        self.assertIn(cap["max_task_energy"], ("mittel", "niedrig"))

    def test_high_energy_offday_is_charged(self):
        upsert_checkin(self.db, FREI_DI, energy="hoch", sleep_quality="gut")
        cap = compute_day_capacity(self.db, FREI_DI)
        self.assertEqual(cap["level"], "geladen")
        self.assertTrue(cap["prefer_physical"])
        self.assertTrue(cap["allow_physical"])

    def test_physical_block(self):
        upsert_checkin(self.db, FREI_DI, energy="hoch", physical_ready=False)
        cap = compute_day_capacity(self.db, FREI_DI)
        self.assertFalse(cap["allow_physical"])

    def test_energy_fits(self):
        self.assertTrue(energy_fits("niedrig", "mittel"))
        self.assertTrue(energy_fits(None, "niedrig"))
        self.assertFalse(energy_fits("hoch", "mittel"))
        self.assertTrue(energy_fits("hoch", "hoch"))


class EnergyAwarePlanningTest(AdaptiveBaseTest):
    def test_hard_todo_scheduled_without_checkin(self):
        t = self._todo("Keller ausräumen", estimated_minutes=60, energy_required="hoch")
        res = auto_plan_todos(self.db, FREI_DI)
        ids = [s["todo_id"] for s in res["suggestions"]]
        self.assertIn(t.id, ids)
        self.assertEqual(res["deferred"], [])

    def test_hard_todo_deferred_when_exhausted(self):
        t = self._todo("Keller ausräumen", estimated_minutes=60, energy_required="hoch")
        upsert_checkin(self.db, FREI_DI, energy="niedrig", sleep_quality="schlecht")
        res = auto_plan_todos(self.db, FREI_DI)
        ids = [s["todo_id"] for s in res["suggestions"]]
        self.assertNotIn(t.id, ids)
        self.assertTrue(any(d["todo_id"] == t.id and d["reason"] == "energie" for d in res["deferred"]))

    def test_capacity_cap_defers_surplus(self):
        # geschont → cap ~180 Min; 5×60 Min mittel-Energie-Todos passen nicht alle.
        upsert_checkin(self.db, FREI_DI, energy="niedrig")
        for i in range(5):
            self._todo(f"Aufgabe {i}", estimated_minutes=60, energy_required="mittel")
        res = auto_plan_todos(self.db, FREI_DI)
        self.assertLessEqual(res["planned_minutes"], res["capacity"]["cap_minutes"])
        self.assertTrue(any(d["reason"] == "kapazitaet" for d in res["deferred"]))


class ProgressTest(AdaptiveBaseTest):
    def test_progress_basic(self):
        h = self._habit(target_hours_per_week=3.0)
        prog = build_progress(self.db)
        item = next(i for i in prog["habits"] if i["habit_id"] == h.id)
        self.assertEqual(item["target_minutes"], 180)
        self.assertEqual(item["done_minutes"], 0)
        self.assertEqual(item["remaining_minutes"], 180)
        self.assertFalse(item["endangered"])

    def test_progress_endangered_with_minimum(self):
        h = self._habit(weekly_minimum_hours=2.0)
        prog = build_progress(self.db)
        item = next(i for i in prog["habits"] if i["habit_id"] == h.id)
        self.assertTrue(item["endangered"])

    def test_done_minutes_counted(self):
        h = self._habit()
        ws = FREI_DI - timedelta(days=FREI_DI.weekday())
        from datetime import datetime
        s = HabitSession(
            habit_id=h.id,
            start=datetime(ws.year, ws.month, ws.day, 17, 0, tzinfo=BERLIN),
            end=datetime(ws.year, ws.month, ws.day, 18, 0, tzinfo=BERLIN),
            status="completed",
            week_iso=_week_iso_str(ws),
        )
        self.db.add(s)
        self.db.commit()
        prog = build_progress(self.db, ws)
        item = next(i for i in prog["habits"] if i["habit_id"] == h.id)
        self.assertEqual(item["done_minutes"], 60)


class SuggestionsTest(AdaptiveBaseTest):
    def test_structure(self):
        out = build_suggestions(self.db, FREI_DI)
        for key in ("date", "capacity", "free_minutes", "suggestions", "choice", "top"):
            self.assertIn(key, out)
        self.assertIsInstance(out["suggestions"], list)

    def test_sport_when_charged(self):
        upsert_checkin(self.db, FREI_DI, energy="hoch", sleep_quality="gut")
        out = build_suggestions(self.db, FREI_DI)
        kinds = {s["kind"] for s in out["suggestions"]}
        self.assertIn("sport", kinds)

    def test_no_sport_when_exhausted_but_rest_offered(self):
        upsert_checkin(self.db, FREI_DI, energy="niedrig", sleep_quality="schlecht", physical_ready=False)
        out = build_suggestions(self.db, FREI_DI)
        kinds = {s["kind"] for s in out["suggestions"]}
        self.assertNotIn("sport", kinds)
        self.assertIn("erholung", kinds)

    def test_overdue_todo_surfaces(self):
        self._todo("Steuer abgeben", due_date=FREI_DI - timedelta(days=3), estimated_minutes=30)
        out = build_suggestions(self.db, FREI_DI)
        titles = [s["title"] for s in out["suggestions"]]
        self.assertIn("Steuer abgeben", titles)


class CrossAppFailSoftTest(unittest.TestCase):
    def test_gather_signals_disabled(self):
        prev = settings.CROSS_APP_ENABLED
        settings.CROSS_APP_ENABLED = False
        try:
            cross_app.clear_cache()
            sig = cross_app.gather_signals()
            self.assertFalse(sig["available"])
        finally:
            settings.CROSS_APP_ENABLED = prev

    def test_dead_endpoint_is_soft(self):
        prev_en = settings.CROSS_APP_ENABLED
        prev_url = settings.FITNESS_URL
        settings.CROSS_APP_ENABLED = True
        settings.FITNESS_URL = "http://127.0.0.1:9"  # toter Port → connection refused
        try:
            cross_app.clear_cache()
            self.assertIsNone(cross_app.get_muscle_freshness())
            self.assertIsNone(cross_app.days_since_last_workout())
        finally:
            settings.CROSS_APP_ENABLED = prev_en
            settings.FITNESS_URL = prev_url


if __name__ == "__main__":
    unittest.main()
