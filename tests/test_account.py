"""Tests für DSGVO-Funktionen (backend/account.py): Export, Löschung, Retention.

Kritisch: Löschung darf NUR die eigenen Daten treffen, nicht System-Kalender
(owner_sub NULL) und nicht fremde Tenants. unittest-Style.
"""

import os
import unittest
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/kalender-unittest-account.db')
os.environ.setdefault('KALENDER_PASSWORD', 'test-password')
os.environ.setdefault('SECRET_KEY', 'test-secret')

import backend.tenant  # noqa: F401  (registriert Tenant-Scoping global)
from backend.account import (
    account_summary,
    delete_account_data,
    export_account_data,
    purge_old_data,
)
from backend.database import Base, SessionLocal, engine
from backend.models import (
    Calendar,
    Contact,
    Event,
    HabitSession,
    SecretaryRun,
    Todo,
)

# ★ Eigener Mandant statt der konfigurierten Vorbelegung. Bis zum 2026-09-06
# stand hier `app_settings.DEFAULT_OWNER_SUB`, und weil der Quelltext damals die
# Kennung eines konkreten Menschen als Vorbelegung trug, war das eine echte
# Kennung. Seit sie Konfiguration ist, waere es auf einem unkonfigurierten Wirt
# der leere String, und diese Tests haetten Mandanten-Isolation ausgerechnet
# gegen den Wert geprueft, der "nicht konfiguriert" bedeutet. Sie waeren gruen
# geblieben, nur ohne Aussage. Testdaten gehoeren in den Test.
OWNER = "account-test-owner"
SYS_CAL = "system-termine-0000-0000-000000000000"


def _scoped(sub):
    db = SessionLocal()
    db.info["owner_sub"] = sub
    return db


def _system():
    db = SessionLocal()
    db.info["owner_sub"] = None
    return db


class AccountTestBase(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        # System-Kalender (owner_sub NULL) über System-Session
        db = _system()
        try:
            db.add(Calendar(id=SYS_CAL, name="Termine", color="#3788d8", is_system=True))
            db.commit()
        finally:
            db.close()

    def _seed_tenant(self, sub):
        db = _scoped(sub)
        try:
            cal = Calendar(name=f"Kal {sub}", color="#111111")
            db.add(cal)
            db.flush()
            db.add(Event(calendar_id=cal.id, title=f"Termin {sub}",
                         start=datetime(2026, 7, 13, 10, tzinfo=timezone.utc),
                         end=datetime(2026, 7, 13, 11, tzinfo=timezone.utc)))
            db.add(Contact(name=f"Kontakt {sub}"))
            db.add(Todo(title=f"Aufgabe {sub}"))
            db.commit()
        finally:
            db.close()


class ExportTest(AccountTestBase):
    def test_export_contains_own_data_only(self):
        self._seed_tenant(OWNER)
        self._seed_tenant("tenant-B")
        db = _scoped(OWNER)
        try:
            data = export_account_data(db, OWNER)
        finally:
            db.close()
        self.assertEqual(len(data["contacts"]), 1)
        self.assertEqual(data["contacts"][0]["name"], f"Kontakt {OWNER}")
        # System-Kalender (NULL) NICHT im Export
        cal_ids = [c["id"] for c in data["calendars"]]
        self.assertNotIn(SYS_CAL, cal_ids)
        self.assertEqual(len(data["calendars"]), 1)

    def test_summary_counts(self):
        self._seed_tenant(OWNER)
        db = _scoped(OWNER)
        try:
            s = account_summary(db, OWNER)
        finally:
            db.close()
        self.assertEqual(s["contacts"], 1)
        self.assertEqual(s["events"], 1)
        self.assertEqual(s["todos"], 1)


class DeleteTest(AccountTestBase):
    def test_delete_removes_own_keeps_system_and_others(self):
        self._seed_tenant(OWNER)
        self._seed_tenant("tenant-B")
        db = _scoped(OWNER)
        try:
            counts = delete_account_data(db, OWNER)
        finally:
            db.close()
        self.assertGreaterEqual(counts["contacts"], 1)

        # Owner-Daten weg
        db = _scoped(OWNER)
        try:
            self.assertEqual(db.query(Contact).count(), 0)
            self.assertEqual(db.query(Todo).count(), 0)
        finally:
            db.close()

        # System-Kalender bleibt
        db = _system()
        try:
            self.assertIsNotNone(db.query(Calendar).filter(Calendar.id == SYS_CAL).first())
        finally:
            db.close()

        # tenant-B unberührt
        db = _scoped("tenant-B")
        try:
            self.assertEqual(db.query(Contact).count(), 1)
            self.assertEqual(db.query(Todo).count(), 1)
            self.assertEqual(db.query(Event).count(), 1)
        finally:
            db.close()


class PurgeTest(AccountTestBase):
    def test_purge_removes_old_keeps_fresh(self):
        now = datetime.now(timezone.utc)
        db = _scoped(OWNER)
        try:
            from backend.models import Habit
            habit = Habit(name="Sport", target_hours_per_week=3.0)
            db.add(habit)
            db.flush()
            hid = habit.id
            # alte verworfene Session (100 Tage) + frische verworfene (10 Tage)
            db.add(HabitSession(habit_id=hid, start=now - timedelta(days=101),
                                end=now - timedelta(days=100), status="dismissed", week_iso="2026-W10"))
            db.add(HabitSession(habit_id=hid, start=now - timedelta(days=11),
                                end=now - timedelta(days=10), status="dismissed", week_iso="2026-W20"))
            # alter Sekretär-Marker (100 Tage) + frischer
            db.add(SecretaryRun(owner_sub=OWNER, run_date=date.today() - timedelta(days=100), kind="planning"))
            db.add(SecretaryRun(owner_sub=OWNER, run_date=date.today() - timedelta(days=1), kind="planning"))
            db.commit()
        finally:
            db.close()

        counts = purge_old_data()
        self.assertEqual(counts["habit_sessions"], 1)
        self.assertEqual(counts["secretary_runs"], 1)

        db = _system()
        try:
            self.assertEqual(db.query(HabitSession).count(), 1)  # frische bleibt
            self.assertEqual(db.query(SecretaryRun).count(), 1)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
