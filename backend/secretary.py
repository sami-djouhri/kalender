"""Automatischer Sekretär, der Kalender plant sich selbst.

Dieses Modul macht aus dem Kalender einen proaktiven Assistenten:

- **Tagesplanung** (`run_daily_planning`): plant morgens verbindlich Pool-Todos in
  freie Slots (via scheduler.auto_plan_todos, commit=True). Verbindlich, aber
  reversibel, es werden nur `scheduling_mode == "pool"`-Todos angefasst; der User
  kann jedes geplante Todo mit einem Klick zurück in den Pool schieben.
- **Konflikt-Erkennung** (`detect_day_conflicts`): read-only. Findet Doppelbuchungen,
  überfällige Aufgaben und Überlast (mehr fällig als in den Tag passt).
- **Briefing / Abend-Nudge** (`build_briefing`, `build_evening_review`): kompakte
  Tagesübersicht, die per ntfy an den Owner gepusht und im Dashboard gezeigt wird.
- **Tick** (`run_secretary_tick`): der Orchestrator, den der 60s-Runner aufruft.
  Iteriert über alle Tenants mit Daten, plant pro Tenant **gescopt** (kein
  Cross-Tenant-Mischen) und pusht das Briefing **nur an den Owner** (kein
  Cross-Tenant-Leak an ein gemeinsames ntfy-Topic).

Regelbasiert und deterministisch, kein LLM, keine externen Calls außer dem
optionalen ntfy-Push. Zeiten/Schalter liegen in der `settings`-Tabelle (global,
faktischer Single-User-Betrieb), Defaults siehe unten.
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import settings as app_settings
from backend import mandant_einstellungen
from backend.database import SessionLocal
from backend.models import (
    Contact,
    DailyGoal,
    DailyReview,
    Event,
    Habit,
    SecretaryRun,
    Setting,
    Todo,
)
from backend.routers.daytype import DAYTYPE_IDS, _get_daytype_display
from backend.scheduler import DEFAULT_TODO_MINUTES, auto_plan_todos, compute_free_slots

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")

# --- Konfigurations-Defaults (überschreibbar via settings-Tabelle) ---
DEFAULT_MORNING_HOUR = 6      # ab wann morgens geplant + gebrieft wird
DEFAULT_EVENING_HOUR = 20     # ab wann der Abend-Nudge feuert
CONFIG_KEYS = {
    "secretary_enabled": "1",
    "secretary_autoplan": "1",
    "secretary_morning_hour": str(DEFAULT_MORNING_HOUR),
    "secretary_evening_hour": str(DEFAULT_EVENING_HOUR),
}

_WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _as_berlin(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BERLIN)
    return value.astimezone(BERLIN)


def _clock(value: datetime) -> str:
    return _as_berlin(value).strftime("%H:%M")


# --- Konfiguration (je Mandant, siehe backend/mandant_einstellungen.py) ---

def _mandant(db: Session) -> str:
    """Mandant dieser Session; ohne Angabe der Owner (headerlose CORE-Pfade)."""
    return db.info.get("owner_sub") or app_settings.DEFAULT_OWNER_SUB


def get_config(db: Session) -> dict:
    """Sekretär-Konfiguration DIESES Mandanten (mit Defaults).

    Die vier Werte lagen bis 2026-09-27 global in ``settings``: zwei Mandanten
    teilten sich also einen Schalter, eine Morgenstunde und eine Abendstunde, und
    wer sie umstellte, stellte sie für alle um. Sie liegen jetzt je Mandant.

    ★ Der Rückfall auf die alten globalen Zeilen gilt **nur für den Owner**. Sonst
    erbte jeder neue Mandant dessen Einstellungen, und das sähe wie eine bewusste
    Vorgabe aus, obwohl es ein Altbestand ist. (Gemessen am 2026-09-27: in dieser
    Installation liegt keiner der vier Werte überhaupt vor, der Sekretär lief immer
    auf Vorgaben. Das Umstellen kostet hier deshalb keine Migration.)
    """
    sub = _mandant(db)
    rows = {}
    for key in CONFIG_KEYS:
        wert = mandant_einstellungen.wert_lesen(db, sub, key)
        if wert is not None:
            rows[key] = wert
    if sub == app_settings.DEFAULT_OWNER_SUB:
        for s in db.query(Setting).filter(Setting.key.in_(CONFIG_KEYS.keys())).all():
            rows.setdefault(s.key, s.value)
    def _bool(key: str) -> bool:
        return rows.get(key, CONFIG_KEYS[key]) not in ("0", "false", "False", "")
    def _hour(key: str) -> int:
        try:
            return max(0, min(23, int(rows.get(key, CONFIG_KEYS[key]))))
        except (TypeError, ValueError):
            return int(CONFIG_KEYS[key])
    return {
        "enabled": _bool("secretary_enabled"),
        "autoplan": _bool("secretary_autoplan"),
        "morning_hour": _hour("secretary_morning_hour"),
        "evening_hour": _hour("secretary_evening_hour"),
    }


def set_config(db: Session, **kwargs) -> dict:
    """Einzelne Konfigwerte setzen (bool → '1'/'0', hour → str)."""
    mapping = {
        "enabled": "secretary_enabled",
        "autoplan": "secretary_autoplan",
        "morning_hour": "secretary_morning_hour",
        "evening_hour": "secretary_evening_hour",
    }
    for field, key in mapping.items():
        if field not in kwargs or kwargs[field] is None:
            continue
        val = kwargs[field]
        if field in ("enabled", "autoplan"):
            str_val = "1" if val else "0"
        else:
            str_val = str(max(0, min(23, int(val))))
        mandant_einstellungen.wert_setzen(db, _mandant(db), key, str_val)
    return get_config(db)


# --- Konflikt-Erkennung (read-only) ---

def _timed_events(db: Session, day: date) -> list[Event]:
    day_start = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
    day_end = day_start + timedelta(days=1)
    return (
        db.query(Event)
        .filter(
            Event.calendar_id.notin_(DAYTYPE_IDS),
            Event.all_day == False,  # noqa: E712
            Event.start < day_end,
            Event.end > day_start,
        )
        .order_by(Event.start)
        .all()
    )


def detect_day_conflicts(db: Session, day: date) -> list[dict]:
    """Findet Konflikte eines Tages (read-only, keine Änderungen).

    Arten: 'doppelbuchung' (überlappende Termine), 'ueberfaellig' (offene Todos
    mit vergangenem Fälligkeitsdatum), 'ueberlast' (heute fällige Aufgaben passen
    nicht mehr in die freien Slots des Tages).
    """
    conflicts: list[dict] = []

    # 1. Doppelbuchungen: überlappende getaktete Termine
    events = _timed_events(db, day)
    for i in range(len(events)):
        a = events[i]
        a_end = _as_berlin(a.end)
        for j in range(i + 1, len(events)):
            b = events[j]
            b_start = _as_berlin(b.start)
            if b_start < a_end:
                conflicts.append({
                    "type": "doppelbuchung",
                    "severity": "hoch",
                    "message": (
                        f"'{a.title}' ({_clock(a.start)}-{_clock(a.end)}) und "
                        f"'{b.title}' ({_clock(b.start)}-{_clock(b.end)}) überschneiden sich."
                    ),
                    "event_ids": [a.id, b.id],
                })
            else:
                break  # nach Start sortiert, kein späterer überlappt mehr mit a

    # 2. Überfällige Aufgaben
    overdue = (
        db.query(Todo)
        .filter(
            Todo.completed == False,  # noqa: E712
            Todo.status.notin_(["done", "cancelled"]),
            Todo.due_date.isnot(None),
            Todo.due_date < day,
        )
        .all()
    )
    if overdue:
        titles = ", ".join(t.title for t in overdue[:3])
        more = f" (+{len(overdue) - 3} weitere)" if len(overdue) > 3 else ""
        conflicts.append({
            "type": "ueberfaellig",
            "severity": "hoch" if len(overdue) >= 3 else "mittel",
            "message": f"{len(overdue)} überfällige Aufgabe(n): {titles}{more}.",
            "todo_ids": [t.id for t in overdue],
        })

    # 3. Überlast: heute fällige, noch offene Aufgaben passen nicht in die freien Slots
    due_today = (
        db.query(Todo)
        .filter(
            Todo.completed == False,  # noqa: E712
            Todo.status.notin_(["done", "cancelled"]),
            Todo.due_date == day,
        )
        .all()
    )
    needed = sum((t.estimated_minutes or DEFAULT_TODO_MINUTES) for t in due_today)
    if needed > 0:
        free = compute_free_slots(db, day)
        available = sum(int((e - s).total_seconds() / 60) for s, e in free)
        if needed > available:
            conflicts.append({
                "type": "ueberlast",
                "severity": "mittel",
                "message": (
                    f"Heute fällig: ~{needed} Min Aufgaben, aber nur {available} Min frei. "
                    "Etwas verschieben oder Puffer schaffen."
                ),
                "needed_minutes": needed,
                "available_minutes": available,
            })

    return conflicts


# --- Briefing / Abend-Review ---

def _todo_min(t: Todo) -> int:
    return t.estimated_minutes or DEFAULT_TODO_MINUTES


def build_briefing(db: Session, day: date) -> dict:
    """Kompaktes Morgen-Briefing für einen Tag (tenant-gescopt über die Session)."""
    day_start = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
    day_end = day_start + timedelta(days=1)
    day_type = _get_daytype_display(db, day)

    events = (
        db.query(Event)
        .filter(
            Event.calendar_id.notin_(DAYTYPE_IDS),
            Event.start < day_end,
            Event.end > day_start,
        )
        .order_by(Event.all_day.desc(), Event.start)
        .all()
    )
    termine = [
        {
            "id": e.id,
            "title": e.title,
            "all_day": e.all_day,
            "start": e.start.isoformat(),
            "end": e.end.isoformat(),
            "time": None if e.all_day else f"{_clock(e.start)}-{_clock(e.end)}",
            "location": e.location,
        }
        for e in events
    ]
    first_timed = next((t for t in termine if not t["all_day"]), None)

    goal_rows = (
        db.query(DailyGoal)
        .filter(DailyGoal.date == day, DailyGoal.status.in_(["planned", "active"]))
        .all()
    )
    goal_rows.sort(key=lambda g: {"A": 0, "B": 1, "C": 2}.get(g.priority, 1))
    goals = [{"id": g.id, "title": g.title, "priority": g.priority} for g in goal_rows]
    focus_goal = next((g for g in goals if g["priority"] == "A"), None)

    planned = (
        db.query(Todo)
        .filter(
            Todo.completed == False,  # noqa: E712
            Todo.status.notin_(["done", "cancelled"]),
            Todo.scheduled_start.isnot(None),
            Todo.scheduled_start < day_end,
            Todo.scheduled_start >= day_start,
        )
        .order_by(Todo.scheduled_start)
        .all()
    )
    planned_todos = [
        {
            "id": t.id,
            "title": t.title,
            "time": _clock(t.scheduled_start) if t.scheduled_start else None,
            "minutes": _todo_min(t),
        }
        for t in planned
    ]

    conflicts = detect_day_conflicts(db, day)
    birthdays = _todays_birthdays(db, day)

    return {
        "date": day.isoformat(),
        "weekday": _WEEKDAYS[day.weekday()],
        "day_type": day_type,
        "termine": termine,
        "termine_count": len(termine),
        "next_event": first_timed,
        "goals": goals,
        "focus_goal": focus_goal,
        "planned_todos": planned_todos,
        "conflicts": conflicts,
        "birthdays": birthdays,
    }


def _todays_birthdays(db: Session, day: date) -> list[dict]:
    out = []
    for c in db.query(Contact).filter(Contact.birthday.isnot(None)).all():
        if c.birthday.month == day.month and c.birthday.day == day.day:
            out.append({"name": c.name, "age": day.year - c.birthday.year})
    return out


def briefing_to_text(briefing: dict) -> str:
    """Verdichtet ein Briefing zu einem kurzen ntfy-Text (nur Owner-seitig)."""
    lines: list[str] = []
    n = briefing["termine_count"]
    if briefing["focus_goal"]:
        lines.append(f"Fokus: {briefing['focus_goal']['title']}")
    if briefing["next_event"]:
        ev = briefing["next_event"]
        lines.append(f"Nächster Termin: {ev['title']} {ev['time']}")
    elif n == 0:
        lines.append("Keine Termine heute.")
    if n:
        lines.append(f"{n} Termin(e) heute.")
    if briefing["planned_todos"]:
        lines.append(f"{len(briefing['planned_todos'])} Aufgabe(n) eingeplant.")
    for c in briefing["conflicts"]:
        lines.append(f"⚠ {c['message']}")
    for b in briefing["birthdays"]:
        lines.append(f"🎂 {b['name']} wird heute {b['age']}.")
    return "\n".join(lines) if lines else "Nichts Besonderes heute."


def build_evening_review(db: Session, day: date) -> dict:
    """Abend-Nudge: offene Fokus-Ziele + Review-Erinnerung."""
    open_goals = (
        db.query(DailyGoal)
        .filter(DailyGoal.date == day, DailyGoal.status.in_(["planned", "active"]))
        .all()
    )
    has_review = (
        db.query(DailyReview).filter(DailyReview.date == day).first() is not None
    )
    incomplete = [g.title for g in open_goals]
    return {
        "date": day.isoformat(),
        "open_goals": incomplete,
        "has_review": has_review,
    }


def evening_to_text(review: dict) -> str:
    lines = []
    if review["open_goals"]:
        lines.append("Noch offen: " + ", ".join(review["open_goals"][:3]))
    if not review["has_review"]:
        lines.append("Kurzes Tagesreview? Wie lief der Tag?")
    return "\n".join(lines) if lines else "Guter Tag. Bis morgen."


# --- Tagesplanung (verbindlich, reversibel) ---

def run_daily_planning(db: Session, day: date) -> dict:
    """Plant Pool-Todos verbindlich in freie Slots (auto_plan_todos commit=True).

    Reversibel: nur Todos mit scheduling_mode == 'pool' werden angefasst. Gibt eine
    Zusammenfassung zurück (Anzahl geplanter Todos).
    """
    result = auto_plan_todos(db, day, commit=True)
    return {
        "planned_count": len(result.get("suggestions", [])),
        "free_slots": len(result.get("free_slots", [])),
    }


# --- Idempotenz-Marker ---

def _already_ran(db: Session, owner_sub: str, day: date, kind: str) -> bool:
    return (
        db.query(SecretaryRun)
        .filter(
            SecretaryRun.owner_sub == owner_sub,
            SecretaryRun.run_date == day,
            SecretaryRun.kind == kind,
        )
        .first()
        is not None
    )


def _mark_ran(db: Session, owner_sub: str, day: date, kind: str, summary: str | None) -> bool:
    db.add(SecretaryRun(owner_sub=owner_sub, run_date=day, kind=kind, summary=summary))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


# --- Tenant-Ermittlung ---

def tenants_with_data(db: Session) -> list[str]:
    """Distinct owner_sub über planungsrelevante Tabellen (ungescopte System-Session)."""
    subs: set[str] = set()
    for model in (Todo, DailyGoal, Habit):
        for (sub,) in db.query(func.distinct(model.owner_sub)).all():
            if sub:
                subs.add(sub)
    return sorted(subs)


def _scoped_session(owner_sub: str) -> Session:
    db = SessionLocal()
    db.info["owner_sub"] = owner_sub
    return db


def _system_session() -> Session:
    db = SessionLocal()
    db.info["owner_sub"] = None
    return db


# --- Orchestrator-Tick ---

def run_secretary_tick(now: datetime | None = None, sender=None) -> dict:
    """Ein Sekretär-Tick (vom 60s-Runner aufgerufen).

    Morgens (>= morning_hour): pro Tenant verbindlich planen + Briefing erzeugen,
    Push nur an den Owner. Abends (>= evening_hour): Abend-Nudge an den Owner.
    Jede Aktion feuert pro Tag genau einmal (SecretaryRun-Marker).

    `sender` ist optional (ntfy). Fehlt er, wird nur geplant/markiert, nicht gepusht.
    """
    now = _as_berlin(now or datetime.now(BERLIN))
    today = now.date()
    owner = app_settings.DEFAULT_OWNER_SUB

    sys_db = _system_session()
    try:
        cfg = get_config(sys_db)
        if not cfg["enabled"]:
            return {"skipped": "disabled"}
        tenants = tenants_with_data(sys_db)
    finally:
        sys_db.close()

    result = {"planned_tenants": 0, "briefing_sent": False, "evening_sent": False}
    is_morning = now.hour >= cfg["morning_hour"]
    is_evening = now.hour >= cfg["evening_hour"]

    # --- Morgens: Retention-Purge (systemweit, einmal täglich) ---
    if is_morning:
        db = _system_session()
        try:
            if not _already_ran(db, "__system__", today, "purge"):
                from backend.account import purge_old_data

                purged = purge_old_data(db)
                total = sum(purged.values())
                _mark_ran(db, "__system__", today, "purge", f"purged={total}")
        except Exception:
            logger.exception("Retention-Purge im Sekretär-Tick fehlgeschlagen")
            db.rollback()
        finally:
            db.close()

    # --- Morgens: planen (alle Tenants) + Briefing (nur Owner) ---
    if is_morning:
        for sub in tenants:
            db = _scoped_session(sub)
            try:
                if _already_ran(db, sub, today, "planning"):
                    continue
                summary = None
                if cfg["autoplan"]:
                    plan = run_daily_planning(db, today)
                    summary = f"planned={plan['planned_count']}"
                if _mark_ran(db, sub, today, "planning", summary):
                    result["planned_tenants"] += 1
            except Exception:
                logger.exception("Sekretär-Planung für Tenant %s fehlgeschlagen", sub)
                db.rollback()
            finally:
                db.close()

        # Briefing-Push nur an den Owner (eigenes ntfy-Topic, kein Cross-Tenant-Leak)
        db = _scoped_session(owner)
        try:
            if not _already_ran(db, owner, today, "briefing"):
                briefing = build_briefing(db, today)
                if sender is not None:
                    payload = {
                        "title": f"Guten Morgen: {briefing['weekday']}",
                        "message": briefing_to_text(briefing),
                        "priority": "default",
                        "tags": "calendar,sunrise",
                    }
                    try:
                        if sender(payload):
                            result["briefing_sent"] = True
                    except Exception:
                        logger.exception("Briefing-Push fehlgeschlagen")
                _mark_ran(db, owner, today, "briefing", None)
        except Exception:
            logger.exception("Briefing-Erzeugung fehlgeschlagen")
            db.rollback()
        finally:
            db.close()

    # --- Abends: Nudge nur an den Owner ---
    if is_evening:
        db = _scoped_session(owner)
        try:
            if not _already_ran(db, owner, today, "evening"):
                review = build_evening_review(db, today)
                if sender is not None:
                    payload = {
                        "title": "Tagesabschluss",
                        "message": evening_to_text(review),
                        "priority": "low",
                        "tags": "calendar,crescent_moon",
                    }
                    try:
                        if sender(payload):
                            result["evening_sent"] = True
                    except Exception:
                        logger.exception("Abend-Nudge-Push fehlgeschlagen")
                _mark_ran(db, owner, today, "evening", None)
        except Exception:
            logger.exception("Abend-Nudge fehlgeschlagen")
            db.rollback()
        finally:
            db.close()

    return result
