import os
import unittest

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-reviews.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.database import Base, engine
from backend.main import app, create_jwt_token


class ReviewsTest(unittest.TestCase):
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

    def test_get_missing_review_returns_null(self):
        r = self.client.get("/api/reviews/2026-07-09", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json())

    def test_upsert_creates_then_updates(self):
        r = self.client.put(
            "/api/reviews/2026-07-10",
            json={"what_went_well": "viel geschafft", "energy_level": "high"},
            headers=self.h,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["energy_level"], "high")
        # zweiter Upsert aktualisiert dasselbe Datum
        r2 = self.client.put(
            "/api/reviews/2026-07-10",
            json={"blockers": "Unterbrechungen"},
            headers=self.h,
        )
        self.assertEqual(r2.json()["blockers"], "Unterbrechungen")
        self.assertEqual(r2.json()["what_went_well"], "viel geschafft")

    def test_invalid_energy_rejected(self):
        r = self.client.put(
            "/api/reviews/2026-07-11",
            json={"energy_level": "ultra"},
            headers=self.h,
        )
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
