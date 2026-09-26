"""Sekretär-API: proaktive Tagesplanung, Briefing, Konflikte, Konfiguration.

Alle Routen laufen tenant-gescopt über get_db (X-Saganta-Sub bzw. Owner-Fallback).
Der automatische Lauf steckt im Hintergrund-Runner (session_notifications); diese
Routen sind die On-Demand-/Frontend-Fassade davon.
"""

from datetime import date as date_type

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.secretary import (
    build_briefing,
    build_evening_review,
    detect_day_conflicts,
    get_config,
    run_daily_planning,
    set_config,
)

router = APIRouter(prefix="/api/secretary", tags=["secretary"])


def _resolve_day(day: date_type | None) -> date_type:
    return day or date_type.today()


@router.get("/briefing")
def briefing(
    date: date_type | None = Query(None),
    db: Session = Depends(get_db),
):
    """Kompaktes Tagesbriefing (Termine, Fokus, geplante Aufgaben, Konflikte, Geburtstage)."""
    return build_briefing(db, _resolve_day(date))


@router.get("/conflicts")
def conflicts(
    date: date_type | None = Query(None),
    db: Session = Depends(get_db),
):
    """Konflikte des Tages (Doppelbuchungen, Überfälliges, Überlast): read-only."""
    day = _resolve_day(date)
    return {"date": day.isoformat(), "conflicts": detect_day_conflicts(db, day)}


@router.post("/plan-day")
def plan_day(
    date: date_type | None = Query(None),
    db: Session = Depends(get_db),
):
    """Plant Pool-Todos verbindlich in die freien Slots des Tages (reversibel)."""
    day = _resolve_day(date)
    summary = run_daily_planning(db, day)
    return {"date": day.isoformat(), **summary}


@router.get("/evening")
def evening(
    date: date_type | None = Query(None),
    db: Session = Depends(get_db),
):
    """Abend-Nudge: offene Fokus-Ziele + Review-Status."""
    return build_evening_review(db, _resolve_day(date))


class SecretaryConfig(BaseModel):
    enabled: bool | None = None
    autoplan: bool | None = None
    morning_hour: int | None = Field(default=None, ge=0, le=23)
    evening_hour: int | None = Field(default=None, ge=0, le=23)


@router.get("/config")
def read_config(db: Session = Depends(get_db)):
    """Aktuelle Sekretär-Konfiguration (global)."""
    return get_config(db)


@router.put("/config")
def update_config(data: SecretaryConfig, db: Session = Depends(get_db)):
    """Sekretär-Konfiguration ändern (an/aus, Auto-Planung, Uhrzeiten)."""
    return set_config(db, **data.model_dump(exclude_unset=True))
