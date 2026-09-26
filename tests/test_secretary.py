"""Tests für den automatischen Sekretär (backend/secretary.py).

Deckt ab: Konflikt-Erkennung (Doppelbuchung/Überfällig/Überlast), Briefing-Aufbau,
Konfiguration, sowie den Orchestrator-Tick (Idempotenz + Tenant-Scoping + Push nur
an den Owner). unittest-Style (Projekt nutzt `python -m unittest discover -s tests`).
"""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/kalender-unittest-secretary.db')
os.environ.setdefault('KALENDER_PASSWORD', 'test-password')
os.environ.setdefault('SECRET_KEY', 'test-secret')

import backend.tenant  # noqa: F401  (registriert Tenant-Scoping global)
from backend.config import settings as app_settings
from backend.database import Base, SessionLocal, engine
from backend.models import Calendar, Contact, DailyGoal, Event, SecretaryRun, Todo
from backend import secretary

BERLIN = secretary.BERLIN

# ★ Eigener Mandant statt der konfigurierten Vorbelegung. Bis zum 2026-09-06
# stand hier `app_settings.DEFAULT_OWNER_SUB`, und weil der Quelltext damals die
# Kennung eines konkreten Menschen als Vorbelegung trug, war das nie leer. Seit
# die Kennung Konfiguration ist, ist die Vorbelegung leer. Der Tick stempelte
# dann `owner_sub = ""`, `tenants_with_data` filtert leere Werte heraus, und der
# Test scheiterte an `planned_tenants == 0`. Ein Test soll nicht davon abhaengen,
# was auf dem Wirt konfiguriert ist, auf dem er zufaellig laeuft.
OWNER = "secretary-test-owner"
CAL_ID = "cal-secretary-test-0000"


def _dt(day, h, m=0):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=BERLIN)


def _scoped(sub):
    db = SessionLocal()
    db.info["owner_sub"] = sub
    return db


class SecretaryTestBase(unittest.TestCase):
    def setUp(self):
        # ⚠️ Nur fuer die Dauer dieses Tests, nicht beim Import. Die Einstellung
        # ist prozessweit, und unittest laedt alle Testmodule in denselben
        # Prozess: eine Zuweisung auf Modulebene traf test_account mit, das
        # seinen eigenen OWNER ebenfalls beim Import aus dieser Einstellung
        # liest. Wer zuerst importiert wird, haette dann gewonnen, und die
        # Reihenfolge kommt aus der alphabetischen Suche.
        self._owner_vorher = app_settings.DEFAULT_OWNER_SUB
        app_settings.DEFAULT_OWNER_SUB = OWNER
        self.addCleanup(
            setattr, app_settings, "DEFAULT_OWNER_SUB", self._owner_vorher
        )
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        # Ein Kalender für den Owner (Events brauchen FK)
        db = _scoped(OWNER)
        try:
            db.add(Calendar(id=CAL_ID, name="Test", color="#3788d8"))
            db.commit()
        finally:
            db.close()
        self.today = date(2026, 7, 13)  # Montag

    def _event(self, db, title, h1, m1, h2, m2, all_day=False):
        db.add(Event(
            calendar_id=CAL_ID, title=title,
            start=_dt(self.today, h1, m1), end=_dt(self.today, h2, m2),
            all_day=all_day,
        ))

    def _pool_todo(self, db, title, minutes, due=None):
        db.add(Todo(
            title=title, scheduling_mode="pool", estimated_minutes=minutes,
            due_date=due, status="open", completed=False,
        ))


class ConflictTest(SecretaryTestBase):
    def test_doppelbuchung_detected(self):
        db = _scoped(OWNER)
        try:
            self._event(db, "Meeting A", 10, 0, 11, 0)
            self._event(db, "Meeting B", 10, 30, 11, 30)
            db.commit()
            conflicts = secretary.detect_day_conflicts(db, self.today)
        finally:
            db.close()
        types = [c["type"] for c in conflicts]
        self.assertIn("doppelbuchung", types)

    def test_no_conflict_for_adjacent_events(self):
        db = _scoped(OWNER)
        try:
            self._event(db, "A", 10, 0, 11, 0)
            self._event(db, "B", 11, 0, 12, 0)  # grenzt an, überlappt nicht
            db.commit()
            conflicts = secretary.detect_day_conflicts(db, self.today)
        finally:
            db.close()
        self.assertNotIn("doppelbuchung", [c["type"] for c in conflicts])

    def test_overdue_detected(self):
        db = _scoped(OWNER)
        try:
            self._pool_todo(db, "Alt", 30, due=self.today - timedelta(days=2))
            db.commit()
            conflicts = secretary.detect_day_conflicts(db, self.today)
        finally:
            db.close()
        self.assertIn("ueberfaellig", [c["type"] for c in conflicts])

    def test_overload_when_due_today_exceeds_free(self):
        db = _scoped(OWNER)
        try:
            # Tag fast komplett mit Terminen zubauen, dann 4h Aufgaben fällig
            self._event(db, "Ganztag-Block", 8, 0, 21, 30)
            self._pool_todo(db, "Groß", 240, due=self.today)
            db.commit()
            conflicts = secretary.detect_day_conflicts(db, self.today)
        finally:
            db.close()
        self.assertIn("ueberlast", [c["type"] for c in conflicts])


class BriefingTest(SecretaryTestBase):
    def test_briefing_structure_and_birthday(self):
        db = _scoped(OWNER)
        try:
            self._event(db, "Zahnarzt", 15, 0, 16, 0)
            db.add(DailyGoal(title="Steuer machen", date=self.today, priority="A", status="planned"))
            db.add(Contact(name="Oma", birthday=date(1950, 7, 13)))
            db.commit()
            briefing = secretary.build_briefing(db, self.today)
        finally:
            db.close()
        self.assertEqual(briefing["termine_count"], 1)
        self.assertIsNotNone(briefing["next_event"])
        self.assertEqual(briefing["focus_goal"]["title"], "Steuer machen")
        self.assertEqual(len(briefing["birthdays"]), 1)
        text = secretary.briefing_to_text(briefing)
        self.assertIn("Zahnarzt", text)
        self.assertIn("Steuer machen", text)

    def test_empty_briefing_text(self):
        db = _scoped(OWNER)
        try:
            briefing = secretary.build_briefing(db, self.today)
        finally:
            db.close()
        self.assertEqual(briefing["termine_count"], 0)
        self.assertIsInstance(secretary.briefing_to_text(briefing), str)


class ConfigTest(SecretaryTestBase):
    def test_defaults_and_set(self):
        db = _scoped(OWNER)
        try:
            cfg = secretary.get_config(db)
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["morning_hour"], secretary.DEFAULT_MORNING_HOUR)
            cfg2 = secretary.set_config(db, enabled=False, morning_hour=7)
            self.assertFalse(cfg2["enabled"])
            self.assertEqual(cfg2["morning_hour"], 7)
        finally:
            db.close()


class TickTest(SecretaryTestBase):
    def _seed_planable(self, sub):
        db = _scoped(sub)
        try:
            self._pool_todo(db, f"Aufgabe {sub}", 30, due=self.today)
            db.commit()
        finally:
            db.close()

    def test_tick_plans_and_is_idempotent(self):
        self._seed_planable(OWNER)
        morning = _dt(self.today, 7, 0)
        sent = []

        res1 = secretary.run_secretary_tick(now=morning, sender=lambda p: sent.append(p) or True)
        self.assertGreaterEqual(res1["planned_tenants"], 1)
        self.assertTrue(res1["briefing_sent"])

        # Owner-Todo ist jetzt verbindlich geplant
        db = _scoped(OWNER)
        try:
            todo = db.query(Todo).first()
            self.assertEqual(todo.scheduling_mode, "planned_day")
            self.assertIsNotNone(todo.scheduled_start)
            markers = db.query(SecretaryRun).filter(SecretaryRun.kind == "planning").count()
            self.assertEqual(markers, 1)
        finally:
            db.close()

        # Zweiter Tick am selben Tag: nichts Neues geplant, kein zweites Briefing
        sent.clear()
        res2 = secretary.run_secretary_tick(now=morning, sender=lambda p: sent.append(p) or True)
        self.assertEqual(res2["planned_tenants"], 0)
        self.assertFalse(res2["briefing_sent"])

    def test_tick_scopes_per_tenant(self):
        self._seed_planable(OWNER)
        self._seed_planable("tenant-B")
        morning = _dt(self.today, 7, 0)
        secretary.run_secretary_tick(now=morning, sender=lambda p: True)

        for sub in (OWNER, "tenant-B"):
            db = _scoped(sub)
            try:
                todos = db.query(Todo).all()
                self.assertEqual(len(todos), 1, f"{sub} sieht nur eigene Todos")
                self.assertEqual(todos[0].scheduling_mode, "planned_day")
            finally:
                db.close()

    def test_disabled_tick_does_nothing(self):
        db = _scoped(OWNER)
        try:
            secretary.set_config(db, enabled=False)
        finally:
            db.close()
        self._seed_planable(OWNER)
        res = secretary.run_secretary_tick(now=_dt(self.today, 7, 0), sender=lambda p: True)
        self.assertEqual(res.get("skipped"), "disabled")

    def test_no_briefing_before_morning_hour(self):
        self._seed_planable(OWNER)
        res = secretary.run_secretary_tick(now=_dt(self.today, 3, 0), sender=lambda p: True)
        self.assertEqual(res["planned_tenants"], 0)
        self.assertFalse(res["briefing_sent"])


if __name__ == "__main__":
    unittest.main()
