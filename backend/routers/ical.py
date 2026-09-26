from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from icalendar import Calendar as ICalCalendar
from icalendar import Event as ICalEvent
from icalendar.prop import vRecur
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Calendar, Event, Setting
from backend.recurrence import parse_exdates

router = APIRouter(tags=["ical"])


def verify_feed_token(token: str, db: Session) -> bool:
    setting = db.query(Setting).filter(Setting.key == "feed_token").first()
    if not setting:
        return False
    return token == setting.value


def _new_calendar(name: str) -> ICalCalendar:
    ical = ICalCalendar()
    ical.add("prodid", "-//Kalender//host//DE")
    ical.add("version", "2.0")
    ical.add("x-wr-calname", name)
    ical.add("x-wr-timezone", "Europe/Berlin")
    return ical


def _add_events(ical: ICalCalendar, events: list[Event]) -> None:
    for event in events:
        ie = ICalEvent()
        ie.add("uid", f"{event.id}@kalender")
        ie.add("summary", event.title)
        if event.all_day:
            ie.add("dtstart", event.start.date())
            ie.add("dtend", event.end.date())
        else:
            ie.add("dtstart", event.start)
            ie.add("dtend", event.end)
        if event.description:
            ie.add("description", event.description)
        if event.location:
            ie.add("location", event.location)
        if event.recurrence_rule:
            try:
                ie.add("rrule", vRecur.from_ical(event.recurrence_rule))
            except (ValueError, KeyError):
                pass  # defekte RRULE nicht in den Feed schreiben
            exdates = parse_exdates(event.recurrence_exdates)
            for iso_day in sorted(exdates):
                try:
                    ie.add("exdate", date.fromisoformat(iso_day))
                except ValueError:
                    continue
        ie.add("dtstamp", event.created_at)
        ical.add_component(ie)


def _ics_response(ical: ICalCalendar, filename: str, download: bool) -> Response:
    # Default: inline text/calendar so subscription clients (HA, Apple, Google,
    # Thunderbird) consume it live. ?download=1 forces a file download.
    headers = {"Cache-Control": "public, max-age=300"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{filename}.ics"'
    return Response(
        content=ical.to_ical(),
        media_type="text/calendar; charset=utf-8",
        headers=headers,
    )


@router.get("/api/ical")
def ical_feed_all(
    token: str = Query(...),
    download: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Combined ICS feed across all calendars: one subscribable URL for external systems / HA."""
    if not verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")
    ical = _new_calendar("Kalender (alle)")
    _add_events(ical, db.query(Event).all())
    return _ics_response(ical, "kalender-alle", download)


@router.get("/api/tagesdecke/ical")
def ical_feed_tagesdecke(
    token: str = Query(...),
    tage_zurueck: int = Query(7, ge=0, le=31),
    tage_voraus: int = Query(14, ge=0, le=31),
    download: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Die Tagesdecke als abonnierbarer Kalender.

    Damit landet die Tagesplanung auf dem Handy, ohne dass sie in `events` steht.
    Genau das war der Kompromiss: sichtbar wie ein echter Termin, aber getrennt
    von der Tabelle, an der Tagestyp und Weckkette hängen.

    ⚠️ Das Fenster ist bewusst eng. Für jeden nicht festgeschriebenen Tag wird die
    Decke frisch gerechnet, und ein Abo-Client fragt alle paar Minuten. Ein Jahr
    Vorausschau wären mehrere tausend Abfragen pro Abruf auf einem Pi.

    Verworfene Blöcke kommen nicht in den Feed: was nicht stattgefunden hat,
    gehört nicht in einen Kalender, den man zur Orientierung ansieht.
    """
    if not verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")

    from datetime import timedelta

    from backend.tagesdecke import ARTEN, baue_decke

    heute = date.today()
    ical = _new_calendar("Tagesdecke")
    for versatz in range(-tage_zurueck, tage_voraus + 1):
        tag = heute + timedelta(days=versatz)
        try:
            decke = baue_decke(db, tag)
        except Exception:
            # Ein einzelner kaputter Tag darf das Abo nicht sprengen: der Client
            # zeigt dann gar nichts mehr an, und man sucht den Fehler im Handy.
            continue
        for block in decke["bloecke"]:
            if block["status"] == "verworfen":
                continue
            eintrag = ICalEvent()
            kennung = block["id"] or f"{tag.isoformat()}-{block['start'].strftime('%H%M')}"
            eintrag.add("uid", f"decke-{kennung}@kalender")
            eintrag.add("summary", f"{block['titel']}")
            eintrag.add("dtstart", block["start"])
            eintrag.add("dtend", block["ende"])
            beschreibung = ARTEN.get(block["art"], block["art"])
            if block["begruendung"]:
                beschreibung = f"{beschreibung}: {block['begruendung']}"
            eintrag.add("description", beschreibung)
            eintrag.add("categories", [block["art"]])
            eintrag.add("dtstamp", datetime.now(timezone.utc))
            ical.add_component(eintrag)
    return _ics_response(ical, "tagesdecke", download)


@router.get("/api/calendars/{calendar_id}/ical")
def ical_feed(
    calendar_id: str,
    token: str = Query(...),
    download: bool = Query(False),
    db: Session = Depends(get_db),
):
    if not verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")

    cal = db.query(Calendar).filter(Calendar.id == calendar_id).first()
    if not cal:
        raise HTTPException(status_code=404, detail="Calendar not found")

    ical = _new_calendar(cal.name)
    _add_events(ical, db.query(Event).filter(Event.calendar_id == calendar_id).all())
    return _ics_response(ical, cal.name, download)
