"""Assistent-API: adaptiver Sekretär (Check-in, Kapazität, Vorschläge, Fortschritt).

Alle Routen laufen tenant-gescopt über get_db (X-Saganta-Sub bzw. Owner-Fallback)
und sind in main.py JWT-gated eingebunden. On-Demand-/Frontend-Fassade der
adaptiven Schicht; der proaktive Teil (Push-Nudges) steckt im Hintergrund-Runner.
"""

from datetime import date as date_type

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Literal

from backend.assistant import build_today
from backend.daily_energy import compute_day_capacity, get_checkin, upsert_checkin
from backend.database import get_db
from backend.suggestions import build_progress, build_suggestions

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


def _resolve_day(day: date_type | None) -> date_type:
    return day or date_type.today()


def _checkin_out(row) -> dict | None:
    if row is None:
        return None
    return {
        "date": row.date.isoformat(),
        "sleep_quality": row.sleep_quality,
        "energy": row.energy,
        "mood": row.mood,
        "physical_ready": row.physical_ready,
        "note": row.note,
        "source": row.source,
    }


@router.get("/today")
def today(date: date_type | None = Query(None), db: Session = Depends(get_db)):
    """Angereichertes Tagesbild: Briefing + Kapazität + Check-in + Top-Vorschläge."""
    return build_today(db, _resolve_day(date))


@router.get("/capacity")
def capacity(date: date_type | None = Query(None), db: Session = Depends(get_db)):
    """Tageskapazität (Level/Faktor/Caps) aus Check-in + Day-Type + Strain."""
    return compute_day_capacity(db, _resolve_day(date))


@router.get("/suggestions")
def suggestions(date: date_type | None = Query(None), db: Session = Depends(get_db)):
    """Gerankte Vorschläge + optionale Entscheidungsfrage („Wozu hast du Lust?")."""
    return build_suggestions(db, _resolve_day(date))


@router.get("/progress")
def progress(week_start: date_type | None = Query(None), db: Session = Depends(get_db)):
    """Messbarer Wochenfortschritt je Habit + Kategorie-Balance."""
    return build_progress(db, week_start)


@router.get("/checkin")
def read_checkin(date: date_type | None = Query(None), db: Session = Depends(get_db)):
    """Check-in eines Tages (oder null, wenn noch keiner erfasst)."""
    day = _resolve_day(date)
    return {"date": day.isoformat(), "checkin": _checkin_out(get_checkin(db, day))}


class CheckInInput(BaseModel):
    date: date_type | None = None
    sleep_quality: Literal["gut", "mittel", "schlecht"] | None = None
    energy: Literal["hoch", "mittel", "niedrig"] | None = None
    mood: Literal["gut", "neutral", "mies"] | None = None
    physical_ready: bool | None = None
    note: str | None = Field(default=None, max_length=1000)


@router.post("/checkin")
def submit_checkin(data: CheckInInput, db: Session = Depends(get_db)):
    """Morgen-Check-in anlegen/aktualisieren → beeinflusst Planung + Vorschläge sofort."""
    day = _resolve_day(data.date)
    row = upsert_checkin(
        db, day,
        sleep_quality=data.sleep_quality,
        energy=data.energy,
        mood=data.mood,
        physical_ready=data.physical_ready,
        note=data.note,
        source="manual",
    )
    # Direkt die neu berechnete Kapazität mitgeben (UI kann sofort reagieren).
    return {
        "date": day.isoformat(),
        "checkin": _checkin_out(row),
        "capacity": compute_day_capacity(db, day),
    }
