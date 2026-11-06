"""Zeit von Hand ueber HTTP: Timer und Haken. Die Logik steht in `backend/handzeit.py`.

Neben `/api/zeit/ingest` (Melder, eigener Token) und bewusst NICHT darin: hier
schreibt ein angemeldeter Mensch, also gilt die normale Anmeldung samt Mandant.
"""
from datetime import date as date_type, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import handzeit
from backend.database import get_db
from backend.zeit_ingest import IngestFehler, _als_zeitpunkt

router = APIRouter(prefix="/api/zeit", tags=["zeit"])


class TimerStart(BaseModel):
    titel: str = Field(..., min_length=1, max_length=300)
    art: str | None = None
    bezug_typ: str | None = Field(default=None, pattern=r"^(session|event|todo)$")
    bezug_id: str | None = Field(default=None, max_length=200)
    # „laeuft seit": wer erst nach zehn Minuten daran denkt, traegt den Beginn nach.
    seit: str | None = None


class TimerStopp(BaseModel):
    ende: str | None = None


class Haken(BaseModel):
    titel: str = Field(..., min_length=1, max_length=300)
    start: str
    ende: str
    art: str | None = None
    bezug_typ: str | None = Field(default=None, pattern=r"^(session|event|todo)$")
    bezug_id: str | None = Field(default=None, max_length=200)


def _fehler(exc: Exception):
    if isinstance(exc, LookupError):
        raise HTTPException(404, str(exc))
    raise HTTPException(422, str(exc))


@router.get("/timer")
def timer(db: Session = Depends(get_db)):
    """Der laufende Timer oder `null`. Die App fragt das beim Oeffnen."""
    return {"laeuft": handzeit.als_antwort(handzeit.laufender(db))}


@router.post("/timer/start")
def timer_start(data: TimerStart, db: Session = Depends(get_db)):
    try:
        seit = _als_zeitpunkt(data.seit, "seit") if data.seit else None
        zeile = handzeit.starten(
            db, titel=data.titel, art=data.art,
            bezug_typ=data.bezug_typ, bezug_id=data.bezug_id, seit=seit,
        )
    except (IngestFehler, LookupError) as exc:
        _fehler(exc)
    return {"laeuft": handzeit.als_antwort(zeile)}


@router.post("/timer/stopp")
def timer_stopp(data: TimerStopp | None = None, db: Session = Depends(get_db)):
    try:
        ende = _als_zeitpunkt(data.ende, "ende") if data and data.ende else None
        ergebnis = handzeit.stoppen(db, ende=ende)
    except IngestFehler as exc:
        _fehler(exc)
    if ergebnis is None:
        raise HTTPException(409, "Es laeuft kein Timer")
    return ergebnis


@router.post("/haken", status_code=201)
def haken(data: Haken, db: Session = Depends(get_db)):
    try:
        return handzeit.abhaken(
            db, titel=data.titel, start=data.start, ende=data.ende, art=data.art,
            bezug_typ=data.bezug_typ, bezug_id=data.bezug_id,
        )
    except (IngestFehler, LookupError) as exc:
        _fehler(exc)


@router.get("/belege")
def belege(
    von: date_type = Query(...),
    bis: date_type = Query(...),
    db: Session = Depends(get_db),
):
    """Hand-Messungen mit Bezug im Zeitraum, woran eine Oberflaeche „erledigt" zeigt."""
    if (bis - von).days > 120:
        raise HTTPException(400, "Zeitraum hoechstens 120 Tage")
    return {"belege": handzeit.belege(db, von, bis), "laeuft": handzeit.als_antwort(handzeit.laufender(db))}
