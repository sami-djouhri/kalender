import os
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.dashboard import build_dashboard_data
from backend.database import Base, SessionLocal, engine
from backend.models import Calendar, Event, Todo

BERLIN = ZoneInfo('Europe/Berlin')


class DashboardDataTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_build_dashboard_data_summarizes_events_todos_and_agenda(self):
        today = date.today()
        now = datetime.now(BERLIN).replace(microsecond=0)
        event_start = now - timedelta(minutes=10)
        event_end = now + timedelta(hours=1)

        calendar = Calendar(id='calendar-dashboard', name='Termine', color='#3788d8')
        self.db.add(calendar)
        self.db.commit()

        self.db.add_all(
            [
                Event(
                    id='event-current',
                    calendar_id=calendar.id,
                    title='Laufender Termin',
                    start=event_start,
                    end=event_end,
                    all_day=False,
                ),
                Todo(
                    id='todo-overdue',
                    title='Ueberfaellige Aufgabe',
                    priority='dringend',
                    due_date=today - timedelta(days=1),
                    due_time='08:30',
                ),
                Todo(
                    id='todo-today',
                    title='Heutige Aufgabe',
                    priority='hoch',
                    due_date=today,
                    due_time='09:00',
                ),
                Todo(
                    id='todo-floating',
                    title='Freie Aufgabe',
                    priority='niedrig',
                ),
                Todo(
                    id='todo-future',
                    title='Zukunftsaufgabe',
                    priority='dringend',
                    due_date=today + timedelta(days=1),
                ),
                Todo(
                    id='todo-completed',
                    title='Erledigte Aufgabe',
                    priority='hoch',
                    due_date=today,
                    completed=True,
                ),
            ]
        )
        self.db.commit()

        payload = build_dashboard_data(self.db)

        self.assertEqual(payload['termine'][0]['id'], 'event-current')
        self.assertEqual(payload['next_event']['id'], 'event-current')
        self.assertTrue(payload['next_event']['is_now'])
        self.assertEqual([event['id'] for event in payload['upcoming_events']], ['event-current'])

        todos = payload['todos']
        self.assertEqual(todos['total_open'], 3)
        self.assertEqual(todos['overdue_count'], 1)
        self.assertEqual(todos['today_count'], 1)
        self.assertEqual(todos['floating_count'], 1)
        self.assertEqual(todos['important_count'], 2)
        self.assertEqual(todos['overdue'][0]['id'], 'todo-overdue')
        self.assertEqual(todos['today'][0]['id'], 'todo-today')
        self.assertEqual(todos['floating'][0]['id'], 'todo-floating')
        self.assertEqual(
            {todo['id'] for todo in todos['important']},
            {'todo-overdue', 'todo-today'},
        )

        agenda_types = [item['type'] for item in payload['agenda']]
        self.assertIn('event', agenda_types)
        self.assertIn('todo', agenda_types)
        self.assertEqual(payload['agenda'][0]['todo']['id'], 'todo-overdue')


if __name__ == '__main__':
    unittest.main()
