import os
import unittest

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-search.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.database import Base, engine
from backend.main import TERMINE_CAL_ID, app, create_jwt_token


class SearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.h = {"Authorization": f"Bearer {create_jwt_token()}"}
        cls.client.post("/api/events", json={
            "calendar_id": TERMINE_CAL_ID, "title": "Zahnarzt Termin",
            "start": "2026-09-07T09:00:00+00:00", "end": "2026-09-07T10:00:00+00:00",
        }, headers=cls.h)
        cls.client.post("/api/todos", json={"title": "Zahnpasta kaufen"}, headers=cls.h)

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def test_search_finds_event_and_todo(self):
        r = self.client.get("/api/search", params={"q": "Zahn"}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        kinds = {res["kind"] for res in r.json()["results"]}
        self.assertIn("event", kinds)
        self.assertIn("todo", kinds)

    def test_search_event_carries_date(self):
        r = self.client.get("/api/search", params={"q": "Zahnarzt"}, headers=self.h)
        ev = [x for x in r.json()["results"] if x["kind"] == "event"][0]
        self.assertEqual(ev["date"], "2026-09-07")
        self.assertEqual(ev["icon"], "📅")

    def test_search_empty_query_rejected(self):
        r = self.client.get("/api/search", params={"q": ""}, headers=self.h)
        self.assertEqual(r.status_code, 422)

    def test_search_no_match_empty(self):
        r = self.client.get("/api/search", params={"q": "xyzqqq"}, headers=self.h)
        self.assertEqual(r.json()["results"], [])


if __name__ == "__main__":
    unittest.main()
