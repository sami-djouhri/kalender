"""Weg-Termine: aus einem Termin mit Ort wird ein Eintrag „Weg zu X" davor,
und nach dem letzten Termin des Tages ein Eintrag „Weg nach Hause".

Der Weg ist ein eigener Termin und keine Beschriftung am Zieltermin. Das ist eine
Entscheidung mit Folgen, und zwar den gewuenschten: ein eigener Eintrag belegt
Zeit, und damit sieht der Tagesplaner (`scheduler.py`) ihn als belegt an und legt
keine Aufgabe hinein. Eine blosse Anzeige am Termin waere huebscher und
folgenlos: man haette die Zahl gelesen und trotzdem eine Aufgabe in die halbe
Stunde geplant, in der man im Bus sitzt.

Erkannt wird ein Weg-Termin an `travel_for_event_id`, nicht am Titel. Ein Titel
ist Text, den jemand aendert; danach faende der naechste Lauf seinen eigenen
Eintrag nicht wieder und legte einen zweiten an. Die RICHTUNG steht in
`travel_kind` (leer = Hinweg, "heim" = Weg danach), denn beide zeigen auf
denselben Termin.

Seit dem 2026-10-31 dazu:

* **Arbeit und Schule sind Aufenthalte.** Die Tagestyp-Ereignisse tragen keine
  Koordinaten; gibt es genau EINEN Ort der Art `arbeit` bzw. `schule`, gilt er
  als ihr Ort. Damit bekommt ein Arbeitstag einen Arbeitsweg, und ein Termin
  am Abend startet am Arbeitsplatz statt zu Hause. Der Tagestyp-Pfad selbst
  (CORE, Home Assistant liest ihn) wird dafuer nicht angefasst: nur gelesen.
* **Der Heimweg** (`tag_planen`): nach dem letzten Aufenthalt des Tages, der
  nicht zu Hause ist. Startet zum Ende dieses Aufenthalts. Frueher stand hier,
  es gebe bewusst keinen Rueckweg, weil die Annahme „wann man geht" bei den
  meisten Terminen falsch sei. Das bleibt wahr, und deshalb ist der Heimweg
  ein Vorschlag mit sichtbarer Annahme (ab Terminende) und kein Wecker. Der
  Owner hat ihn ausdruecklich verlangt (2026-10-31).
* **Einkauf** wird nur vorgeschlagen, nicht eingeplant (Entscheid): steht
  etwas auf der Einkaufsliste, nennt der Heimweg das, und gibt es einen Ort
  der Art `einkauf`, auch die Ankunft zu Hause ueber den Markt.
* **Bus und Bahn** (`transit`, Vorgabe seit dem Owner kein Auto hat): der Block
  beginnt bei der Abfahrt der passenden Verbindung, siehe `wegzeit.py`.
  Serien werden je Tagesart gerechnet (werktags, samstags, sonntags), weil ein
  Fahrplan vom Wochentag abhaengt; der Weg traegt die laengste der Zeiten.

Was dieses Modul bewusst NICHT tut: **Es weckt niemanden.** Die Aufstehzeit wird
in `aufstehzeit()` nur BERECHNET und angezeigt. Die feste Weckzeit je Tagestyp
(`sleep_schedule.py`) bleibt unberuehrt, und daran haengt die Weckkette in Home
Assistant.
"""

import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Event, Place
from backend.recurrence import expand_event
from backend.system_calendars import DAYTYPE_CALENDARS, WEGE_CALENDAR_ID
from backend.wegzeit import BESCHRIFTUNG, WegzeitFehlt, verbindung, verfuegbar, wegzeit

logger = logging.getLogger(__name__)

HEIM = "heim"
# Tagestyp-Kalender, deren Ereignisse einen Ort haben koennen, und welche Ortsart.
TAGESTYP_ORT = {DAYTYPE_CALENDARS["arbeit"]: "arbeit", DAYTYPE_CALENDARS["schule"]: "schule"}
EINKAUF_MINUTEN = 20


# --- Orte -------------------------------------------------------------------

def _ort_der_art(db: Session, art: str) -> Place | None:
    """Der eine Ort dieser Art mit Koordinaten, sonst None.

    Bei zwei Arbeitsorten ist nicht zu entscheiden, wohin ein Arbeitstag fuehrt;
    geraten wird nicht, es gibt dann eben keinen Arbeitsweg.
    """
    orte = (db.query(Place)
            .filter(Place.kind == art, Place.lat.isnot(None), Place.lon.isnot(None))
            .limit(2).all())
    return orte[0] if len(orte) == 1 else None


def _zuhause(db: Session) -> tuple[tuple[float, float] | None, str]:
    zuhause = db.query(Place).filter(Place.kind == "zuhause").first()
    heim = (zuhause.lat, zuhause.lon) if zuhause and zuhause.lat is not None else None
    return heim, (zuhause.name if zuhause else "zu Hause")


def _ziel(db: Session, event: Event) -> tuple[tuple[float, float] | None, str]:
    """Wo findet der Termin statt? Eigener Ort, sonst der Ort seines Tagestyps."""
    if event.lat is not None and event.lon is not None:
        return (event.lat, event.lon), (event.location or event.title)
    art = TAGESTYP_ORT.get(event.calendar_id)
    if art and not event.all_day:
        ort = _ort_der_art(db, art)
        if ort is not None:
            return (ort.lat, ort.lon), ort.name
    return None, ""


def _wie(referenz: datetime, zeit: datetime) -> datetime:
    """Zeit auf die Zonen-Konvention von `referenz` bringen (DB: Wanduhr ohne Zone)."""
    if referenz.tzinfo is None and zeit.tzinfo is not None:
        return zeit.replace(tzinfo=None)
    return zeit


# --- Aufenthalte eines Tages -------------------------------------------------

def _aufenthalte(db: Session, tag: date) -> list[dict]:
    """Alle Termine des Tages mit Ort, Serien expandiert, chronologisch.

    Ein Eintrag: {event, start, ende, ort, name}. Wege selbst und ganztaegige
    Termine zaehlen nicht, sie sind kein Ort, an dem man sich aufhaelt.
    """
    beginn = datetime.combine(tag, datetime.min.time())
    ende = beginn + timedelta(days=1)
    kandidaten = (
        db.query(Event)
        .filter(
            Event.start < ende,
            Event.travel_for_event_id.is_(None),
            Event.calendar_id != WEGE_CALENDAR_ID,
            Event.all_day == False,  # noqa: E712
        )
        .filter((Event.recurrence_rule.isnot(None)) | (Event.start >= beginn))
        .all()
    )
    out = []
    for e in kandidaten:
        ort, name = _ziel(db, e)
        if not ort:
            continue
        for inst in expand_event(e, beginn, ende):
            s, en = inst["start"], inst["end"]
            if s.tzinfo is not None:
                s, en = s.replace(tzinfo=None), en.replace(tzinfo=None)
            if beginn <= s < ende:
                out.append({"event": e, "start": inst["start"], "ende": inst["end"],
                            "ort": ort, "name": name})
    out.sort(key=lambda a: _wie(beginn, a["start"]))
    return out


def startort(db: Session, event: Event) -> tuple[tuple[float, float] | None, str]:
    """Von wo aus faengt der Weg an? Liefert (koordinaten, beschriftung).

    * **Einzeltermin:** vom letzten Aufenthalt des Tages, der vorher endet
      (Termin mit Ort, Arbeit, Schule). Wer um 9 in der Stadt war, laeuft um 11
      nicht von zu Hause los.
    * **Serientermin:** immer von zu Hause. Was an einem Dienstag vorher lag,
      sagt nichts ueber den Dienstag in drei Wochen.
    """
    heim, heim_name = _zuhause(db)
    if event.recurrence_rule:
        return heim, heim_name
    vorher = [a for a in _aufenthalte(db, event.start.date())
              if a["event"].id != event.id and _wie(event.start, a["ende"]) <= event.start]
    if vorher:
        letzter = vorher[-1]
        return letzter["ort"], letzter["name"]
    return heim, heim_name


# --- Hinweg --------------------------------------------------------------------

def _weg_termin(db: Session, event: Event) -> Event | None:
    return (
        db.query(Event)
        .filter(Event.travel_for_event_id == event.id,
                (Event.travel_kind.is_(None)) | (Event.travel_kind != HEIM))
        .first()
    )


def weg_entfernen(db: Session, event_id: str, nur_hinweg: bool = False) -> None:
    """Die Wege zu einem Termin loeschen (Hinweg, und ohne `nur_hinweg` auch Heimwege)."""
    q = db.query(Event).filter(Event.travel_for_event_id == event_id)
    if nur_hinweg:
        q = q.filter((Event.travel_kind.is_(None)) | (Event.travel_kind != HEIM))
    for weg in q.all():
        db.delete(weg)


def _tagesart(d: date) -> str:
    return "sonntags" if d.weekday() == 6 else "samstags" if d.weekday() == 5 else "werktags"


def _serie_mit_fahrplan(db: Session, event: Event, start_koord, ziel) -> tuple[int | None, str]:
    """Bus-und-Bahn-Wegzeit einer Serie: je Tagesart einmal, die laengste zaehlt.

    Ein Fahrplan haengt am Wochentag; eine Serie Mo bis Sa hat werktags einen
    anderen Bus als samstags. Ein Weg-Termin kann nur EINE Dauer tragen (er
    erbt die Regel der Serie), also traegt er die laengste: zu frueh losgehen
    kostet Minuten, zu spaet den Termin.
    """
    heute = datetime.combine(max(date.today(), event.start.date()), datetime.min.time())
    if event.start.tzinfo is not None:
        heute = heute.replace(tzinfo=event.start.tzinfo)
    gesehen: dict[str, int | None] = {}
    for inst in expand_event(event, heute, heute + timedelta(days=21)):
        art = _tagesart(inst["start"].date())
        if art in gesehen:
            continue
        ankunft = inst["start"] - timedelta(minutes=settings.WEG_PUFFER_MINUTEN)
        v = verbindung(start_koord, ziel, ankunft=ankunft)
        gesehen[art] = (round((_wie(inst["start"], v["abfahrt"]) - ankunft).total_seconds()
                              / -60) if v else None)
        if len(gesehen) == 3:
            break
    werte = [m for m in gesehen.values() if m]
    if not werte:
        return None, ""
    teile = [f"{art} {m} min" for art, m in gesehen.items() if m]
    fehlt = [art for art, m in gesehen.items() if not m]
    text = ", ".join(teile)
    if fehlt:
        text += f"; keine Verbindung {', '.join(fehlt)}"
    return max(werte), text


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

    ziel, _ = _ziel(db, event)
    if not ziel or not verfuegbar():
        weg_entfernen(db, event.id, nur_hinweg=True)
        return None

    start_koord, start_name = startort(db, event)
    if not start_koord:
        weg_entfernen(db, event.id, nur_hinweg=True)
        return None
    if start_koord == ziel:
        # Gleicher Ort wie vorher: es gibt nichts zurueckzulegen.
        weg_entfernen(db, event.id, nur_hinweg=True)
        return None

    mittel = mittel or event.travel_mode or settings.WEG_STANDARD_MITTEL
    ende = event.start - timedelta(minutes=settings.WEG_PUFFER_MINUTEN)
    zusatz = ""
    try:
        if mittel == "transit" and settings.OTP_URL:
            if event.recurrence_rule:
                minuten, zusatz = _serie_mit_fahrplan(db, event, start_koord, ziel)
            else:
                v = verbindung(start_koord, ziel, ankunft=ende)
                minuten = None
                if v:
                    minuten = max(1, round((ende - _wie(ende, v["abfahrt"])).total_seconds() / 60))
                    zusatz = v["text"]
        else:
            minuten = wegzeit(start_koord, ziel, mittel, ankunft=ende)
    except WegzeitFehlt as e:
        logger.info("Kein Weg fuer %r: %s", event.title, e)
        weg_entfernen(db, event.id, nur_hinweg=True)
        return None
    if not minuten:
        weg_entfernen(db, event.id, nur_hinweg=True)
        return None

    beginn = ende - timedelta(minutes=minuten)
    weg = _weg_termin(db, event)
    titel = f"Weg zu {event.title}"
    beschreibung = (
        f"{minuten} min {BESCHRIFTUNG.get(mittel, mittel)} ab {start_name}. "
        + (f"{zusatz}. " if zusatz else "")
        + "Berechnet, wird bei jeder Aenderung des Termins neu gesetzt."
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
            # genau dann, wenn die Arbeit sich wiederholt. Mit Fahrplan traegt
            # er die laengste Zeit der Tagesarten (`_serie_mit_fahrplan`).
            recurrence_rule=event.recurrence_rule,
            recurrence_exdates=event.recurrence_exdates,
        )
        # owner_sub nur uebernehmen, wenn das Ziel einen hat. Sonst stempelt
        #   das Tenant-Scoping den der Session; ein None hier machte den Weg
        #   unveraenderlich (Zeilen ohne Besitzer sind gesperrt).
        if event.owner_sub:
            weg.owner_sub = event.owner_sub
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


# --- Heimweg -------------------------------------------------------------------

def _einkauf_hinweis(db: Session, sub: str | None, start_koord, ab: datetime,
                     heim, ankunft_direkt: datetime | None) -> str:
    """Vorschlag, nicht Plan: steht Einkauf an, sagt der Heimweg es.

    Gibt es einen Ort der Art `einkauf`, rechnet er die Ankunft zu Hause ueber
    den Markt (Weg hin, `EINKAUF_MINUTEN` im Laden, Weg heim) gegen den
    direkten Weg. Faellt etwas davon aus, bleibt es beim Satz ohne Zahl.
    """
    try:
        from backend.cross_app import shopping_open_count
        anzahl, namen = shopping_open_count(sub)
    except Exception as e:  # Fremddienst; ein Heimweg scheitert nie daran
        logger.info("Einkaufsliste nicht lesbar: %s", e)
        return ""
    if not anzahl:
        return ""
    satz = f"Einkauf steht an ({anzahl} Artikel" + (f", z. B. {', '.join(namen)}" if namen else "") + ")."
    markt = _ort_der_art(db, "einkauf")
    if markt is None or not settings.OTP_URL or ankunft_direkt is None:
        return satz
    try:
        hin = verbindung(start_koord, (markt.lat, markt.lon), abfahrt=ab)
        if not hin:
            return satz
        weiter = hin["ankunft"] + timedelta(minutes=EINKAUF_MINUTEN)
        heimweg = verbindung((markt.lat, markt.lon), heim, abfahrt=weiter)
    except WegzeitFehlt:
        return satz
    if not heimweg:
        return satz
    an = _wie(ab, heimweg["ankunft"])
    mehr = round((an - ankunft_direkt).total_seconds() / 60)
    return (f"{satz} Über {markt.name}: zu Hause {an:%H:%M} statt "
            f"{ankunft_direkt:%H:%M} (+{mehr} min, {EINKAUF_MINUTEN} min im Laden).")


def tag_planen(db: Session, tag: date, mittel: str | None = None) -> dict:
    """Arbeitsweg und Heimweg eines Tages anlegen, anpassen oder entfernen.

    Der Aufrufer committet. Rueckgabe zum Ansehen: {hinwege, heimweg}.
    """
    ergebnis = {"datum": tag.isoformat(), "hinwege": 0, "heimweg": None}
    beginn = datetime.combine(tag, datetime.min.time())
    ende = beginn + timedelta(days=1)
    vorhanden = (db.query(Event)
                 .filter(Event.travel_kind == HEIM, Event.start >= beginn, Event.start < ende)
                 .all())

    # Arbeits- und Schultage: deren Hinweg legt sonst niemand an, weil der
    # Tagestyp-Pfad (CORE) bewusst nicht in dieses Modul ruft.
    for e in (db.query(Event)
              .filter(Event.calendar_id.in_(list(TAGESTYP_ORT)),
                      Event.start >= beginn, Event.start < ende).all()):
        if weg_aktualisieren(db, e, mittel) is not None:
            ergebnis["hinwege"] += 1

    heim, heim_name = _zuhause(db)
    aufenthalte = [a for a in _aufenthalte(db, tag) if a["ort"] != heim]
    if not heim or not aufenthalte or not verfuegbar():
        for w in vorhanden:
            db.delete(w)
        return ergebnis
    letzter = aufenthalte[-1]
    quelle = letzter["event"]
    ab = letzter["ende"]
    mittel = mittel or quelle.travel_mode or settings.WEG_STANDARD_MITTEL

    zusatz = ""
    try:
        if mittel == "transit" and settings.OTP_URL:
            v = verbindung(letzter["ort"], heim, abfahrt=ab)
            if not v:
                start = an = None
            else:
                start, an = _wie(ab, v["abfahrt"]), _wie(ab, v["ankunft"])
                zusatz = v["text"]
        else:
            # Valhalla rechnet ohne Uhrzeit; uebergeben wird sie trotzdem, der
            # Vertrag von `wegzeit` fragt nach einer Ankunft, hier ist es die
            # fruehestmoegliche Abfahrt (die Dauer haengt nicht davon ab).
            minuten = wegzeit(letzter["ort"], heim, mittel, ankunft=ab)
            start = ab if minuten else None
            an = ab + timedelta(minutes=minuten) if minuten else None
    except WegzeitFehlt as e:
        logger.info("Kein Heimweg am %s: %s", tag, e)
        start = an = None
    if start is None:
        for w in vorhanden:
            db.delete(w)
        return ergebnis

    einkauf = _einkauf_hinweis(db, quelle.owner_sub, letzter["ort"], ab, heim, an)
    minuten = max(1, round((an - start).total_seconds() / 60))
    beschreibung = (
        f"{minuten} min {BESCHRIFTUNG.get(mittel, mittel)} ab {letzter['name']}, "
        f"Annahme: Aufbruch zum Ende von „{quelle.title}“ ({ab:%H:%M}). "
        + (f"{zusatz}. " if zusatz else "")
        + (f"{einkauf} " if einkauf else "")
        + "Berechnet, wird bei Aenderungen am Tag neu gesetzt."
    )
    weg = next((w for w in vorhanden if w.travel_for_event_id == quelle.id), None)
    for w in vorhanden:
        if w is not weg:
            db.delete(w)
    if weg is None:
        weg = Event(calendar_id=WEGE_CALENDAR_ID, travel_for_event_id=quelle.id,
                    travel_kind=HEIM, title="", start=start, end=an)
        if quelle.owner_sub:
            weg.owner_sub = quelle.owner_sub
        db.add(weg)
    weg.title = f"Weg nach Hause ({heim_name})" if heim_name != "zu Hause" else "Weg nach Hause"
    weg.description = beschreibung
    weg.start, weg.end = start, an
    weg.travel_mode = mittel
    weg.lat, weg.lon = heim
    ergebnis["heimweg"] = {"ab": start.isoformat(), "an": an.isoformat(),
                           "von": letzter["name"], "einkauf": bool(einkauf)}
    return ergebnis


def tage_planen(db: Session, von: date, tage: int = 7, mittel: str | None = None) -> list[dict]:
    """`tag_planen` fuer mehrere Tage. Ein Tag, der scheitert, haelt die anderen nicht auf."""
    out = []
    for i in range(tage):
        tag = von + timedelta(days=i)
        try:
            out.append(tag_planen(db, tag, mittel))
        except Exception as e:  # fail-soft: ein Weg ist nie wichtiger als der Kalender
            logger.warning("Wege fuer %s nicht planbar: %s", tag, e)
            out.append({"datum": tag.isoformat(), "fehler": str(e)})
    return out


def termine_nachziehen(db: Session, ab: date) -> int:
    """Hinwege aller kommenden Termine mit Ort neu rechnen. Gibt die Zahl der Wege zurueck.

    Gebraucht nach einer Aenderung an den Orten (Umzug, neuer Arbeitsplatz).
    `tage_planen` zieht nur Arbeits- und Heimwege nach; ein Termin, dessen Weg
    von zu Hause startet, behielte sonst die alte Adresse als Start, bis
    jemand den Termin selbst speichert.
    """
    beginn = datetime.combine(ab, datetime.min.time())
    termine = (db.query(Event)
               .filter(Event.lat.isnot(None), Event.lon.isnot(None),
                       Event.all_day == False,  # noqa: E712
                       Event.travel_for_event_id.is_(None),
                       Event.calendar_id != WEGE_CALENDAR_ID,
                       (Event.start >= beginn) | (Event.recurrence_rule.isnot(None)))
               .all())
    zahl = 0
    for e in termine:
        try:
            if weg_aktualisieren(db, e) is not None:
                zahl += 1
        except Exception as fehler:  # fail-soft wie tage_planen
            logger.warning("Weg zu %s nicht nachgezogen: %s", e.id, fehler)
    return zahl


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


def run_wege_tick(jetzt: datetime | None = None) -> dict | None:
    """Einmal am Tag (ab 04:00): Arbeits-, Schul- und Heimwege fuer heute und
    die naechsten sieben Tage planen.

    Noetig, weil Arbeits- und Schultage ueber den Tagestyp-Pfad entstehen (CORE)
    und der bewusst nicht in dieses Modul ruft; Termine planen ihren Tag beim
    Speichern selbst neu (`routers/events.py`). Nur fuer den Owner, wie die
    uebrigen Automatiken des Runners (Entscheid SAGKAL-011). Idempotent ueber
    `SecretaryRun` (kind `wege`).
    """
    from zoneinfo import ZoneInfo

    from backend.assistant import _already_ran, _mark_ran, _owner_session

    owner = settings.DEFAULT_OWNER_SUB
    if not owner or not verfuegbar():
        return None
    jetzt = jetzt or datetime.now(ZoneInfo("Europe/Berlin"))
    if jetzt.hour < 4:
        return None
    heute = jetzt.date()
    db = _owner_session()
    try:
        if _already_ran(db, owner, heute, "wege"):
            return None
        ergebnis = tage_planen(db, heute, 8)
        db.commit()
        heimwege = sum(1 for e in ergebnis if e.get("heimweg"))
        _mark_ran(db, owner, heute, "wege", f"{heimwege} Heimwege in 8 Tagen")
        return {"tage": ergebnis}
    finally:
        db.close()
