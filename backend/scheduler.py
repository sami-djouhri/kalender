"""Habit session scheduling engine with learning-aware load management."""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from math import ceil
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")

from sqlalchemy.orm import Session

from backend.models import DailyGoal, DailyLoad, Event, Habit, HabitSession, Todo
from backend.routers.daytype import (
    DAYTYPE_CALENDARS,
    FEIERTAG_CALENDAR_ID,
    _get_daytype_display,
)

# Calendar IDs whose events control day-type logic (time windows, krank skip)
# but should NOT block time slots in the scheduler.
_DAYTYPE_CALENDAR_IDS = set(DAYTYPE_CALENDARS.values()) | {FEIERTAG_CALENDAR_ID}

# --- General caps ---
WEEKDAY_DAILY_CAP_MINUTES = 180   # 3h an Wochentagen (alle Habits)
WEEKEND_DAILY_CAP_MINUTES = 480   # 8h am Wochenende/Urlaub/Feiertag (alle Habits)
BREAK_BETWEEN_SESSIONS_MINUTES = 10

# --- Learning-specific caps ---
WEEKDAY_LEARNING_CAP_MINUTES = 90    # Max Lernen pro Werktag
WEEKEND_LEARNING_CAP_MINUTES = 240   # Max Lernen pro WE-Tag (4h)

# --- Strain system ---
STRAIN_DECAY_PER_REST = 0.4           # Erholung pro Ruhetag
STRAIN_INCREASE_PER_HOUR = 0.15       # Belastung pro Lernstunde
STRAIN_THRESHOLD_REST = 0.7           # Ab hier: Intensitaet reduzieren

# --- Redistribution ---
MAX_EXTRA_MINUTES_PER_DAY = 30        # Max Zusatzminuten bei Umverteilung
REDISTRIBUTION_HORIZON_DAYS = 7

# --- Input/Review ratios ---
INPUT_WEEKEND_RATIO = 0.7             # 70% WE-Lernen = Input
REVIEW_WEEKDAY_RATIO = 0.7            # 70% WT-Lernen = Review

WEEKDAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def calculate_focus_minutes(session_duration: int, focus_block: int, break_min: int) -> int:
    """Calculate focus minutes from a calendar session duration using Pomodoro-style cycles."""
    cycle = focus_block + break_min
    n = session_duration // cycle
    r = session_duration - n * cycle
    return n * focus_block + min(r, focus_block)


def sessions_needed_for_focus_target(
    target_focus_min: float, focus_block: int, break_min: int, session_duration: int
) -> int:
    """How many calendar sessions are needed to reach a focus-minutes target."""
    focus_per_session = calculate_focus_minutes(session_duration, focus_block, break_min)
    if focus_per_session <= 0:
        return 1
    return ceil(target_focus_min / focus_per_session)


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute)."""
    h, m = time_str.split(":")
    return int(h), int(m)


def _week_iso_str(d: date) -> str:
    """Return ISO week string like '2026-W09'."""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_start_from_date(d: date) -> date:
    """Return the Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def _ensure_aware(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (default Berlin)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BERLIN)
    return dt


# --- Strain calculation ---

def calculate_strain(db: Session, target_date: date) -> float:
    """Calculate rolling 7-day strain with exponential decay.

    Each learning hour increases strain by 0.15, each rest day reduces by 0.4.
    Yesterday = full weight, day before = 85%, etc.
    """
    strain = 0.0
    for days_ago in range(1, 8):
        d = target_date - timedelta(days=days_ago)
        decay = 0.85 ** (days_ago - 1)

        load = db.query(DailyLoad).filter(DailyLoad.date == d).first()
        if load:
            if load.is_off_day:
                strain -= STRAIN_DECAY_PER_REST * decay
            else:
                hours = load.learning_minutes / 60.0
                strain += STRAIN_INCREASE_PER_HOUR * hours * decay

    return max(0.0, min(1.0, strain))


def get_or_create_daily_load(db: Session, d: date) -> DailyLoad:
    """Get or create a DailyLoad row for a given date."""
    load = db.query(DailyLoad).filter(DailyLoad.date == d).first()
    if not load:
        load = DailyLoad(date=d)
        db.add(load)
        db.flush()
    return load


def update_daily_load_learning(db: Session, d: date):
    """Recalculate learning_minutes and strain for a date from completed/accepted sessions."""
    load = get_or_create_daily_load(db, d)

    d_start = datetime(d.year, d.month, d.day, tzinfo=BERLIN)
    d_end = d_start + timedelta(days=1)

    # Sum learning minutes from completed + accepted sessions of lernen habits
    sessions = (
        db.query(HabitSession)
        .join(Habit)
        .filter(
            HabitSession.start < d_end,
            HabitSession.end > d_start,
            HabitSession.status.in_(["completed", "accepted"]),
            Habit.category == "lernen",
        )
        .all()
    )
    total_min = sum(
        int((_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60)
        for s in sessions
    )
    load.learning_minutes = total_min
    load.strain = calculate_strain(db, d)
    db.flush()


def _get_strain_level(strain: float) -> str:
    """Return human-readable strain level in German."""
    if strain < 0.3:
        return "niedrig"
    elif strain < 0.5:
        return "mittel"
    elif strain < 0.7:
        return "hoch"
    else:
        return "kritisch"


# --- Slot finding (shared) ---

def _get_occupied_slots(db: Session, week_start: date, week_end: date) -> list[tuple[datetime, datetime]]:
    """Get all occupied time slots (events + existing sessions) for a week."""
    ws = datetime(week_start.year, week_start.month, week_start.day, tzinfo=BERLIN)
    we = datetime(week_end.year, week_end.month, week_end.day, 23, 59, 59, tzinfo=BERLIN)

    events = (
        db.query(Event)
        .filter(
            Event.start < we,
            Event.end > ws,
            Event.calendar_id.notin_(_DAYTYPE_CALENDAR_IDS),
        )
        .all()
    )
    slots = []
    for e in events:
        if e.all_day:
            e_start = _ensure_aware(e.start)
            e_end = _ensure_aware(e.end)
            day = e_start.date()
            last_blocked_day = (e_end - timedelta(seconds=1)).date()
            while day <= last_blocked_day:
                day_begin = datetime(day.year, day.month, day.day, 0, 0, tzinfo=BERLIN)
                day_finish = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=BERLIN)
                slots.append((day_begin, day_finish))
                day += timedelta(days=1)
        else:
            slots.append((_ensure_aware(e.start), _ensure_aware(e.end)))

    sessions = (
        db.query(HabitSession)
        .filter(
            HabitSession.start < we,
            HabitSession.end > ws,
            HabitSession.status.notin_(["dismissed", "overridden"]),
        )
        .all()
    )
    for s in sessions:
        s_start = _ensure_aware(s.start)
        s_end = _ensure_aware(s.end)
        slots.append((s_start, s_end))
        slots.append((s_end, s_end + timedelta(minutes=BREAK_BETWEEN_SESSIONS_MINUTES)))

    # Tagesziele mit Zeitblock blockieren wie weiche Termine (höher als Habits).
    slots.extend(_get_goal_blocks(db, ws, we))

    return slots


# Goal-Status, die noch Zeit beanspruchen (zukünftig planungsrelevant).
_ACTIVE_GOAL_STATUSES = ("planned", "active")


def _get_goal_blocks(db: Session, ws: datetime, we: datetime) -> list[tuple[datetime, datetime]]:
    """Zeitblöcke aktiver Tagesziele im Zeitfenster [ws, we)."""
    goals = (
        db.query(DailyGoal)
        .filter(
            DailyGoal.scheduled_start.isnot(None),
            DailyGoal.scheduled_end.isnot(None),
            DailyGoal.scheduled_start < we,
            DailyGoal.scheduled_end > ws,
            DailyGoal.status.in_(_ACTIVE_GOAL_STATUSES),
        )
        .all()
    )
    return [(_ensure_aware(g.scheduled_start), _ensure_aware(g.scheduled_end)) for g in goals]


def _find_free_slots(
    day: date,
    window_start_str: str,
    window_end_str: str,
    occupied: list[tuple[datetime, datetime]],
    min_duration_minutes: int,
) -> list[tuple[datetime, datetime]]:
    """Find free sub-slots within a time window on a given day."""
    sh, sm = _parse_time(window_start_str)
    eh, em = _parse_time(window_end_str)

    window_start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=BERLIN)
    window_end = datetime(day.year, day.month, day.day, eh, em, tzinfo=BERLIN)

    if window_end <= window_start:
        return []

    blockers = []
    for occ_start, occ_end in occupied:
        if occ_end > window_start and occ_start < window_end:
            blockers.append((max(occ_start, window_start), min(occ_end, window_end)))

    blockers.sort(key=lambda x: x[0])

    free = []
    cursor = window_start
    for blk_start, blk_end in blockers:
        if blk_start > cursor:
            gap_minutes = (blk_start - cursor).total_seconds() / 60
            if gap_minutes >= min_duration_minutes:
                free.append((cursor, blk_start))
        cursor = max(cursor, blk_end)

    if cursor < window_end:
        gap_minutes = (window_end - cursor).total_seconds() / 60
        if gap_minutes >= min_duration_minutes:
            free.append((cursor, window_end))

    return free


def _merge_same_day_sessions(db: Session, habit_id: str, week_iso: str):
    """Merge adjacent pending sessions for the same habit on the same day."""
    sessions = (
        db.query(HabitSession)
        .filter(
            HabitSession.habit_id == habit_id,
            HabitSession.week_iso == week_iso,
            HabitSession.status == "pending",
        )
        .order_by(HabitSession.start)
        .all()
    )

    by_day: dict[date, list[HabitSession]] = defaultdict(list)
    for s in sessions:
        by_day[_ensure_aware(s.start).date()].append(s)

    for day_sessions in by_day.values():
        if len(day_sessions) <= 1:
            continue
        day_sessions.sort(key=lambda s: _ensure_aware(s.start))

        keeper = day_sessions[0]
        for extra in day_sessions[1:]:
            extra_start = _ensure_aware(extra.start)
            keeper_end = _ensure_aware(keeper.end)
            gap_minutes = (extra_start - keeper_end).total_seconds() / 60
            if gap_minutes <= BREAK_BETWEEN_SESSIONS_MINUTES:
                extra_end = _ensure_aware(extra.end)
                if extra_end > keeper_end:
                    keeper.end = extra.end
                db.delete(extra)
            else:
                keeper = extra


# --- Consecutive learning days check ---

def _count_consecutive_learning_days(db: Session, target_date: date, habit_id: str) -> int:
    """Count consecutive days before target_date that have completed/accepted learning sessions."""
    count = 0
    d = target_date - timedelta(days=1)
    while count < 14:  # safety limit
        d_start = datetime(d.year, d.month, d.day, tzinfo=BERLIN)
        d_end = d_start + timedelta(days=1)
        has_session = (
            db.query(HabitSession)
            .filter(
                HabitSession.habit_id == habit_id,
                HabitSession.start < d_end,
                HabitSession.end > d_start,
                HabitSession.status.in_(["completed", "accepted", "pending"]),
            )
            .first()
        )
        if has_session:
            count += 1
            d -= timedelta(days=1)
        else:
            break
    return count


def _is_off_day(db: Session, d: date) -> bool:
    """Check if a date is a learning off-day."""
    load = db.query(DailyLoad).filter(DailyLoad.date == d).first()
    return load.is_off_day if load else False


# --- Day info helpers ---

def _is_weekend_like(db: Session, day: date) -> bool:
    """Check if day uses weekend time windows."""
    day_type = _get_daytype_display(db, day)
    return day_type in ("wochenende", "urlaub", "feiertag")


def _get_learning_cap(db: Session, day: date) -> int:
    """Get the learning minutes cap for a specific day."""
    return WEEKEND_LEARNING_CAP_MINUTES if _is_weekend_like(db, day) else WEEKDAY_LEARNING_CAP_MINUTES


def _get_general_cap(db: Session, day: date) -> int:
    """Get the general daily cap for a specific day."""
    return WEEKEND_DAILY_CAP_MINUTES if _is_weekend_like(db, day) else WEEKDAY_DAILY_CAP_MINUTES


def _get_existing_learning_minutes(db: Session, day: date) -> int:
    """Get total scheduled learning minutes for a day (non-dismissed)."""
    d_start = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
    d_end = d_start + timedelta(days=1)
    sessions = (
        db.query(HabitSession)
        .join(Habit)
        .filter(
            HabitSession.start < d_end,
            HabitSession.end > d_start,
            HabitSession.status.notin_(["dismissed"]),
            Habit.category == "lernen",
        )
        .all()
    )
    return sum(
        int((_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60)
        for s in sessions
    )


# --- Session type assignment ---

def _assign_session_type(is_weekend_like: bool) -> str:
    """Determine session type based on day type.

    Weekend-like days favor input (new material), weekdays favor review.
    """
    import random
    if is_weekend_like:
        return "input" if random.random() < INPUT_WEEKEND_RATIO else "review"
    else:
        return "review" if random.random() < REVIEW_WEEKDAY_RATIO else "input"


# --- Place a single session ---

def _place_session(
    db: Session,
    habit: Habit,
    day: date,
    occupied: list[tuple[datetime, datetime]],
    habit_minutes_per_day: dict[date, float],
    week_iso: str,
    session_type: str = "standard",
    max_minutes: int | None = None,
    learning_minutes_per_day: dict[date, float] | None = None,
) -> HabitSession | None:
    """Try to place a single session on a given day. Returns the session or None."""
    from backend.sleep_schedule import get_buffer_start

    day_type = _get_daytype_display(db, day)
    is_wl = day_type in ("wochenende", "urlaub", "feiertag")
    w_start = habit.weekend_start if is_wl else habit.weekday_start
    w_end = habit.weekend_end if is_wl else habit.weekday_end

    # Clamp window end to buffer zone start
    buf_h, buf_m = get_buffer_start(day_type)
    buf_str = f"{buf_h:02d}:{buf_m:02d}"
    if buf_str < w_end:
        w_end = buf_str

    session_duration = min(habit.session_duration_minutes, max_minutes) if max_minutes else habit.session_duration_minutes
    # Also cap by max_session_minutes for learning habits
    if habit.category == "lernen":
        session_duration = min(session_duration, habit.max_session_minutes)

    # General daily cap: reduce session_duration if cap is nearly full
    general_cap = WEEKEND_DAILY_CAP_MINUTES if is_wl else WEEKDAY_DAILY_CAP_MINUTES
    remaining_cap = general_cap - habit_minutes_per_day.get(day, 0)
    if remaining_cap < 15:
        return None
    session_duration = min(session_duration, int(remaining_cap))

    free_slots = _find_free_slots(day, w_start, w_end, occupied, session_duration)
    if not free_slots:
        return None

    slot_start, slot_end = free_slots[0]
    session_end = slot_start + timedelta(minutes=session_duration)
    if session_end > slot_end:
        session_end = slot_end

    session = HabitSession(
        habit_id=habit.id,
        start=slot_start,
        end=session_end,
        status="pending",
        week_iso=week_iso,
        session_type=session_type,
    )
    db.add(session)
    occupied.append((slot_start, session_end))
    break_end = session_end + timedelta(minutes=BREAK_BETWEEN_SESSIONS_MINUTES)
    occupied.append((session_end, break_end))
    session_minutes = (session_end - slot_start).total_seconds() / 60
    habit_minutes_per_day[day] = habit_minutes_per_day.get(day, 0) + session_minutes
    if habit.category == "lernen" and learning_minutes_per_day is not None:
        learning_minutes_per_day[day] = learning_minutes_per_day.get(day, 0) + session_minutes
    return session


# --- Learning habit scheduler ---

def _schedule_learning_habit(
    db: Session,
    habit: Habit,
    week_start: date,
    week_iso: str,
    occupied: list[tuple[datetime, datetime]],
    habit_minutes_per_day: dict[date, float],
    learning_minutes_per_day: dict[date, float] | None = None,
) -> int:
    """Schedule a learning habit with strain awareness, off-days, and input/review types."""
    today = date.today()
    created = 0

    # 1. Target = exactly what the user configured
    target_minutes = habit.target_hours_per_week * 60

    # 2. Subtract already scheduled (non-dismissed) sessions
    existing_sessions = (
        db.query(HabitSession)
        .filter(
            HabitSession.habit_id == habit.id,
            HabitSession.week_iso == week_iso,
            HabitSession.status.notin_(["dismissed"]),
        )
        .all()
    )
    existing_minutes = sum(
        (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
        for s in existing_sessions
    )
    remaining_minutes = max(0, target_minutes - existing_minutes)

    if remaining_minutes <= 0:
        return 0

    # 3. Reserve time for review session (included in target budget)
    review_minutes = min(60, remaining_minutes)
    distribution_minutes = remaining_minutes - review_minutes

    # 4. Weekday/Weekend split
    weekday_minutes = distribution_minutes * habit.weekday_target_ratio
    weekend_minutes = distribution_minutes * (1 - habit.weekday_target_ratio)

    # 5. Build available days (consecutive check is per-habit, no global off-days)
    weekday_days = []
    weekend_days = []
    for day_offset in range(7):
        day = week_start + timedelta(days=day_offset)
        if day < today:
            continue
        day_type = _get_daytype_display(db, day)
        if day_type == "krank":
            continue

        # Per-habit consecutive rest check (skip day for THIS habit only)
        consecutive = _count_consecutive_learning_days(db, day, habit.id)
        if consecutive >= habit.max_consecutive_days:
            continue

        is_wl = day_type in ("wochenende", "urlaub", "feiertag")
        if is_wl:
            weekend_days.append(day)
        else:
            weekday_days.append(day)

    # 8. Distribute minutes across available days
    session_cap = min(habit.session_duration_minutes, habit.max_session_minutes)

    def _distribute_to_days(days: list[date], total_min: float, is_weekend: bool) -> tuple[int, float]:
        """Distribute minutes across days. Returns (sessions_placed, minutes_placed)."""
        nonlocal created
        if not days or total_min <= 0:
            return 0, 0.0
        per_day = total_min / len(days)
        placed = 0
        minutes_placed = 0.0

        for day in days:
            day_budget = per_day
            while day_budget >= session_cap * 0.5:  # at least half a session
                this_session = min(session_cap, day_budget)
                if this_session < 15:
                    break
                s_type = _assign_session_type(is_weekend)
                result = _place_session(
                    db, habit, day, occupied, habit_minutes_per_day,
                    week_iso, session_type=s_type, max_minutes=int(this_session),
                    learning_minutes_per_day=learning_minutes_per_day,
                )
                if result:
                    placed += 1
                    created += 1
                    actual = (_ensure_aware(result.end) - _ensure_aware(result.start)).total_seconds() / 60
                    day_budget -= actual
                    minutes_placed += actual
                else:
                    break
        return placed, minutes_placed

    _, weekend_placed = _distribute_to_days(weekend_days, weekend_minutes, is_weekend=True)
    _, weekday_placed = _distribute_to_days(weekday_days, weekday_minutes, is_weekend=False)

    # Overflow: redistribute unfilled minutes to the other side
    weekend_leftover = max(0, weekend_minutes - weekend_placed)
    weekday_leftover = max(0, weekday_minutes - weekday_placed)
    if weekend_leftover > 0 and weekday_days:
        _, extra_wd = _distribute_to_days(weekday_days, weekend_leftover, is_weekend=False)
        weekday_placed += extra_wd
    if weekday_leftover > 0 and weekend_days:
        _, extra_we = _distribute_to_days(weekend_days, weekday_leftover, is_weekend=True)
        weekend_placed += extra_we

    # 9. Weekly review session (from reserved budget, pinned)
    review_placed = 0.0
    existing_review = (
        db.query(HabitSession)
        .filter(
            HabitSession.habit_id == habit.id,
            HabitSession.week_iso == week_iso,
            HabitSession.session_type == "review",
            HabitSession.pinned == True,
            HabitSession.status.notin_(["dismissed"]),
        )
        .first()
    )
    if not existing_review:
        review_candidates = []
        for wd in [6, 5, 4]:
            candidate = week_start + timedelta(days=wd)
            if candidate >= today:
                day_type = _get_daytype_display(db, candidate)
                if day_type != "krank":
                    review_candidates.append(candidate)

        for review_day in review_candidates:
            result = _place_session(
                db, habit, review_day, occupied, habit_minutes_per_day,
                week_iso, session_type="review", max_minutes=int(review_minutes),
                learning_minutes_per_day=learning_minutes_per_day,
            )
            if result:
                result.pinned = True
                created += 1
                review_placed = (_ensure_aware(result.end) - _ensure_aware(result.start)).total_seconds() / 60
                break
    else:
        review_placed = (_ensure_aware(existing_review.end) - _ensure_aware(existing_review.start)).total_seconds() / 60

    # 10. Gap-fill: if total placed is still under target, try to fill remaining
    total_placed = weekday_placed + weekend_placed + review_placed
    gap = remaining_minutes - total_placed
    if gap >= 15:
        # Try all available days (weekend first for more capacity)
        all_days = weekend_days + weekday_days
        for day in all_days:
            if gap < 15:
                break
            is_wl = day in weekend_days
            this_session = min(session_cap, int(gap))
            if this_session < 15:
                break
            result = _place_session(
                db, habit, day, occupied, habit_minutes_per_day,
                week_iso, session_type="standard", max_minutes=this_session,
                learning_minutes_per_day=learning_minutes_per_day,
            )
            if result:
                created += 1
                actual = (_ensure_aware(result.end) - _ensure_aware(result.start)).total_seconds() / 60
                gap -= actual

    return created


# --- Simple habit scheduler (lesen + sonstige) ---

def _schedule_simple_habit(
    db: Session,
    habit: Habit,
    week_start: date,
    week_iso: str,
    occupied: list[tuple[datetime, datetime]],
    habit_minutes_per_day: dict[date, float],
) -> int:
    """Schedule a non-learning habit using minute-based distribution."""
    today = date.today()

    # Target = exactly what the user configured
    target_minutes = habit.target_hours_per_week * 60

    # Subtract already scheduled (accepted/completed) sessions
    existing_sessions = (
        db.query(HabitSession)
        .filter(
            HabitSession.habit_id == habit.id,
            HabitSession.week_iso == week_iso,
            HabitSession.status.notin_(["dismissed"]),
        )
        .all()
    )
    existing_minutes = sum(
        (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
        for s in existing_sessions
    )
    remaining_minutes = max(0, target_minutes - existing_minutes)
    if remaining_minutes < 15:
        return 0

    # Collect available days (skip past, skip krank)
    available_days = []
    for day_offset in range(7):
        day = week_start + timedelta(days=day_offset)
        if day < today:
            continue
        day_type = _get_daytype_display(db, day)
        if day_type == "krank":
            continue
        available_days.append(day)

    if not available_days:
        return 0

    # Distribute remaining minutes across available days
    created = 0
    session_cap = habit.session_duration_minutes

    # Calculate how many days we actually need (avoid spreading too thin)
    min_session = max(session_cap, 15)
    days_needed = max(1, ceil(remaining_minutes / min_session))
    # Use at most days_needed days, spread evenly across the week
    if days_needed < len(available_days):
        step = len(available_days) / days_needed
        selected_days = [available_days[int(i * step)] for i in range(days_needed)]
    else:
        selected_days = available_days

    per_day = remaining_minutes / len(selected_days)

    for day in selected_days:
        day_budget = per_day
        while day_budget >= min(session_cap * 0.5, 15):
            this_session = min(session_cap, int(day_budget))
            if this_session < 15:
                break
            result = _place_session(
                db, habit, day, occupied, habit_minutes_per_day, week_iso,
                session_type="standard", max_minutes=this_session,
            )
            if result:
                created += 1
                actual = (_ensure_aware(result.end) - _ensure_aware(result.start)).total_seconds() / 60
                day_budget -= actual
            else:
                break

    return created


# --- Main scheduler entry point ---

def schedule_week(db: Session, week_start: date) -> int:
    """Schedule habit sessions for a given week. Returns count of newly created sessions.

    Uses a clean-slate approach: deletes all pending non-pinned sessions first,
    then schedules from scratch. This prevents session accumulation from merging.

    Transaction safety: the delete+create cycle is wrapped in a savepoint so that
    a failure during session creation rolls back to the pre-delete state rather
    than leaving the week with deleted sessions and no replacements.
    """
    week_end = week_start + timedelta(days=6)
    week_iso = _week_iso_str(week_start)

    habits = db.query(Habit).filter(Habit.active == True).all()
    if not habits:
        return 0

    # Use a savepoint so we can roll back the delete+create atomically
    # without affecting the outer transaction.
    nested = db.begin_nested()
    try:
        # Clean slate: delete all pending non-pinned sessions for this week
        deleted_count = db.query(HabitSession).filter(
            HabitSession.week_iso == week_iso,
            HabitSession.status == "pending",
            HabitSession.pinned == False,
        ).delete()

        # Also clear auto-rest off-days for this week (they'll be recreated if needed)
        db.query(DailyLoad).filter(
            DailyLoad.date >= week_start,
            DailyLoad.date <= week_end,
            DailyLoad.is_off_day == True,
            DailyLoad.off_day_source == "auto_rest",
        ).update({"is_off_day": False, "off_day_source": None})
        db.flush()

        occupied = _get_occupied_slots(db, week_start, week_end)
        created = 0

        # Per-day tracking for daily cap checks (includes accepted/completed sessions)
        habit_minutes_per_day: dict[date, float] = {}
        learning_minutes_per_day: dict[date, float] = {}
        for day_offset in range(7):
            d = week_start + timedelta(days=day_offset)
            d_start = datetime(d.year, d.month, d.day, tzinfo=BERLIN)
            d_end = d_start + timedelta(days=1)
            day_sessions = (
                db.query(HabitSession)
                .filter(
                    HabitSession.start < d_end,
                    HabitSession.end > d_start,
                    HabitSession.status.notin_(["dismissed"]),
                )
                .all()
            )
            habit_minutes_per_day[d] = sum(
                (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
                for s in day_sessions
            )
            # Track learning minutes separately
            learning_sessions = [
                s for s in day_sessions
                if s.habit and s.habit.category == "lernen"
            ]
            learning_minutes_per_day[d] = sum(
                (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
                for s in learning_sessions
            )

        # Dispatch: learning habits first (smallest target first), then lesen, then sonstige.
        # Innerhalb jeder Gruppe: Habits mit gefährdetem Wochenminimum zuerst (greifen Slots
        # vor den anderen, steigen damit über normale/Pool-Todos).
        def _endangered(h: Habit) -> int:
            return 0 if weekly_minimum_endangered(db, h, week_iso) else 1

        learning_habits = sorted(
            [h for h in habits if h.category == "lernen"],
            key=lambda h: (_endangered(h), h.target_hours_per_week),
        )
        lesen_habits = sorted(
            [h for h in habits if h.category == "lesen"], key=_endangered
        )
        sonstige_habits = sorted(
            [h for h in habits if h.category == "sonstige"], key=_endangered
        )

        for habit in learning_habits:
            created += _schedule_learning_habit(
                db, habit, week_start, week_iso, occupied, habit_minutes_per_day,
                learning_minutes_per_day,
            )

        for habit in lesen_habits:
            created += _schedule_simple_habit(
                db, habit, week_start, week_iso, occupied, habit_minutes_per_day,
            )

        for habit in sonstige_habits:
            created += _schedule_simple_habit(
                db, habit, week_start, week_iso, occupied, habit_minutes_per_day,
            )

        db.flush()
        nested.commit()
    except Exception:
        nested.rollback()
        logger.exception(
            "schedule_week failed for %s: savepoint rolled back, pending sessions restored",
            week_iso,
        )
        raise

    db.commit()
    return created


# --- Reschedule single session ---

def reschedule_single_session(
    db: Session,
    dismissed_session: HabitSession,
    duration_override: int | None = None,
) -> HabitSession | None:
    """Try to find a new slot for a dismissed session within the same week."""
    habit = dismissed_session.habit
    if not habit or not habit.active:
        return None

    year, week_num = dismissed_session.week_iso.split("-W")
    jan4 = date(int(year), 1, 4)
    week_start = jan4 - timedelta(days=jan4.weekday()) + timedelta(weeks=int(week_num) - 1)
    week_end = week_start + timedelta(days=6)

    today = date.today()
    search_start = max(week_start, today)

    if search_start > week_end:
        return None

    occupied = _get_occupied_slots(db, week_start, week_end)

    now = datetime.now(timezone.utc)
    day_start_berlin = datetime(today.year, today.month, today.day, tzinfo=BERLIN)
    occupied.append((day_start_berlin, now))

    session_duration = duration_override if duration_override else habit.session_duration_minutes

    search_days = []
    for day_offset in range((week_end - search_start).days + 1):
        search_days.append(search_start + timedelta(days=day_offset))

    # For learning habits, prefer weekend-like days and skip off-days
    if habit.category == "lernen":
        search_days = [d for d in search_days if not _is_off_day(db, d)]
        search_days.sort(
            key=lambda d: (
                0 if _get_daytype_display(db, d) in ("wochenende", "urlaub", "feiertag") else 1,
                d,
            )
        )
    elif habit.learning_mode:
        search_days.sort(
            key=lambda d: (
                0 if _get_daytype_display(db, d) in ("wochenende", "urlaub", "feiertag") else 1,
                d,
            )
        )

    from backend.sleep_schedule import get_buffer_start as _get_buf_start

    for day in search_days:
        day_type = _get_daytype_display(db, day)

        if day_type == "krank":
            continue

        is_wl = day_type in ("wochenende", "urlaub", "feiertag")
        w_start = habit.weekend_start if is_wl else habit.weekday_start
        w_end = habit.weekend_end if is_wl else habit.weekday_end

        # Clamp window end to buffer zone start
        buf_h, buf_m = _get_buf_start(day_type)
        buf_str = f"{buf_h:02d}:{buf_m:02d}"
        if buf_str < w_end:
            w_end = buf_str

        free_slots = _find_free_slots(day, w_start, w_end, occupied, session_duration)
        if free_slots:
            # General daily cap check
            daily_cap = WEEKEND_DAILY_CAP_MINUTES if is_wl else WEEKDAY_DAILY_CAP_MINUTES
            day_start_dt = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
            day_end_dt = day_start_dt + timedelta(days=1)
            current_day_sessions = (
                db.query(HabitSession)
                .filter(
                    HabitSession.start < day_end_dt,
                    HabitSession.end > day_start_dt,
                    HabitSession.status.notin_(["dismissed"]),
                )
                .all()
            )
            current_total = sum(
                (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
                for s in current_day_sessions
            )
            if current_total + session_duration > daily_cap:
                continue

            # Learning-specific daily cap check
            if habit.category == "lernen":
                learning_cap = WEEKEND_LEARNING_CAP_MINUTES if is_wl else WEEKDAY_LEARNING_CAP_MINUTES
                existing_learning = _get_existing_learning_minutes(db, day)
                if existing_learning + session_duration > learning_cap:
                    continue

            slot_start, slot_end = free_slots[0]
            session_end = slot_start + timedelta(minutes=session_duration)
            if session_end > slot_end:
                session_end = slot_end

            new_session = HabitSession(
                habit_id=habit.id,
                start=slot_start,
                end=session_end,
                status="pending",
                week_iso=dismissed_session.week_iso,
                session_type=dismissed_session.session_type if dismissed_session.session_type != "standard" else "standard",
            )
            db.add(new_session)
            db.commit()
            _merge_same_day_sessions(db, habit.id, dismissed_session.week_iso)
            db.commit()
            db.refresh(new_session)
            return new_session

    return None


# --- Reschedule week ---

def reschedule_week(db: Session, target_date: date):
    """Reschedule the week. schedule_week handles clean-slate internally."""
    week_start = _week_start_from_date(target_date)
    schedule_week(db, week_start)


# --- Learning dismiss handler ---

def dismiss_learning_session_day(
    db: Session,
    dismissed_session: HabitSession,
) -> dict:
    """Handle dismissing a learning session: marks day as off-day, dismisses all learning sessions.

    Returns info dict with off_day_activated, deferred_minutes, message.
    """
    habit = dismissed_session.habit
    session_date = _ensure_aware(dismissed_session.start).date()

    # 1. Mark day as off-day
    load = get_or_create_daily_load(db, session_date)
    load.is_off_day = True
    load.off_day_source = "dismiss"

    # 2. Dismiss all pending learning sessions on this day
    d_start = datetime(session_date.year, session_date.month, session_date.day, tzinfo=BERLIN)
    d_end = d_start + timedelta(days=1)
    day_learning_sessions = (
        db.query(HabitSession)
        .join(Habit)
        .filter(
            HabitSession.start < d_end,
            HabitSession.end > d_start,
            HabitSession.status == "pending",
            Habit.category == "lernen",
        )
        .all()
    )

    total_deferred = 0
    for s in day_learning_sessions:
        minutes = int((_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60)
        total_deferred += minutes
        s.status = "dismissed"

    # 3. Store deferred minutes
    load.deferred_minutes += total_deferred
    db.flush()

    return {
        "off_day_activated": True,
        "deferred_minutes": total_deferred,
        "message": "Ruhetag aktiviert. Die verpasste Zeit wird sanft auf die naechsten Tage verteilt.",
    }


def check_conflicts_after_event_change(db: Session, event: Event):
    """Reschedule the entire week(s) affected by the event to optimally redistribute habits."""
    evt_start = _ensure_aware(event.start)
    target_date = evt_start.date()
    reschedule_week(db, target_date)

    evt_end = _ensure_aware(event.end)
    end_date = (evt_end - timedelta(seconds=1)).date()
    if _week_start_from_date(end_date) != _week_start_from_date(target_date):
        reschedule_week(db, end_date)


# --- Tagesziel ↔ Habit-Override ---

def apply_goal_override(db: Session, goal: DailyGoal) -> int:
    """Markiere Habit-Sessions, die ein Ziel mit Zeitblock verdrängt, als 'overridden'.

    Termine bleiben unberührt (Events sind harte Blocker). Nur Sessions von Habits mit
    can_be_overridden_by_goals werden verdrängt; verdrängte Sessions verschwinden nicht,
    sondern werden markiert (overridden_by_goal_id) und die Woche neu gerechnet, damit der
    Habit anderswo neu eingeplant wird ('rescheduled'). Gibt Anzahl verdrängter Sessions.
    """
    if not goal.scheduled_start or not goal.scheduled_end:
        return 0
    if goal.status not in _ACTIVE_GOAL_STATUSES:
        return 0

    g_start = _ensure_aware(goal.scheduled_start)
    g_end = _ensure_aware(goal.scheduled_end)

    sessions = (
        db.query(HabitSession)
        .join(Habit)
        .filter(
            HabitSession.start < g_end,
            HabitSession.end > g_start,
            HabitSession.status.in_(["pending", "accepted"]),
            Habit.can_be_overridden_by_goals == True,  # noqa: E712
        )
        .all()
    )
    count = 0
    for s in sessions:
        s.status = "overridden"
        s.overridden_by_goal_id = goal.id
        count += 1
    db.flush()

    if count:
        reschedule_week(db, g_start.date())
    return count


def release_goal_block(db: Session, goal: DailyGoal) -> int:
    """Gib durch ein Ziel belegte Zeit wieder frei (z.B. bei 'Ziel aufgeben').

    Verdrängte Habit-Sessions werden wieder freigegeben und die Woche neu gerechnet, sodass
    Habits/Pool-Todos den Tag weiterplanen können. Verknüpfte Todos bleiben unberührt (offen).
    """
    overridden = (
        db.query(HabitSession)
        .filter(HabitSession.overridden_by_goal_id == goal.id)
        .all()
    )
    count = 0
    for s in overridden:
        s.overridden_by_goal_id = None
        if s.status == "overridden":
            s.status = "pending"
        count += 1
    db.flush()

    # Tag neu rechnen: Goal blockiert nicht mehr (Status meist 'abandoned'), Slots werden frei.
    target = _ensure_aware(goal.scheduled_start).date() if goal.scheduled_start else goal.date
    reschedule_week(db, target)
    return count


# --- Freie-Slot-Berechnung + Pool-Todo-Auto-Plan ---

_TODO_PRIORITY_ORDER = {"dringend": 0, "hoch": 1, "mittel": 2, "niedrig": 3}
DEFAULT_TODO_MINUTES = 30          # Annahme, wenn estimated_minutes fehlt
DAY_PLAN_WINDOW_START = "08:00"
DAY_PLAN_WINDOW_END = "22:00"


def weekly_minimum_endangered(db: Session, habit: Habit, week_iso: str) -> bool:
    """True, wenn das gesetzte Wochenminimum eines Habits noch nicht erreicht ist."""
    if not habit.weekly_minimum_hours:
        return False
    sessions = (
        db.query(HabitSession)
        .filter(
            HabitSession.habit_id == habit.id,
            HabitSession.week_iso == week_iso,
            HabitSession.status.in_(["accepted", "completed"]),
        )
        .all()
    )
    done_min = sum(
        (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
        for s in sessions
    )
    return done_min < habit.weekly_minimum_hours * 60


def compute_free_slots(
    db: Session,
    day: date,
    window_start: str = DAY_PLAN_WINDOW_START,
    window_end: str = DAY_PLAN_WINDOW_END,
    min_minutes: int = 15,
) -> list[tuple[datetime, datetime]]:
    """Freie Zeitfenster eines Tages (Events + Sessions + Goal-Blöcke + geplante Todos abgezogen)."""
    ws = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
    we = ws + timedelta(days=1)
    occupied = _get_occupied_slots(db, day, day)

    # Bereits geplante Todos (planned_day/fixed_slot mit Zeitblock) ebenfalls blockieren.
    planned = (
        db.query(Todo)
        .filter(
            Todo.scheduled_start.isnot(None),
            Todo.scheduled_end.isnot(None),
            Todo.scheduled_start < we,
            Todo.scheduled_end > ws,
            Todo.status.notin_(["done", "cancelled"]),
            Todo.completed == False,  # noqa: E712
        )
        .all()
    )
    for t in planned:
        occupied.append((_ensure_aware(t.scheduled_start), _ensure_aware(t.scheduled_end)))

    # Vergangene Tageszeit von heute als belegt markieren.
    now = datetime.now(BERLIN)
    if day == now.date():
        occupied.append((ws, now))

    return _find_free_slots(day, window_start, window_end, occupied, min_minutes)


def _eligible_pool_todos(db: Session, day: date) -> list[Todo]:
    """Pool-Todos, die an diesem Tag eingeplant werden dürfen."""
    todos = (
        db.query(Todo)
        .filter(
            Todo.scheduling_mode == "pool",
            Todo.completed == False,  # noqa: E712
            Todo.status.notin_(["done", "cancelled"]),
        )
        .all()
    )
    eligible = []
    for t in todos:
        if t.earliest_start_date and t.earliest_start_date > day:
            continue
        if t.due_date and t.due_date < day:
            # überfällig: trotzdem einplanbar (hohe Dringlichkeit)
            pass
        eligible.append(t)

    def _key(t: Todo) -> tuple:
        return (
            _TODO_PRIORITY_ORDER.get(t.priority, 2),
            t.due_date or date.max,
            t.estimated_minutes or DEFAULT_TODO_MINUTES,
        )

    eligible.sort(key=_key)
    return eligible


# Präferenz-bewusste Slot-Bewertung für Pool-Todos. Todos tragen keinen activity_type,
# daher dient der generische Tageszeit-Score (preferences.generic_time_curve) als bester
# „wann bin ich gut drauf?"-Proxy; ohne Lern-Daten greift eine Energie-Heuristik.
_TODO_ENERGY_WEIGHT = {"hoch": 1.5, "mittel": 1.0, "niedrig": 0.6}


def _slot_pref_score(todo: Todo, start: datetime, day: date, time_curve: dict) -> float:
    """Wie gut passt dieser Slot zur Aufgabe? Höher = besser. Deterministisch, None-safe."""
    from backend.preferences import bucket_for_hour

    hour = _ensure_aware(start).astimezone(BERLIN).hour
    gf = time_curve.get(bucket_for_hour(hour))  # None ohne genug Lern-Daten für den Bucket
    weight = _TODO_ENERGY_WEIGHT.get(todo.energy_required, 1.0)
    score = 0.0
    if gf is not None:
        score += gf * weight
    else:
        # Fallback: Anspruchsvolles in den frischen Vormittag, Lockeres eher später.
        if todo.energy_required == "hoch":
            score += 1.0 if hour < 13 else -0.6
        elif todo.energy_required == "niedrig":
            score += 0.3 if hour >= 14 else 0.0
    if todo.due_date and todo.due_date <= day:  # Fristsachen zuerst am Tag
        score += 0.4
    score -= max(0, hour - 8) * 0.02  # sanfter „früher ist besser"-Tiebreak
    return score


def _slot_reason(todo: Todo, start: datetime, day: date, time_curve: dict) -> str:
    """Menschenlesbare Begründung für die Slot-Wahl (immer sichtbar: Owner-Weiche)."""
    from backend.preferences import bucket_for_hour

    hour = _ensure_aware(start).astimezone(BERLIN).hour
    bucket = bucket_for_hour(hour)
    gf = time_curve.get(bucket)
    if todo.due_date and todo.due_date < day:
        return f"überfällig, in {bucket} eingeplant"
    if todo.energy_required == "hoch":
        if gf is not None and gf >= 0.3:
            return f"anspruchsvoll → {bucket}, wo du gut drauf bist"
        return f"anspruchsvoll → in den frischen {bucket}"
    if gf is not None and gf >= 0.3:
        return f"{bucket} passt dir erfahrungsgemäß gut"
    return f"in {bucket} eingeplant"


def auto_plan_todos(db: Session, day: date, commit: bool = False, capacity: dict | None = None, prefer_time: bool = False) -> dict:
    """Plane Pool-Todos in freie Slots ein (Vorschlag oder verbindlich bei commit).

    Kleine Todos zuerst in kleine Slots, große brauchen längere Slots. Respektiert
    estimated_minutes, due_date, earliest_start_date. Termine/Ziele/Habits bleiben unberührt.

    **Energie-bewusst (adaptive Sekretär):** Existiert für den Tag ein `DailyCheckIn`,
    wird die Tageskapazität berücksichtigt: Todos mit zu hoher `energy_required` werden
    an geschonten Tagen übersprungen und das geplante Gesamtvolumen bei `cap_minutes`
    gedeckelt (Burnout-Schutz: bewusst Luft lassen). **Ohne Check-in** greift nichts davon
    → identisches Verhalten wie vor der Epoche (rückwärtskompatibel).
    """
    from backend.daily_energy import compute_day_capacity, energy_fits
    from backend.preferences import generic_time_curve

    if capacity is None:
        capacity = compute_day_capacity(db, day)
    # Präferenz-Kurve nur bei prefer_time laden (ein Query), sonst Alt-Verhalten unberührt.
    time_curve = generic_time_curve(db, day) if prefer_time else {}
    # Adaptive Zwänge nur mit explizitem Tagessignal (Check-in), sonst Alt-Verhalten.
    apply_adaptive = bool(capacity.get("has_checkin"))
    max_energy = capacity.get("max_task_energy", "hoch")
    cap_minutes = capacity.get("cap_minutes")

    free_slots = compute_free_slots(db, day)
    todos = _eligible_pool_todos(db, day)

    # verbleibende Kapazität je Slot
    slots = [[s, e, int((e - s).total_seconds() / 60)] for s, e in free_slots]
    suggestions: list[dict] = []
    deferred: list[dict] = []
    planned_minutes = 0

    for todo in todos:
        need = todo.estimated_minutes or DEFAULT_TODO_MINUTES

        # Energie-Filter: an geschonten Tagen keine kraftraubenden Aufgaben einplanen.
        if apply_adaptive and not energy_fits(todo.energy_required, max_energy):
            deferred.append({"todo_id": todo.id, "title": todo.title, "reason": "energie"})
            continue
        # Kapazitäts-Deckel: den Tag bewusst nicht randvoll planen.
        if apply_adaptive and cap_minutes is not None and planned_minutes + need > cap_minutes:
            deferred.append({"todo_id": todo.id, "title": todo.title, "reason": "kapazitaet"})
            continue

        # Slot-Wahl: standardmäßig kleinster passender Slot (gute Ausnutzung); mit
        # prefer_time der Slot mit der besten Tageszeit-Passung (kleinerer Slot als Tiebreak).
        fitting = [s for s in slots if s[2] >= need]
        if prefer_time:
            fitting.sort(key=lambda s: (-_slot_pref_score(todo, s[0], day, time_curve), s[2]))
        else:
            fitting.sort(key=lambda s: s[2])
        candidate = fitting[0] if fitting else None
        if not candidate:
            continue
        start = candidate[0]
        end = start + timedelta(minutes=need)
        suggestions.append({
            "todo_id": todo.id,
            "title": todo.title,
            "start": start,
            "end": end,
            "minutes": need,
            "reason": _slot_reason(todo, start, day, time_curve) if prefer_time else None,
        })
        planned_minutes += need
        # Slot kürzen (+10 min Puffer)
        candidate[0] = end + timedelta(minutes=10)
        candidate[2] = int((candidate[1] - candidate[0]).total_seconds() / 60)

        if commit:
            todo.scheduling_mode = "planned_day"
            todo.planned_date = day
            todo.scheduled_start = start
            todo.scheduled_end = end
            todo.status = "scheduled"

    if commit:
        db.commit()

    return {
        "date": day,
        "free_slots": [
            {"start": s, "end": e, "minutes": int((e - s).total_seconds() / 60)}
            for s, e in free_slots
        ],
        "suggestions": suggestions,
        "deferred": deferred,
        "planned_minutes": planned_minutes,
        "capacity": capacity,
        "committed": commit,
    }


def list_backlog(db: Session, threshold: int = 3) -> list[dict]:
    """Offene Aufgaben, die zu oft aufgeschoben wurden (`defer_count >= threshold`): read-only.

    Basis für die UI-Warnung und den Eskalations-Nudge. Sortiert nach Aufschub-Häufigkeit.
    """
    todos = (
        db.query(Todo)
        .filter(
            Todo.completed == False,  # noqa: E712
            Todo.status.notin_(["done", "cancelled"]),
            Todo.defer_count >= threshold,
        )
        .order_by(Todo.defer_count.desc())
        .all()
    )
    return [
        {
            "todo_id": t.id,
            "title": t.title,
            "defer_count": t.defer_count or 0,
            "due_date": t.due_date.isoformat() if t.due_date else None,
        }
        for t in todos
    ]


def escalate_backlog(db: Session, day: date) -> int:
    """Automatische Verfall-Erkennung: überfällige, unerledigte Aufgaben zählen **einmal pro
    Tag** als „wieder verschoben" (`defer_count += 1`). Idempotent über `last_deferred_at`
    (mehrere Tick-Läufe am selben Tag zählen nicht doppelt). Gibt die Zahl neu Hochgezählter
    zurück. Ergänzt das manuelle `POST /api/todos/{id}/defer` um die stille-Verrottung-Erkennung.
    """
    overdue = (
        db.query(Todo)
        .filter(
            Todo.completed == False,  # noqa: E712
            Todo.status.notin_(["done", "cancelled", "scheduled"]),
            Todo.due_date.isnot(None),
            Todo.due_date < day,
        )
        .all()
    )
    # Idempotenz über den realen Kalendertag der Zählung (nicht `day`): ein Aufschlag pro Tag,
    # egal wie oft der 60s-Tick läuft, und deckt sich mit dem manuellen `/defer` (gleicher Tag).
    now_day = datetime.now(BERLIN).date()
    bumped = 0
    for t in overdue:
        last = t.last_deferred_at
        last_day = _ensure_aware(last).astimezone(BERLIN).date() if last else None
        if last_day == now_day:
            continue  # heute schon erfasst
        t.defer_count = (t.defer_count or 0) + 1
        t.last_deferred_at = datetime.now(BERLIN)
        t.defer_reason = "überfällig, nicht erledigt"
        bumped += 1
    if bumped:
        db.commit()
    return bumped
