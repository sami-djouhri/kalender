"""Tests für Quick-Capture (backend/capture.py): regelbasiertes deutsches Parsing.

unittest-Style. Deckt Zeit-/Datums-/Dauer-Erkennung, Typ-Heuristik, Titel-Cleaning
und den Commit-Pfad ab. Der LLM-Fallback wird nur strukturell geprüft (kein Call).
"""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/kalender-unittest-capture.db')
os.environ.setdefault('KALENDER_PASSWORD', 'test-password')
os.environ.setdefault('SECRET_KEY', 'test-secret')

from backend.capture import BERLIN, Capture, capture, parse_capture

# Fixer Bezugspunkt: Montag, 2026-07-13, 09:00 Berlin
NOW = datetime(2026, 7, 13, 9, 0, tzinfo=BERLIN)


def p(text):
    return parse_capture(text, now=NOW)


class TimeParsingTest(unittest.TestCase):
    def test_uhr(self):
        self.assertEqual(p("Zahnarzt 15 Uhr").start_time, "15:00")

    def test_colon(self):
        self.assertEqual(p("Call 09:30").start_time, "09:30")

    def test_um(self):
        self.assertEqual(p("Meeting um 14").start_time, "14:00")

    def test_halb(self):
        self.assertEqual(p("Termin halb 4").start_time, "03:30")

    def test_h_suffix(self):
        self.assertEqual(p("Sport 18h").start_time, "18:00")


class DateParsingTest(unittest.TestCase):
    def test_morgen(self):
        self.assertEqual(p("Zahnarzt morgen").date, "2026-07-14")

    def test_uebermorgen(self):
        self.assertEqual(p("Frisör übermorgen").date, "2026-07-15")

    def test_heute(self):
        self.assertEqual(p("Anruf heute").date, "2026-07-13")

    def test_weekday_ahead(self):
        # Montag ist heute -> nächstes "Freitag" ist 2026-07-17
        self.assertEqual(p("Kino Freitag").date, "2026-07-17")

    def test_dotted_date_no_year(self):
        self.assertEqual(p("Urlaub 24.12.").date, "2026-12-24")

    def test_dotted_date_past_rolls_to_next_year(self):
        # 01.01. liegt vor dem 13.07.2026 -> nächstes Jahr
        self.assertEqual(p("Neujahr 1.1.").date, "2027-01-01")

    def test_month_name(self):
        self.assertEqual(p("Konzert 3. August").date, "2026-08-03")


class TypeAndTitleTest(unittest.TestCase):
    def test_timed_is_event(self):
        c = p("Zahnarzt morgen 15 Uhr")
        self.assertEqual(c.type, "event")
        self.assertEqual(c.title, "Zahnarzt")
        self.assertEqual(c.date, "2026-07-14")
        self.assertEqual(c.start_time, "15:00")
        self.assertEqual(c.confidence, "hoch")

    def test_todo_hint_beats_time(self):
        c = p("erinnere mich an Steuer")
        self.assertEqual(c.type, "todo")
        self.assertIn("Steuer", c.title)

    def test_no_time_is_todo(self):
        c = p("Rasen mähen")
        self.assertEqual(c.type, "todo")
        self.assertEqual(c.title, "Rasen mähen")

    def test_duration_sets_end(self):
        c = p("Sport morgen 18 Uhr für 90 min")
        self.assertEqual(c.start_time, "18:00")
        self.assertEqual(c.end_time, "19:30")

    def test_duration_hours(self):
        c = p("Workshop 10 Uhr 2 stunden")
        self.assertEqual(c.end_time, "12:00")

    def test_title_strips_filler(self):
        c = p("Termin beim Zahnarzt am Freitag um 15 Uhr")
        self.assertEqual(c.type, "event")
        self.assertIn("Zahnarzt", c.title)
        self.assertNotIn("Freitag", c.title)
        self.assertNotIn("15", c.title)

    def test_event_without_date_defaults_today(self):
        c = p("Standup 09:00")
        self.assertEqual(c.type, "event")
        self.assertEqual(c.date, "2026-07-13")


class CaptureWrapperTest(unittest.TestCase):
    def test_capture_returns_rules_without_llm(self):
        c = capture("Zahnarzt morgen 15 Uhr", now=NOW)
        self.assertEqual(c.source, "rules")
        self.assertIsInstance(c, Capture)


class CommitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from backend.database import Base, engine
        from backend.main import app, create_jwt_token
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.h = {"Authorization": f"Bearer {create_jwt_token()}"}

    @classmethod
    def tearDownClass(cls):
        from backend.database import Base, engine
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def test_parse_endpoint(self):
        r = self.client.post("/api/capture", json={"text": "Zahnarzt morgen 15 Uhr"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["type"], "event")

    def test_commit_event(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        r = self.client.post("/api/capture/commit", headers=self.h, json={
            "type": "event", "title": "Zahnarzt", "date": tomorrow,
            "start_time": "15:00", "end_time": "16:00",
        })
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["type"], "event")
        # Termin taucht in der Event-Liste auf
        evs = self.client.get(f"/api/events?start={tomorrow}T00:00:00&end={tomorrow}T23:59:59", headers=self.h).json()
        self.assertTrue(any(e["title"] == "Zahnarzt" for e in evs))

    def test_commit_todo(self):
        r = self.client.post("/api/capture/commit", headers=self.h, json={
            "type": "todo", "title": "Rasen mähen",
        })
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["type"], "todo")


if __name__ == "__main__":
    unittest.main()
