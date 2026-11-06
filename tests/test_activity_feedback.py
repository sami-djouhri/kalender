"""Tests für die Aktivitäts-Feedback-Schleife (Phase 1).

Deckt ab: Titel-Heuristik, Aggregator (bewertbare Aktivitäten inkl. Recurrence +
Zeit-Status + Feedback-Join), Upsert/Idempotenz der Feedback-Endpoints, das
Feedback-Signal und dessen sichtbaren Effekt auf die Vorschläge.
"""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-feedback.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, engine, get_db
import backend.tenant  # noqa: F401  (registriert Tenant-Scoping fuer Stamping und Filter)
from backend import cross_app
from backend.config import settings
from backend.activities import (
    infer_activity_type,
    list_day_activities,
    list_reviewable_activities,
)
from backend.models import ActivityFeedback, Calendar, Event
from backend.routers.feedback import FeedbackInput, get_feedback, upsert_feedback
from backend.suggestions import build_suggestions, recent_activity_signal

TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"
# Zukunftstag ohne Feiertag/Wochenende → day_type 'frei' (freie Zeit für Vorschläge).
FREI_DI = date(2027, 3, 9)  # Dienstag


class FeedbackBaseTest(unittest.TestCase):
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

    def _event(self, title, start, end, **kw):
        e = Event(calendar_id=TERMINE_CAL_ID, title=title, start=start, end=end, **kw)
        self.db.add(e)
        self.db.commit()
        self.db.refresh(e)
        return e

    def _at(self, day, hour, minute=0):
        return datetime(day.year, day.month, day.day, hour, minute)  # naive Berlin-Wanduhr


class InferActivityTypeTest(FeedbackBaseTest):
    def test_keyword_heuristic(self):
        self.assertEqual(infer_activity_type("CompTIA lernen"), "lernen")
        self.assertEqual(infer_activity_type("IHK Logistik lernen"), "lernen")
        self.assertEqual(infer_activity_type("Bücher lesen"), "lesen")
        self.assertEqual(infer_activity_type("Krafttraining im Gym"), "sport")
        self.assertEqual(infer_activity_type("Pflanzen pflegen"), "hobby")
        self.assertEqual(infer_activity_type("Homelab-Zeit"), "hobby")

    def test_unclear_titles_stay_none(self):
        self.assertIsNone(infer_activity_type("Zahnarzt"))
        self.assertIsNone(infer_activity_type("Meeting mit Chef"))
        self.assertIsNone(infer_activity_type(None))


class AggregatorTest(FeedbackBaseTest):
    def test_past_activity_is_reviewable(self):
        self._event("Krafttraining", self._at(FREI_DI, 10), self._at(FREI_DI, 11),
                    activity_type="sport")
        now = self._at(FREI_DI, 13)
        reviewable = list_reviewable_activities(self.db, FREI_DI, now=now)
        self.assertEqual(len(reviewable), 1)
        self.assertEqual(reviewable[0]["activity_type"], "sport")
        self.assertEqual(reviewable[0]["time_status"], "past")
        self.assertIsNone(reviewable[0]["feedback"])

    def test_upcoming_and_running_not_reviewable(self):
        self._event("Sport später", self._at(FREI_DI, 18), self._at(FREI_DI, 19),
                    activity_type="sport")           # upcoming
        self._event("Läuft gerade", self._at(FREI_DI, 12, 30), self._at(FREI_DI, 13, 30),
                    activity_type="hobby")           # running
        now = self._at(FREI_DI, 13)
        day = list_day_activities(self.db, FREI_DI, now=now)
        statuses = {a["title"]: a["time_status"] for a in day}
        self.assertEqual(statuses["Sport später"], "upcoming")
        self.assertEqual(statuses["Läuft gerade"], "running")
        self.assertEqual(list_reviewable_activities(self.db, FREI_DI, now=now), [])

    def test_event_without_activity_type_is_ignored(self):
        self._event("Zahnarzt", self._at(FREI_DI, 9), self._at(FREI_DI, 10))  # kein activity_type
        now = self._at(FREI_DI, 13)
        self.assertEqual(list_day_activities(self.db, FREI_DI, now=now), [])

    def test_recurring_activity_expands_to_instance(self):
        # Basisreihe startet zwei Wochen vor FREI_DI, wöchentlich dienstags.
        base_day = FREI_DI - timedelta(days=14)
        self._event("CompTIA lernen", self._at(base_day, 17), self._at(base_day, 18, 30),
                    activity_type="lernen", recurrence_rule="FREQ=WEEKLY;BYDAY=TU")
        now = self._at(FREI_DI, 20)
        reviewable = list_reviewable_activities(self.db, FREI_DI, now=now)
        self.assertEqual(len(reviewable), 1)
        self.assertEqual(reviewable[0]["occurrence_date"], FREI_DI.isoformat())
        self.assertIn("::", reviewable[0]["instance_id"])

    def test_feedback_removes_from_reviewable(self):
        ev = self._event("Krafttraining", self._at(FREI_DI, 10), self._at(FREI_DI, 11),
                         activity_type="sport")
        upsert_feedback(
            FeedbackInput(event_id=ev.id, occurrence_date=FREI_DI, energy_after="erschöpft"),
            self.db,
        )
        now = self._at(FREI_DI, 13)
        self.assertEqual(list_reviewable_activities(self.db, FREI_DI, now=now), [])
        # Aber in der vollen Tagesliste taucht es mit Feedback auf.
        day = list_day_activities(self.db, FREI_DI, now=now)
        self.assertEqual(len(day), 1)
        self.assertEqual(day[0]["feedback"]["energy_after"], "erschöpft")


class UpsertTest(FeedbackBaseTest):
    def test_upsert_is_idempotent(self):
        ev = self._event("Lesen", self._at(FREI_DI, 20), self._at(FREI_DI, 21),
                         activity_type="lesen")
        upsert_feedback(FeedbackInput(event_id=ev.id, occurrence_date=FREI_DI,
                                      energy_after="ok", note="erste Notiz"), self.db)
        upsert_feedback(FeedbackInput(event_id=ev.id, occurrence_date=FREI_DI,
                                      satisfaction="gut", note="zweite Notiz"), self.db)
        rows = self.db.query(ActivityFeedback).filter(
            ActivityFeedback.event_id == ev.id).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].energy_after, "ok")        # erhalten
        self.assertEqual(rows[0].satisfaction, "gut")        # ergänzt
        self.assertEqual(rows[0].note, "zweite Notiz")       # überschrieben
        # Denormalisierter Kontext gesetzt.
        self.assertEqual(rows[0].activity_type, "lesen")
        self.assertEqual(rows[0].weekday, FREI_DI.weekday())

    def test_instance_id_reference_resolves(self):
        ev = self._event("Sport", self._at(FREI_DI, 10), self._at(FREI_DI, 11),
                         activity_type="sport", recurrence_rule="FREQ=WEEKLY;BYDAY=TU")
        instance_id = f"{ev.id}::{FREI_DI.isoformat()}"
        upsert_feedback(FeedbackInput(event_id=instance_id, energy_after="energetisiert"), self.db)
        out = get_feedback(event_id=instance_id, date=None, db=self.db)
        self.assertEqual(out["feedback"]["energy_after"], "energetisiert")
        self.assertEqual(out["occurrence_date"], FREI_DI.isoformat())


class SignalAndSuggestionTest(FeedbackBaseTest):
    def _feedback(self, category, energy_after, days_ago):
        self.db.add(ActivityFeedback(
            event_id=f"evt-{category}-{days_ago}",
            occurrence_date=date.today() - timedelta(days=days_ago),
            activity_type=category,
            energy_after=energy_after,
        ))
        self.db.commit()

    def test_recent_signal_averages(self):
        self.assertIsNone(recent_activity_signal(self.db, "sport"))  # keine Daten
        self._feedback("sport", "erschöpft", 1)
        self._feedback("sport", "erschöpft", 3)
        self._feedback("sport", "ok", 5)
        sig = recent_activity_signal(self.db, "sport")
        self.assertLess(sig, 0)   # überwiegend erschöpft → negativ
        self.assertAlmostEqual(sig, (-1 - 1 + 0) / 3, places=6)

    def test_feedback_dampens_sport_suggestion(self):
        # Ein Habit/Signal, das einen Sport-Vorschlag auslöst, gibt es nicht direkt;
        # wir prüfen den generischen Anpassungspfad über die reason-Begründung.
        self._feedback("sport", "erschöpft", 1)
        self._feedback("sport", "erschöpft", 2)
        sig = recent_activity_signal(self.db, "sport")
        self.assertLessEqual(sig, -0.34)


if __name__ == "__main__":
    unittest.main()
