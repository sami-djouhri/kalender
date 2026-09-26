"""Aktivitäts-Feedback-API, die „Randnotiz nach der Aktivität".

Retrospektives Feedback pro Aktivitäts-Instanz (Event mit `activity_type`): der Owner
gibt nach Sport/Lernen ein kurzes Rating („💪 stark / 🙂 ok / 🥵 kaputt") + optionale
Notiz. Treibt deterministisch die Vorschläge (Phase 1) und ist die Datenbasis fürs
Präferenz-Lernen (Phase 2). Tenant-gescopt über get_db, in main.py JWT-gated.
"""

from datetime import date as date_type, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Literal

from backend.activities import feedback_out, list_reviewable_activities
from backend.database import get_db
from backend.models import ActivityFeedback, Event
from backend.planning import suggest_best_time
from backend.preferences import build_insights, compute_preferences

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


def _parse_ref(event_id: str, occurrence_date: date_type | None) -> tuple[str, date_type]:
    """Instanz-ID '{base}::{YYYY-MM-DD}' ODER Basis-ID + occurrence_date → (base_id, occ_date)."""
    if "::" in event_id:
        base_id, _, iso = event_id.partition("::")
        try:
            return base_id, date_type.fromisoformat(iso[:10])
        except ValueError:
            raise HTTPException(status_code=400, detail="Ungültige Instanz-ID")
    if occurrence_date is None:
        raise HTTPException(
            status_code=422,
            detail="occurrence_date erforderlich (oder Instanz-ID im Format {base}::{YYYY-MM-DD})",
        )
    return event_id, occurrence_date


@router.get("/reviewable")
def reviewable(
    date: date_type | None = Query(None),
    lookback: int = Query(0, ge=0, le=14),
    db: Session = Depends(get_db),
):
    """Vorbei-gegangene Aktivitäten ohne Feedback (Haupt-Quelle fürs UI + Nudge).

    `lookback` > 0 nimmt auch die letzten N Tage mit rein (rückwirkendes Bewerten),
    neueste zuerst. `lookback=0` = nur der Zieltag.
    """
    day = date or date_type.today()
    activities: list[dict] = []
    for offset in range(0, lookback + 1):
        activities.extend(list_reviewable_activities(db, day - timedelta(days=offset)))
    # neueste zuerst
    activities.sort(key=lambda a: a["start"], reverse=True)
    return {"date": day.isoformat(), "lookback": lookback, "activities": activities}


@router.get("/insights")
def insights(db: Session = Depends(get_db)):
    """Was Saganta über die Aktivitäts-Präferenzen gelernt hat (menschenlesbar + roh)."""
    return {"insights": build_insights(db), "preferences": compute_preferences(db)}


@router.get("/best-slot")
def best_slot(
    activity_type: str = Query(..., pattern=r"^(lernen|sport|lesen|hobby|sonstige)$"),
    days: int = Query(7, ge=1, le=14),
    duration: int = Query(60, ge=15, le=240),
    db: Session = Depends(get_db),
):
    """Präferenz-bewusster Slot-Vorschlag: wann diese Aktivität in den nächsten Tagen am besten passt."""
    return {
        "activity_type": activity_type,
        "suggestion": suggest_best_time(db, activity_type, horizon_days=days, duration_min=duration),
    }


@router.get("")
def get_feedback(
    event_id: str = Query(...),
    date: date_type | None = Query(None),
    db: Session = Depends(get_db),
):
    """Einzelnes Feedback einer Aktivitäts-Instanz lesen (für das EventModal)."""
    base_id, occ = _parse_ref(event_id, date)
    row = (
        db.query(ActivityFeedback)
        .filter(ActivityFeedback.event_id == base_id, ActivityFeedback.occurrence_date == occ)
        .first()
    )
    return {"event_id": base_id, "occurrence_date": occ.isoformat(), "feedback": feedback_out(row)}


class FeedbackInput(BaseModel):
    # event_id darf Basis-ID oder Instanz-ID ('{base}::{date}') sein.
    event_id: str = Field(..., min_length=1)
    occurrence_date: date_type | None = None
    energy_after: Literal["energetisiert", "ok", "erschöpft"] | None = None
    satisfaction: Literal["gut", "mittel", "schlecht"] | None = None
    took_place: bool = True
    note: str | None = Field(default=None, max_length=2000)


@router.post("")
def upsert_feedback(data: FeedbackInput, db: Session = Depends(get_db)):
    """Feedback für eine Aktivitäts-Instanz anlegen/aktualisieren (upsert)."""
    base_id, occ = _parse_ref(data.event_id, data.occurrence_date)
    event = db.query(Event).filter(Event.id == base_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Aktivität nicht gefunden")

    row = (
        db.query(ActivityFeedback)
        .filter(ActivityFeedback.event_id == base_id, ActivityFeedback.occurrence_date == occ)
        .first()
    )
    if row is None:
        row = ActivityFeedback(event_id=base_id, occurrence_date=occ, activity_kind="event")
        db.add(row)

    # Denormalisierter Kontext (Instanz-Zeit aus dem Basis-Event auf occ gelegt).
    row.activity_type = event.activity_type
    row.title = event.title
    row.weekday = occ.weekday()
    try:
        duration = event.end - event.start
        row.scheduled_start = event.start.replace(year=occ.year, month=occ.month, day=occ.day)
        row.scheduled_end = row.scheduled_start + duration
    except ValueError:
        row.scheduled_start = event.start
        row.scheduled_end = event.end

    # Ratings + Notiz (nur gesetzte Felder ändern; took_place immer übernehmen).
    if data.energy_after is not None:
        row.energy_after = data.energy_after
    if data.satisfaction is not None:
        row.satisfaction = data.satisfaction
    row.took_place = data.took_place
    if data.note is not None:
        row.note = data.note.strip() or None

    db.commit()
    db.refresh(row)
    return {
        "event_id": base_id,
        "occurrence_date": occ.isoformat(),
        "feedback": feedback_out(row),
    }
