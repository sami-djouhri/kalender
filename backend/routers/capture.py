"""Quick-Capture-API: Freitext zu Termin/Aufgabe.

POST /api/capture       -> parst Freitext, gibt einen Vorschlag zurück (legt nichts an)
POST /api/capture/commit -> legt aus einem (ggf. korrigierten) Vorschlag Event/Todo an

Der Zwei-Schritt (parsen, dann bestätigen) ist bewusst: der User sieht immer, was
erkannt wurde, bevor etwas verbindlich angelegt wird.
"""

from datetime import date as date_type
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.capture import capture as run_capture
from backend.database import get_db
from backend.models import Event, Todo
from backend.routers.events import _tag_nachplanen
from backend.scheduler import block_vorschlaege, check_conflicts_after_event_change, reschedule_week

router = APIRouter(prefix="/api/capture", tags=["capture"])

# Standard-Kalender für per Capture angelegte Termine (system "Termine").
TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"


class CaptureRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)
    allow_llm: bool = True


class CaptureCommit(BaseModel):
    type: str = Field(..., pattern=r"^(event|todo)$")
    title: str = Field(..., min_length=1, max_length=500)
    date: date_type | None = None
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    end_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    dauer_min: int | None = Field(default=None, ge=1, le=24 * 60)


@router.post("")
def parse(data: CaptureRequest, db: Session = Depends(get_db)):
    """Freitext parsen und Vorschlag zurückgeben (regelbasiert, ggf. LLM-Fallback).

    Hat der Text eine Dauer, aber keine Uhrzeit („morgen 2h Keller"), stehen in
    `vorschlaege` bis zu drei freie Zeiten. Angelegt wird erst beim Commit mit
    der gewählten Zeit (Entscheid 2026-11-01).
    """
    result = run_capture(data.text, allow_llm=data.allow_llm)
    out = result.to_dict()
    out["vorschlaege"] = []
    if result.dauer_min and not result.start_time:
        tag = date_type.fromisoformat(result.date) if result.date else None
        out["vorschlaege"] = block_vorschlaege(db, result.dauer_min, tag)
    return out


def _combine(day: date_type, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


@router.post("/commit", status_code=201)
def commit(data: CaptureCommit, db: Session = Depends(get_db)):
    """Legt aus einem Vorschlag verbindlich Termin oder Aufgabe an."""
    if data.type == "event":
        day = data.date or date_type.today()
        if data.start_time:
            start = _combine(day, data.start_time)
            if data.end_time and data.end_time > data.start_time:
                end = _combine(day, data.end_time)
            else:
                end = start + timedelta(minutes=data.dauer_min or 60)
            all_day = False
        elif data.dauer_min:
            # Ein Block ohne gewählte Zeit wäre ein Ganztagstermin und hielte
            # den Tag für Habits komplett zu. Erst Zeit wählen.
            raise HTTPException(422, "Block ohne Uhrzeit: bitte einen Vorschlag wählen")
        else:
            start = datetime(day.year, day.month, day.day)
            end = start + timedelta(days=1)
            all_day = True
        event = Event(
            calendar_id=TERMINE_CAL_ID,
            title=data.title,
            start=start,
            end=end,
            all_day=all_day,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        # Wie `POST /api/events`: Heimweg nachziehen und Habit-Sessions, die
        # jetzt im Weg liegen, neu verteilen. Fehlte hier bis 2026-11-01.
        _tag_nachplanen(db, event.start.date())
        db.refresh(event)
        check_conflicts_after_event_change(db, event)
        return {"type": "event", "id": event.id, "title": event.title, "start": event.start.isoformat()}

    # todo
    todo = Todo(
        title=data.title,
        due_date=data.date,
        due_time=data.start_time,
        estimated_minutes=data.dauer_min,
        scheduling_mode="pool",
        status="open",
    )
    if data.date and data.start_time and data.end_time and data.end_time > data.start_time:
        # Gewählter Vorschlag: fester Block, den der Habit-Planer respektiert.
        todo.scheduled_start = _combine(data.date, data.start_time)
        todo.scheduled_end = _combine(data.date, data.end_time)
        todo.planned_date = data.date
        todo.scheduling_mode = "fixed_slot"
        todo.status = "scheduled"
    db.add(todo)
    db.commit()
    db.refresh(todo)
    if todo.scheduled_start:
        reschedule_week(db, todo.planned_date)
        db.refresh(todo)
    return {"type": "todo", "id": todo.id, "title": todo.title, "due_date": todo.due_date.isoformat() if todo.due_date else None}
