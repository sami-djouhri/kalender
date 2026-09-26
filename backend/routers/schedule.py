"""Freie-Slot-Berechnung + Pool-Todo-Auto-Scheduling.

Reihenfolge der Belegung (siehe scheduler.py): Termine (fix) > Tagesziele mit Zeitblock >
Habit-Sessions > geplante Todos. Pool-Todos werden nur in die danach freien Slots vorgeschlagen
oder (commit=true) verbindlich eingeplant.
"""

from datetime import date as date_type

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.scheduler import auto_plan_todos, compute_free_slots

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


@router.get("/day")
def schedule_day(
    date: date_type = Query(...),
    prefer_time: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Berechnete freie Slots + (nicht-verbindliche) Pool-Todo-Vorschläge für einen Tag.

    `prefer_time=true` → präferenz-bewusste Slot-Wahl mit sichtbarer Begründung je Vorschlag
    (für den „Plane meinen Tag"-Vorschau-Modus).
    """
    return auto_plan_todos(db, date, commit=False, prefer_time=prefer_time)


@router.get("/free-slots")
def free_slots(
    date: date_type = Query(...),
    db: Session = Depends(get_db),
):
    slots = compute_free_slots(db, date)
    return {
        "date": date,
        "free_slots": [
            {"start": s, "end": e, "minutes": int((e - s).total_seconds() / 60)}
            for s, e in slots
        ],
    }


@router.post("/auto-plan")
def auto_plan(
    date: date_type = Query(...),
    commit: bool = Query(False),
    prefer_time: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Pool-Todos in freie Slots einplanen. commit=true schreibt die Belegung verbindlich.

    `prefer_time=true` gewichtet die Slot-Wahl nach gelernter Tageszeit-Passung (mit Begründung).
    """
    return auto_plan_todos(db, date, commit=commit, prefer_time=prefer_time)
