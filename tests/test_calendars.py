"""Kalender-Container: System-Satz anlegen, aber nichts Fremdes wegraeumen.

Hintergrund: `_ensure_default_calendar()` hat bis 2026-08 bei JEDEM Start alle
nicht-System-Kalender geloescht und deren Termine in den GETEILTEN Kalender
'Termine' geschoben: ungescoped ueber alle Mandanten. Solange es nur den Owner
gab, traf das nichts; mit mehreren Nutzern waere es stiller Datenverlust plus ein
Sichtbarkeits-Leck gewesen. Dieser Test haelt die Entschaerfung fest.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-calendars.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from sqlalchemy import text  # noqa: E402

from backend.database import Base, SessionLocal, engine  # noqa: E402
from backend.main import _ensure_default_calendar  # noqa: E402
from backend.models import Calendar, Event  # noqa: E402
from backend.system_calendars import TERMINE_CAL_ID  # noqa: E402

FREMDER_SUB = "fremder-mandant-0001"


class EnsureDefaultCalendarTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def test_legt_termine_kalender_an(self):
        _ensure_default_calendar()
        cal = self.db.query(Calendar).filter(Calendar.id == TERMINE_CAL_ID).one_or_none()
        self.assertIsNotNone(cal)
        self.assertTrue(cal.is_system)

    def test_ist_idempotent(self):
        _ensure_default_calendar()
        _ensure_default_calendar()
        anzahl = self.db.query(Calendar).filter(Calendar.id == TERMINE_CAL_ID).count()
        self.assertEqual(anzahl, 1)

    def test_laesst_fremden_kalender_samt_terminen_stehen(self):
        """Der eigentliche Regressionstest: ein Boot darf fremde Daten nicht anfassen."""
        self.db.add(Calendar(
            id="cal-fremd-0001",
            name="Sport",
            color="#ff0000",
            is_system=False,
            owner_sub=FREMDER_SUB,
        ))
        start = datetime.now(timezone.utc) + timedelta(days=1)
        self.db.add(Event(
            calendar_id="cal-fremd-0001",
            title="Laufrunde",
            start=start,
            end=start + timedelta(hours=1),
            all_day=False,
            owner_sub=FREMDER_SUB,
        ))
        self.db.commit()

        _ensure_default_calendar()

        # Bewusst rohes SQL statt ORM: eine normale Session ist auf den Owner
        # gescoped und wuerde die Zeilen eines fremden Mandanten gar nicht sehen:
        # der Test wuerde dann "geloescht" melden, obwohl nur gefiltert wurde.
        with engine.connect() as conn:
            cal = conn.execute(
                text("SELECT id FROM calendars WHERE id = :i"), {"i": "cal-fremd-0001"}
            ).fetchone()
            self.assertIsNotNone(cal, "fremder Kalender wurde beim Start geloescht")

            ev = conn.execute(
                text("SELECT calendar_id FROM events WHERE title = :t"), {"t": "Laufrunde"}
            ).fetchone()
            self.assertIsNotNone(ev, "fremder Termin ist verschwunden")
            self.assertEqual(
                ev[0], "cal-fremd-0001",
                "fremder Termin wurde in den geteilten 'Termine'-Kalender verschoben",
            )


if __name__ == "__main__":
    unittest.main()
