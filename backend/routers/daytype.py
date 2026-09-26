from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
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
from backend.wanduhr import tagesgrenzen


def _verify_feed_token(token: str, db: Session) -> bool:
    setting = db.query(Setting).filter(Setting.key == "feed_token").first()
    if not setting:
        return False
    return token == setting.value


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


def _tagestyp_setzen(db: Session, day: date, dtype: str, half: bool = False) -> str:
    """Setzt den Tagestyp eines Tages und stellt sicher, dass er eindeutig bleibt.

    Umschalt-Verhalten: derselbe Typ noch einmal → der Tag wird 'frei'.

    Gemeinsamer Kern von `POST /api/day-type/set` (JWT, Oberflaeche) und
    `POST /api/day-type/set-external` (Feed-Token, dev-portal). Beide Endpunkte
    trugen bis 2026-08-24 je eine eigene Kopie dieser rund fuenfzig Zeilen, mit
    bereits auseinandergelaufenem Verhalten: nur die JWT-Variante kannte halbe
    Urlaubstage. Zwei Wahrheiten fuer den Wert, an dem der Wecker haengt.
    """
    vorhanden = _daytype_ereignisse(db, day)
    aktueller_typ = vorhanden[0][0] if vorhanden else None
    aktuell_halb = bool(vorhanden) and "(1/2)" in (vorhanden[0][1].title or "")

    # Immer ALLE abraeumen, nicht nur das erste. Das repariert nebenbei Tage, die
    # aus frueheren Doppeleintraegen mehrdeutig geworden sind.
    for _, ereignis in vorhanden:
        db.delete(ereignis)

    umschalten = aktueller_typ == dtype and aktuell_halb == half
    if not umschalten:
        titles = {"arbeit": "Arbeit", "schule": "Schule", "urlaub": "Urlaub",
                  "krank": "Krankenschein"}
        title = titles.get(dtype, dtype.capitalize())
        if half and dtype == "urlaub":
            title = "Urlaub (1/2)"
        evt_data = _make_daytype_event(dtype, day)
        db.add(Event(
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


def _make_daytype_event(dtype: str, day: date) -> dict:
    """Return start, end, all_day for a daytype event on a given date."""
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
    return {"date": day.isoformat(), "type": _get_daytype(db, day)}


@public_router.get("/today")
def get_day_type_today(
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")
    today = date.today()
    return {"date": today.isoformat(), "type": _get_daytype(db, today)}


@public_router.get("/tomorrow")
def get_day_type_tomorrow(
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")
    tomorrow = date.today() + timedelta(days=1)
    return {"date": tomorrow.isoformat(), "type": _get_daytype(db, tomorrow)}


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
    """Set day type via feed token (for external systems like dev-portal)."""
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
        block = None
        if dtype == "arbeit":
            t = ARBEIT_TIMES["friday"] if d.weekday() == 4 else ARBEIT_TIMES["default"]
            block = {
                "start": f"{t[0]:02d}:{t[1]:02d}",
                "end": f"{t[2]:02d}:{t[3]:02d}",
            }
        elif dtype == "schule":
            t = SCHULE_TIMES
            block = {
                "start": f"{t[0]:02d}:{t[1]:02d}",
                "end": f"{t[2]:02d}:{t[3]:02d}",
            }

        result.append({
            "date": d.isoformat(),
            "type": dtype,
            "wake": f"{wake_h:02d}:{wake_m:02d}",
            "bed": f"{bed_h:02d}:{bed_m:02d}",
            "buffer_start": f"{buf[0]:02d}:{buf[1]:02d}",
            "block": block,
        })
    return result


@protected_router.post("/set")
def set_day_type(
    date_str: str = Query(..., alias="date"),
    dtype: str = Query(..., alias="type"),
    half: bool = Query(False),
    db: Session = Depends(get_db),
):
    _typ_pruefen(dtype)
    day = _tag_parsen(date_str)
    return {"date": day.isoformat(), "type": _tagestyp_setzen(db, day, dtype, half)}
