"""Tests für präferenz-bewusste Tagesplanung + Backlog-Verfall (Phase 3 der Kalender-Vision).

Deckt: `_slot_pref_score`/`_slot_reason` (Slot-Scoring), `auto_plan_todos(prefer_time=…)`
(inkl. Alt-Verhalten-Regression), `generic_time_curve` (leer ohne Daten) sowie `list_backlog`
und `escalate_backlog` (automatische Verfall-Erkennung, idempotent pro Tag).
"""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-planbacklog.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, engine, get_db
import backend.tenant  # noqa: F401  (registriert Tenant-Scoping)
from backend import cross_app
from backend.config import settings
from backend.models import Calendar, Todo
from backend.preferences import generic_time_curve
from backend.scheduler import (
    BERLIN,
    _slot_pref_score,
    _slot_reason,
    auto_plan_todos,
    escalate_backlog,
    list_backlog,
)

TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"
FREI_DI = date(2027, 3, 9)  # Dienstag, kein Feiertag/WE → day_type 'frei', voller freier Tag


def _at(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=BERLIN)


class PlanBacklogBaseTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
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
        kw.setdefault("scheduling_mode", "pool")
        t = Todo(title=title, **kw)
        self.db.add(t)
        self.db.commit()
        self.db.refresh(t)
        return t


class SlotScoreTest(PlanBacklogBaseTest):
    def test_hard_todo_prefers_morning(self):
        t = self._todo("Steuererklärung", estimated_minutes=60, energy_required="hoch")
        # Ohne Lern-Daten greift die Heuristik: anspruchsvoll → Vormittag besser als Abend.
        morning = _slot_pref_score(t, _at(FREI_DI, 8), FREI_DI, {})
        evening = _slot_pref_score(t, _at(FREI_DI, 19), FREI_DI, {})
        self.assertGreater(morning, evening)

    def test_light_todo_ok_in_afternoon(self):
        t = self._todo("Ablage sortieren", estimated_minutes=30, energy_required="niedrig")
        afternoon = _slot_pref_score(t, _at(FREI_DI, 15), FREI_DI, {})
        morning = _slot_pref_score(t, _at(FREI_DI, 9), FREI_DI, {})
        self.assertGreaterEqual(afternoon, morning)

    def test_learned_curve_beats_heuristic(self):
        t = self._todo("Konzept schreiben", estimated_minutes=60, energy_required="hoch")
        # Gelernte Kurve sagt: abends bist du top → schlägt den Vormittags-Default.
        curve = {"morgens": -0.5, "abends": 1.2}
        morning = _slot_pref_score(t, _at(FREI_DI, 8), FREI_DI, curve)
        evening = _slot_pref_score(t, _at(FREI_DI, 19), FREI_DI, curve)
        self.assertGreater(evening, morning)

    def test_reason_is_human_readable(self):
        t = self._todo("Steuererklärung", estimated_minutes=60, energy_required="hoch")
        r = _slot_reason(t, _at(FREI_DI, 8), FREI_DI, {})
        self.assertIsInstance(r, str)
        self.assertIn("anspruchsvoll", r)

    def test_overdue_reason(self):
        t = self._todo("Rechnung zahlen", estimated_minutes=20, due_date=FREI_DI - timedelta(days=3))
        r = _slot_reason(t, _at(FREI_DI, 10), FREI_DI, {})
        self.assertIn("überfällig", r)


class AutoPlanPreferTest(PlanBacklogBaseTest):
    def test_prefer_time_attaches_reason(self):
        self._todo("Aufgabe", estimated_minutes=30, energy_required="hoch")
        res = auto_plan_todos(self.db, FREI_DI, prefer_time=True)
        self.assertGreaterEqual(len(res["suggestions"]), 1)
        for s in res["suggestions"]:
            self.assertIsInstance(s["reason"], str)
            self.assertTrue(s["reason"])

    def test_default_has_no_reason_backward_compatible(self):
        # Alt-Verhalten (Sekretär-Pfad): kein prefer_time → reason None, Todo trotzdem geplant.
        t = self._todo("Aufgabe", estimated_minutes=30)
        res = auto_plan_todos(self.db, FREI_DI)
        ids = [s["todo_id"] for s in res["suggestions"]]
        self.assertIn(t.id, ids)
        self.assertIsNone(res["suggestions"][0]["reason"])

    def test_hard_todo_lands_in_morning_slot(self):
        # Ein einzelnes anspruchsvolles Todo an einem leeren Tag → Vormittags-Slot.
        self._todo("Deep Work", estimated_minutes=60, energy_required="hoch")
        res = auto_plan_todos(self.db, FREI_DI, prefer_time=True)
        self.assertEqual(len(res["suggestions"]), 1)
        start = res["suggestions"][0]["start"]
        self.assertLess(start.astimezone(BERLIN).hour, 13)


class GenericCurveTest(PlanBacklogBaseTest):
    def test_empty_without_feedback(self):
        # Ohne ActivityFeedback keine Kurve → Planer fällt auf Heuristik zurück.
        self.assertEqual(generic_time_curve(self.db, FREI_DI), {})


class BacklogTest(PlanBacklogBaseTest):
    def test_list_backlog_threshold_and_completed(self):
        self._todo("A", defer_count=3)
        self._todo("B", defer_count=1)          # unter Schwelle
        self._todo("C", defer_count=5, completed=True, status="done")  # erledigt
        rows = list_backlog(self.db, threshold=3)
        titles = [r["title"] for r in rows]
        self.assertIn("A", titles)
        self.assertNotIn("B", titles)
        self.assertNotIn("C", titles)
        # nach defer_count absteigend
        self.assertEqual(rows[0]["title"], "A")

    def test_escalate_bumps_overdue_and_is_idempotent(self):
        t = self._todo("Alt", due_date=FREI_DI - timedelta(days=2))
        bumped1 = escalate_backlog(self.db, FREI_DI)
        self.db.refresh(t)
        self.assertEqual(bumped1, 1)
        self.assertEqual(t.defer_count, 1)
        self.assertIsNotNone(t.last_deferred_at)
        # Zweiter Lauf am selben realen Tag zählt NICHT doppelt.
        bumped2 = escalate_backlog(self.db, FREI_DI)
        self.db.refresh(t)
        self.assertEqual(bumped2, 0)
        self.assertEqual(t.defer_count, 1)

    def test_escalate_ignores_not_due_and_done(self):
        no_due = self._todo("Ohne Frist")
        future = self._todo("Zukunft", due_date=FREI_DI + timedelta(days=5))
        done = self._todo("Fertig", due_date=FREI_DI - timedelta(days=5), completed=True, status="done")
        bumped = escalate_backlog(self.db, FREI_DI)
        self.assertEqual(bumped, 0)
        for t in (no_due, future, done):
            self.db.refresh(t)
            self.assertEqual(t.defer_count, 0)

    def test_escalate_then_appears_in_backlog_after_threshold(self):
        t = self._todo("Chronisch", defer_count=2, due_date=FREI_DI - timedelta(days=1))
        escalate_backlog(self.db, FREI_DI)  # 2 → 3
        self.db.refresh(t)
        self.assertEqual(t.defer_count, 3)
        rows = list_backlog(self.db, threshold=3)
        self.assertIn("Chronisch", [r["title"] for r in rows])


if __name__ == "__main__":
    unittest.main()
