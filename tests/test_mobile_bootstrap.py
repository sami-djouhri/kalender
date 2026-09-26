import os
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, SessionLocal, engine
from backend.models import Calendar, DailyGoal, DailyReview, Event, Habit, HabitSession, Todo
from backend.routers.mobile import mobile_bootstrap

BERLIN = ZoneInfo('Europe/Berlin')


class MobileBootstrapTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_mobile_bootstrap_returns_calendar_domain_payloads(self):
        day = date(2026, 6, 3)
        start = datetime(2026, 6, 3, 9, 0, tzinfo=BERLIN)
        end = start + timedelta(hours=1)

        calendar = Calendar(id='cal-1', name='Termine', color='#3788d8', is_system=True)
        habit = Habit(
            id='habit-1',
            name='Deep Work',
            color='#4a9eff',
            target_hours_per_week=2,
            session_duration_minutes=60,
        )
        goal = DailyGoal(id='goal-1', title='Tagesfokus', date=day, priority='A')
        review = DailyReview(date=day, what_went_well='gut', energy_level='high')
        self.db.add_all([calendar, habit, goal, review])
        self.db.commit()

        self.db.add_all(
            [
                Event(
                    id='event-1',
                    calendar_id=calendar.id,
                    title='Termin',
                    start=start,
                    end=end,
                    all_day=False,
                ),
                Todo(
                    id='todo-1',
                    title='Aufgabe',
                    priority='hoch',
                    due_date=day,
                    goal_id='goal-1',
                ),
                HabitSession(
                    id='session-1',
                    habit_id=habit.id,
                    start=start + timedelta(hours=2),
                    end=end + timedelta(hours=2),
                    status='pending',
                    week_iso='2026-W23',
                ),
            ]
        )
        self.db.commit()

        payload = mobile_bootstrap(start=day, end=day, db=self.db)

        self.assertEqual(payload['range'], {'start': '2026-06-03', 'end': '2026-06-03'})
        # Einzeltermin: schlichte ID, keine Instanzform, siehe _event_payload.
        self.assertEqual(payload['events'][0]['id'], 'event-1')
        self.assertFalse(payload['events'][0]['is_recurring_instance'])
        self.assertEqual(payload['events'][0]['calendar_name'], 'Termine')
        self.assertEqual(payload['todos'][0]['id'], 'todo-1')
        self.assertEqual(payload['habits'][0]['id'], 'habit-1')
        self.assertEqual(payload['sessions'][0]['id'], 'session-1')
        self.assertEqual(payload['daytypes'], [{'date': '2026-06-03', 'type': 'frei'}])
        self.assertIn('dashboard', payload)
        self.assertIn('ntfy', payload)
        self.assertEqual(payload['todos'][0]['goal_id'], 'goal-1')
        self.assertEqual(len(payload['goals']), 1)
        self.assertEqual(payload['goals'][0]['id'], 'goal-1')
        self.assertEqual(payload['goals'][0]['linked_todo_ids'], ['todo-1'])
        self.assertEqual(len(payload['reviews']), 1)
        self.assertEqual(payload['reviews'][0]['energy_level'], 'high')


    def test_serientermine_erscheinen_im_fenster(self):
        """Eine Serie, deren Basistermin VOR dem Fenster liegt, muss drin sein.

        ★ Genau dieser Fall fehlte und war der Grund, warum der Bootstrap bis
        2026-08-24 fast leer blieb: er filterte die ``events``-Tabelle roh, und
        der Basisdatensatz einer woechentlichen Reihe liegt beim ersten
        Vorkommen, also meist weit vor dem abgefragten Fenster. Der Test
        darueber benutzt einen Einzeltermin im Fenster und war deshalb gruen,
        waehrend live 12 von 12 Terminen fehlten.

        Der Test prueft absichtlich ein Fenster, in dem der Basistermin NICHT
        liegt. Laege er drin, waere er auch mit dem alten, kaputten Code
        gefunden worden, und der Test waere wertlos.
        """
        basis = datetime(2026, 6, 3, 9, 0, tzinfo=BERLIN)  # Mittwoch
        kalender = Calendar(id='cal-1', name='Termine', color='#3788d8', is_system=True)
        self.db.add(kalender)
        self.db.commit()
        self.db.add(
            Event(
                id='serie-1',
                calendar_id=kalender.id,
                title='Woechentlich',
                start=basis,
                end=basis + timedelta(hours=1),
                all_day=False,
                recurrence_rule='RRULE:FREQ=WEEKLY;BYDAY=WE',
            )
        )
        self.db.commit()

        # Drei Wochen spaeter: hier gibt es genau ein Vorkommen (24.06.), aber
        # keinen Basisdatensatz.
        fenster = date(2026, 6, 22)
        payload = mobile_bootstrap(start=fenster, end=fenster + timedelta(days=6), db=self.db)

        titel = [e['title'] for e in payload['events']]
        self.assertEqual(titel, ['Woechentlich'], f"Serie fehlt im Fenster: {payload['events']}")
        instanz = payload['events'][0]
        self.assertEqual(instanz['series_id'], 'serie-1')
        self.assertTrue(instanz['is_recurring_instance'])
        self.assertTrue(instanz['id'].startswith('serie-1::'))
        self.assertEqual(instanz['calendar_name'], 'Termine')
        self.assertTrue(instanz['start'].startswith('2026-06-24'), instanz['start'])

    def test_ausgenommener_serientag_fehlt(self):
        """Ein per EXDATE abgesagtes Vorkommen darf nicht auftauchen.

        Sonst haette man die Serien zwar sichtbar gemacht, aber die Absagen
        verloren, eine Ansicht, die mehr behauptet als der Kalender weiss.
        """
        basis = datetime(2026, 6, 3, 9, 0, tzinfo=BERLIN)
        kalender = Calendar(id='cal-1', name='Termine', color='#3788d8', is_system=True)
        self.db.add(kalender)
        self.db.commit()
        self.db.add(
            Event(
                id='serie-2',
                calendar_id=kalender.id,
                title='Woechentlich',
                start=basis,
                end=basis + timedelta(hours=1),
                all_day=False,
                recurrence_rule='RRULE:FREQ=WEEKLY;BYDAY=WE',
                recurrence_exdates='2026-06-24',
            )
        )
        self.db.commit()

        fenster = date(2026, 6, 22)
        payload = mobile_bootstrap(start=fenster, end=fenster + timedelta(days=6), db=self.db)
        self.assertEqual(payload['events'], [])

    def test_mobile_bootstrap_route_requires_authenticated_user(self):
        from fastapi.routing import APIRoute

        from backend.main import app, get_current_user

        routes = [
            route
            for route in app.routes
            if isinstance(route, APIRoute) and route.path == "/api/mobile/bootstrap"
        ]

        self.assertEqual(len(routes), 1)
        self.assertIn(get_current_user, [dep.dependency for dep in routes[0].dependencies])
        self.assertIn(get_current_user, [dep.call for dep in routes[0].dependant.dependencies])


if __name__ == "__main__":
    unittest.main()
