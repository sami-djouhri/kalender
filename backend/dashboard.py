"""Shared dashboard data builder used by both JWT and feed_token endpoints."""

import calendar as cal_mod
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.orm import Session

BERLIN = ZoneInfo("Europe/Berlin")

from backend.models import Calendar, Contact, DailyGoal, Event, Todo
from backend.routers.daytype import DAYTYPE_IDS, FEIERTAG_CALENDAR_ID, _get_daytype_display
from backend.secretary import detect_day_conflicts

GOAL_PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2}


TODO_PRIORITY_ORDER = {
    "dringend": 0,
    "hoch": 1,
    "mittel": 2,
    "niedrig": 3,
}


def _as_berlin(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BERLIN)
    return value.astimezone(BERLIN)


def _event_payload(evt: Event, calendar_name: str, calendar_color: str, now: datetime) -> dict:
    start = _as_berlin(evt.start)
    end = _as_berlin(evt.end)
    return {
        "id": evt.id,
        "title": evt.title,
        "calendar": calendar_name,
        "color": calendar_color,
        "start": evt.start.isoformat(),
        "end": evt.end.isoformat(),
        "all_day": evt.all_day,
        "location": evt.location,
        "is_now": start <= now < end,
        "minutes_until": int((start - now).total_seconds() // 60),
    }


def _todo_payload(todo: Todo, today: date) -> dict:
    days_until = (todo.due_date - today).days if todo.due_date else None
    return {
        "id": todo.id,
        "title": todo.title,
        "description": todo.description,
        "priority": todo.priority,
        "due_date": todo.due_date.isoformat() if todo.due_date else None,
        "due_time": todo.due_time,
        "recurrence": todo.recurrence,
        "project_id": todo.project_id,
        "days_until": days_until,
        "is_overdue": days_until is not None and days_until < 0,
    }


def _todo_sort_key(todo: Todo) -> tuple:
    return (
        TODO_PRIORITY_ORDER.get(todo.priority, 2),
        todo.due_date or date.max,
        todo.due_time or "99:99",
        todo.title.lower(),
    )


def _agenda_sort_key(item: dict) -> tuple:
    if item["type"] == "todo":
        todo = item["todo"]
        if todo["is_overdue"]:
            return (0, TODO_PRIORITY_ORDER.get(todo["priority"], 2), todo["due_date"] or "", todo["due_time"] or "99:99")
        if todo["due_time"]:
            return (3, todo["due_time"], TODO_PRIORITY_ORDER.get(todo["priority"], 2), todo["title"].lower())
        return (5, TODO_PRIORITY_ORDER.get(todo["priority"], 2), todo["title"].lower())

    event = item["event"]
    if event["all_day"]:
        return (1, event["title"].lower())
    return (2, event["start"], event["title"].lower())


def build_dashboard_data(db: Session) -> dict:
    now = datetime.now(BERLIN)
    today = now.date()
    now_start = datetime(today.year, today.month, today.day, tzinfo=BERLIN)
    now_end = now_start + timedelta(days=1)

    day_type = _get_daytype_display(db, today)
    tomorrow = today + timedelta(days=1)
    tomorrow_type = _get_daytype_display(db, tomorrow)

    # Today's events (non-system calendars)
    today_events = (
        db.query(Event, Calendar.name.label("calendar_name"), Calendar.color.label("calendar_color"))
        .join(Calendar, Event.calendar_id == Calendar.id)
        .filter(
            Event.calendar_id.notin_(DAYTYPE_IDS),
            Event.start < now_end,
            Event.end > now_start,
        )
        .order_by(Event.all_day.desc(), Event.start)
        .all()
    )

    termine = []
    for evt, cal_name, cal_color in today_events:
        termine.append(_event_payload(evt, cal_name, cal_color, now))

    upcoming_end = now_start + timedelta(days=7)
    upcoming_rows = (
        db.query(Event, Calendar.name.label("calendar_name"), Calendar.color.label("calendar_color"))
        .join(Calendar, Event.calendar_id == Calendar.id)
        .filter(
            Event.calendar_id.notin_(DAYTYPE_IDS),
            Event.end >= now,
            Event.start < upcoming_end,
        )
        .order_by(Event.start)
        .all()
    )
    upcoming_events = [
        _event_payload(evt, cal_name, cal_color, now)
        for evt, cal_name, cal_color in upcoming_rows
    ]
    upcoming_events.sort(
        key=lambda evt: (
            1 if evt["all_day"] else 0,
            0 if evt["is_now"] else 1,
            evt["start"],
        )
    )
    next_event = upcoming_events[0] if upcoming_events else None

    open_todos = (
        db.query(Todo)
        .filter(Todo.completed == False)  # noqa: E712
        .filter(or_(Todo.due_date <= today, Todo.due_date == None))  # noqa: E711
        .all()
    )
    open_todos.sort(key=_todo_sort_key)

    overdue_todos = [_todo_payload(t, today) for t in open_todos if t.due_date and t.due_date < today]
    today_todos = [_todo_payload(t, today) for t in open_todos if t.due_date == today]
    floating_todos = [_todo_payload(t, today) for t in open_todos if not t.due_date]
    important_todos = [
        t for t in [*overdue_todos, *today_todos, *floating_todos]
        if t["priority"] in {"dringend", "hoch"}
    ]

    agenda = (
        [{"type": "event", "event": event} for event in termine]
        + [{"type": "todo", "todo": todo} for todo in [*overdue_todos, *today_todos, *floating_todos]]
    )
    agenda.sort(key=_agenda_sort_key)

    # Next 3 birthdays
    upcoming_birthdays = []
    contacts_with_bday = db.query(Contact).filter(Contact.birthday.isnot(None)).all()
    for c in contacts_with_bday:
        bday_this_year = None
        try:
            bday_this_year = date(today.year, c.birthday.month, c.birthday.day)
        except ValueError:
            if c.birthday.month == 2 and c.birthday.day == 29:
                if not cal_mod.isleap(today.year):
                    bday_this_year = date(today.year, 2, 28)

        if bday_this_year and bday_this_year >= today:
            age = today.year - c.birthday.year
            days_until = (bday_this_year - today).days
            upcoming_birthdays.append({
                "name": c.name,
                "date": bday_this_year.isoformat(),
                "age": age,
                "days_until": days_until,
            })
        else:
            next_year = today.year + 1
            try:
                bday_next = date(next_year, c.birthday.month, c.birthday.day)
            except ValueError:
                if c.birthday.month == 2 and c.birthday.day == 29:
                    if not cal_mod.isleap(next_year):
                        bday_next = date(next_year, 2, 28)
                    else:
                        bday_next = date(next_year, 2, 29)
                else:
                    continue
            age = next_year - c.birthday.year
            days_until = (bday_next - today).days
            upcoming_birthdays.append({
                "name": c.name,
                "date": bday_next.isoformat(),
                "age": age,
                "days_until": days_until,
            })

    upcoming_birthdays.sort(key=lambda x: x["days_until"])
    # Show next 3 upcoming birthdays within 30 days
    upcoming_birthdays = [b for b in upcoming_birthdays if b["days_until"] <= 30][:3]

    # Next Feiertag (only if today or tomorrow)
    next_feiertag = (
        db.query(Event)
        .filter(
            Event.calendar_id == FEIERTAG_CALENDAR_ID,
            Event.start >= now_start,
        )
        .order_by(Event.start)
        .first()
    )

    feiertag_info = None
    if next_feiertag:
        ft_date = (
            next_feiertag.start.date()
            if hasattr(next_feiertag.start, "date")
            else date.fromisoformat(str(next_feiertag.start)[:10])
        )
        days_until = (ft_date - today).days
        feiertag_info = {
            "name": next_feiertag.title,
            "date": ft_date.isoformat(),
            "days_until": days_until,
        }

    tomorrow_weekday = [
        "Montag", "Dienstag", "Mittwoch", "Donnerstag",
        "Freitag", "Samstag", "Sonntag",
    ][tomorrow.weekday()]

    # --- Tagesziele (heute): Tagesfokus, kein Todo ---
    goal_rows = db.query(DailyGoal).filter(DailyGoal.date == today).all()
    goal_rows.sort(key=lambda g: (GOAL_PRIORITY_ORDER.get(g.priority, 1), g.created_at))
    goals = [
        {
            "id": g.id,
            "title": g.title,
            "description": g.description,
            "priority": g.priority,
            "status": g.status,
            "category": g.category,
            "estimated_minutes": g.estimated_minutes,
            "scheduled_start": g.scheduled_start.isoformat() if g.scheduled_start else None,
            "scheduled_end": g.scheduled_end.isoformat() if g.scheduled_end else None,
            "abandoned_reason": g.abandoned_reason,
        }
        for g in goal_rows
    ]
    active_goals = [g for g in goals if g["status"] in ("planned", "active")]

    # --- Geplante Todos heute (mit Zeitblock / Tag) ---
    planned_rows = (
        db.query(Todo)
        .filter(
            Todo.completed == False,  # noqa: E712
            Todo.status.notin_(["done", "cancelled"]),
            Todo.scheduling_mode.in_(["planned_day", "fixed_slot"]),
        )
        .all()
    )
    planned_todos = []
    for t in planned_rows:
        pday = (
            _as_berlin(t.scheduled_start).date() if t.scheduled_start
            else (t.planned_date or t.due_date)
        )
        if pday == today:
            planned_todos.append({
                "id": t.id,
                "title": t.title,
                "priority": t.priority,
                "scheduled_start": t.scheduled_start.isoformat() if t.scheduled_start else None,
                "scheduled_end": t.scheduled_end.isoformat() if t.scheduled_end else None,
                "estimated_minutes": t.estimated_minutes,
            })
    planned_todos.sort(key=lambda x: (x["scheduled_start"] or "99:99", x["title"].lower()))

    return {
        "today": today.isoformat(),
        "weekday": [
            "Montag", "Dienstag", "Mittwoch", "Donnerstag",
            "Freitag", "Samstag", "Sonntag",
        ][today.weekday()],
        "day_type": day_type,
        "tomorrow": tomorrow.isoformat(),
        "tomorrow_weekday": tomorrow_weekday,
        "tomorrow_type": tomorrow_type,
        "termine": termine,
        "next_event": next_event,
        "upcoming_events": upcoming_events[:5],
        "todos": {
            "overdue": overdue_todos,
            "today": today_todos,
            "floating": floating_todos,
            "important": important_todos,
            "total_open": len(open_todos),
            "overdue_count": len(overdue_todos),
            "today_count": len(today_todos),
            "floating_count": len(floating_todos),
            "important_count": len(important_todos),
        },
        "agenda": agenda,
        "birthdays": upcoming_birthdays,
        "next_feiertag": feiertag_info,
        "goals": goals,
        "goals_active_count": len(active_goals),
        "planned_todos": planned_todos,
        "secretary": {
            "conflicts": detect_day_conflicts(db, today),
        },
    }
