from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Calendar, Event
from backend.recurrence import add_exdate, expand_events, set_until
from backend.scheduler import (
    _ensure_aware,
    _week_start_from_date,
    check_conflicts_after_event_change,
    reschedule_week,
)
from backend.schemas import EventCreate, EventResponse, EventUpdate
from backend.system_calendars import DAYTYPE_CALENDARS, GENERATED_CALENDAR_IDS, WEGE_CALENDAR_ID
from backend.wanduhr import als_wanduhr
from backend.wege import weg_aktualisieren, weg_entfernen

router = APIRouter(prefix="/api/events", tags=["events"])

# Kalender, in die ueber /api/events weder geschrieben noch verschoben werden darf.
#
# * Feiertage/Geburtstage werden erzeugt (holidays_nrw bzw. aus Kontakten) und beim
#   Start neu abgeglichen, von Hand Eingetragenes waere beim naechsten Neustart weg.
# * Die vier Tagestyp-Kalender sind **Steuerdaten, keine Ablage**. Was dort liegt,
#   bestimmt ueber /api/day-type/today das Weckverhalten. Der vorgesehene Weg ist
#   `POST /api/day-type/set` (bzw. `/set-external`), der genau ein Ereignis je Tag
#   garantiert. Ein von Hand eingetragener zweiter Eintrag machte den Tag
#   mehrdeutig, und der Umschalter der Oberflaeche konnte ihn nicht mehr
#   aufloesen: er meldete „frei", waehrend Home Assistant weiter „urlaub" las.
#   Gemessen in scripts/simulieren.py, Sonde „Tagestyp-Kalender als Steuerdaten".
GESPERRTE_KALENDER = GENERATED_CALENDAR_IDS | set(DAYTYPE_CALENDARS.values())

_SPERRGRUND = {
    **{cal: "Feiertage und Geburtstage werden erzeugt: Einträge dort gingen beim "
            "nächsten Start verloren." for cal in GENERATED_CALENDAR_IDS},
    **{cal: "Tagestypen werden über POST /api/day-type/set gesetzt, nicht als Termin "
            "angelegt, sonst wird der Tag mehrdeutig." for cal in DAYTYPE_CALENDARS.values()},
    # Eigener Grund statt der Feiertags-Begründung von oben: die stimmte für
    # diesen Kalender inhaltlich nicht und hätte beim Lesen in die Irre geführt.
    WEGE_CALENDAR_ID: "Wege werden aus dem Ort des Zieltermins berechnet. Ein von "
                      "Hand angelegter Eintrag würde beim nächsten Speichern des "
                      "Termins überschrieben. Ort am Termin setzen statt hier.",
}


def _kalender_pruefen(calendar_id: str) -> None:
    """Wehrt Schreibzugriffe auf erzeugte und auf Steuer-Kalender ab."""
    if calendar_id in GESPERRTE_KALENDER:
        raise HTTPException(status_code=403, detail=_SPERRGRUND[calendar_id])


def _schreibschutz(event: Event) -> None:
    """Verweigert Änderung/Löschung an Zeilen, die dem Aufrufer nicht gehören.

    Zwei Faelle, beide vor 2026-08-24 offen (gemessen in scripts/simulieren.py,
    Sonde „Schutz der globalen Feiertage"):

    1. **Erzeugte Kalender.** Anlegen war mit 403 gesperrt, Ändern und Löschen
       nicht, die Sperre sass allein im Erzeugungspfad.
    2. **Zeilen ohne Besitzer.** Feiertags-Events sind die einzigen, die *jeder*
       Mandant sieht (``owner_sub IS NULL``, siehe backend/tenant.py). Genau
       deshalb liess sich der 25.12. von einem beliebigen Mandanten fuer **alle**
       loeschen: samt Tagestyp, an dem Heiz- und Weckautomatik haengen.
    """
    if event.calendar_id in GESPERRTE_KALENDER:
        raise HTTPException(status_code=403, detail=_SPERRGRUND[event.calendar_id])
    if event.owner_sub is None:
        raise HTTPException(
            status_code=403,
            detail="Systemzeilen ohne Besitzer sind für alle Mandanten sichtbar und "
                   "können nicht geändert werden.",
        )


@router.get("", response_model=list[EventResponse])
def list_events(
    calendar_id: str | None = Query(None),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    db: Session = Depends(get_db),
):
    # Fenstergrenzen zuerst auf die Hauskonvention bringen (Berliner Wanduhr,
    # zonenlos: siehe backend/wanduhr.py). Das Frontend schickt sie als
    # `Date.toISOString()`, also `…T22:00:00.000Z` fuer Mitternacht Berlin.
    # Ungerechnet verglichen lag das Fenster um den UTC-Versatz daneben: Termine
    # nach 22:00 am letzten Tag der Ansicht fehlten und erschienen am Folgetag.
    if start is not None:
        start = als_wanduhr(start)
    if end is not None:
        end = als_wanduhr(end)

    base = db.query(Event)
    if calendar_id:
        base = base.filter(Event.calendar_id == calendar_id)

    # Ohne Fenster keine Recurrence-Expansion (sonst unbegrenzt): Basis-Events roh.
    if start is None or end is None:
        q = base
        if start:
            q = q.filter(Event.end >= start)
        if end:
            q = q.filter(Event.start <= end)
        return q.order_by(Event.start).all()

    # Mit Fenster: einmalige Events per DB filtern, wiederkehrende separat holen
    # (Basisstart kann vor dem Fenster liegen, recurst aber hinein) und expandieren.
    one_off = base.filter(
        Event.recurrence_rule.is_(None),
        Event.end >= start,
        Event.start <= end,
    ).all()
    recurring = base.filter(
        Event.recurrence_rule.isnot(None),
        Event.start <= end,
    ).all()
    return expand_events(one_off + recurring, start, end)


@router.post("", response_model=EventResponse, status_code=201)
def create_event(data: EventCreate, db: Session = Depends(get_db)):
    cal = db.query(Calendar).filter(Calendar.id == data.calendar_id).first()
    if not cal:
        raise HTTPException(status_code=404, detail="Calendar not found")
    _kalender_pruefen(data.calendar_id)
    event = Event(**data.model_dump())
    db.add(event)
    db.flush()          # braucht die id, bevor der Weg auf sie zeigen kann
    weg_aktualisieren(db, event)
    db.commit()
    db.refresh(event)
    check_conflicts_after_event_change(db, event)
    return event


def _resolve_base(event_id: str, db: Session) -> Event:
    """Akzeptiert Reihen-ID oder Instanz-ID ('{base}::{date}') und liefert das Basis-Event."""
    base_id = event_id.split("::", 1)[0]
    event = db.query(Event).filter(Event.id == base_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.get("/{event_id}", response_model=EventResponse)
def get_event(event_id: str, db: Session = Depends(get_db)):
    return _resolve_base(event_id, db)


@router.put("/{event_id}", response_model=EventResponse)
def update_event(event_id: str, data: EventUpdate, db: Session = Depends(get_db)):
    event = _resolve_base(event_id, db)
    _schreibschutz(event)
    if data.calendar_id is not None:
        # Sonst waere das Verschieben der Schleichweg in einen gesperrten Kalender:
        # anlegen im Termine-Kalender, dann per PUT in den Feiertagskalender ziehen.
        _kalender_pruefen(data.calendar_id)
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(event, key, value)
    weg_aktualisieren(db, event)
    db.commit()
    db.refresh(event)
    check_conflicts_after_event_change(db, event)
    return event


def _reschedule_around(db: Session, evt_start: datetime, evt_end: datetime) -> None:
    start_date = _ensure_aware(evt_start).date()
    end_date = (_ensure_aware(evt_end) - timedelta(seconds=1)).date()
    reschedule_week(db, start_date)
    if _week_start_from_date(end_date) != _week_start_from_date(start_date):
        reschedule_week(db, end_date)


@router.delete("/{event_id}", status_code=204)
def delete_event(event_id: str, db: Session = Depends(get_db)):
    """Löscht das gesamte Event bzw. die ganze Reihe (Basis-ID oder Instanz-ID)."""
    event = _resolve_base(event_id, db)
    _schreibschutz(event)
    evt_start, evt_end = event.start, event.end
    # Erst der Weg, dann der Termin: sonst bliebe ein „Weg zu X" stehen, dessen
    # Ziel es nicht mehr gibt, und niemand fuehle sich dafuer zustaendig.
    weg_entfernen(db, event.id)
    db.delete(event)
    db.commit()
    _reschedule_around(db, evt_start, evt_end)


@router.delete("/{event_id}/instances/{occ_date}", status_code=204)
def delete_event_instance(event_id: str, occ_date: str, db: Session = Depends(get_db)):
    """'Nur dieser Termin': Einzelinstanz per EXDATE aus der Reihe nehmen."""
    event = _resolve_base(event_id, db)
    _schreibschutz(event)
    if not event.recurrence_rule:
        raise HTTPException(status_code=400, detail="Event is not recurring")
    try:
        day = datetime.fromisoformat(occ_date[:10]).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid occurrence date")
    event.recurrence_exdates = add_exdate(event.recurrence_exdates, day.isoformat())
    db.commit()


@router.post("/{event_id}/truncate", status_code=204)
def truncate_series(event_id: str, occ_date: str = Query(...), db: Session = Depends(get_db)):
    """'Diese und folgende': Reihe per UNTIL vor der Instanz abschneiden."""
    event = _resolve_base(event_id, db)
    _schreibschutz(event)
    if not event.recurrence_rule:
        raise HTTPException(status_code=400, detail="Event is not recurring")
    try:
        day = datetime.fromisoformat(occ_date[:10]).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid occurrence date")
    # UNTIL eine Sekunde vor der abzuschneidenden Instanz → sie und alle späteren
    # entfallen. Gerechnet wird in Wanduhrzeit (event.start liegt bereits so vor),
    # damit UNTIL und die Serienstarts derselben Zeitrechnung folgen.
    until = als_wanduhr(event.start).replace(
        year=day.year, month=day.month, day=day.day
    ) - timedelta(seconds=1)
    event.recurrence_rule = set_until(event.recurrence_rule, until)
    db.commit()
