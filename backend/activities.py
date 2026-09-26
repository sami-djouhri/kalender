"""Bewertbare Aktivitäten eines Tages, die Brücke zwischen Event-Gleis und Feedback.

Ein Event mit `activity_type` ist eine flexible, **bewertbare Aktivität**. Diese Datei
listet die Instanzen eines Tages (inkl. Recurrence-Expansion), markiert ihren Zeit-Status
(`upcoming`/`running`/`past`) und joint das evtl. schon erfasste `ActivityFeedback`.
Grundlage für das „Wie liefen deine Aktivitäten?"-UI, den proaktiven Nudge und (Phase 2)
das Präferenz-Lernen.

Zeitkonvention: Events liegen als **Berlin-Wanduhr (naiv)** in der DB (verifiziert:
CompTIA Mo 17:00, Arbeit 07:00–16:00). Wir rechnen konsequent in naiver Berlin-Wanduhr,
damit „vorbei" mit dem stimmt, was der Owner im Kalender sieht.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from backend.models import ActivityFeedback, Event
from backend.recurrence import expand_events
from backend.routers.daytype import DAYTYPE_IDS

BERLIN = ZoneInfo("Europe/Berlin")

# Titel-Schlüsselwörter → Aktivitäts-Kategorie (konservativ; unklare Titel bleiben NULL).
# Genutzt beim einmaligen Backfill (main.py) und potenziell beim Anlegen neuer Events.
ACTIVITY_TYPE_KEYWORDS = {
    "lernen": ("lern", "comptia", "tryhackme", "ihk", "allgemeinwissen", "studium", "kurs", "vokab", "prüfung"),
    "lesen": ("lesen", "buch", "bücher", "book", "nietzsche", "roman"),
    "sport": ("sport", "gym", "training", "workout", "lauf", "joggen", "fitness", "yoga", "rad", "schwimm"),
    "hobby": ("homelab", "pflanzen", "garten", "hobby", "projekt", "basteln", "musik", "malen", "kochen"),
}


def infer_activity_type(title: str | None) -> str | None:
    """Leitet aus einem Event-Titel eine Aktivitäts-Kategorie ab (oder None, wenn unklar)."""
    t = (title or "").lower()
    for category, keywords in ACTIVITY_TYPE_KEYWORDS.items():
        if any(k in t for k in keywords):
            return category
    return None


def _naive_berlin(dt: datetime) -> datetime:
    """datetime → naive Berlin-Wanduhr (aware wird nach Berlin konvertiert)."""
    if dt.tzinfo is not None:
        return dt.astimezone(BERLIN).replace(tzinfo=None)
    return dt


def _now_naive(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(BERLIN).replace(tzinfo=None)
    return _naive_berlin(now)


def feedback_out(fb: ActivityFeedback | None) -> dict | None:
    """Serialisiert ein Feedback für die API (kompakt, UI-orientiert)."""
    if fb is None:
        return None
    return {
        "id": fb.id,
        "energy_after": fb.energy_after,
        "satisfaction": fb.satisfaction,
        "took_place": fb.took_place,
        "note": fb.note,
        "updated_at": fb.updated_at.isoformat() if fb.updated_at else None,
    }


def list_day_activities(db: Session, day: date, now: datetime | None = None) -> list[dict]:
    """Alle Aktivitäts-Event-Instanzen eines Tages mit Zeit-Status + Feedback.

    Nur Events mit gesetztem `activity_type` (die flexiblen, bewertbaren Aktivitäten);
    Daytype-System-Events (Arbeit/Schule/…) bleiben außen vor.
    """
    now_naive = _now_naive(now)
    day_start = datetime(day.year, day.month, day.day)
    day_end = day_start + timedelta(days=1)

    base = db.query(Event).filter(
        Event.activity_type.isnot(None),
        Event.calendar_id.notin_(DAYTYPE_IDS),
    )
    one_off = base.filter(
        Event.recurrence_rule.is_(None),
        Event.end >= day_start,
        Event.start <= day_end,
    ).all()
    recurring = base.filter(
        Event.recurrence_rule.isnot(None),
        Event.start <= day_end,
    ).all()
    instances = expand_events(one_off + recurring, day_start, day_end)

    # Feedback des Tages einmal laden → Map nach Basis-Event-ID (Unique je event+Tag).
    fb_by_event = {
        fb.event_id: fb
        for fb in db.query(ActivityFeedback).filter(
            ActivityFeedback.occurrence_date == day
        ).all()
    }

    out: list[dict] = []
    for inst in instances:
        start = _naive_berlin(inst["start"])
        end = _naive_berlin(inst["end"])
        base_id = inst.get("series_id") or inst["id"].split("::", 1)[0]
        if end <= now_naive:
            status = "past"
        elif start <= now_naive < end:
            status = "running"
        else:
            status = "upcoming"
        out.append({
            "event_id": base_id,
            "instance_id": inst["id"],
            "occurrence_date": day.isoformat(),
            "title": inst["title"],
            "activity_type": inst.get("activity_type"),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "time_status": status,
            "feedback": feedback_out(fb_by_event.get(base_id)),
        })
    out.sort(key=lambda a: a["start"])
    return out


def list_reviewable_activities(db: Session, day: date, now: datetime | None = None) -> list[dict]:
    """Vorbei-gegangene Aktivitäten des Tages OHNE Feedback (Nudge + „Wie lief's?"-UI)."""
    return [
        a for a in list_day_activities(db, day, now)
        if a["time_status"] == "past" and a["feedback"] is None
    ]
