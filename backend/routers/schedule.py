"""Freie-Slot-Berechnung + Pool-Todo-Auto-Scheduling.

Reihenfolge der Belegung (siehe scheduler.py): Termine (fix) > Tagesziele mit Zeitblock >
Habit-Sessions > geplante Todos. Pool-Todos werden nur in die danach freien Slots vorgeschlagen
oder (commit=true) verbindlich eingeplant.
"""

from datetime import date as date_type
from datetime import datetime
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.scheduler import (
    _TODO_PRIORITY_ORDER,
    DEFAULT_TODO_MINUTES,
    auto_plan_todos,
    compute_free_slots,
)

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


class FremdeAufgabe(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=500)
    priority: str = Field(default="mittel", pattern=r"^(dringend|hoch|mittel|niedrig)$")
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    # ⚠️ Englisch wie im Todo-Schema; der Planer rechnet deutsch (siehe unten).
    energy_required: str | None = Field(default=None, pattern=r"^(low|medium|high)$")
    due_date: date_type | None = None


class Zeitraum(BaseModel):
    start: datetime
    end: datetime


class VorschlagAnfrage(BaseModel):
    date: date_type
    aufgaben: list[FremdeAufgabe] = Field(default_factory=list, max_length=500)
    belegt: list[Zeitraum] = Field(default_factory=list, max_length=500)


# Der Planer vergleicht Energie deutsch (`daily_energy._ENERGY_ORDER`,
# `_TODO_ENERGY_WEIGHT`), das Todo-Schema schreibt sie englisch.
_ENERGIE_DEUTSCH = {"low": "niedrig", "medium": "mittel", "high": "hoch"}


@router.post("/vorschlag")
def vorschlag(anfrage: VorschlagAnfrage, db: Session = Depends(get_db)):
    """Zeiten fuer Aufgaben, die NICHT in diesem Dienst liegen (Aufgaben-Dienst).

    Dieselbe Rechnung wie `/day` mit `prefer_time` (freie Fenster, Kapazitaet,
    Energie, Tageszeit-Passung), aber auf den mitgeschickten Aufgaben und nur
    lesend: festgehalten wird beim Besitzer der Aufgabe. `belegt` sind Zeiten,
    die dort schon vergeben sind und hier kein Termin sind.
    """
    fremde = [
        SimpleNamespace(
            id=a.id,
            title=a.title,
            priority=a.priority,
            estimated_minutes=a.estimated_minutes,
            energy_required=_ENERGIE_DEUTSCH.get(a.energy_required or ""),
            due_date=a.due_date,
        )
        for a in anfrage.aufgaben
    ]
    fremde.sort(key=lambda t: (
        _TODO_PRIORITY_ORDER.get(t.priority, 2),
        t.due_date or date_type.max,
        t.estimated_minutes or DEFAULT_TODO_MINUTES,
    ))
    return auto_plan_todos(
        db,
        anfrage.date,
        commit=False,
        prefer_time=True,
        fremde=fremde,
        belegt=[(z.start, z.end) for z in anfrage.belegt],
    )
