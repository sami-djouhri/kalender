"""Tests für das Präferenz-Lernen (preferences.py): Buckets, gleitende Aggregation,
time_fit, Insights. Deterministisch, ohne Netz."""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-prefs.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, engine, get_db
import backend.tenant  # noqa: F401  (registriert Tenant-Scoping fuer Stamping und Filter)
from backend.models import ActivityFeedback
from backend.preferences import (
    HALF_LIFE_DAYS,
    build_insights,
    bucket_for_hour,
    compute_preferences,
    time_fit,
)


class PrefBase(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = next(get_db())

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _fb(self, activity_type, hour, energy_after, days_ago=1, satisfaction=None, took_place=True):
        d = date.today() - timedelta(days=days_ago)
        self.db.add(ActivityFeedback(
            event_id=f"e-{activity_type}-{hour}-{days_ago}",
            occurrence_date=d,
            activity_type=activity_type,
            scheduled_start=datetime(d.year, d.month, d.day, hour, 0),
            weekday=d.weekday(),
            energy_after=energy_after,
            satisfaction=satisfaction,
            took_place=took_place,
        ))
        self.db.commit()


class BucketTest(PrefBase):
    def test_buckets(self):
        self.assertEqual(bucket_for_hour(8), "morgens")
        self.assertEqual(bucket_for_hour(12), "mittags")
        self.assertEqual(bucket_for_hour(16), "nachmittags")
        self.assertEqual(bucket_for_hour(20), "abends")
        self.assertEqual(bucket_for_hour(3), "nachts")


class ComputeTest(PrefBase):
    def test_empty(self):
        self.assertEqual(compute_preferences(self.db), {})
        self.assertIsNone(time_fit(self.db, "sport", 10))
        self.assertIsNone(time_fit(self.db, "", 10))

    def test_morning_sport_positive(self):
        for i in range(3):
            self._fb("sport", 10, "energetisiert", days_ago=i + 1)
        prefs = compute_preferences(self.db)
        self.assertIn("sport", prefs)
        self.assertGreater(prefs["sport"]["buckets"]["morgens"]["score"], 0.8)
        self.assertEqual(prefs["sport"]["count"], 3)
        self.assertGreaterEqual(time_fit(self.db, "sport", 10) or 0, 0.8)

    def test_time_fit_none_below_min_samples(self):
        self._fb("sport", 10, "energetisiert")  # nur 1 Datenpunkt
        self.assertIsNone(time_fit(self.db, "sport", 10))  # MIN_SAMPLES=2

    def test_only_took_place_counts(self):
        self._fb("sport", 10, "energetisiert", days_ago=1)
        self._fb("sport", 10, "energetisiert", days_ago=2, took_place=False)  # abgesagt zählt nicht
        prefs = compute_preferences(self.db)
        self.assertEqual(prefs["sport"]["buckets"]["morgens"]["count"], 1)

    def test_recent_weighted_stronger_than_old(self):
        # frisch positiv, alt (weit jenseits Halbwertszeit) negativ → Aggregat bleibt positiv.
        self._fb("lernen", 17, "energetisiert", days_ago=1)
        self._fb("lernen", 17, "energetisiert", days_ago=2)
        self._fb("lernen", 17, "erschöpft", days_ago=int(HALF_LIFE_DAYS * 4))
        score = compute_preferences(self.db)["lernen"]["buckets"]["nachmittags"]["score"]
        self.assertGreater(score, 0.4)

    def test_satisfaction_folds_in(self):
        self._fb("lesen", 20, "ok", days_ago=1, satisfaction="gut")
        self._fb("lesen", 20, "ok", days_ago=2, satisfaction="gut")
        # energy ok(0) + satisfaction gut(+0.5) → +0.5
        self.assertAlmostEqual(compute_preferences(self.db)["lesen"]["buckets"]["abends"]["score"], 0.5, places=2)


class InsightsTest(PrefBase):
    def test_best_and_worst_time(self):
        for i in range(3):
            self._fb("sport", 10, "energetisiert", days_ago=i + 1)  # morgens gut
        for i in range(3):
            self._fb("sport", 20, "erschöpft", days_ago=i + 1)  # abends platt
        ins = build_insights(self.db)
        sport = next(x for x in ins if x["activity_type"] == "sport")
        self.assertEqual(sport["best_bucket"], "morgens")
        self.assertEqual(sport["worst_bucket"], "abends")
        self.assertIn("morgens", sport["text"])
        self.assertIn("platt", sport["text"])

    def test_no_insight_without_clear_preference(self):
        for i in range(3):
            self._fb("lesen", 20, "ok", days_ago=i + 1)  # neutral (0) → keine Aussage
        self.assertFalse([x for x in build_insights(self.db) if x["activity_type"] == "lesen"])

    def test_no_insight_below_min_samples(self):
        self._fb("hobby", 15, "energetisiert")  # 1 Datenpunkt < MIN_SAMPLES
        self.assertEqual(build_insights(self.db), [])


if __name__ == "__main__":
    unittest.main()
