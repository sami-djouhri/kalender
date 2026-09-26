"""Habit-Sessions gegen die Wanduhr-Konvention (backend/wanduhr.py).

Drei Endpunkte verglichen bis 2026-09-03 UTC-Zeiten mit den zonenlos als
Berliner Wanduhr gespeicherten Session-Zeiten; SQLite verwirft die Zone des
Vergleichswerts, die Fenster lagen damit um den UTC-Versatz daneben:

* `/sessions/upcoming` fand als Notification-Poller nie die richtigen Sessions.
* `/sessions/today` verlor Sessions zwischen 22:00 und 24:00.
* `start_early`/`cancelled` schrieben eine um den Versatz falsche Uhrzeit.

Dazu der neue Parameter: `/sessions/upcoming?days=N` liefert die
pending-Sessions der naechsten N Tage. Saganta (BFF und Oberflaeche) schickte
`days=7` von Anfang an, bekam aber bis zur Einfuehrung des Parameters das
10-Minuten-Fenster und zeigte deshalb fast immer eine leere Vorschau.
"""
import os
import unittest
from datetime import datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-upcoming.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.config import settings
from backend.database import Base, SessionLocal, engine
from backend.main import app, create_jwt_token
from backend.models import Habit, HabitSession
from backend.wanduhr import jetzt


class HabitSessionWanduhrTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.h = {"Authorization": f"Bearer {create_jwt_token()}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def setUp(self):
        db = SessionLocal()
        # Die API liest mit DEFAULT_OWNER_SUB; direkt geschriebene Testdaten
        # muessen demselben Mandanten gehoeren, sonst prueft der Test Leere.
        db.info["owner_sub"] = settings.DEFAULT_OWNER_SUB
        db.query(HabitSession).delete()
        db.query(Habit).delete()
        db.add(Habit(
            id='habit-up',
            name='Lesen',
            color='#4a9eff',
            active=True,
            target_hours_per_week=2,
            session_duration_minutes=60,
        ))
        db.commit()
        db.close()

    def _sessions_anlegen(self, eintraege):
        db = SessionLocal()
        db.info["owner_sub"] = settings.DEFAULT_OWNER_SUB
        for sid, start, ende, status in eintraege:
            db.add(HabitSession(
                id=sid,
                habit_id='habit-up',
                start=start,
                end=ende,
                status=status,
                week_iso='2026-W36',
            ))
        db.commit()
        db.close()

    def _ids(self, pfad, params=None):
        r = self.client.get(pfad, params=params or {}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return [s['id'] for s in r.json()]

    # --- /sessions/upcoming -----------------------------------------------

    def _upcoming_daten(self):
        now = jetzt()
        self._sessions_anlegen([
            ('gleich', now + timedelta(minutes=5), now + timedelta(minutes=65), 'pending'),
            ('morgen', now + timedelta(days=1), now + timedelta(days=1, hours=1), 'pending'),
            ('in-sechs-tagen', now + timedelta(days=6), now + timedelta(days=6, hours=1), 'pending'),
            ('in-zwanzig-tagen', now + timedelta(days=20), now + timedelta(days=20, hours=1), 'pending'),
            ('morgen-dismissed', now + timedelta(days=1, hours=2), now + timedelta(days=1, hours=3), 'dismissed'),
        ])

    def test_ohne_days_bleibt_das_10_minuten_fenster(self):
        self._upcoming_daten()
        self.assertEqual(self._ids('/api/habits/sessions/upcoming'), ['gleich'])

    def test_mit_days_kommen_die_naechsten_tage(self):
        self._upcoming_daten()
        self.assertEqual(self._ids('/api/habits/sessions/upcoming', {'days': 7}),
                         ['gleich', 'morgen', 'in-sechs-tagen'])

    def test_days_ausserhalb_der_grenzen_gibt_422(self):
        for wert in (0, 40):
            r = self.client.get('/api/habits/sessions/upcoming',
                                params={'days': wert}, headers=self.h)
            self.assertEqual(r.status_code, 422, r.text)

    # --- /sessions/today ----------------------------------------------------

    def test_today_behaelt_die_spaete_abendsession(self):
        """Vor dem Wanduhr-Fix endete das SQL-Tagesfenster um 22:00 Wanduhr."""
        heute = jetzt().replace(hour=0, minute=0, second=0, microsecond=0)
        self._sessions_anlegen([
            ('spaeter-abend', heute + timedelta(hours=23),
             heute + timedelta(days=1), 'pending'),
        ])
        self.assertIn('spaeter-abend', self._ids('/api/habits/sessions/today'))

    # --- start_early schreibt Wanduhr ---------------------------------------

    def test_start_early_speichert_wanduhrzeit(self):
        now = jetzt()
        self._sessions_anlegen([
            ('frueh-starten', now + timedelta(hours=3),
             now + timedelta(hours=4), 'pending'),
        ])
        r = self.client.post('/api/habits/sessions/frueh-starten/action',
                             json={'action': 'start_early'}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)

        db = SessionLocal()
        db.info["owner_sub"] = settings.DEFAULT_OWNER_SUB
        gespeichert = db.query(HabitSession).filter(
            HabitSession.id == 'frueh-starten').one().start
        db.close()
        # Vor dem Fix lag der Wert um den UTC-Versatz (1 bis 2 Stunden) daneben
        abstand = abs((gespeichert - jetzt()).total_seconds())
        self.assertLess(abstand, 300, f"start={gespeichert} weicht {abstand}s ab")


if __name__ == '__main__':
    unittest.main()
