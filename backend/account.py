"""DSGVO-Kernfunktionen: Datenexport (Art. 20), Konto-Löschung (Art. 17), Retention.

Alle nutzerbezogenen Daten hängen an ``owner_sub``. Export/Löschung filtern IMMER
explizit ``owner_sub == sub``, nicht nur über das ORM-Auto-Scoping, weil dessen
SHARED-Regel (Calendar/Event) ``owner_sub == sub OR owner_sub IS NULL`` lautet und
ein blindes Bulk-Delete sonst die System-Kalender/Feiertage (NULL) mitlöschen würde.

Retention (``purge_old_data``) läuft im System-Kontext über alle Tenants und entfernt
nur echte Wegwerf-Daten (erledigte Sessions, alte Logs): Termine/Kontakte bleiben.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models import (
    ActivityFeedback,
    Calendar,
    Contact,
    DailyCheckIn,
    DailyGoal,
    DailyLoad,
    DailyReview,
    Event,
    EventReminderLog,
    Habit,
    HabitSession,
    Project,
    SecretaryRun,
    SessionNotification,
    Todo,
    TodoCompletion,
)

logger = logging.getLogger(__name__)


def _current_sub(db: Session) -> str:
    sub = db.info.get("owner_sub")
    return sub or settings.DEFAULT_OWNER_SUB


def _serialize_row(obj) -> dict:
    out = {}
    for col in obj.__table__.columns:
        val = getattr(obj, col.name)
        if isinstance(val, (datetime, date)):
            val = val.isoformat()
        out[col.name] = val
    return out


# Reihenfolge für den Export (Nutzdaten des Tenants).
_EXPORT_MODELS = [
    ("calendars", Calendar),
    ("events", Event),
    ("contacts", Contact),
    ("projects", Project),
    ("habits", Habit),
    ("habit_sessions", HabitSession),
    ("todos", Todo),
    ("todo_completions", TodoCompletion),
    ("goals", DailyGoal),
    ("reviews", DailyReview),
    ("daily_load", DailyLoad),
    # Fehlten bis 2026-08 im Auskunfts-Export (Art. 20), dabei sind Check-ins
    # (Schlaf, Stimmung, Energie) und Aktivitaets-Randnotizen samt LLM-Auswertung
    # mit die persoenlichsten Daten im System.
    ("checkins", DailyCheckIn),
    ("activity_feedback", ActivityFeedback),
    ("secretary_runs", SecretaryRun),
]


def export_account_data(db: Session, sub: str | None = None) -> dict:
    """Vollständiger Export aller Daten des Tenants als serialisierbares dict (Art. 20)."""
    sub = sub or _current_sub(db)
    data = {
        "schema": "saganta-kalender-export/v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    for key, model in _EXPORT_MODELS:
        rows = db.query(model).filter(model.owner_sub == sub).all()
        data[key] = [_serialize_row(r) for r in rows]
    return data


def account_summary(db: Session, sub: str | None = None) -> dict:
    """Transparenz-Übersicht: wie viele Datensätze pro Kategorie gespeichert sind."""
    sub = sub or _current_sub(db)
    return {
        key: db.query(model).filter(model.owner_sub == sub).count()
        for key, model in _EXPORT_MODELS
    }


# Löschreihenfolge: Kinder vor Eltern (FK-sicher). Alle mit explizitem owner_sub-Filter.
# ActivityFeedback haengt per FK an Event und MUSS deshalb davor stehen.
_DELETE_MODELS = [
    TodoCompletion,
    HabitSession,
    Todo,
    DailyGoal,
    DailyReview,
    DailyLoad,
    DailyCheckIn,
    ActivityFeedback,
    SecretaryRun,
    Habit,
    Project,
    Event,
    Contact,
    Calendar,
]


def delete_account_data(db: Session, sub: str | None = None) -> dict:
    """Löscht ALLE Daten des Tenants (Art. 17). System-Daten (owner_sub NULL) bleiben.

    Gibt pro Tabelle die Anzahl gelöschter Zeilen zurück. Nutzt explizites
    ``owner_sub == sub`` (schützt SHARED-System-Zeilen) und skip_tenant, damit die
    Löschung nicht zusätzlich vom Auto-Scope verändert wird.
    """
    sub = sub or _current_sub(db)
    counts: dict[str, int] = {}
    for model in _DELETE_MODELS:
        stmt = (
            delete(model)
            .where(model.owner_sub == sub)
            .execution_options(skip_tenant=True)
        )
        result = db.execute(stmt)
        counts[model.__tablename__] = result.rowcount or 0
    db.commit()
    return counts


# --- Retention / Auto-Purge (System-weit, alle Tenants) ---

RETENTION = {
    "sessions_done_days": 90,     # erledigte/verworfene Habit-Sessions
    "todos_done_days": 365,       # abgeschlossene Aufgaben
    "goals_days": 365,            # alte Tagesziele
    "load_days": 365,             # Tages-Last-Metriken
    "logs_days": 90,              # Reminder-/Notification-/Sekretär-Logs
}


def purge_old_data(db: Session | None = None) -> dict:
    """Entfernt alte Wegwerf-Daten über alle Tenants (Datensparsamkeit, Art. 5).

    Läuft im System-Kontext (ungescopt). Nutzdaten (Termine, Kontakte, Habits)
    bleiben unangetastet, nur abgeschlossene Sessions, erledigte Aufgaben und
    interne Logs werden nach Ablauf der Frist gelöscht.
    """
    own = db is None
    if own:
        db = SessionLocal()
        db.info["owner_sub"] = None  # System-Kontext
    now = datetime.now(timezone.utc)
    today = now.date()
    counts: dict[str, int] = {}
    try:
        opts = {"skip_tenant": True}

        counts["habit_sessions"] = db.execute(
            delete(HabitSession)
            .where(
                HabitSession.status.in_(["dismissed", "overridden"]),
                HabitSession.end < now - timedelta(days=RETENTION["sessions_done_days"]),
            )
            .execution_options(**opts)
        ).rowcount or 0

        counts["todos"] = db.execute(
            delete(Todo)
            .where(
                Todo.completed == True,  # noqa: E712
                Todo.completed_at.isnot(None),
                Todo.completed_at < now - timedelta(days=RETENTION["todos_done_days"]),
            )
            .execution_options(**opts)
        ).rowcount or 0

        counts["goals"] = db.execute(
            delete(DailyGoal)
            .where(DailyGoal.date < today - timedelta(days=RETENTION["goals_days"]))
            .execution_options(**opts)
        ).rowcount or 0

        counts["daily_load"] = db.execute(
            delete(DailyLoad)
            .where(DailyLoad.date < today - timedelta(days=RETENTION["load_days"]))
            .execution_options(**opts)
        ).rowcount or 0

        counts["event_reminder_log"] = db.execute(
            delete(EventReminderLog)
            .where(EventReminderLog.sent_at < now - timedelta(days=RETENTION["logs_days"]))
            .execution_options(**opts)
        ).rowcount or 0

        counts["session_notifications"] = db.execute(
            delete(SessionNotification)
            .where(SessionNotification.sent_at < now - timedelta(days=RETENTION["logs_days"]))
            .execution_options(**opts)
        ).rowcount or 0

        counts["secretary_runs"] = db.execute(
            delete(SecretaryRun)
            .where(SecretaryRun.run_date < today - timedelta(days=RETENTION["logs_days"]))
            .execution_options(**opts)
        ).rowcount or 0

        db.commit()
    except Exception:
        logger.exception("Retention-Purge fehlgeschlagen")
        db.rollback()
    finally:
        if own:
            db.close()
    return counts
