import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-scheduler.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, engine, get_db
from backend import scheduler
from backend.scheduler import (
    BERLIN,
    WEEKDAY_DAILY_CAP_MINUTES,
    _ensure_aware,
    _find_free_slots,
    _parse_time,
    _week_start_from_date,
    apply_goal_override,
    calculate_focus_minutes,
    check_conflicts_after_event_change,
    release_goal_block,
    schedule_week,
    weekly_minimum_endangered,
)
from backend.models import Calendar, DailyGoal, Event, Habit, HabitSession

TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"


def _occ(day, s, e):
    sh, sm = s
    eh, em = e
    return (
        datetime(day.year, day.month, day.day, sh, sm, tzinfo=BERLIN),
        datetime(day.year, day.month, day.day, eh, em, tzinfo=BERLIN),
    )


class SchedulerPureTest(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(_parse_time("17:30"), (17, 30))

    def test_week_start_is_monday(self):
        # 2026-07-08 ist ein Mittwoch → Wochenstart Montag 06.
        self.assertEqual(_week_start_from_date(date(2026, 7, 8)), date(2026, 7, 6))

    def test_find_free_slots_gap_around_blocker(self):
        day = date(2026, 7, 7)
        occupied = [_occ(day, (18, 0), (19, 0))]
        free = _find_free_slots(day, "17:00", "20:00", occupied, 30)
        # 17-18 und 19-20 frei
        self.assertEqual(len(free), 2)
        self.assertEqual(free[0][0].hour, 17)
        self.assertEqual(free[1][1].hour, 20)

    def test_find_free_slots_respects_min_duration(self):
        day = date(2026, 7, 7)
        occupied = [_occ(day, (17, 20), (19, 40))]
        # Reste 17:00-17:20 (20min) und 19:40-20:00 (20min) < 30min → keine
        free = _find_free_slots(day, "17:00", "20:00", occupied, 30)
        self.assertEqual(free, [])

    def test_find_free_slots_inverted_window(self):
        day = date(2026, 7, 7)
        self.assertEqual(_find_free_slots(day, "20:00", "17:00", [], 15), [])

    def test_calculate_focus_minutes(self):
        # Reine Fokuszeit ohne Pausen kleiner gleich Sessiondauer
        self.assertLessEqual(calculate_focus_minutes(90, 25, 5), 90)


class SchedulerDbTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = next(get_db())
        self.db.add(Calendar(id=TERMINE_CAL_ID, name="Termine", is_system=True))
        self.db.commit()
        # Zukunftswoche ohne Feiertage/Off-Days
        self.ws = _week_start_from_date(date(2027, 3, 10))  # Mo 2027-03-08
        self.week_iso = scheduler._week_iso_str(self.ws)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _make_habit(self, **kw):
        defaults = dict(
            name="Gitarre",
            target_hours_per_week=3.0,
            session_duration_minutes=60,
            weekday_start="17:00",
            weekday_end="21:00",
            weekend_start="10:00",
            weekend_end="18:00",
            active=True,
        )
        defaults.update(kw)
        h = Habit(**defaults)
        self.db.add(h)
        self.db.commit()
        self.db.refresh(h)
        return h

    def _sessions(self):
        return (
            self.db.query(HabitSession)
            .filter(HabitSession.week_iso == self.week_iso)
            .all()
        )

    def test_schedule_week_creates_non_overlapping_sessions(self):
        self._make_habit()
        count = schedule_week(self.db, self.ws)
        self.assertGreater(count, 0)
        sessions = sorted(self._sessions(), key=lambda s: _ensure_aware(s.start))
        for a, b in zip(sessions, sessions[1:]):
            self.assertLessEqual(_ensure_aware(a.end), _ensure_aware(b.start),
                                 "Sessions duerfen sich nicht ueberlappen")

    def test_schedule_week_is_idempotent(self):
        self._make_habit()
        schedule_week(self.db, self.ws)
        n1 = len(self._sessions())
        schedule_week(self.db, self.ws)
        n2 = len(self._sessions())
        self.assertLessEqual(abs(n2 - n1), 1, "Re-Scheduling darf Sessions nicht vervielfachen")

    def test_weekday_cap_respected(self):
        # Sehr großes Ziel → Tagescap muss greifen
        self._make_habit(target_hours_per_week=40.0, session_duration_minutes=60)
        schedule_week(self.db, self.ws)
        by_day = {}
        for s in self._sessions():
            d = _ensure_aware(s.start).date()
            mins = (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
            by_day[d] = by_day.get(d, 0) + mins
        for d, mins in by_day.items():
            if d.weekday() < 5:  # Werktag
                self.assertLessEqual(mins, WEEKDAY_DAILY_CAP_MINUTES + 1)

    def test_event_conflict_reschedules_away(self):
        self._make_habit()
        schedule_week(self.db, self.ws)
        sessions = self._sessions()
        self.assertTrue(sessions)
        target = sessions[0]
        ev_start = _ensure_aware(target.start)
        ev_end = _ensure_aware(target.end)
        event = Event(
            calendar_id=TERMINE_CAL_ID,
            title="Arzttermin",
            start=ev_start,
            end=ev_end,
            all_day=False,
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        check_conflicts_after_event_change(self.db, event)
        # Keine pending Session darf jetzt noch exakt im Event-Fenster liegen.
        clash = (
            self.db.query(HabitSession)
            .filter(
                HabitSession.week_iso == self.week_iso,
                HabitSession.status == "pending",
                HabitSession.start < ev_end,
                HabitSession.end > ev_start,
            )
            .all()
        )
        self.assertEqual(clash, [], "Habit-Session ueberlappt weiterhin den Termin")

    def test_weekly_minimum_endangered(self):
        h = self._make_habit(weekly_minimum_hours=2.0)
        # Noch keine accepted/completed Session → gefaehrdet
        self.assertTrue(weekly_minimum_endangered(self.db, h, self.week_iso))
        # Habit ohne Minimum → nie gefaehrdet
        h2 = self._make_habit(name="Lesen", weekly_minimum_hours=None)
        self.assertFalse(weekly_minimum_endangered(self.db, h2, self.week_iso))

    def test_goal_override_and_release(self):
        h = self._make_habit(can_be_overridden_by_goals=True)
        schedule_week(self.db, self.ws)
        sessions = self._sessions()
        self.assertTrue(sessions)
        s = sessions[0]
        goal = DailyGoal(
            date=_ensure_aware(s.start).date(),
            title="Tiefarbeit",
            priority="A",
            status="active",
            scheduled_start=_ensure_aware(s.start),
            scheduled_end=_ensure_aware(s.end),
        )
        self.db.add(goal)
        self.db.commit()
        self.db.refresh(goal)
        n = apply_goal_override(self.db, goal)
        self.assertGreaterEqual(n, 1)
        self.db.refresh(s)
        self.assertEqual(s.overridden_by_goal_id, goal.id)
        # Freigeben stellt die Session wieder her
        goal.status = "abandoned"
        self.db.commit()
        released = release_goal_block(self.db, goal)
        self.assertGreaterEqual(released, 1)


if __name__ == "__main__":
    unittest.main()
