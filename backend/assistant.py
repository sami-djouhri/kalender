"""Adaptiver Assistent: angereichertes Tagesbild + proaktive Push-Nudges.

Ergänzt den bestehenden Sekretär (secretary.py, unangetastet) um die adaptive
Schicht dieser Epoche:

* `build_today`: das angereicherte Tagesbild: klassisches Briefing (Termine, Ziele,
  geplante Todos, Konflikte, Geburtstage) + Tageskapazität + Check-in-Status +
  Top-Vorschläge/Entscheidungsfrage. Die eine Antwort, die das Frontend braucht.
* `run_assistant_tick`: läuft im 60s-Runner NACH dem Sekretär-Tick und schickt
  (owner-only, idempotent) zwei proaktive Nudges:
    - morgens ohne Check-in → „Wie hast du geschlafen?" (stimmt den Tag ab)
    - nach Arbeit/Schule + freie Zeit → „Feierabend, Lust auf …?" (Top-Vorschlag)

Idempotenz über `SecretaryRun` (neue kinds `checkin_prompt`/`activity_nudge`): jeder
Nudge feuert pro Tag genau einmal, wird beim Tages-Rollover automatisch wieder frei.
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import settings as app_settings
from backend.daily_energy import compute_day_capacity, get_checkin
from backend.database import SessionLocal
from backend.models import DailyCheckIn, SecretaryRun
from backend.suggestions import build_progress, build_suggestions

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")


def _as_berlin(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BERLIN)
    return value.astimezone(BERLIN)


# --- Angereichertes Tagesbild ---

def build_today(db: Session, day: date | None = None, now: datetime | None = None) -> dict:
    """Alles, was das Frontend für „heute" braucht, in einer Antwort."""
    day = day or date.today()
    # Briefing stammt aus dem bestehenden Sekretär (Import zur Laufzeit; die Datei
    # ist über den *secret*-Namensfilter nur für Tooling gesperrt, nicht für Python).
    from backend.secretary import build_briefing

    briefing = build_briefing(db, day)
    capacity = compute_day_capacity(db, day, now=now)
    suggest = build_suggestions(db, day, now=now)
    progress = build_progress(db)

    endangered = [h for h in progress["habits"] if h["endangered"]]
    # Backlog-Warnung (read-only): chronisch verschobene Aufgaben sichtbar machen, damit
    # nichts still verrottet. Lazy-Import gegen Zyklus (scheduler zieht daily_energy/preferences).
    from backend.scheduler import list_backlog

    backlog = list_backlog(db, app_settings.BACKLOG_ESCALATE_THRESHOLD)
    return {
        **briefing,
        "capacity": capacity,
        "has_checkin": capacity["has_checkin"],
        "checkin": capacity.get("checkin"),
        "suggestions": suggest["suggestions"],
        "choice": suggest["choice"],
        "top_suggestion": suggest["top"],
        "free_minutes": suggest["free_minutes"],
        "next_free": suggest["next_free"],
        "backlog": backlog,
        "progress": {
            "categories": progress["categories"],
            "endangered_count": len(endangered),
            "endangered": [{"name": h["name"], "remaining_minutes": h["remaining_minutes"]} for h in endangered],
        },
    }


# --- Idempotenz-Helfer (eigene, damit secretary.py unberührt bleibt) ---

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


def _owner_session() -> Session:
    db = SessionLocal()
    db.info["owner_sub"] = app_settings.DEFAULT_OWNER_SUB
    return db


# --- Proaktive Nudges ---

def _checkin_prompt_text() -> dict:
    return {
        "title": "Kurzer Check-in",
        "message": "Wie hast du geschlafen? Ein kurzer Check-in stimmt deinen Tag ab "
                   "(Energie, Schlaf, Lust auf Bewegung).",
        "priority": "default",
        "tags": "calendar,sunny",
    }


def _activity_nudge_text(suggest: dict) -> dict | None:
    top = None
    # Bevorzuge eine diskretionäre Aktivität (Sport/Hobby/Buch), nicht Todos.
    for s in suggest.get("suggestions", []):
        if s["kind"] in ("sport", "hobby", "lesen", "erholung"):
            top = s
            break
    if not top:
        return None
    choice = suggest.get("choice")
    if choice and len(choice.get("options", [])) >= 2:
        opts = " oder ".join(o["title"].split("–")[0].strip() for o in choice["options"][:2])
        msg = f"{choice['prompt']} {opts}?"
    else:
        msg = f"Feierabend: Lust auf {top['title']}? ({top['subtitle']})".strip()
    return {
        "title": "Wie wär's?",
        "message": msg,
        "priority": "low",
        "tags": "calendar,muscle",
    }


def _minutes_since_end(activity: dict, now: datetime) -> float:
    """Minuten seit Ende einer Aktivitäts-Instanz (activity['end'] = naive Berlin-ISO)."""
    end = datetime.fromisoformat(activity["end"])
    now_naive = _as_berlin(now).replace(tzinfo=None)
    return (now_naive - end).total_seconds() / 60


def _feedback_prompt_text(reviewable: list[dict]) -> dict | None:
    """Ntfy-Text für den Feedback-Nudge (nennt die zuletzt beendete Aktivität)."""
    if not reviewable:
        return None
    latest = reviewable[-1]  # nach Startzeit sortiert → die späteste zuletzt
    n = len(reviewable)
    if n == 1:
        msg = f"Wie war »{latest['title']}«? 💪 stark · 🙂 ok · 🥵 kaputt: kurz eintragen?"
    else:
        msg = (
            f"{n} Aktivitäten warten auf dein Feedback (zuletzt »{latest['title']}«). "
            "💪 / 🙂 / 🥵"
        )
    return {"title": "Wie lief's?", "message": msg, "priority": "low", "tags": "calendar,memo"}


def _backlog_escalation_text(stuck: list[dict]) -> dict | None:
    """Ntfy-Text für die Backlog-Eskalation: nennt die am längsten liegende Aufgabe."""
    if not stuck:
        return None
    top = stuck[0]  # nach defer_count absteigend sortiert
    n = len(stuck)
    if n == 1:
        msg = (
            f"»{top['title']}« hast du {top['defer_count']}× verschoben: "
            "heute dranbleiben oder bewusst streichen?"
        )
    else:
        msg = (
            f"{n} Aufgaben stauen sich (zuletzt »{top['title']}«, {top['defer_count']}× "
            "verschoben): heute eine davon klären?"
        )
    return {"title": "Bleibt liegen", "message": msg, "priority": "default", "tags": "calendar,warning"}


def run_assistant_tick(now: datetime | None = None, sender=None) -> dict:
    """Ein Assistenten-Tick (nach dem Sekretär-Tick). Owner-only, idempotent.

    Schickt morgens einen Check-in-Prompt (wenn noch kein Check-in), nach
    Arbeit/Schule einen Aktivitäts-Nudge (wenn freie Zeit da ist) und – sobald eine
    Aktivität frisch vorbei ist – einen Feedback-Nudge („Wie war …?"). Ohne `sender`
    (kein ntfy) werden nur die Marker gesetzt, kein Push, kein Fehler.
    """
    result = {
        "checkin_prompt_sent": False,
        "activity_nudge_sent": False,
        "feedback_prompt_sent": False,
        "backlog_escalation_sent": False,
    }
    if not app_settings.ASSISTANT_NUDGES_ENABLED:
        return {"skipped": "disabled"}

    now = _as_berlin(now or datetime.now(BERLIN))
    today = now.date()
    owner = app_settings.DEFAULT_OWNER_SUB

    # Sekretär-Morning-Hour wiederverwenden (gleicher Rhythmus wie das Briefing).
    from backend.secretary import get_config

    db = _owner_session()
    try:
        cfg = get_config(db)
        morning_hour = cfg.get("morning_hour", 6)
    except Exception:
        morning_hour = 6
    finally:
        db.close()

    # 1) Morgen: Check-in-Prompt, solange noch kein Check-in vorliegt.
    if now.hour >= morning_hour:
        db = _owner_session()
        try:
            if not _already_ran(db, owner, today, "checkin_prompt") and get_checkin(db, today) is None:
                if sender is not None:
                    try:
                        if sender(_checkin_prompt_text()):
                            result["checkin_prompt_sent"] = True
                    except Exception:
                        logger.exception("Check-in-Prompt-Push fehlgeschlagen")
                _mark_ran(db, owner, today, "checkin_prompt", None)
        except Exception:
            logger.exception("Check-in-Prompt-Tick fehlgeschlagen")
            db.rollback()
        finally:
            db.close()

    # 2) Nach Feierabend (Arbeit/Schule): Aktivitäts-Nudge bei freier Zeit.
    if now.hour >= app_settings.ASSISTANT_ACTIVITY_NUDGE_HOUR:
        db = _owner_session()
        try:
            if not _already_ran(db, owner, today, "activity_nudge"):
                capacity = compute_day_capacity(db, today, now=now)
                if capacity["day_type"] in ("arbeit", "schule"):
                    suggest = build_suggestions(db, today, now=now)
                    if suggest["free_minutes"] >= 30:
                        payload = _activity_nudge_text(suggest)
                        if payload and sender is not None:
                            try:
                                if sender(payload):
                                    result["activity_nudge_sent"] = True
                            except Exception:
                                logger.exception("Aktivitäts-Nudge-Push fehlgeschlagen")
                        # Marker auch ohne Push/ohne Vorschlag setzen (einmal pro Tag prüfen).
                        _mark_ran(db, owner, today, "activity_nudge", None)
        except Exception:
            logger.exception("Aktivitäts-Nudge-Tick fehlgeschlagen")
            db.rollback()
        finally:
            db.close()

    # 3) Feedback-Nudge: sobald eine Aktivität frisch vorbei ist und noch nicht bewertet.
    #    Zeitlich nicht an eine feste Stunde gebunden (Aktivitäten enden abends), aber
    #    nur bei „frisch" (Ende ≤ 120 Min her) → kein Nudge für alte/gestrige Slots.
    db = _owner_session()
    try:
        if not _already_ran(db, owner, today, "feedback_prompt"):
            from backend.activities import list_reviewable_activities

            reviewable = list_reviewable_activities(db, today, now=now)
            fresh = [a for a in reviewable if _minutes_since_end(a, now) <= 120]
            if fresh:
                payload = _feedback_prompt_text(reviewable)
                if payload and sender is not None:
                    try:
                        if sender(payload):
                            result["feedback_prompt_sent"] = True
                    except Exception:
                        logger.exception("Feedback-Nudge-Push fehlgeschlagen")
                _mark_ran(db, owner, today, "feedback_prompt", None)
    except Exception:
        logger.exception("Feedback-Nudge-Tick fehlgeschlagen")
        db.rollback()
    finally:
        db.close()

    # 4) Backlog-Eskalation (einmal pro Tag ab morning_hour): überfällige Aufgaben zählen
    #    automatisch als „wieder verschoben"; staut sich etwas ≥ Schwelle, kommt ein Nudge.
    if now.hour >= morning_hour:
        db = _owner_session()
        try:
            if not _already_ran(db, owner, today, "backlog_escalation"):
                from backend.scheduler import escalate_backlog, list_backlog

                escalate_backlog(db, today)
                stuck = list_backlog(db, app_settings.BACKLOG_ESCALATE_THRESHOLD)
                if stuck:
                    payload = _backlog_escalation_text(stuck)
                    if payload and sender is not None:
                        try:
                            if sender(payload):
                                result["backlog_escalation_sent"] = True
                        except Exception:
                            logger.exception("Backlog-Eskalations-Push fehlgeschlagen")
                # Marker immer setzen (escalate_backlog ist selbst idempotent/Tag).
                _mark_ran(db, owner, today, "backlog_escalation", None)
        except Exception:
            logger.exception("Backlog-Eskalations-Tick fehlgeschlagen")
            db.rollback()
        finally:
            db.close()

    return result
