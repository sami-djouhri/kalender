import os
import unittest

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-todosched.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.database import Base, SessionLocal, engine
from backend.main import app, create_jwt_token
from backend.models import Todo


class TodoSchedulingTest(unittest.TestCase):
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
        db.query(Todo).delete()
        db.commit()
        db.close()

    def _create(self, **kw):
        body = {"title": "Aufgabe"}
        body.update(kw)
        r = self.client.post("/api/todos", json=body, headers=self.h)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_new_todo_defaults_to_pool(self):
        t = self._create(estimated_minutes=20)
        self.assertEqual(t["scheduling_mode"], "pool")
        self.assertEqual(t["status"], "open")
        self.assertEqual(t["defer_count"], 0)

    def test_defer_increments_and_never_loses(self):
        t = self._create()
        r = self.client.post(f"/api/todos/{t['id']}/defer", json={"reason": "keine Zeit"}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["defer_count"], 1)
        self.assertEqual(body["defer_reason"], "keine Zeit")
        self.assertIsNotNone(body["last_deferred_at"])
        # erneut aufschieben
        r2 = self.client.post(f"/api/todos/{t['id']}/defer", json={"planned_date": "2026-07-05"}, headers=self.h)
        b2 = r2.json()
        self.assertEqual(b2["defer_count"], 2)
        self.assertEqual(b2["scheduling_mode"], "planned_day")
        self.assertEqual(b2["status"], "scheduled")
        # Todo existiert weiterhin (nicht verloren)
        self.assertEqual(self.client.get(f"/api/todos/{t['id']}", headers=self.h).status_code, 200)

    def test_schedule_modes(self):
        t = self._create()
        r = self.client.post(
            f"/api/todos/{t['id']}/schedule",
            json={"scheduling_mode": "fixed_slot",
                  "scheduled_start": "2026-07-05T17:00:00+02:00",
                  "scheduled_end": "2026-07-05T17:30:00+02:00"},
            headers=self.h,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scheduling_mode"], "fixed_slot")
        self.assertEqual(r.json()["status"], "scheduled")

    def test_cancel_is_not_done(self):
        t = self._create()
        r = self.client.post(f"/api/todos/{t['id']}/cancel", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "cancelled")
        self.assertFalse(r.json()["completed"])

    def test_complete_syncs_status(self):
        t = self._create()
        r = self.client.post(f"/api/todos/{t['id']}/complete", headers=self.h)
        self.assertEqual(r.json()["status"], "done")
        self.assertTrue(r.json()["completed"])

    def test_auto_plan_suggests_pool_todo_in_free_slot(self):
        self._create(title="Klein", estimated_minutes=20)
        r = self.client.get("/api/schedule/day?date=2026-07-06", headers=self.h)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertGreaterEqual(len(data["free_slots"]), 1)
        self.assertGreaterEqual(len(data["suggestions"]), 1)
        self.assertFalse(data["committed"])

    def test_auto_plan_commit_writes_schedule(self):
        t = self._create(title="Commit-Test", estimated_minutes=20)
        r = self.client.post("/api/schedule/auto-plan?date=2026-07-06&commit=true", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["committed"])
        after = self.client.get(f"/api/todos/{t['id']}", headers=self.h).json()
        self.assertEqual(after["scheduling_mode"], "planned_day")
        self.assertEqual(after["status"], "scheduled")
        self.assertIsNotNone(after["scheduled_start"])

    def test_vorschlag_fuer_fremde_aufgaben_schreibt_nichts(self):
        """Aufgaben aus dem Aufgaben-Dienst: geplant wird, festgehalten nicht hier."""
        eigene = self._create(title="Eigenes Todo", estimated_minutes=20)
        r = self.client.post("/api/schedule/vorschlag", headers=self.h, json={
            "date": "2026-07-06",
            "aufgaben": [
                {"id": "fremd-1", "title": "Fremd", "estimated_minutes": 30, "energy_required": "high"},
                {"id": "fremd-2", "title": "Dringend", "priority": "hoch", "estimated_minutes": 15},
            ],
        })
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        ids = [s["todo_id"] for s in data["suggestions"]]
        self.assertEqual(ids, ["fremd-2", "fremd-1"], "hoch vor mittel, eigene Todos nicht dabei")
        self.assertFalse(data["committed"])
        after = self.client.get(f"/api/todos/{eigene['id']}", headers=self.h).json()
        self.assertEqual(after["scheduling_mode"], "pool")

    def test_vorschlag_meidet_belegte_zeit(self):
        frei = self.client.post("/api/schedule/vorschlag", headers=self.h, json={
            "date": "2026-07-06", "aufgaben": [{"id": "a", "title": "A", "estimated_minutes": 30}],
        }).json()
        erster = frei["suggestions"][0]
        r = self.client.post("/api/schedule/vorschlag", headers=self.h, json={
            "date": "2026-07-06",
            "aufgaben": [{"id": "a", "title": "A", "estimated_minutes": 30}],
            "belegt": [{"start": erster["start"], "end": erster["end"]}],
        }).json()
        self.assertNotEqual(r["suggestions"][0]["start"], erster["start"])

    def test_vorschlag_weist_unsinn_ab(self):
        r = self.client.post("/api/schedule/vorschlag", headers=self.h, json={
            "date": "2026-07-06", "aufgaben": [{"id": "a", "title": "A", "energy_required": "hoch"}],
        })
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
