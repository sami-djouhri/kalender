"""ntfy notifications for habit session start/end cadence."""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib import error, parse, request
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from backend.config import settings
from backend.database import SessionLocal
from backend.models import Event, EventReminderLog, HabitSession, SessionNotification

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class NotificationCandidate:
    session: HabitSession
    kind: str
    scheduled_for: datetime
    payload: dict[str, str]


def _as_berlin(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BERLIN)
    return value.astimezone(BERLIN)


def _clock(value: datetime) -> str:
    return _as_berlin(value).strftime("%H:%M")


def _notification_exists(db: Session, session_id: str, kind: str) -> bool:
    return (
        db.query(SessionNotification)
        .filter(
            SessionNotification.session_id == session_id,
            SessionNotification.kind == kind,
        )
        .first()
        is not None
    )


def _mark_sent(
    db: Session,
    session_id: str,
    kind: str,
    scheduled_for: datetime,
    sent_at: datetime,
) -> bool:
    db.add(
        SessionNotification(
            session_id=session_id,
            kind=kind,
            scheduled_for=scheduled_for,
            sent_at=sent_at,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def build_session_notification_payload(session: HabitSession, kind: str) -> dict[str, str]:
    habit_name = session.habit.name if session.habit else "Habit"
    start = _as_berlin(session.start)
    end = _as_berlin(session.end)

    if kind == "start_due":
        return {
            "title": "Habit startet bald",
            "message": (
                f"{habit_name} startet um {_clock(start)}. "
                "Bereit machen und in der Kalender-App starten, verschieben oder ausfallen lassen."
            ),
            "priority": "default",
            "tags": "calendar",
        }

    if kind == "start_now":
        return {
            "title": "Habit jetzt starten",
            "message": (
                f"{habit_name} laeuft jetzt bis {_clock(end)}. "
                "Session in der Kalender-App starten oder verschieben."
            ),
            "priority": "high",
            "tags": "calendar,alarm_clock",
        }

    if kind == "end_due":
        return {
            "title": "Habit endet bald",
            "message": (
                f"{habit_name} endet um {_clock(end)}. "
                "Abschliessen oder abbrechen, wenn du fertig bist."
            ),
            "priority": "default",
            "tags": "calendar,white_check_mark",
        }

    raise ValueError(f"Unknown session notification kind: {kind}")


def iter_due_session_notifications(
    session: HabitSession,
    now: datetime,
    start_lead_minutes: int,
    end_lead_minutes: int,
):
    start = _as_berlin(session.start)
    end = _as_berlin(session.end)
    now = _as_berlin(now)

    if session.status == "pending":
        start_due_until = now + timedelta(minutes=max(0, start_lead_minutes))
        if now < start <= start_due_until:
            yield "start_due", start
        if start <= now < end:
            yield "start_now", start

    if session.status == "accepted":
        end_due_until = now + timedelta(minutes=max(0, end_lead_minutes))
        if now <= end <= end_due_until:
            yield "end_due", end


def collect_due_session_notifications(
    db: Session,
    now: datetime | None = None,
) -> list[NotificationCandidate]:
    now = now or datetime.now(BERLIN)
    now = _as_berlin(now)
    max_lead = max(
        0,
        settings.SESSION_NOTIFY_LEAD_MINUTES_START,
        settings.SESSION_NOTIFY_LEAD_MINUTES_END,
    )
    lookback = now - timedelta(minutes=1)
    lookahead = now + timedelta(minutes=max_lead + 1)

    sessions = (
        db.query(HabitSession)
        .options(joinedload(HabitSession.habit))
        .filter(
            HabitSession.status.in_(["pending", "accepted"]),
            HabitSession.end >= lookback,
            HabitSession.start <= lookahead,
        )
        .order_by(HabitSession.start)
        .all()
    )

    candidates: list[NotificationCandidate] = []
    for session in sessions:
        if session.habit and not session.habit.active:
            continue
        for kind, scheduled_for in iter_due_session_notifications(
            session,
            now,
            settings.SESSION_NOTIFY_LEAD_MINUTES_START,
            settings.SESSION_NOTIFY_LEAD_MINUTES_END,
        ):
            if _notification_exists(db, session.id, kind):
                continue
            candidates.append(
                NotificationCandidate(
                    session=session,
                    kind=kind,
                    scheduled_for=scheduled_for,
                    payload=build_session_notification_payload(session, kind),
                )
            )
    return candidates


def send_ntfy_notification(payload: dict[str, str]) -> bool:
    if not settings.NTFY_URL or not settings.NTFY_TOPIC:
        return False

    topic = parse.quote(settings.NTFY_TOPIC.strip(), safe="")
    url = f"{settings.NTFY_URL.rstrip('/')}/{topic}"
    headers = {
        "Title": payload["title"],
        "Priority": payload.get("priority", "default"),
        "Tags": payload.get("tags", "calendar"),
    }
    if settings.NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {settings.NTFY_TOKEN}"

    req = request.Request(
        url,
        data=payload["message"].encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=8) as response:
            status = getattr(response, "status", 200)
            return 200 <= status < 300
    except (error.URLError, TimeoutError, OSError):
        logger.exception("Failed to send ntfy session notification to %s", url)
        return False


def process_session_notifications(
    db: Session,
    sender=send_ntfy_notification,
    now: datetime | None = None,
) -> int:
    if not settings.NTFY_URL or not settings.NTFY_TOPIC:
        return 0

    sent = 0
    for candidate in collect_due_session_notifications(db, now=now):
        try:
            ok = sender(candidate.payload)
        except Exception:
            logger.exception(
                "Session notification sender failed for %s/%s",
                candidate.session.id,
                candidate.kind,
            )
            continue
        if not ok:
            continue
        if _mark_sent(
            db,
            candidate.session.id,
            candidate.kind,
            candidate.scheduled_for,
            datetime.now(timezone.utc),
        ):
            sent += 1
    return sent


def process_event_reminders(
    db: Session,
    sender=send_ntfy_notification,
    now: datetime | None = None,
) -> int:
    """Versendet ntfy-Erinnerungen für Termine mit gesetztem reminder_minutes.

    v1: nur nicht-wiederkehrende Events (recurrence_rule IS NULL). Wiederkehrende
    Instanzen sind ein Folgeschritt. Jede Erinnerung feuert genau einmal (EventReminderLog).
    """
    if not settings.NTFY_URL or not settings.NTFY_TOPIC:
        return 0

    now = _as_berlin(now or datetime.now(BERLIN))
    # Fenster: Events, deren Reminder-Zeitpunkt (start - reminder_minutes) jetzt erreicht ist
    # und die noch nicht in der Vergangenheit gestartet sind (max. 1 Tag Vorlauf).
    horizon = now + timedelta(days=1)
    events = (
        db.query(Event)
        .filter(
            Event.reminder_minutes.isnot(None),
            Event.recurrence_rule.is_(None),
            Event.start >= now - timedelta(minutes=1),
            Event.start <= horizon,
        )
        .all()
    )

    sent = 0
    for event in events:
        start = _as_berlin(event.start)
        fire_at = start - timedelta(minutes=event.reminder_minutes or 0)
        if now < fire_at:
            continue  # Reminder-Zeitpunkt noch nicht erreicht
        if db.query(EventReminderLog).filter(EventReminderLog.event_id == event.id).first():
            continue  # bereits versendet
        lead = max(0, int((start - now).total_seconds() // 60))
        when = "jetzt" if lead == 0 else f"in {lead} Min ({_clock(start)})"
        payload = {
            "title": "Termin-Erinnerung",
            "message": f"{event.title} {when}.",
            "priority": "default",
            "tags": "calendar,bell",
        }
        try:
            ok = sender(payload)
        except Exception:
            logger.exception("Event reminder sender failed for %s", event.id)
            continue
        if not ok:
            continue
        db.add(EventReminderLog(event_id=event.id, fire_at=fire_at, sent_at=datetime.now(timezone.utc)))
        try:
            db.commit()
            sent += 1
        except IntegrityError:
            db.rollback()
    return sent


class SessionNotificationRunner:
    def __init__(self, interval_seconds: int = 60):
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="session-ntfy", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self):
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                process_session_notifications(db)
                process_event_reminders(db)
            except Exception:
                logger.exception("Session notification scheduler tick failed")
            finally:
                db.close()
            # Sekretär-Tick: verbindliche Tagesplanung + Briefing/Abend-Nudge.
            # Läuft auch ohne ntfy (Planung ist von Push entkoppelt); der Push
            # erfolgt nur, wenn ntfy konfiguriert ist. Eigene Sessions pro Tenant
            # innerhalb des Ticks (tenant-gescopt), daher hier keine db-Übergabe.
            ntfy_ready = bool(settings.NTFY_URL and settings.NTFY_TOPIC)
            try:
                from backend.secretary import run_secretary_tick

                run_secretary_tick(
                    sender=send_ntfy_notification if ntfy_ready else None
                )
            except Exception:
                logger.exception("Sekretär-Tick fehlgeschlagen")
            # Adaptiver Assistent: proaktive Nudges (Check-in-Prompt, Aktivitäts-Nudge).
            # Läuft nach dem Sekretär, ebenfalls von Push entkoppelt (ohne ntfy = No-op).
            try:
                from backend.assistant import run_assistant_tick

                run_assistant_tick(
                    sender=send_ntfy_notification if ntfy_ready else None
                )
            except Exception:
                logger.exception("Assistenten-Tick fehlgeschlagen")
            # Slow-lane: offene Aktivitäts-Notizen vom lokalen LLM auswerten (opt-in,
            # No-op ohne FEEDBACK_LLM_ENABLED). Push-unabhängig, gedrosselt pro Tick.
            try:
                from backend.feedback_llm import process_pending_feedback_notes

                process_pending_feedback_notes()
            except Exception:
                logger.exception("Feedback-LLM-Tick fehlgeschlagen")
            self._stop.wait(self.interval_seconds)


_runner: SessionNotificationRunner | None = None


def start_session_notification_runner() -> SessionNotificationRunner | None:
    """Startet den Hintergrund-Runner.

    Läuft immer, auch ohne ntfy, weil der Sekretär (Tagesplanung) davon
    unabhängig ist. Session-/Event-Notifications sind ohne ntfy schlicht No-ops.
    """
    global _runner
    if not settings.NTFY_URL or not settings.NTFY_TOPIC:
        logger.info(
            "ntfy-Push deaktiviert (NTFY_URL/NTFY_TOPIC fehlt): Runner läuft für Sekretär-Planung weiter"
        )
    elif not settings.NTFY_URL.startswith("https://"):
        logger.warning(
            "NTFY_URL nutzt kein HTTPS (%s): Push-Inhalte werden unverschlüsselt übertragen. "
            "Für den Betrieb einen HTTPS-Endpunkt konfigurieren.",
            settings.NTFY_URL,
        )
    if _runner is not None:
        return _runner
    _runner = SessionNotificationRunner()
    _runner.start()
    return _runner


def stop_session_notification_runner():
    global _runner
    if _runner is None:
        return
    _runner.stop()
    _runner = None
