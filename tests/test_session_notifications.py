import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, SessionLocal, engine
from backend.config import settings
from backend.models import Habit, HabitSession, SessionNotification
from backend.session_notifications import (
    collect_due_session_notifications,
    process_session_notifications,
    send_ntfy_notification,
)

BERLIN = ZoneInfo('Europe/Berlin')


class SessionNotificationTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _habit(self, habit_id='habit-1', active=True):
        habit = Habit(
            id=habit_id,
            name='Deep Work',
            color='#4a9eff',
            active=active,
            target_hours_per_week=2,
            session_duration_minutes=60,
        )
        self.db.add(habit)
        self.db.commit()
        return habit

    def _session(self, habit, session_id, start, end, status='pending'):
        session = HabitSession(
            id=session_id,
            habit_id=habit.id,
            start=start,
            end=end,
            status=status,
            week_iso='2026-W23',
        )
        self.db.add(session)
        self.db.commit()
        return session

    def test_process_session_notifications_dedupes_same_session_kind(self):
        now = datetime(2026, 6, 3, 10, 0, tzinfo=BERLIN)
        habit = self._habit()
        self._session(habit, 'session-1', now + timedelta(minutes=5), now + timedelta(hours=1))
        payloads = []
        sender = lambda payload: payloads.append(payload) or True

        with patch.object(settings, 'NTFY_URL', 'http://ntfy.local'),              patch.object(settings, 'NTFY_TOPIC', 'kalender-test'),              patch.object(settings, 'SESSION_NOTIFY_LEAD_MINUTES_START', 10),              patch.object(settings, 'SESSION_NOTIFY_LEAD_MINUTES_END', 5):
            self.assertEqual(process_session_notifications(self.db, sender=sender, now=now), 1)
            self.assertEqual(process_session_notifications(self.db, sender=sender, now=now), 0)

        self.assertEqual(len(payloads), 1)
        self.assertEqual(self.db.query(SessionNotification).count(), 1)

    def test_collect_due_session_notifications_filters_status_and_windows(self):
        now = datetime(2026, 6, 3, 10, 0, tzinfo=BERLIN)
        habit = self._habit()
        self._session(habit, 'start-due', now + timedelta(minutes=5), now + timedelta(hours=1))
        self._session(habit, 'start-now', now - timedelta(minutes=1), now + timedelta(minutes=30))
        self._session(habit, 'end-due', now - timedelta(hours=1), now + timedelta(minutes=4), status='accepted')
        self._session(habit, 'too-early', now + timedelta(minutes=30), now + timedelta(hours=2))
        self._session(habit, 'dismissed', now + timedelta(minutes=5), now + timedelta(hours=1), status='dismissed')
        self._session(habit, 'completed', now - timedelta(hours=2), now - timedelta(hours=1), status='completed')

        with patch.object(settings, 'SESSION_NOTIFY_LEAD_MINUTES_START', 10),              patch.object(settings, 'SESSION_NOTIFY_LEAD_MINUTES_END', 5):
            candidates = collect_due_session_notifications(self.db, now=now)

        self.assertEqual(
            {(candidate.session.id, candidate.kind) for candidate in candidates},
            {
                ('start-due', 'start_due'),
                ('start-now', 'start_now'),
                ('end-due', 'end_due'),
            },
        )

    def test_send_ntfy_notification_posts_headers_and_body(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout):
            captured['url'] = req.full_url
            captured['timeout'] = timeout
            captured['body'] = req.data.decode('utf-8')
            captured['title'] = req.get_header('Title')
            captured['priority'] = req.get_header('Priority')
            captured['tags'] = req.get_header('Tags')
            captured['auth'] = req.get_header('Authorization')
            return Response()

        with patch.object(settings, 'NTFY_URL', 'http://ntfy.local'),              patch.object(settings, 'NTFY_TOPIC', 'kalender sessions'),              patch.object(settings, 'NTFY_TOKEN', 'secret-token'),              patch('backend.session_notifications.request.urlopen', fake_urlopen):
            ok = send_ntfy_notification(
                {
                    'title': 'Habit startet bald',
                    'message': 'Deep Work startet um 10:05.',
                    'priority': 'high',
                    'tags': 'calendar',
                }
            )

        self.assertTrue(ok)
        self.assertEqual(
            captured,
            {
                'url': 'http://ntfy.local/kalender%20sessions',
                'timeout': 8,
                'body': 'Deep Work startet um 10:05.',
                'title': 'Habit startet bald',
                'priority': 'high',
                'tags': 'calendar',
                'auth': 'Bearer secret-token',
            },
        )


if __name__ == '__main__':
    unittest.main()
