import hmac
from typing import Annotated
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

# Termine liegen als Berliner Wanduhr ohne Zone in der DB (backend/wanduhr.py).
# Dieses Modul rechnet deshalb zonenlos. BERLIN bleibt nur, weil tests/test_daytype.py
# es historisch von hier importiert.
BERLIN = ZoneInfo("Europe/Berlin")

from backend.birthday_utils import GEBURTSTAGE_CALENDAR_ID
from backend.database import get_db
from backend.models import Calendar, DailyGoal, Event, Habit, HabitSession, Setting

# Fixed calendar IDs: kanonisch in backend/system_calendars.py (importfrei, damit
# tenant.py sie ohne Router-Zyklus nutzen kann). Hier nur re-exportiert, weil Tests
# und Konsumenten sie historisch von hier importieren.
from backend.system_calendars import (  # noqa: F401
    DAYTYPE_CALENDARS,
    DAYTYPE_IDS,
    FEIERTAG_CALENDAR_ID,
)
from backend.arbeitszeiten import ZEIT_TYPEN, zeiten_parsen
from backend.arbeitszeiten import vorgabe as arbeitszeit_vorgabe
from backend.tenant import _session_sub
from backend.wanduhr import als_wanduhr, tagesgrenzen


def _verify_feed_token(token: str, db: Session) -> bool:
    setting = db.query(Setting).filter(Setting.key == "feed_token").first()
    if not setting or not token or not setting.value:
        return False
    # compare_digest statt ==: die Route ist ohne Anmeldung erreichbar und kann
    # den Tagestyp schreiben (/set-external), ein Vergleich mit fruehem Abbruch
    # verraet ueber die Laufzeit, wie viele Zeichen schon stimmen.
    return hmac.compare_digest(token.encode("utf-8"), setting.value.encode("utf-8"))


def _schreib_mandant(db: Session) -> str:
    """Der Mandant, dem ein neues Tagestyp-Ereignis gehoert. Ohne einen: 503.

    Ausdruecklich statt ueber den ``before_flush``-Stempel in ``tenant.py``.
    Der stempelt nur, wenn die Session einen Mandanten traegt; ist
    ``DEFAULT_OWNER_SUB`` leer (``.env.tenant`` fehlt und die Ableitung beim Start
    scheitert), entsteht ein Ereignis, das kein Mandant sieht. Es liest sich dann
    als ``frei``, ohne einen einzigen Fehler, und daran haengt das 05:35-Weckfenster.
    Genau so sind ab dem 07.09. 98 Arbeits- und Schultage verschwunden.

    Die Pruefung steht VOR dem Abraeumen: im System-Kontext liefe
    ``_daytype_ereignisse`` ungescoped, ein ``db.delete`` raeumte dann die
    Tagestypen ALLER Mandanten an diesem Tag ab.
    """
    # Dieselbe Lesart wie das Scoping: ohne Eintrag gilt DEFAULT_OWNER_SUB.
    sub = _session_sub(db)
    if not sub:
        raise HTTPException(
            503,
            "Mandant unbekannt, Tagestyp wird nicht geschrieben "
            "(DEFAULT_OWNER_SUB fehlt, saganta/scripts/owner-kennung-eintragen.sh)",
        )
    return sub


def _get_daytype(db: Session, day: date) -> str:
    """Return the day type for a given date.

    Priority: feiertag > krank > urlaub > schule > arbeit > frei ('krank' meldet
    sich nach aussen als 'frei', fuer die Automatik ist ein Krankheitstag ein
    freier Tag, in der Oberflaeche bleibt er unterscheidbar).
    Uses overlap detection so both all-day and timed events are found.
    """
    day_start, day_end = tagesgrenzen(day)

    # Check Feiertag first (highest priority)
    feiertag = (
        db.query(Event)
        .filter(
            Event.calendar_id == FEIERTAG_CALENDAR_ID,
            Event.start < day_end,
            Event.end > day_start,
        )
        .first()
    )
    if feiertag:
        return "feiertag"

    # Check Krankenschein (counts as "frei" for HA automations)
    krank = (
        db.query(Event)
        .filter(
            Event.calendar_id == DAYTYPE_CALENDARS["krank"],
            Event.start < day_end,
            Event.end > day_start,
        )
        .first()
    )
    if krank:
        return "frei"

    # Check user-set day types (urlaub > schule > arbeit)
    for dtype in ("urlaub", "schule", "arbeit"):
        cal_id = DAYTYPE_CALENDARS[dtype]
        event = (
            db.query(Event)
            .filter(
                Event.calendar_id == cal_id,
                Event.start < day_end,
                Event.end > day_start,
            )
            .first()
        )
        if event:
            return dtype

    return "frei"


def _get_daytype_display(db: Session, day: date) -> str:
    """Like _get_daytype but returns display-friendly types including 'krank' and 'wochenende'."""
    day_start, day_end = tagesgrenzen(day)

    feiertag = (
        db.query(Event)
        .filter(Event.calendar_id == FEIERTAG_CALENDAR_ID, Event.start < day_end, Event.end > day_start)
        .first()
    )
    if feiertag:
        return "feiertag"

    krank = (
        db.query(Event)
        .filter(Event.calendar_id == DAYTYPE_CALENDARS["krank"], Event.start < day_end, Event.end > day_start)
        .first()
    )
    if krank:
        return "krank"

    for dtype in ("urlaub", "schule", "arbeit"):
        cal_id = DAYTYPE_CALENDARS[dtype]
        event = (
            db.query(Event)
            .filter(Event.calendar_id == cal_id, Event.start < day_end, Event.end > day_start)
            .first()
        )
        if event:
            return dtype

    if day.weekday() >= 5:
        return "wochenende"

    return "frei"


def _daytypes_display_bereich(db: Session, start: date, tage: int) -> dict[date, str]:
    """Display-Tagestyp je Tag eines zusammenhaengenden Bereichs mit EINER Abfrage.

    Aufloesung je Tag identisch zu `_get_daytype_display`; gebuendelt, weil
    /week und /week-schedule sonst bis zu 5 Abfragen je Tag stellen (gemessen
    ~200 ms fuer eine Woche).
    """
    bereich_start, _ = tagesgrenzen(start)
    _, bereich_end = tagesgrenzen(start + timedelta(days=tage - 1))
    prio_cal_ids = [FEIERTAG_CALENDAR_ID, DAYTYPE_CALENDARS["krank"]] + [
        DAYTYPE_CALENDARS[t] for t in ("urlaub", "schule", "arbeit")
    ]
    events = (
        db.query(Event.calendar_id, Event.start, Event.end)
        .filter(
            Event.calendar_id.in_(prio_cal_ids),
            Event.start < bereich_end,
            Event.end > bereich_start,
        )
        .all()
    )

    typ_je_cal = {
        FEIERTAG_CALENDAR_ID: "feiertag",
        DAYTYPE_CALENDARS["krank"]: "krank",
        DAYTYPE_CALENDARS["urlaub"]: "urlaub",
        DAYTYPE_CALENDARS["schule"]: "schule",
        DAYTYPE_CALENDARS["arbeit"]: "arbeit",
    }
    rang = {cal_id: i for i, cal_id in enumerate(prio_cal_ids)}

    result: dict[date, str] = {}
    for i in range(tage):
        d = start + timedelta(days=i)
        day_start, day_end = tagesgrenzen(d)
        treffer = [e for e in events if e.start < day_end and e.end > day_start]
        if treffer:
            bester = min(treffer, key=lambda e: rang[e.calendar_id])
            result[d] = typ_je_cal[bester.calendar_id]
        elif d.weekday() >= 5:
            result[d] = "wochenende"
        else:
            result[d] = "frei"
    return result


def _tageszeiten(db: Session, day: date, typ: str) -> tuple[str | None, str | None]:
    """Beginn und Ende (HH:MM) des Ereignisses, das den Tagestyp traegt.

    Nur fuer Arbeit und Schule, sonst ``(None, None)``. Das ist die Quelle, aus
    der Home Assistant seine Weck- und Abgangszeiten ableitet: die Uhrzeit des
    Ereignisses, nicht eine Konstante und nicht die Wochenvorgabe.
    """
    if typ not in ("arbeit", "schule"):
        return None, None
    beginn, ende = tagesgrenzen(day)
    ereignis = (
        db.query(Event)
        .filter(
            Event.calendar_id == DAYTYPE_CALENDARS[typ],
            Event.start < ende,
            Event.end > beginn,
        )
        .order_by(Event.start)
        .first()
    )
    if ereignis is None or ereignis.all_day:
        return None, None
    return als_wanduhr(ereignis.start).strftime("%H:%M"), als_wanduhr(ereignis.end).strftime("%H:%M")


def _tagestyp_antwort(db: Session, day: date) -> dict:
    """Die Antwort der eingefrorenen Ebene 1. ``date`` und ``type`` wie immer;
    ``start``/``end`` seit 2026-10-30 additiv (Vertrag: nur erweitern)."""
    typ = _get_daytype(db, day)
    start, ende = _tageszeiten(db, day, typ)
    return {"date": day.isoformat(), "type": typ, "start": start, "end": ende}


# Arbeit/Schule time definitions
ARBEIT_TIMES = {
    "default": (7, 0, 16, 0),    # Mo-Do: 7:00 - 16:00 (reale Arbeitszeit)
    "friday":  (7, 0, 13, 0),    # Fr: 7:00 - 13:00
}
SCHULE_TIMES = (8, 0, 14, 40)    # 8:00 - 14:40


# Reihenfolge, in der ein Tag aufgeloest wird. Muss mit `_get_daytype` und
# `_get_daytype_display` uebereinstimmen, deshalb steht sie hier einmal und wird
# dort nicht noch einmal hingeschrieben.
DAYTYPE_PRIORITAET = ("krank", "urlaub", "schule", "arbeit")


def _daytype_ereignisse(db: Session, day: date) -> list[tuple[str, Event]]:
    """Alle Tagestyp-Ereignisse eines Tages, nach Prioritaet sortiert.

    Ein Tag soll genau ein Tagestyp-Ereignis tragen. Dass er trotzdem mehrere
    haben *kann*, hat den Umschalter frueher in eine Luege getrieben: er suchte
    das erste Ereignis in **Wörterbuch-Reihenfolge** (arbeit, schule, urlaub,
    krank) statt nach Prioritaet, loeschte genau dieses eine und meldete
    anschliessend „frei": waehrend das zweite liegen blieb und Home Assistant
    weiter „urlaub" las. Oberflaeche und Weckautomatik sagten Verschiedenes.

    Diese Funktion liefert deshalb **alle**; die Aufrufer raeumen alle ab.
    """
    beginn, ende = tagesgrenzen(day)
    gefunden: list[tuple[str, Event]] = []
    for dtype in DAYTYPE_PRIORITAET:
        for ereignis in (
            db.query(Event)
            .filter(
                Event.calendar_id == DAYTYPE_CALENDARS[dtype],
                Event.start < ende,
                Event.end > beginn,
            )
            .all()
        ):
            gefunden.append((dtype, ereignis))
    return gefunden


def _tagestyp_setzen(
    db: Session, day: date, dtype: str, half: bool = False, zeiten: tuple | None = None,
) -> str:
    """Setzt den Tagestyp eines Tages und stellt sicher, dass er eindeutig bleibt.

    Umschalt-Verhalten: derselbe Typ noch einmal → der Tag wird 'frei'.

    Gemeinsamer Kern von `POST /api/day-type/set` (JWT, Oberflaeche) und
    `POST /api/day-type/set-external` (Feed-Token, tardis). Beide Endpunkte
    trugen bis 2026-08-24 je eine eigene Kopie dieser rund fuenfzig Zeilen, mit
    bereits auseinandergelaufenem Verhalten: nur die JWT-Variante kannte halbe
    Urlaubstage. Zwei Wahrheiten fuer den Wert, an dem der Wecker haengt.
    """
    mandant = _schreib_mandant(db)
    vorhanden = _daytype_ereignisse(db, day)
    aktueller_typ = vorhanden[0][0] if vorhanden else None
    aktuell_halb = bool(vorhanden) and "(1/2)" in (vorhanden[0][1].title or "")

    # Immer ALLE abraeumen, nicht nur das erste. Das repariert nebenbei Tage, die
    # aus frueheren Doppeleintraegen mehrdeutig geworden sind.
    for _, ereignis in vorhanden:
        db.delete(ereignis)

    # Mit ausdruecklichen Uhrzeiten wird GESETZT, nie umgeschaltet: wer die
    # Arbeitszeit eines schon eingetragenen Arbeitstags aendert, will ihn nicht
    # loeschen, auch nicht, wenn die neue Zeit zufaellig der alten gleicht.
    umschalten = zeiten is None and aktueller_typ == dtype and aktuell_halb == half
    if not umschalten:
        if zeiten is None and dtype in ZEIT_TYPEN:
            zeiten = arbeitszeit_vorgabe(db, mandant, dtype, day.weekday())
        titles = {"arbeit": "Arbeit", "schule": "Schule", "urlaub": "Urlaub",
                  "krank": "Krankenschein"}
        title = titles.get(dtype, dtype.capitalize())
        if half and dtype == "urlaub":
            title = "Urlaub (1/2)"
        evt_data = _make_daytype_event(dtype, day, zeiten)
        db.add(Event(
            owner_sub=mandant,
            calendar_id=DAYTYPE_CALENDARS[dtype],
            title=title,
            start=evt_data["start"],
            end=evt_data["end"],
            all_day=evt_data["all_day"],
        ))

    db.commit()
    from backend.scheduler import reschedule_week
    reschedule_week(db, day)
    return "frei" if umschalten else dtype


def _tag_parsen(date_str: str) -> date:
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")


def _typ_pruefen(dtype: str) -> None:
    if dtype not in DAYTYPE_CALENDARS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type. Must be one of: {', '.join(DAYTYPE_CALENDARS.keys())}",
        )


def _make_daytype_event(dtype: str, day: date, zeiten: tuple | None = None) -> dict:
    """Return start, end, all_day for a daytype event on a given date.

    ``zeiten`` = (start_h, start_m, ende_h, ende_m) fuer Arbeit/Schule; ohne
    Angabe gilt die eingebaute Konstante. Die Vorgabe je Mandant loesen die
    Setz-Pfade auf (``backend/arbeitszeiten.py``), nicht diese Funktion: sie wird
    auch aus den Start-Migrationen gerufen, die keinen Mandanten kennen.
    """
    if dtype in ("arbeit", "schule") and zeiten is not None:
        t = zeiten
        return {
            "start": datetime(day.year, day.month, day.day, t[0], t[1]),
            "end": datetime(day.year, day.month, day.day, t[2], t[3]),
            "all_day": False,
        }
    if dtype == "arbeit":
        t = ARBEIT_TIMES["friday"] if day.weekday() == 4 else ARBEIT_TIMES["default"]
        return {
            "start": datetime(day.year, day.month, day.day, t[0], t[1]),
            "end": datetime(day.year, day.month, day.day, t[2], t[3]),
            "all_day": False,
        }
    elif dtype == "schule":
        t = SCHULE_TIMES
        return {
            "start": datetime(day.year, day.month, day.day, t[0], t[1]),
            "end": datetime(day.year, day.month, day.day, t[2], t[3]),
            "all_day": False,
        }
    else:  # urlaub, krank
        day_start, day_end = tagesgrenzen(day)
        return {
            "start": day_start,
            "end": day_end,
            "all_day": True,
        }


# --- Public router (feed_token auth, for HA) ---

public_router = APIRouter(prefix="/api/day-type", tags=["day-type"])


@public_router.get("")
def get_day_type(
    date_str: str = Query(..., alias="date"),
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")
    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")
    return _tagestyp_antwort(db, day)


@public_router.get("/today")
def get_day_type_today(
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")
    return _tagestyp_antwort(db, date.today())


@public_router.get("/tomorrow")
def get_day_type_tomorrow(
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")
    return _tagestyp_antwort(db, date.today() + timedelta(days=1))


@public_router.get("/events")
def get_events_for_date(
    token: str = Query(...),
    date_str: str = Query(None, alias="date"),
    db: Session = Depends(get_db),
):
    """Return personal events (non-daytype calendars) for a given date."""
    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")

    if date_str:
        try:
            day = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")
    else:
        day = date.today()

    day_start, day_end = tagesgrenzen(day)

    events = (
        db.query(Event, Calendar.name.label("calendar_name"))
        .join(Calendar, Event.calendar_id == Calendar.id)
        .filter(
            Event.calendar_id.notin_(DAYTYPE_IDS),
            Event.start < day_end,
            Event.end > day_start,
        )
        .order_by(Event.all_day.desc(), Event.start)
        .all()
    )

    result = []
    for event, calendar_name in events:
        result.append({
            "title": event.title,
            "calendar": calendar_name,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "all_day": event.all_day,
        })

    return {"date": day.isoformat(), "events": result}


@public_router.get("/goals")
def get_goals_for_date(
    token: str = Query(...),
    date_str: str = Query(None, alias="date"),
    db: Session = Depends(get_db),
):
    """Return live daily goals for a date (feed_token auth, for briefing/digest)."""
    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")

    if date_str:
        try:
            day = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")
    else:
        day = date.today()

    priority_order = {"A": 0, "B": 1, "C": 2}
    goals = (
        db.query(DailyGoal)
        .filter(
            DailyGoal.date == day,
            DailyGoal.status.in_(("planned", "active")),
        )
        .all()
    )
    goals.sort(key=lambda g: (priority_order.get(g.priority, 1), g.created_at))

    result = [
        {
            "id": g.id,
            "title": g.title,
            "priority": g.priority,
            "status": g.status,
            "estimated_minutes": g.estimated_minutes,
            "category": g.category,
        }
        for g in goals
    ]
    return {"date": day.isoformat(), "goals": result}


@public_router.get("/sessions")
def get_sessions_for_date(
    token: str = Query(...),
    date_str: str = Query(None, alias="date"),
    db: Session = Depends(get_db),
):
    """Heutige geplante Habit-Sessions (feed_token-Auth, fürs Briefing/Digest).

    Liefert die Lern-/Gewohnheits-Blöcke des Tages (Sprache/Mathematik/… ), damit
    das Saganta-Briefing „Dein Tag" die geplante Routine ansagen kann. Nur
    offene Sessions (pending/accepted): bereits erledigte sind nicht mehr Tagesplan.
    """
    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")

    if date_str:
        try:
            day = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")
    else:
        day = date.today()

    day_start, day_end = tagesgrenzen(day)

    rows = (
        db.query(HabitSession, Habit.name, Habit.category)
        .join(Habit, HabitSession.habit_id == Habit.id)
        .filter(
            HabitSession.start < day_end,
            HabitSession.end > day_start,
            HabitSession.status.in_(("pending", "accepted")),
        )
        .order_by(HabitSession.start)
        .all()
    )

    result = [
        {
            "title": name,
            "category": category,
            "start": session.start.isoformat(),
            "end": session.end.isoformat(),
        }
        for session, name, category in rows
    ]

    return {"date": day.isoformat(), "sessions": result}


@public_router.get("/dashboard")
def get_dashboard_public(
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    """Return dashboard data (today, events, birthdays, feiertag) for external systems."""
    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")
    from backend.dashboard import build_dashboard_data
    return build_dashboard_data(db)


@public_router.post("/set-external")
def set_day_type_public(
    token: str = Query(...),
    date_str: str = Query(..., alias="date"),
    dtype: str = Query(..., alias="type"),
    db: Session = Depends(get_db),
):
    """Set day type via feed token (for external systems like tardis)."""
    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")
    _typ_pruefen(dtype)
    day = _tag_parsen(date_str)
    return {"date": day.isoformat(), "type": _tagestyp_setzen(db, day, dtype)}


# --- Protected router (JWT auth, for Frontend) ---

protected_router = APIRouter(prefix="/api/day-type", tags=["day-type"])


@protected_router.get("/week")
def get_week_day_types_protected(
    week_start: str = Query(...),
    db: Session = Depends(get_db),
):
    """Return day types for Mon-Sun of a given week (JWT auth for frontend)."""
    try:
        start = date.fromisoformat(week_start)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")
    typen = _daytypes_display_bereich(db, start, 7)
    result = []
    for i in range(7):
        d = start + timedelta(days=i)
        result.append({
            "date": d.isoformat(),
            "type": typen[d],
        })
    return result


@protected_router.get("/bereich")
def get_day_types_bereich(
    start: date = Query(...),
    tage: int = Query(..., ge=1, le=400),
    db: Session = Depends(get_db),
):
    """Display-Tagestyp je Tag fuer einen laengeren Bereich (Jahresansicht der App).

    Dieselbe Aufloesung wie /week, nur ohne die 7-Tage-Grenze: die App soll den
    Tagestyp nicht selbst aus Terminen ableiten (keine zweite Daytype-Logik).
    Ein Jahr kostet eine Abfrage, gemessen 0,4 s.
    """
    typen = _daytypes_display_bereich(db, start, tage)
    return [{"date": d.isoformat(), "type": t} for d, t in sorted(typen.items())]


@protected_router.get("/week-schedule")
def get_week_schedule(
    week_start: str = Query(...),
    db: Session = Depends(get_db),
):
    """Return day types with sleep schedule and work/school blocks for a week."""
    from backend.sleep_schedule import get_buffer_start, get_schedule

    try:
        start = date.fromisoformat(week_start)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")

    typen = _daytypes_display_bereich(db, start, 7)
    result = []
    for i in range(7):
        d = start + timedelta(days=i)
        dtype = typen[d]
        sched = get_schedule(dtype)
        buf = get_buffer_start(dtype)

        wake_h, wake_m = sched["wake"]
        bed_h, bed_m = sched["bed"]

        # Build work/school block if applicable
        # Aus dem Ereignis, nicht aus der Konstante: seit 2026-10-30 kann jeder
        # Tag seine eigene Arbeitszeit tragen.
        block = None
        b_start, b_ende = _tageszeiten(db, d, dtype)
        if b_start:
            block = {"start": b_start, "end": b_ende}

        result.append({
            "date": d.isoformat(),
            "type": dtype,
            "wake": f"{wake_h:02d}:{wake_m:02d}",
            "bed": f"{bed_h:02d}:{bed_m:02d}",
            "buffer_start": f"{buf[0]:02d}:{buf[1]:02d}",
            "block": block,
        })
    return result


def _zeiten_fuer_typ(dtype: str, von: str | None, bis: str | None) -> tuple | None:
    """``von``/``bis`` gelten nur fuer Arbeit und Schule; sonst klar ablehnen."""
    zeiten = zeiten_parsen(von, bis)
    if zeiten is not None and dtype not in ZEIT_TYPEN:
        raise HTTPException(400, f"Uhrzeiten gibt es nur fuer {' und '.join(ZEIT_TYPEN)}, nicht fuer {dtype}")
    return zeiten


@protected_router.post("/set")
def set_day_type(
    date_str: str = Query(..., alias="date"),
    dtype: str = Query(..., alias="type"),
    half: bool = Query(False),
    von: Annotated[str | None, Query()] = None,
    bis: Annotated[str | None, Query()] = None,
    db: Session = Depends(get_db),
):
    """Tagestyp eines Tages. Ohne ``von``/``bis`` schaltet derselbe Typ den Tag
    frei; mit ihnen wird Arbeit/Schule zu genau diesen Uhrzeiten gesetzt."""
    _typ_pruefen(dtype)
    zeiten = _zeiten_fuer_typ(dtype, von, bis)
    day = _tag_parsen(date_str)
    typ = _tagestyp_setzen(db, day, dtype, half, zeiten)
    start, ende = _tageszeiten(db, day, typ)
    return {"date": day.isoformat(), "type": typ, "start": start, "end": ende}


@protected_router.post("/set-range")
def set_day_type_range(
    von: str = Query(...),
    bis: str = Query(...),
    dtype: str = Query(..., alias="type"),
    half: bool = Query(False),
    nur_wochentage: bool = Query(True),
    von_zeit: Annotated[str | None, Query()] = None,
    bis_zeit: Annotated[str | None, Query()] = None,
    db: Session = Depends(get_db),
):
    """Setzt einen Tagestyp fuer einen ganzen Zeitraum.

    ``von_zeit``/``bis_zeit`` (HH:MM) legen Arbeit/Schule im ganzen Zeitraum zu
    diesen Uhrzeiten an; ohne sie gilt die Wochenvorgabe je Tag. (``von``/``bis``
    sind hier schon die Daten, daher die laengeren Namen.)

    Warum das kein Schleifenaufruf von `/set` ist, und zwar aus drei
    unabhaengigen Gruenden:

    1. **`/set` schaltet um.** Derselbe Typ noch einmal macht den Tag `frei`.
       Ueber einen Zeitraum gelegt ist das sinnlos: ein Urlaub vom 5. bis 19.,
       bei dem der 8. schon als Urlaub eingetragen war, hinterliesse genau an
       diesem Tag ein Loch. Hier wird **gesetzt**, nicht umgeschaltet; zum
       Leeren gibt es `type=frei`.
    2. **`/set` plant die Woche nach jedem Tag neu.** Drei Wochen Urlaub waeren
       21 `reschedule_week`-Laeufe fuer dieselben drei Wochen. Hier laeuft es
       einmal je betroffener Woche.
    3. **Ein Telefon unterwegs hat eine Verbindung, kein Dutzend.** 21 einzelne
       POST ueber ein schwankendes Mobilnetz heisst 21 Gelegenheiten,
       mittendrin abzubrechen, und dann ist der halbe Urlaub eingetragen.

    Was uebersprungen wird, kommt im Ergebnis zurueck statt still zu
    verschwinden: Feiertage (man arbeitet dort nicht und verbraucht auch keinen
    Urlaubstag) und auf Wunsch die Wochenenden.
    """
    if dtype != "frei":
        _typ_pruefen(dtype)
    zeiten = _zeiten_fuer_typ(dtype, von_zeit, bis_zeit)
    beginn = _tag_parsen(von)
    ende = _tag_parsen(bis)
    if ende < beginn:
        raise HTTPException(status_code=400, detail="bis liegt vor von")
    if (ende - beginn).days > 366:
        raise HTTPException(status_code=400, detail="Zeitraum darf 366 Tage nicht ueberschreiten")

    gesetzt: list[str] = []
    uebersprungen: list[dict] = []
    wochen: set[tuple[int, int]] = set()

    tag = beginn
    while tag <= ende:
        grund = _bereich_auslassgrund(db, tag, dtype, nur_wochentage)
        if grund:
            uebersprungen.append({"date": tag.isoformat(), "grund": grund})
        else:
            _tagestyp_schreiben(db, tag, dtype, half, zeiten)
            gesetzt.append(tag.isoformat())
            wochen.add(tag.isocalendar()[:2])
        tag += timedelta(days=1)

    db.commit()

    # Erst nach dem Schreiben und einmal je Woche. reschedule_week liest den
    # Tagestyp, deshalb muss das Commit davor liegen.
    from backend.scheduler import reschedule_week
    for jahr, woche in sorted(wochen):
        reschedule_week(db, date.fromisocalendar(jahr, woche, 1))

    return {
        "von": beginn.isoformat(),
        "bis": ende.isoformat(),
        "type": dtype,
        "gesetzt": gesetzt,
        "uebersprungen": uebersprungen,
        "wochen_neu_geplant": len(wochen),
    }


def _bereich_auslassgrund(db: Session, tag: date, dtype: str, nur_wochentage: bool) -> str | None:
    """Warum ein Tag im Zeitraum nicht gesetzt wird, oder None.

    Feiertage bleiben in jedem Fall unberuehrt: ein Feiertag gewinnt ohnehin
    gegen jeden gesetzten Typ (`_get_daytype`), ein Urlaubstag darauf waere also
    unsichtbar und zugleich im Urlaubskonto verbucht.
    """
    if nur_wochentage and tag.weekday() >= 5:
        return "wochenende"
    if dtype != "frei":
        beginn, schluss = tagesgrenzen(tag)
        feiertag = (
            db.query(Event)
            .filter(
                Event.calendar_id == FEIERTAG_CALENDAR_ID,
                Event.start < schluss,
                Event.end > beginn,
            )
            .first()
        )
        if feiertag:
            return "feiertag"
    return None


def _tagestyp_schreiben(
    db: Session, tag: date, dtype: str, half: bool, zeiten: tuple | None = None,
) -> None:
    """Setzt den Tagestyp eines Tages ohne Umschalten und ohne Commit.

    Der Kern von [_tagestyp_setzen] ohne dessen zwei Eigenschaften, die im
    Zeitraum falsch waeren: das Umschalten und das Neuplanen je Tag.
    """
    mandant = _schreib_mandant(db)
    for _, ereignis in _daytype_ereignisse(db, tag):
        db.delete(ereignis)
    if dtype == "frei":
        return
    titles = {"arbeit": "Arbeit", "schule": "Schule", "urlaub": "Urlaub",
              "krank": "Krankenschein"}
    title = titles.get(dtype, dtype.capitalize())
    if half and dtype == "urlaub":
        title = "Urlaub (1/2)"
    if zeiten is None and dtype in ZEIT_TYPEN:
        zeiten = arbeitszeit_vorgabe(db, mandant, dtype, tag.weekday())
    evt_data = _make_daytype_event(dtype, tag, zeiten)
    db.add(Event(
        owner_sub=mandant,
        calendar_id=DAYTYPE_CALENDARS[dtype],
        title=title,
        start=evt_data["start"],
        end=evt_data["end"],
        all_day=evt_data["all_day"],
    ))


@protected_router.get("/urlaub-bilanz")
def get_urlaub_bilanz(
    jahr: int = Query(...),
    db: Session = Depends(get_db),
):
    """Zaehlt die Urlaubstage eines Jahres.

    Halbe Tage zaehlen halb: `_tagestyp_setzen` schreibt sie als Titel
    „Urlaub (1/2)", und das ist die einzige Stelle, an der der Unterschied
    ueberhaupt festgehalten ist.

    ⚠️ Hier steht **kein Jahresanspruch**. Wie viele Tage jemandem zustehen,
    steht in seinem Arbeitsvertrag und nicht in diesem Dienst; die Zahl kommt
    aus der App und wird dort gepflegt. Ein hier eingebauter Vorgabewert waere
    genau die Sorte Zahl, die stimmt, bis sie es nicht mehr tut, und dann
    niemandem auffaellt.
    """
    if jahr < 1970 or jahr > 2200:
        raise HTTPException(status_code=400, detail="Jahr ausserhalb des Bereichs")
    beginn = date(jahr, 1, 1)
    ende = date(jahr, 12, 31)
    anfang, _ = tagesgrenzen(beginn)
    _, schluss = tagesgrenzen(ende)
    ereignisse = (
        db.query(Event)
        .filter(
            Event.calendar_id == DAYTYPE_CALENDARS["urlaub"],
            Event.start < schluss,
            Event.end > anfang,
        )
        .all()
    )
    ganze = [e for e in ereignisse if "(1/2)" not in (e.title or "")]
    halbe = [e for e in ereignisse if "(1/2)" in (e.title or "")]
    return {
        "jahr": jahr,
        "ganze_tage": len(ganze),
        "halbe_tage": len(halbe),
        "verbraucht": len(ganze) + len(halbe) / 2,
        "tage": sorted(e.start.date().isoformat() for e in ereignisse),
    }


# --- Arbeitszeiten: Wochenvorgabe je Mandant (seit 2026-10-30) ---


@protected_router.get("/arbeitszeiten")
def get_arbeitszeiten(db: Session = Depends(get_db)):
    """Mit welchen Uhrzeiten ein neuer Arbeits- oder Schultag angelegt wird, je
    Wochentag (0 = Montag). ``eigen`` sagt, ob der Wert eingestellt oder die
    eingebaute Vorgabe ist."""
    from backend.arbeitszeiten import wochenplan
    return wochenplan(db, _schreib_mandant(db))


@protected_router.put("/arbeitszeiten")
def put_arbeitszeiten(eingabe: dict = Body(...), db: Session = Depends(get_db)):
    """Uebernimmt ``{"arbeit": [{"wochentag": 0, "von": "08:00", "bis": "17:00"}, ...]}``.

    Wirkt nur auf Tage, die DANACH angelegt werden. Schon eingetragene
    Arbeitstage behalten ihre Uhrzeit: die liegt im Ereignis, und daraus liest
    Home Assistant. Eine Vorgabe, die rueckwirkend Wecker verschiebt, waere
    die ueberraschendere Antwort.
    """
    from backend.arbeitszeiten import wochenplan_setzen
    return wochenplan_setzen(db, _schreib_mandant(db), eingabe)
