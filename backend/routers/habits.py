from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from backend.database import get_db
from backend.models import DailyLoad, Habit, HabitSession, Project
from backend.scheduler import (
    WEEKDAY_NAMES,
    _ensure_aware,
    _get_strain_level,
    _is_off_day,
    calculate_focus_minutes,
    calculate_strain,
    dismiss_learning_session_day,
    get_or_create_daily_load,
    reschedule_single_session,
    reschedule_week,
    schedule_week,
    update_daily_load_learning,
)
from backend.routers.daytype import _get_daytype_display
from backend.wanduhr import als_wanduhr, jetzt
from backend.schemas import (
    DailyLoadResponse,
    HabitCreate,
    HabitResponse,
    HabitSessionAction,
    HabitSessionResponse,
    HabitUpdate,
    HabitWeeklyProgress,
    LoadOverviewResponse,
)

router = APIRouter(prefix="/api/habits", tags=["habits"])


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _week_iso(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _parse_week_param(week: str | None) -> tuple[date, str]:
    """Parse an optional week string '2026-W09' or default to current week."""
    if week:
        try:
            year, w = week.split("-W")
            jan4 = date(int(year), 1, 4)
            ws = jan4 - timedelta(days=jan4.weekday()) + timedelta(weeks=int(w) - 1)
            return ws, week
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="Ungueliges Wochenformat, benutze YYYY-WNN")
    today = date.today()
    ws = _week_start(today)
    return ws, _week_iso(today)


def _session_to_response(s: HabitSession) -> dict:
    return {
        "id": s.id,
        "habit_id": s.habit_id,
        "start": s.start,
        "end": s.end,
        "status": s.status,
        "week_iso": s.week_iso,
        "focus_minutes_completed": s.focus_minutes_completed,
        "session_type": s.session_type,
        "pinned": s.pinned,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "habit_name": s.habit.name if s.habit else None,
        "habit_color": s.habit.color if s.habit else None,
    }


def _update_daily_load_on_completion(db: Session, session: HabitSession):
    """Update DailyLoad when a learning session completes."""
    if session.habit and session.habit.category == "lernen":
        session_date = _ensure_aware(session.start).date()
        update_daily_load_learning(db, session_date)


# --- Habit CRUD ---

@router.get("", response_model=list[HabitResponse])
def list_habits(db: Session = Depends(get_db)):
    return db.query(Habit).order_by(Habit.name).all()


@router.post("", response_model=HabitResponse, status_code=201)
def create_habit(data: HabitCreate, db: Session = Depends(get_db)):
    if data.project_id:
        project = db.query(Project).filter(Project.id == data.project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Projekt nicht gefunden")
    habit = Habit(**data.model_dump())
    db.add(habit)
    db.commit()
    db.refresh(habit)
    # Trigger scheduling for current + next 3 weeks
    today = date.today()
    for i in range(4):
        schedule_week(db, _week_start(today + timedelta(weeks=i)))
    return habit


@router.get("/weekly-progress", response_model=list[HabitWeeklyProgress])
def weekly_progress(week: str | None = Query(None), db: Session = Depends(get_db)):
    week_start_date, week_iso = _parse_week_param(week)

    # Auto-schedule if no actionable sessions exist for this week
    actionable_count = (
        db.query(HabitSession)
        .filter(
            HabitSession.week_iso == week_iso,
            HabitSession.status.notin_(["dismissed"]),
        )
        .count()
    )
    active_habits = db.query(Habit).filter(Habit.active == True).count()
    if actionable_count == 0 and active_habits > 0:
        schedule_week(db, week_start_date)

    habits = db.query(Habit).filter(Habit.active == True).options(joinedload(Habit.project)).all()
    # Deduplicate (joinedload can cause duplicate rows)
    seen_ids = set()
    unique_habits = []
    for h in habits:
        if h.id not in seen_ids:
            seen_ids.add(h.id)
            unique_habits.append(h)
    habits = unique_habits
    now = datetime.now(timezone.utc)
    result = []

    for habit in habits:
        sessions = (
            db.query(HabitSession)
            .filter(HabitSession.habit_id == habit.id, HabitSession.week_iso == week_iso)
            .all()
        )

        # Auto-accept pending sessions whose start time has passed (zero-friction)
        for s in sessions:
            if s.status == "pending" and _ensure_aware(s.start) <= now and _ensure_aware(s.end) > now:
                s.status = "accepted"
        # Auto-complete accepted sessions whose end time has passed
        for s in sessions:
            if s.status == "accepted" and _ensure_aware(s.end) <= now:
                s.status = "completed"
                if habit.learning_mode and s.focus_minutes_completed is None:
                    dur = int((_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60)
                    s.focus_minutes_completed = calculate_focus_minutes(
                        dur, habit.focus_block_minutes, habit.break_minutes
                    )
                _update_daily_load_on_completion(db, s)
        # Vergangene pending Sessions als verpasst markieren (verhindert Aufstauen)
        for s in sessions:
            if s.status == "pending" and _ensure_aware(s.end) <= now:
                s.status = "dismissed"
        db.commit()

        completed_minutes = sum(
            (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
            for s in sessions if s.status == "completed"
        )
        accepted_minutes = sum(
            (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
            for s in sessions if s.status == "accepted"
        )
        pending_count = sum(1 for s in sessions if s.status == "pending")

        # Next upcoming session
        upcoming = [s for s in sessions if s.status in ("pending", "accepted") and _ensure_aware(s.start) > now]
        upcoming.sort(key=lambda s: s.start)
        next_session = upcoming[0].start if upcoming else None

        # Focus metrics for learning mode habits
        focus_fields = {}
        if habit.learning_mode:
            focus_target = habit.target_hours_per_week * 60
            focus_completed = sum(
                s.focus_minutes_completed
                if s.focus_minutes_completed is not None
                else calculate_focus_minutes(
                    int((_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60),
                    habit.focus_block_minutes, habit.break_minutes,
                )
                for s in sessions if s.status == "completed"
            )
            focus_accepted = sum(
                calculate_focus_minutes(
                    int((_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60),
                    habit.focus_block_minutes, habit.break_minutes,
                )
                for s in sessions if s.status == "accepted"
            )
            focus_done = focus_completed + focus_accepted
            focus_fields = {
                "learning_mode": True,
                "focus_minutes_target": round(focus_target, 1),
                "focus_minutes_done": round(focus_done, 1),
                "pomodoros_target": round(focus_target / habit.focus_block_minutes, 1),
                "pomodoros_done": round(focus_done / habit.focus_block_minutes, 1),
            }

        # Load management fields for learning habits
        load_fields = {}
        if habit.category == "lernen":
            today = date.today()
            current_strain = calculate_strain(db, today)
            # Count off-days this week
            off_count = (
                db.query(DailyLoad)
                .filter(
                    DailyLoad.date >= week_start_date,
                    DailyLoad.date <= week_start_date + timedelta(days=6),
                    DailyLoad.is_off_day == True,
                )
                .count()
            )
            # Sum deferred minutes this week
            deferred = sum(
                dl.deferred_minutes
                for dl in db.query(DailyLoad).filter(
                    DailyLoad.date >= week_start_date,
                    DailyLoad.date <= week_start_date + timedelta(days=6),
                    DailyLoad.deferred_minutes > 0,
                ).all()
            )
            load_fields = {
                "strain": round(current_strain, 2),
                "off_days_this_week": off_count,
                "deferred_minutes": deferred,
            }

        result.append(HabitWeeklyProgress(
            habit_id=habit.id,
            name=habit.name,
            color=habit.color,
            project_name=habit.project.name if habit.project else None,
            target_hours=habit.target_hours_per_week,
            completed_hours=round(completed_minutes / 60, 1),
            accepted_hours=round(accepted_minutes / 60, 1),
            pending_count=pending_count,
            next_session=next_session,
            category=habit.category,
            **focus_fields,
            **load_fields,
        ))

    return result


@router.get("/load-overview")
def load_overview(week: str | None = Query(None), db: Session = Depends(get_db)):
    """Weekly load overview with strain data per day."""
    week_start_date, week_iso = _parse_week_param(week)
    today = date.today()
    current_strain = calculate_strain(db, today)

    days = []
    for day_offset in range(7):
        d = week_start_date + timedelta(days=day_offset)
        load = db.query(DailyLoad).filter(DailyLoad.date == d).first()
        day_type = _get_daytype_display(db, d)

        days.append(DailyLoadResponse(
            date=d,
            weekday=WEEKDAY_NAMES[d.weekday()],
            day_type=day_type,
            learning_minutes=load.learning_minutes if load else 0,
            strain=round(load.strain, 2) if load else 0.0,
            is_off_day=load.is_off_day if load else False,
            off_day_source=load.off_day_source if load else None,
            deferred_minutes=load.deferred_minutes if load else 0,
        ))

    return LoadOverviewResponse(
        week_iso=week_iso,
        current_strain=round(current_strain, 2),
        strain_level=_get_strain_level(current_strain),
        days=days,
    )


@router.post("/off-day")
def set_off_day(
    target_date: date = Query(..., alias="date"),
    db: Session = Depends(get_db),
):
    """Manually set a learning off-day. Dismisses all pending learning sessions on that day."""
    load = get_or_create_daily_load(db, target_date)
    if load.is_off_day:
        raise HTTPException(status_code=400, detail="Tag ist bereits ein Ruhetag")

    load.is_off_day = True
    load.off_day_source = "manual"

    # Dismiss all pending learning sessions on this day
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
    d_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=BERLIN)
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

    load.deferred_minutes += total_deferred
    db.commit()

    # Trigger reschedule for the week
    reschedule_week(db, target_date)

    return {
        "date": target_date.isoformat(),
        "is_off_day": True,
        "dismissed_sessions": len(day_learning_sessions),
        "deferred_minutes": total_deferred,
        "message": "Ruhetag gesetzt. Lern-Sessions wurden verschoben.",
    }


@router.get("/sessions", response_model=list[HabitSessionResponse])
def list_sessions(
    week: str | None = Query(None),
    habit_id: str | None = Query(None),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(HabitSession).options(joinedload(HabitSession.habit))
    if week:
        q = q.filter(HabitSession.week_iso == week)
    if habit_id:
        q = q.filter(HabitSession.habit_id == habit_id)
    if status:
        q = q.filter(HabitSession.status == status)
    sessions = q.order_by(HabitSession.start).all()
    return [_session_to_response(s) for s in sessions]


@router.get("/sessions/today")
def today_sessions(db: Session = Depends(get_db)):
    """Return today's sessions (pending/accepted/completed, not dismissed) with habit info."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Berlin")
    now_local = datetime.now(tz)
    # Tagesgrenzen als Berliner Wanduhr ohne Zone (Hauskonvention backend/wanduhr.py).
    # Vorher stand hier .astimezone(utc): SQLite verwirft die Zone des
    # Vergleichswerts, das SQL-Fenster lag damit um den UTC-Versatz daneben und
    # Sessions zwischen 22:00 und 24:00 fehlten in der Tagesansicht.
    today_start = als_wanduhr(now_local.replace(hour=0, minute=0, second=0, microsecond=0))
    tomorrow_start = als_wanduhr((now_local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
    now = datetime.now(timezone.utc)

    sessions = (
        db.query(HabitSession)
        .options(joinedload(HabitSession.habit))
        .filter(
            HabitSession.start < tomorrow_start,
            HabitSession.end > today_start,
            HabitSession.status.notin_(["dismissed"]),
        )
        .order_by(HabitSession.start)
        .all()
    )

    # Auto-accept pending sessions whose start time has passed (zero-friction)
    for s in sessions:
        if s.status == "pending" and _ensure_aware(s.start) <= now and _ensure_aware(s.end) > now:
            s.status = "accepted"
    # Auto-complete accepted sessions whose end time has passed
    for s in sessions:
        if s.status == "accepted" and _ensure_aware(s.end) <= now:
            s.status = "completed"
            if s.habit and s.habit.learning_mode and s.focus_minutes_completed is None:
                dur = int((_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60)
                s.focus_minutes_completed = calculate_focus_minutes(
                    dur, s.habit.focus_block_minutes, s.habit.break_minutes
                )
            _update_daily_load_on_completion(db, s)
    # Auto-dismiss pending sessions past their end time
    for s in sessions:
        if s.status == "pending" and _ensure_aware(s.end) <= now:
            s.status = "dismissed"
    db.commit()

    # Check if today is a learning off-day
    today_date = now_local.date()
    today_is_off_day = _is_off_day(db, today_date)

    # Re-filter after status changes (dismissed ones should be excluded)
    return [
        {
            **_session_to_response(s),
            "learning_mode": s.habit.learning_mode if s.habit else False,
            "category": s.habit.category if s.habit else "sonstige",
            "is_off_day": today_is_off_day,
        }
        for s in sessions
        if s.status != "dismissed"
    ]


@router.get("/sessions/upcoming", response_model=list[HabitSessionResponse])
def upcoming_sessions(
    days: int | None = Query(None, ge=1, le=31),
    db: Session = Depends(get_db),
):
    """Ohne `days`: Sessions der naechsten 10 Minuten (Notification-Polling des
    nativen Frontends). Mit `days=N`: pending-Sessions der naechsten N Tage
    (Saganta-BFF und -Oberflaeche schicken seit jeher `days=7`; bis zu diesem
    Parameter bekamen sie das 10-Minuten-Fenster und zeigten fast immer leer).
    """
    # Wanduhr statt UTC: die Fenstergrenzen gehen als SQL-Vergleichswerte in
    # die DB, dort liegen die Zeiten als Berliner Wanduhr ohne Zone. Mit UTC
    # lag das 10-Minuten-Fenster um den UTC-Versatz daneben und das
    # Notification-Polling fand nie die richtigen Sessions.
    now = jetzt()
    soon = now + (timedelta(days=days) if days else timedelta(minutes=10))
    sessions = (
        db.query(HabitSession)
        .options(joinedload(HabitSession.habit))
        .filter(
            HabitSession.status == "pending",
            HabitSession.start >= now,
            HabitSession.start <= soon,
        )
        .order_by(HabitSession.start)
        .all()
    )
    return [_session_to_response(s) for s in sessions]


@router.get("/sessions/calendar")
def sessions_calendar(
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    db: Session = Depends(get_db),
):
    """Return sessions in FullCalendar-compatible format."""
    q = db.query(HabitSession).options(joinedload(HabitSession.habit))
    if start:
        q = q.filter(HabitSession.end >= start)
    if end:
        q = q.filter(HabitSession.start <= end)
    q = q.filter(HabitSession.status.notin_(["dismissed"]))
    sessions = q.order_by(HabitSession.start).all()

    result = []
    for s in sessions:
        result.append({
            "id": f"habit-session-{s.id}",
            "title": s.habit.name if s.habit else "Habit",
            "start": s.start.isoformat(),
            "end": s.end.isoformat(),
            "allDay": False,
            "backgroundColor": s.habit.color if s.habit else "#4a9eff",
            "borderColor": s.habit.color if s.habit else "#4a9eff",
            "editable": False,
            "classNames": [
                "fc-habit-session",
                f"fc-habit-{s.status}",
            ],
            "extendedProps": {
                "is_habit_session": True,
                "session_id": s.id,
                "status": s.status,
                "habit_id": s.habit_id,
                "learning_mode": s.habit.learning_mode if s.habit else False,
                "focus_minutes_completed": s.focus_minutes_completed,
                "session_type": s.session_type,
                "category": s.habit.category if s.habit else "sonstige",
            },
        })
    return result


@router.get("/{habit_id}", response_model=HabitResponse)
def get_habit(habit_id: str, db: Session = Depends(get_db)):
    habit = db.query(Habit).filter(Habit.id == habit_id).first()
    if not habit:
        raise HTTPException(status_code=404, detail="Habit nicht gefunden")
    return habit


@router.put("/{habit_id}", response_model=HabitResponse)
def update_habit(habit_id: str, data: HabitUpdate, db: Session = Depends(get_db)):
    habit = db.query(Habit).filter(Habit.id == habit_id).first()
    if not habit:
        raise HTTPException(status_code=404, detail="Habit nicht gefunden")

    update_data = data.model_dump(exclude_unset=True)
    scheduling_fields = {
        "target_hours_per_week", "session_duration_minutes",
        "weekday_start", "weekday_end", "weekend_start", "weekend_end",
        "learning_mode", "focus_block_minutes", "break_minutes",
        "category", "weekday_target_ratio", "max_consecutive_days", "max_session_minutes",
    }
    needs_reschedule = any(k in update_data for k in scheduling_fields)
    was_inactive = not habit.active
    deactivated = "active" in update_data and not update_data["active"]
    reactivated = "active" in update_data and update_data["active"] and was_inactive

    if "project_id" in update_data and update_data["project_id"]:
        project = db.query(Project).filter(Project.id == update_data["project_id"]).first()
        if not project:
            raise HTTPException(status_code=404, detail="Projekt nicht gefunden")

    for key, value in update_data.items():
        setattr(habit, key, value)
    db.commit()
    db.refresh(habit)

    today = date.today()
    week_iso = _week_iso(today)

    if deactivated:
        # Delete all pending sessions for this habit
        db.query(HabitSession).filter(
            HabitSession.habit_id == habit.id,
            HabitSession.status == "pending",
        ).delete()
        db.commit()
        # Reschedule so other habits can fill freed slots
        for i in range(4):
            schedule_week(db, _week_start(today + timedelta(weeks=i)))
    elif reactivated or needs_reschedule:
        # Delete pending non-pinned sessions for this habit and reschedule all
        db.query(HabitSession).filter(
            HabitSession.habit_id == habit.id,
            HabitSession.status == "pending",
            HabitSession.pinned == False,
        ).delete()
        db.commit()
        for i in range(4):
            schedule_week(db, _week_start(today + timedelta(weeks=i)))
    else:
        # Always run schedule_week to fill any missing sessions (idempotent)
        for i in range(4):
            schedule_week(db, _week_start(today + timedelta(weeks=i)))

    return habit


@router.delete("/{habit_id}", status_code=204)
def delete_habit(habit_id: str, db: Session = Depends(get_db)):
    habit = db.query(Habit).filter(Habit.id == habit_id).first()
    if not habit:
        raise HTTPException(status_code=404, detail="Habit nicht gefunden")
    db.delete(habit)
    db.commit()
    # Reschedule so other habits can fill freed slots
    today = date.today()
    for i in range(4):
        schedule_week(db, _week_start(today + timedelta(weeks=i)))


@router.post("/sessions/{session_id}/action")
def session_action(session_id: str, data: HabitSessionAction, db: Session = Depends(get_db)):
    session = (
        db.query(HabitSession)
        .options(joinedload(HabitSession.habit))
        .filter(HabitSession.id == session_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session nicht gefunden")

    now = datetime.now(timezone.utc)

    if data.action == "accepted":
        session.status = "accepted"
        db.commit()
        db.refresh(session)
        return _session_to_response(session)

    elif data.action == "start_early":
        if session.status != "pending":
            raise HTTPException(status_code=400, detail="Nur pending Sessions koennen frueher gestartet werden")
        # als_wanduhr: gespeichert wird zonenlose Berliner Wanduhr; ein aware
        # UTC-Wert landet sonst als um den Versatz falsche Uhrzeit in der DB
        session.start = als_wanduhr(now)
        session.status = "accepted"
        db.commit()
        db.refresh(session)
        return _session_to_response(session)

    elif data.action == "cancelled":
        if session.status != "accepted":
            raise HTTPException(status_code=400, detail="Nur laufende Sessions koennen abgebrochen werden")
        original_end = _ensure_aware(session.end)
        session.end = als_wanduhr(now)
        session.status = "completed"
        if session.habit and session.habit.learning_mode:
            truncated_dur = max(0, int((now - _ensure_aware(session.start)).total_seconds() / 60))
            session.focus_minutes_completed = calculate_focus_minutes(
                truncated_dur, session.habit.focus_block_minutes, session.habit.break_minutes
            )
        _update_daily_load_on_completion(db, session)
        db.commit()
        remaining_minutes = max(0, int((original_end - now).total_seconds() / 60))
        new_session = None
        if remaining_minutes >= 15:
            new_session = reschedule_single_session(db, session, duration_override=remaining_minutes)
        if new_session:
            new_session = (
                db.query(HabitSession)
                .options(joinedload(HabitSession.habit))
                .filter(HabitSession.id == new_session.id)
                .first()
            )
            return _session_to_response(new_session)
        db.refresh(session)
        return _session_to_response(session)

    else:
        # Dismissed
        session.status = "dismissed"
        db.commit()

        # Learning habit: special dismiss behavior
        if session.habit and session.habit.category == "lernen":
            dismiss_info = dismiss_learning_session_day(db, session)
            db.commit()
            # Return enhanced response with off-day info
            return {
                **_session_to_response(session),
                **dismiss_info,
            }

        # Non-learning: try to reschedule
        new_session = reschedule_single_session(db, session)
        if new_session:
            new_session = (
                db.query(HabitSession)
                .options(joinedload(HabitSession.habit))
                .filter(HabitSession.id == new_session.id)
                .first()
            )
            return _session_to_response(new_session)
        return _session_to_response(session)


@router.post("/schedule")
def trigger_schedule(week: str | None = Query(None), db: Session = Depends(get_db)):
    week_start_date, _ = _parse_week_param(week)
    count = schedule_week(db, week_start_date)
    return {"scheduled": count}


@router.post("/schedule-ahead")
def schedule_ahead(weeks: int = Query(4, ge=1, le=12), db: Session = Depends(get_db)):
    """Schedule habit sessions for the next N weeks (idempotent)."""
    today = date.today()
    total = 0
    for i in range(weeks):
        ws = _week_start(today + timedelta(weeks=i))
        total += schedule_week(db, ws)
    return {"scheduled": total, "weeks": weeks}
