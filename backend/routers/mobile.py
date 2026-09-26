from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from backend.config import settings
from backend.dashboard import build_dashboard_data
from backend.database import get_db
from backend.models import Calendar, DailyGoal, DailyReview, Event, Habit, HabitSession, Project, Todo
from backend.recurrence import expand_events
from backend.routers.daytype import _get_daytype_display

BERLIN = ZoneInfo("Europe/Berlin")

router = APIRouter(prefix="/api/mobile", tags=["mobile"])


def _iso_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _calendar_payload(calendar: Calendar) -> dict:
    return {
        "id": calendar.id,
        "name": calendar.name,
        "color": calendar.color,
        "description": calendar.description,
        "is_system": calendar.is_system,
        "created_at": _iso_dt(calendar.created_at),
        "updated_at": _iso_dt(calendar.updated_at),
    }


def _project_payload(project: Project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "color": project.color,
        "icon": project.icon,
        "status": project.status,
        "created_at": _iso_dt(project.created_at),
        "updated_at": _iso_dt(project.updated_at),
    }


def _event_payload(instanz: dict, calendar_name: str | None, calendar_color: str | None) -> dict:
    """Eine (ggf. virtuelle) Event-Instanz aus ``recurrence.expand_events``.

    ★ Bis 2026-08-24 nahm diese Funktion ein ORM-``Event`` und der Bootstrap
    filterte die ``events``-Tabelle roh. Damit fehlten **alle Serientermine**:
    eine woechentliche Reihe hat ihren Basis-Datensatz beim ersten Vorkommen, und
    der faellt fast nie ins abgefragte Fenster. Gemessen am 2026-08-24 fuer
    24.–31.08.: ``/api/events`` mit Fenster lieferte 12 Termine (alle
    Serien-Instanzen), der Bootstrap **0**. Ueber den ganzen August: 54 gegen 9.

    Sichtbar wurde es nie, weil der Bootstrap ausschliesslich die Android-App
    speist und die Luecke dort wie ein leerer Kalender aussieht, nicht wie ein
    Fehler. Jetzt geht der Bootstrap durch dieselbe Expansion wie
    ``routers/events.list_events``, eine Quelle, eine Semantik.

    Zusaetzlich gegenueber vorher: ``series_id``, ``is_recurring_instance`` und
    ``activity_type``. Rein additiv: bestehende Felder behalten Name und Form.
    """
    # ★ Einzeltermine behalten ihre schlichte ID. ``expand_events`` vergibt auch
    # ihnen die Instanzform ``basis::datum``, fuer ``/api/events`` ist das seit
    # jeher so, hier waere es eine Vertragsaenderung an Bestandsclients, die der
    # Fehler gar nicht verlangt: kaputt waren die fehlenden Serien, nicht die IDs
    # der Einzeltermine. Nur echte Serien tragen die Instanzform, weil sie ohne
    # Datum nicht eindeutig waeren.
    ist_serie = bool(instanz.get("recurrence_rule"))
    return {
        "id": instanz["id"] if ist_serie else instanz["series_id"],
        "calendar_id": instanz["calendar_id"],
        "calendar_name": calendar_name,
        "calendar_color": calendar_color,
        "title": instanz["title"],
        "description": instanz["description"],
        "location": instanz["location"],
        "start": _iso_dt(instanz["start"]),
        "end": _iso_dt(instanz["end"]),
        "all_day": instanz["all_day"],
        "recurrence_rule": instanz["recurrence_rule"],
        "series_id": instanz.get("series_id"),
        "is_recurring_instance": instanz.get("is_recurring_instance", False),
        "activity_type": instanz.get("activity_type"),
        "created_at": _iso_dt(instanz.get("created_at")),
        "updated_at": _iso_dt(instanz.get("updated_at")),
    }


def _todo_payload(todo: Todo) -> dict:
    return {
        "id": todo.id,
        "title": todo.title,
        "description": todo.description,
        "priority": todo.priority,
        "due_date": _iso_date(todo.due_date),
        "due_time": todo.due_time,
        "completed": todo.completed,
        "completed_at": _iso_dt(todo.completed_at),
        "recurrence": todo.recurrence,
        "project_id": todo.project_id,
        "goal_id": todo.goal_id,
        "status": todo.status,
        "scheduling_mode": todo.scheduling_mode,
        "estimated_minutes": todo.estimated_minutes,
        "scheduled_start": _iso_dt(todo.scheduled_start),
        "scheduled_end": _iso_dt(todo.scheduled_end),
        "planned_date": _iso_date(todo.planned_date),
        "defer_count": todo.defer_count,
        "created_at": _iso_dt(todo.created_at),
        "updated_at": _iso_dt(todo.updated_at),
    }


def _goal_payload(goal: DailyGoal, linked_todo_ids: list[str]) -> dict:
    return {
        "id": goal.id,
        "title": goal.title,
        "description": goal.description,
        "date": _iso_date(goal.date),
        "priority": goal.priority,
        "status": goal.status,
        "estimated_minutes": goal.estimated_minutes,
        "scheduled_start": _iso_dt(goal.scheduled_start),
        "scheduled_end": _iso_dt(goal.scheduled_end),
        "category": goal.category,
        "review_note": goal.review_note,
        "abandoned_reason": goal.abandoned_reason,
        "linked_todo_ids": linked_todo_ids,
        "created_at": _iso_dt(goal.created_at),
        "updated_at": _iso_dt(goal.updated_at),
    }


def _review_payload(review: DailyReview) -> dict:
    return {
        "date": _iso_date(review.date),
        "what_went_well": review.what_went_well,
        "blockers": review.blockers,
        "abandoned_goals_reason": review.abandoned_goals_reason,
        "carry_over_to_tomorrow": review.carry_over_to_tomorrow,
        "energy_level": review.energy_level,
    }


def _habit_payload(habit: Habit) -> dict:
    return {
        "id": habit.id,
        "name": habit.name,
        "color": habit.color,
        "project_id": habit.project_id,
        "project_name": habit.project.name if habit.project else None,
        "target_hours_per_week": habit.target_hours_per_week,
        "session_duration_minutes": habit.session_duration_minutes,
        "weekday_start": habit.weekday_start,
        "weekday_end": habit.weekday_end,
        "weekend_start": habit.weekend_start,
        "weekend_end": habit.weekend_end,
        "active": habit.active,
        "learning_mode": habit.learning_mode,
        "focus_block_minutes": habit.focus_block_minutes,
        "break_minutes": habit.break_minutes,
        "category": habit.category,
        "weekday_target_ratio": habit.weekday_target_ratio,
        "max_consecutive_days": habit.max_consecutive_days,
        "max_session_minutes": habit.max_session_minutes,
        "created_at": _iso_dt(habit.created_at),
        "updated_at": _iso_dt(habit.updated_at),
    }


def _session_payload(session: HabitSession) -> dict:
    habit = session.habit
    return {
        "id": session.id,
        "habit_id": session.habit_id,
        "habit_name": habit.name if habit else None,
        "habit_color": habit.color if habit else None,
        "learning_mode": habit.learning_mode if habit else False,
        "category": habit.category if habit else "sonstige",
        "start": _iso_dt(session.start),
        "end": _iso_dt(session.end),
        "status": session.status,
        "week_iso": session.week_iso,
        "focus_minutes_completed": session.focus_minutes_completed,
        "session_type": session.session_type,
        "pinned": session.pinned,
        "created_at": _iso_dt(session.created_at),
        "updated_at": _iso_dt(session.updated_at),
    }


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return (
        datetime(start.year, start.month, start.day, tzinfo=BERLIN),
        datetime(end.year, end.month, end.day, tzinfo=BERLIN) + timedelta(days=1),
    )


@router.get("/bootstrap")
def mobile_bootstrap(
    start: date = Query(...),
    end: date = Query(...),
    db: Session = Depends(get_db),
):
    if end < start:
        raise HTTPException(status_code=400, detail="end must be on or after start")
    if (end - start).days > 120:
        raise HTTPException(status_code=400, detail="range must not exceed 120 days")

    range_start, range_end = _day_bounds(start, end)

    calendars = db.query(Calendar).order_by(Calendar.name).all()
    projects = db.query(Project).order_by(Project.name).all()
    habits = (
        db.query(Habit)
        .options(joinedload(Habit.project))
        .order_by(Habit.name)
        .all()
    )

    # Fenstersemantik identisch zu routers/events.list_events: Einzeltermine per
    # DB filtern, Serien getrennt holen (ihr Basisstart liegt meist VOR dem
    # Fenster, sie reichen aber hinein) und beides gemeinsam expandieren.
    einzelne = (
        db.query(Event)
        .filter(
            Event.recurrence_rule.is_(None),
            Event.end >= range_start,
            Event.start <= range_end,
        )
        .all()
    )
    serien = (
        db.query(Event)
        .filter(Event.recurrence_rule.isnot(None), Event.start <= range_end)
        .all()
    )
    instanzen = expand_events(einzelne + serien, range_start, range_end)
    # Kalendername/-farbe je Instanz. Der Kalendersatz ist bereits geladen (sieben
    # Zeilen), eine Zuordnung im Speicher statt eines JOINs je Instanz.
    kalender_schau = {k.id: (k.name, k.color) for k in calendars}

    todos = (
        db.query(Todo)
        .filter(
            or_(
                and_(Todo.due_date == None, Todo.completed == False),  # noqa: E711,E712
                and_(Todo.due_date >= start, Todo.due_date <= end),
            )
        )
        .order_by(Todo.due_date.asc().nullslast(), Todo.created_at.asc())
        .all()
    )

    sessions = (
        db.query(HabitSession)
        .options(joinedload(HabitSession.habit))
        .filter(
            HabitSession.start < range_end,
            HabitSession.end > range_start,
            HabitSession.status.notin_(["dismissed"]),
        )
        .order_by(HabitSession.start)
        .all()
    )

    goals = (
        db.query(DailyGoal)
        .filter(DailyGoal.date >= start, DailyGoal.date <= end)
        .order_by(DailyGoal.date, DailyGoal.priority)
        .all()
    )
    # Verknüpfte Todos je Ziel (eine Abfrage, im Speicher gruppiert)
    linked_rows = (
        db.query(Todo.id, Todo.goal_id)
        .filter(Todo.goal_id.isnot(None))
        .all()
    )
    linked_by_goal: dict[str, list[str]] = {}
    for todo_id, goal_id in linked_rows:
        linked_by_goal.setdefault(goal_id, []).append(todo_id)

    reviews = (
        db.query(DailyReview)
        .filter(DailyReview.date >= start, DailyReview.date <= end)
        .all()
    )

    daytypes = []
    current = start
    while current <= end:
        daytypes.append({"date": current.isoformat(), "type": _get_daytype_display(db, current)})
        current += timedelta(days=1)

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "generated_at": datetime.now(BERLIN).isoformat(),
        "calendars": [_calendar_payload(calendar) for calendar in calendars],
        "projects": [_project_payload(project) for project in projects],
        "events": [
            _event_payload(instanz, *kalender_schau.get(instanz["calendar_id"], (None, None)))
            for instanz in instanzen
        ],
        "todos": [_todo_payload(todo) for todo in todos],
        "habits": [_habit_payload(habit) for habit in habits],
        "sessions": [_session_payload(session) for session in sessions],
        "goals": [_goal_payload(goal, linked_by_goal.get(goal.id, [])) for goal in goals],
        "reviews": [_review_payload(review) for review in reviews],
        "daytypes": daytypes,
        "dashboard": build_dashboard_data(db),
        "ntfy": {
            "enabled": bool(settings.NTFY_URL and settings.NTFY_TOPIC),
            "url": settings.NTFY_URL or None,
            "topic": settings.NTFY_TOPIC or None,
            "lead_minutes_start": settings.SESSION_NOTIFY_LEAD_MINUTES_START,
            "lead_minutes_end": settings.SESSION_NOTIFY_LEAD_MINUTES_END,
        },
    }
