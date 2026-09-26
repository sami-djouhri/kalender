"""Weg-Termine: aus einem Termin mit Ort wird ein Eintrag „Weg zu X" davor.

Der Weg ist ein eigener Termin und keine Beschriftung am Zieltermin. Das ist eine
Entscheidung mit Folgen, und zwar den gewuenschten: ein eigener Eintrag belegt
Zeit, und damit sieht der Tagesplaner (`scheduler.py`) ihn als belegt an und legt
keine Aufgabe hinein. Eine blosse Anzeige am Termin waere huebscher und
folgenlos: man haette die Zahl gelesen und trotzdem eine Aufgabe in die halbe
Stunde geplant, in der man im Bus sitzt.

Erkannt wird ein Weg-Termin an `travel_for_event_id`, nicht am Titel. Ein Titel
ist Text, den jemand aendert; danach faende der naechste Lauf seinen eigenen
Eintrag nicht wieder und legte einen zweiten an.

Was dieses Modul bewusst NICHT tut:

* **Es weckt niemanden.** Die Aufstehzeit wird in `aufstehzeit()` nur BERECHNET
  und angezeigt. Die feste Weckzeit je Tagestyp (`sleep_schedule.py`) bleibt
  unberuehrt, und daran haengt die Weckkette in Home Assistant. Erst wenn die
  gerechnete Zahl ueber Wochen gestimmt hat, ist die Frage sinnvoll, ob sie
  etwas ausloesen darf.
* **Es rechnet keinen Rueckweg.** Ein Heimweg braucht eine Annahme darueber, wann
  man geht, und die ist bei den meisten Terminen falsch.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Calendar, Event, Place
from backend.system_calendars import WEGE_CALENDAR_ID
from backend.wegzeit import BESCHRIFTUNG, WegzeitFehlt, verfuegbar, wegzeit

logger = logging.getLogger(__name__)


def _koordinaten(event: Event) -> tuple[float, float] | None:
    if event.lat is None or event.lon is None:
        return None
    return (event.lat, event.lon)


def startort(db: Session, event: Event) -> tuple[tuple[float, float] | None, str]:
    """Von wo aus faengt der Weg an? Liefert (koordinaten, beschriftung).

    Zwei Faelle, und der Unterschied ist wichtig genug fuer eine eigene Regel:

    * **Einzeltermin:** vom letzten Termin des Tages, der vorher endet und
      Koordinaten hat. Wer um 9 in der Stadt war, laeuft um 11 nicht von zu Hause los.
    * **Serientermin:** immer von zu Hause. Was an einem Dienstag vorher lag,
      sagt nichts ueber den Dienstag in drei Wochen, und eine Serie hat keinen
      eigenen Vorgaenger je Instanz. Lieber eine nachvollziehbare Annahme als
      eine Zahl, die je nach Woche etwas anderes bedeutet.
    """
    zuhause = db.query(Place).filter(Place.kind == "zuhause").first()
    heim = (zuhause.lat, zuhause.lon) if zuhause and zuhause.lat is not None else None
    heim_name = zuhause.name if zuhause else "zu Hause"

    if event.recurrence_rule:
        return heim, heim_name

    tag_start = event.start.replace(hour=0, minute=0, second=0, microsecond=0)
    vorher = (
        db.query(Event)
        .filter(
            Event.start >= tag_start,
            Event.end <= event.start,
            Event.id != event.id,
            Event.lat.isnot(None),
            Event.travel_for_event_id.is_(None),
            Event.all_day == False,  # noqa: E712
        )
        .order_by(Event.end.desc())
        .first()
    )
    if vorher is not None:
        return (vorher.lat, vorher.lon), (vorher.location or vorher.title)
    return heim, heim_name


def _weg_termin(db: Session, event: Event) -> Event | None:
    return (
        db.query(Event)
        .filter(Event.travel_for_event_id == event.id)
        .first()
    )


def weg_entfernen(db: Session, event_id: str) -> None:
    """Den Weg zu einem Termin loeschen, falls es einen gibt."""
    weg = db.query(Event).filter(Event.travel_for_event_id == event_id).first()
    if weg is not None:
        db.delete(weg)


def weg_aktualisieren(db: Session, event: Event, mittel: str | None = None) -> Event | None:
    """Weg-Termin zu `event` anlegen, anpassen oder entfernen.

    Gibt den Weg-Termin zurueck, oder None, wenn es keinen geben soll. Der
    Aufrufer committet: so bleibt ein Termin und sein Weg eine Transaktion, und
    es kann keinen Weg zu einem Termin geben, den es nicht gibt.
    """
    # Ein Weg zum Weg waere eine Endlosschleife, und ganztaegige Termine haben
    # keine Uhrzeit, an der man ankommen koennte.
    if event.travel_for_event_id or event.all_day:
        return None
    if event.calendar_id == WEGE_CALENDAR_ID:
        return None

    ziel = _koordinaten(event)
    if not ziel or not verfuegbar():
        weg_entfernen(db, event.id)
        return None

    start_koord, start_name = startort(db, event)
    if not start_koord:
        weg_entfernen(db, event.id)
        return None
    if start_koord == ziel:
        # Gleicher Ort wie vorher: es gibt nichts zurueckzulegen.
        weg_entfernen(db, event.id)
        return None

    mittel = mittel or event.travel_mode or settings.WEG_STANDARD_MITTEL
    try:
        minuten = wegzeit(start_koord, ziel, mittel, ankunft=event.start)
    except WegzeitFehlt as e:
        logger.info("Kein Weg fuer %r: %s", event.title, e)
        weg_entfernen(db, event.id)
        return None
    if not minuten:
        weg_entfernen(db, event.id)
        return None

    ende = event.start - timedelta(minutes=settings.WEG_PUFFER_MINUTEN)
    beginn = ende - timedelta(minutes=minuten)

    weg = _weg_termin(db, event)
    titel = f"Weg zu {event.title}"
    beschreibung = (
        f"{minuten} min {BESCHRIFTUNG.get(mittel, mittel)} ab {start_name}. "
        f"Berechnet, wird bei jeder Aenderung des Termins neu gesetzt."
    )
    if weg is None:
        weg = Event(
            calendar_id=WEGE_CALENDAR_ID,
            title=titel,
            description=beschreibung,
            start=beginn,
            end=ende,
            travel_for_event_id=event.id,
            travel_mode=mittel,
            lat=ziel[0],
            lon=ziel[1],
            location=event.location,
            # Die Serie erbt die Regel des Ziels: der Arbeitsweg wiederholt sich
            # genau dann, wenn die Arbeit sich wiederholt.
            # ⚠️ Das gilt, solange die Wegzeit nicht vom Wochentag abhaengt. Bei
            # Fuss und Rad stimmt das; sobald ein Fahrplan-Anbieter dazukommt,
            # ist es falsch (Wochenendfahrplan) und muss je Instanz gerechnet werden.
            recurrence_rule=event.recurrence_rule,
            recurrence_exdates=event.recurrence_exdates,
            owner_sub=event.owner_sub,
        )
        db.add(weg)
    else:
        weg.title = titel
        weg.description = beschreibung
        weg.start = beginn
        weg.end = ende
        weg.travel_mode = mittel
        weg.lat, weg.lon = ziel
        weg.location = event.location
        weg.recurrence_rule = event.recurrence_rule
        weg.recurrence_exdates = event.recurrence_exdates
    return weg


def aufstehzeit(db: Session, tag) -> dict | None:
    """Wann muesste man aufstehen, damit der erste Termin des Tages klappt?

    Reine Auskunft. Sie aendert nichts, weckt nichts und wird nirgends
    gespeichert; wer sie nutzt, vergleicht sie mit der festen Weckzeit.

    `vorlauf_minuten` ist bewusst ein grober Wert und keine gelernte Groesse:
    was jemand morgens braucht, weiss der Kalender nicht, und eine erfundene
    Praezision waere schlimmer als eine sichtbare Annahme.
    """
    beginn = datetime.combine(tag, datetime.min.time())
    ende = beginn + timedelta(days=1)
    erster = (
        db.query(Event)
        .filter(
            Event.start >= beginn,
            Event.start < ende,
            Event.all_day == False,  # noqa: E712
        )
        .order_by(Event.start.asc())
        .first()
    )
    if erster is None:
        return None
    vorlauf = settings.WEG_VORLAUF_MINUTEN
    los = erster.start - timedelta(minutes=vorlauf)
    return {
        "datum": tag.isoformat(),
        "erster_termin": erster.title,
        "beginnt": erster.start.isoformat(),
        "ist_weg": bool(erster.travel_for_event_id),
        "vorlauf_minuten": vorlauf,
        "aufstehen": los.isoformat(),
    }
