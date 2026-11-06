"""Tests für den präferenz-bewussten Slot-Planer (planning.py)."""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-planning.db'
os.environ['KALENDER_PASSWORD'] = 'test'
os.environ['SECRET_KEY'] = 'test'

from backend.database import Base, engine, get_db
import backend.tenant  # noqa: F401
from backend.config import settings
from backend.models import ActivityFeedback, Calendar, Event
from backend.planning import suggest_best_time

TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"
FREI_DI = date(2027, 3, 9)  # Dienstag → day_type 'frei'


class PlanningTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self._cross = settings.CROSS_APP_ENABLED
        settings.CROSS_APP_ENABLED = False
        self.db = next(get_db())
        self.db.add(Calendar(id=TERMINE_CAL_ID, name="Termine", is_system=True))
        self.db.commit()

    def tearDown(self):
        settings.CROSS_APP_ENABLED = self._cross
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _fb(self, at, hour, energy, days_before_ref):
        d = FREI_DI - timedelta(days=days_before_ref)
        self.db.add(ActivityFeedback(
            event_id=f"e-{at}-{hour}-{days_before_ref}",
            occurrence_date=d,
            activity_type=at,
            scheduled_start=datetime(d.year, d.month, d.day, hour, 0),
            weekday=d.weekday(),
            energy_after=energy,
            took_place=True,
        ))
        self.db.commit()

    def _event(self, title, start, end):
        self.db.add(Event(calendar_id=TERMINE_CAL_ID, title=title, start=start, end=end))
        self.db.commit()

    def test_no_activity_type(self):
        self.assertIsNone(suggest_best_time(self.db, ""))

    def test_prefers_learned_bucket(self):
        # Sport morgens gelernt-gut, abends platt.
        for i in range(3):
            self._fb("sport", 10, "energetisiert", days_before_ref=i + 2)
            self._fb("sport", 20, "erschöpft", days_before_ref=i + 2)
        # Test-Tag: Mitte (11–18) blockiert → frei bleiben nur morgens + abends.
        self._event("Block", datetime(2027, 3, 9, 11, 0), datetime(2027, 3, 9, 18, 0))
        best = suggest_best_time(self.db, "sport", from_day=FREI_DI, horizon_days=1)
        self.assertIsNotNone(best)
        self.assertEqual(best["bucket"], "morgens")  # Präferenz gewinnt gegen abends
        self.assertLess(best["hour"], 11)
        self.assertGreaterEqual(best["preference_score"], 0.8)

    def test_fallback_without_feedback(self):
        # Ohne Feedback trotzdem ein Vorschlag (nach Kapazität/Wachfenster), pref=None.
        best = suggest_best_time(self.db, "lernen", from_day=FREI_DI, horizon_days=1)
        self.assertIsNotNone(best)
        self.assertIsNone(best["preference_score"])
        self.assertEqual(best["day"], FREI_DI.isoformat())

    def test_none_when_fully_booked(self):
        # Ganzer Tag durch ein großes Event belegt → kein Slot.
        self._event("Ganztags-Block", datetime(2027, 3, 9, 0, 0), datetime(2027, 3, 10, 0, 0))
        best = suggest_best_time(self.db, "lernen", from_day=FREI_DI, horizon_days=1)
        self.assertIsNone(best)


if __name__ == "__main__":
    unittest.main()
