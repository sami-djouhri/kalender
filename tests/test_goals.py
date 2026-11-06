import os
import unittest
from datetime import date, datetime, timedelta, timezone

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-goals.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.database import Base, SessionLocal, engine
from backend.main import app, create_jwt_token
from backend.models import DailyGoal, Habit, HabitSession
from backend.scheduler import _week_iso_str

BERLIN_OFFSET = "+02:00"


class GoalsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()  # run startup migrations
        cls.h = {"Authorization": f"Bearer {create_jwt_token()}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def setUp(self):
        # sauberer Zustand pro Test
        db = SessionLocal()
        db.query(HabitSession).delete()
        db.query(Habit).delete()
        db.query(DailyGoal).delete()
        db.commit()
        db.close()

    def _create_goal(self, **kw):
        body = {"title": "Test-Ziel", "date": "2026-06-30", "priority": "B"}
        body.update(kw)
        r = self.client.post("/api/goals", json=body, headers=self.h)
        self.assertIn(r.status_code, (200, 201), r.text)
        return r.json()

    # --- A-Regel: max ein A pro Tag ---
    def test_second_a_goal_demotes_first_to_b(self):
        g1 = self._create_goal(title="Erstes A", priority="A")
        self.assertEqual(g1["priority"], "A")
        self._create_goal(title="Zweites A", priority="A")
        lst = self.client.get("/api/goals?date=2026-06-30", headers=self.h).json()
        prios = sorted(x["priority"] for x in lst)
        self.assertEqual(prios, ["A", "B"])
        # das ALTE Ziel wurde herabgestuft
        g1_after = self.client.get(f"/api/goals/{g1['id']}", headers=self.h).json()
        self.assertEqual(g1_after["priority"], "B")

    def test_a_rule_only_counts_live_goals(self):
        g1 = self._create_goal(title="Altes A", priority="A")
        self.client.post(f"/api/goals/{g1['id']}/abandon", json={}, headers=self.h)
        # nach Aufgeben darf ein neues A ohne Herabstufung entstehen
        g2 = self._create_goal(title="Neues A", priority="A")
        self.assertEqual(g2["priority"], "A")

    # --- Status-Übergänge ---
    def test_status_transitions(self):
        g = self._create_goal()
        for status in ("active", "achieved", "partial", "missed"):
            r = self.client.put(f"/api/goals/{g['id']}", json={"status": status}, headers=self.h)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["status"], status)

    # --- Aufgeben bleibt historisch sichtbar, blockiert nicht mehr ---
    def test_abandon_keeps_goal_and_sets_reason(self):
        g = self._create_goal()
        r = self.client.post(
            f"/api/goals/{g['id']}/abandon",
            json={"abandoned_reason": "passt heute nicht"},
            headers=self.h,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "abandoned")
        self.assertEqual(body["abandoned_reason"], "passt heute nicht")
        # weiterhin abrufbar (kein Löschen)
        self.assertEqual(self.client.get(f"/api/goals/{g['id']}", headers=self.h).status_code, 200)

    # --- Ziel verdrängt Habit-Session, aufgeben gibt Zeit frei ---
    def _make_habit_with_session(self, day: date, start_h: int, end_h: int) -> tuple[str, str]:
        db = SessionLocal()
        habit = Habit(name="Sport", target_hours_per_week=3.0, can_be_overridden_by_goals=True)
        db.add(habit)
        db.flush()
        start = datetime(day.year, day.month, day.day, start_h, 0, tzinfo=timezone.utc)
        end = datetime(day.year, day.month, day.day, end_h, 0, tzinfo=timezone.utc)
        sess = HabitSession(
            habit_id=habit.id, start=start, end=end, status="pending",
            week_iso=_week_iso_str(day),
        )
        db.add(sess)
        db.commit()
        hid, sid = habit.id, sess.id
        db.close()
        return hid, sid

    def test_goal_overrides_overlapping_habit_session(self):
        # Relatives Zukunftsdatum: schedule_week überspringt vergangene Tage,
        # daher darf der Test-Tag nicht in der Vergangenheit liegen.
        day = date.today() + timedelta(days=1)
        hid, sid = self._make_habit_with_session(day, 17, 19)
        # Ziel mit Zeitblock, der die Session überlappt
        self._create_goal(
            title="Fokus-Block", date=day.isoformat(), priority="A",
            scheduled_start=f"{day.isoformat()}T17:30:00{BERLIN_OFFSET}",
            scheduled_end=f"{day.isoformat()}T18:30:00{BERLIN_OFFSET}",
        )
        db = SessionLocal()
        sess = db.query(HabitSession).filter(HabitSession.id == sid).first()
        self.assertEqual(sess.status, "overridden")
        self.assertIsNotNone(sess.overridden_by_goal_id)
        db.close()

    def test_abandon_releases_overridden_session(self):
        # Relatives Zukunftsdatum (siehe test_goal_overrides): sonst plant
        # schedule_week den freigegebenen Habit nicht neu (Tag liegt in der Vergangenheit).
        day = date.today() + timedelta(days=2)
        hid, sid = self._make_habit_with_session(day, 17, 19)
        g = self._create_goal(
            title="Fokus", date=day.isoformat(), priority="A",
            scheduled_start=f"{day.isoformat()}T17:00:00{BERLIN_OFFSET}",
            scheduled_end=f"{day.isoformat()}T19:00:00{BERLIN_OFFSET}",
        )
        # vorher: Session ist verdrängt
        db = SessionLocal()
        sess = db.query(HabitSession).filter(HabitSession.id == sid).first()
        self.assertEqual(sess.status, "overridden")
        db.close()
        # aufgeben → belegte Zeit wird freigegeben, Habit neu eingeplant
        self.client.post(f"/api/goals/{g['id']}/abandon", json={}, headers=self.h)
        db = SessionLocal()
        # keine Session bleibt durch dieses Ziel verdrängt
        still_overridden = (
            db.query(HabitSession)
            .filter(HabitSession.overridden_by_goal_id == g["id"])
            .count()
        )
        self.assertEqual(still_overridden, 0)
        # der Habit hat wieder mindestens eine aktive (nicht verdrängte) Session
        live = (
            db.query(HabitSession)
            .filter(HabitSession.habit_id == hid, HabitSession.status != "overridden")
            .count()
        )
        self.assertGreaterEqual(live, 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
