import os
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-evtremind.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, engine, get_db
from backend.models import Calendar, Event, EventReminderLog
from backend import config
from backend.session_notifications import process_event_reminders

BERLIN = ZoneInfo("Europe/Berlin")
TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"


class EventReminderTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = next(get_db())
        self.db.add(Calendar(id=TERMINE_CAL_ID, name="Termine", is_system=True))
        self.db.commit()
        # ntfy für den Test scheinbar konfiguriert
        config.settings.NTFY_URL = "http://ntfy.example"
        config.settings.NTFY_TOPIC = "kalender-test"
        self.sent = []

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        config.settings.NTFY_URL = ""
        config.settings.NTFY_TOPIC = ""

    def _fake_sender(self, payload):
        self.sent.append(payload)
        return True

    def _event(self, start, reminder, rule=None):
        ev = Event(calendar_id=TERMINE_CAL_ID, title="Meeting", start=start,
                   end=start + timedelta(hours=1), reminder_minutes=reminder, recurrence_rule=rule)
        self.db.add(ev)
        self.db.commit()
        self.db.refresh(ev)
        return ev

    def test_reminder_fires_within_window(self):
        now = datetime(2026, 9, 7, 8, 50, tzinfo=BERLIN)
        self._event(datetime(2026, 9, 7, 9, 0, tzinfo=BERLIN), reminder=10)  # fire_at 8:50
        n = process_event_reminders(self.db, sender=self._fake_sender, now=now)
        self.assertEqual(n, 1)
        self.assertIn("Meeting", self.sent[0]["message"])

    def test_reminder_not_yet_due(self):
        now = datetime(2026, 9, 7, 8, 30, tzinfo=BERLIN)
        self._event(datetime(2026, 9, 7, 9, 0, tzinfo=BERLIN), reminder=10)  # fire_at 8:50
        n = process_event_reminders(self.db, sender=self._fake_sender, now=now)
        self.assertEqual(n, 0)

    def test_reminder_fires_once(self):
        now = datetime(2026, 9, 7, 8, 55, tzinfo=BERLIN)
        self._event(datetime(2026, 9, 7, 9, 0, tzinfo=BERLIN), reminder=10)
        process_event_reminders(self.db, sender=self._fake_sender, now=now)
        n2 = process_event_reminders(self.db, sender=self._fake_sender, now=now)
        self.assertEqual(n2, 0)
        self.assertEqual(self.db.query(EventReminderLog).count(), 1)

    def test_recurring_event_skipped_v1(self):
        now = datetime(2026, 9, 7, 8, 55, tzinfo=BERLIN)
        self._event(datetime(2026, 9, 7, 9, 0, tzinfo=BERLIN), reminder=10, rule="FREQ=WEEKLY")
        n = process_event_reminders(self.db, sender=self._fake_sender, now=now)
        self.assertEqual(n, 0)

    def test_no_reminder_field_ignored(self):
        now = datetime(2026, 9, 7, 8, 55, tzinfo=BERLIN)
        self._event(datetime(2026, 9, 7, 9, 0, tzinfo=BERLIN), reminder=None)
        n = process_event_reminders(self.db, sender=self._fake_sender, now=now)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
