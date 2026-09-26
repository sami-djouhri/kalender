"""Vorschlags-Engine: „sag mir, was ich als Nächstes tun soll".

Kombiniert freie Zeit + Tageskapazität (Check-in/Strain) + Day-Type + Habit-Quoten
+ Todos + Cross-App-Signale (Fitness/Lager/MealPrep) zu einer **gerankten Liste**
konkreter Vorschläge, und, wenn mehrere gute, gleichwertige Optionen um denselben
freien Slot konkurrieren, zu einer **Entscheidungsfrage** („Wozu hast du Lust?
Sport oder Buch?").

Design-Prinzipien (Owner-Vision):
* Fest bleibt fest: Termine/Ziele/Habits werden hier NICHT umgeplant, nur ergänzt.
* Nach einem langen (Arbeits-)Tag → eher Erholung/Buch statt Sport.
* Körperlich Forderndes bevorzugt an arbeitsfreien Tagen / bei guter Kapazität.
* Ungenutzte freie Zeit sinnvoll füllen, aber nie den Tag überladen (Burnout-Schutz).
* Deterministisch/regelbasiert; Cross-App fail-soft (fehlt ein Signal → weniger Vorschläge).
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from backend import cross_app
from backend.daily_energy import compute_day_capacity, energy_fits
from backend.models import ActivityFeedback, Habit, HabitSession, Todo
from backend.routers.daytype import _get_daytype_display
from backend.scheduler import (
    DEFAULT_TODO_MINUTES,
    _ensure_aware,
    _week_iso_str,
    compute_free_slots,
    weekly_minimum_endangered,
)

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")

# Diskretionäre Arten, die im „Wozu hast du Lust?"-Entscheidungsmodus konkurrieren.
_DISCRETIONARY = {"sport", "hobby", "lesen", "erholung"}

# --- Feedback-Lernen (Phase 1: der erste, sichtbare Lern-Funke) ---
# Wie sich der Owner NACH einer Aktivität gefühlt hat → numerisches Signal.
_ENERGY_AFTER_SCORE = {"energetisiert": 1.0, "ok": 0.0, "erschöpft": -1.0}
# Vorschlags-„kind" → Aktivitäts-Kategorie (nur diskretionäre; Lernen wird auto-geplant).
_KIND_TO_ACTIVITY = {"sport": "sport", "lesen": "lesen", "hobby": "hobby"}


def recent_activity_signal(
    db: Session, category: str, lookback_days: int = 14, ref: date | None = None
) -> float | None:
    """Mittelt `energy_after` der jüngsten Feedbacks einer Kategorie → [-1..1] oder None.

    Positiv = zuletzt gut/energetisiert danach; negativ = zuletzt platt. None = keine
    Daten → neutral, kein Einfluss (rückwärtskompatibel). Deterministisch, kein LLM.
    """
    ref = ref or date.today()
    since = ref - timedelta(days=lookback_days)
    rows = (
        db.query(ActivityFeedback)
        .filter(
            ActivityFeedback.activity_type == category,
            ActivityFeedback.occurrence_date >= since,
            ActivityFeedback.energy_after.isnot(None),
        )
        .all()
    )
    scores = [
        _ENERGY_AFTER_SCORE[r.energy_after]
        for r in rows
        if r.energy_after in _ENERGY_AFTER_SCORE
    ]
    if not scores:
        return None
    return sum(scores) / len(scores)


def _apply_feedback_adjustment(
    db: Session, candidates: list[dict], ref: date | None = None, now_hour: int | None = None
) -> None:
    """Hebt/dämpft diskretionäre Vorschläge nach gelerntem Feedback + macht es sichtbar.

    Zwei Signale, in-place auf `score`/`reason`:
    * **Kategorie-Tendenz** (Phase 1, ±15): wie ging's zuletzt allgemein nach dieser Art?
    * **Zeit-Präferenz** (Phase 2, ±10): passt die aktuelle Tageszeit für diese Aktivität?

    So sieht der Owner sofort, dass sein Feedback wirkt (Vertrauens-Loop). Ohne Daten =
    kein Einfluss (rückwärtskompatibel).
    """
    from backend.preferences import time_fit  # lazy → kein Import-Zyklus preferences↔suggestions

    cache: dict[str, float | None] = {}
    for c in candidates:
        cat = _KIND_TO_ACTIVITY.get(c["kind"])
        if not cat:
            continue
        if cat not in cache:
            cache[cat] = recent_activity_signal(db, cat, ref=ref)
        sig = cache[cat]
        if sig is not None:
            c["score"] = round(c["score"] + sig * 15, 1)
            if sig >= 0.34:
                c["reason"] = (c.get("reason", "") + " · zuletzt gut getan").strip(" ·")
            elif sig <= -0.34:
                c["reason"] = (c.get("reason", "") + " · zuletzt oft platt danach").strip(" ·")
        if now_hour is not None:
            tf = time_fit(db, cat, now_hour, ref=ref)
            if tf is not None:
                c["score"] = round(c["score"] + tf * 10, 1)
                if tf >= 0.4:
                    c["reason"] = (c.get("reason", "") + " · gute Tageszeit dafür").strip(" ·")
                elif tf <= -0.4:
                    c["reason"] = (c.get("reason", "") + " · eher ungünstige Tageszeit").strip(" ·")


def _now_berlin(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(BERLIN)
    if now.tzinfo is None:
        return now.replace(tzinfo=BERLIN)
    return now.astimezone(BERLIN)


def _free_minutes_today(db: Session, day: date, now: datetime) -> tuple[int, datetime | None]:
    """(freie Restminuten ab jetzt, Start des nächsten freien Slots). Für heute now-aware."""
    slots = compute_free_slots(db, day)
    total = 0
    next_start: datetime | None = None
    for s, e in slots:
        s = _ensure_aware(s)
        e = _ensure_aware(e)
        if day == now.date() and e <= now:
            continue
        eff_start = max(s, now) if day == now.date() else s
        mins = int((e - eff_start).total_seconds() / 60)
        if mins <= 0:
            continue
        total += mins
        if next_start is None or eff_start < next_start:
            next_start = eff_start
    return total, next_start


# --- Fortschritt / Quoten ---

def _habit_streak(db: Session, habit_id: str, ref: date) -> int:
    """Aufeinanderfolgende Tage (bis gestern) mit erledigter Session dieses Habits."""
    count = 0
    d = ref - timedelta(days=1)
    while count < 60:
        d_start = datetime(d.year, d.month, d.day, tzinfo=BERLIN)
        d_end = d_start + timedelta(days=1)
        has = (
            db.query(HabitSession)
            .filter(
                HabitSession.habit_id == habit_id,
                HabitSession.start < d_end,
                HabitSession.end > d_start,
                HabitSession.status.in_(["completed", "accepted"]),
            )
            .first()
        )
        if has:
            count += 1
            d -= timedelta(days=1)
        else:
            break
    return count


def build_progress(db: Session, week_start: date | None = None) -> dict:
    """Messbarer Wochenfortschritt je Habit + Kategorie-Balance.

    done = accepted/completed Minuten dieser Woche; planned = pending; target aus Habit.
    """
    today = date.today()
    if week_start is None:
        week_start = today - timedelta(days=today.weekday())
    week_iso = _week_iso_str(week_start)

    habits = db.query(Habit).filter(Habit.active == True).all()  # noqa: E712
    items = []
    cat_totals: dict[str, dict] = {}
    for h in habits:
        sessions = (
            db.query(HabitSession)
            .filter(HabitSession.habit_id == h.id, HabitSession.week_iso == week_iso)
            .all()
        )
        done = sum(
            (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
            for s in sessions
            if s.status in ("completed", "accepted")
        )
        planned = sum(
            (_ensure_aware(s.end) - _ensure_aware(s.start)).total_seconds() / 60
            for s in sessions
            if s.status == "pending"
        )
        target = (h.target_hours_per_week or 0) * 60
        pct = round(min(100, (done / target * 100))) if target else 0
        endangered = weekly_minimum_endangered(db, h, week_iso)
        item = {
            "habit_id": h.id,
            "name": h.name,
            "category": h.category,
            "color": h.color,
            "target_minutes": int(target),
            "done_minutes": int(done),
            "planned_minutes": int(planned),
            "remaining_minutes": max(0, int(target - done)),
            "pct": pct,
            "streak_days": _habit_streak(db, h.id, today),
            "endangered": endangered,
            "weekly_minimum_hours": h.weekly_minimum_hours,
        }
        items.append(item)
        ct = cat_totals.setdefault(h.category, {"done": 0, "target": 0})
        ct["done"] += int(done)
        ct["target"] += int(target)

    items.sort(key=lambda i: (not i["endangered"], i["pct"]))
    categories = [
        {
            "category": cat,
            "done_minutes": v["done"],
            "target_minutes": v["target"],
            "pct": round(min(100, v["done"] / v["target"] * 100)) if v["target"] else 0,
        }
        for cat, v in sorted(cat_totals.items())
    ]
    return {
        "week_start": week_start.isoformat(),
        "week_iso": week_iso,
        "habits": items,
        "categories": categories,
    }


# --- Vorschläge ---

def _s(kind, title, *, subtitle="", reason="", energy="mittel", duration_min=45,
       score=50.0, source="intern", action=None, sid=None) -> dict:
    return {
        "id": sid or f"{kind}:{abs(hash((kind, title))) % 100000}",
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "reason": reason,
        "energy": energy,
        "duration_min": duration_min,
        "score": round(float(score), 1),
        "source": source,
        "action": action or {},
    }


def _sport_suggestion(capacity: dict, signals: dict) -> dict | None:
    """Sport-Vorschlag aus Fitness-Signalen, nur wenn körperlich vertretbar."""
    if not capacity["allow_physical"]:
        return None
    days = signals.get("days_since_workout")
    if days == 0:
        return None  # heute schon trainiert
    freshness = signals.get("muscle_freshness") or {}
    summary = freshness.get("summary") if isinstance(freshness, dict) else None
    ready_muscles = []
    if isinstance(summary, dict):
        # Muskeln mit hoher Readiness = gut erholt → heute trainierbar.
        mf = freshness.get("muscle_freshness") or {}
        if isinstance(mf, dict):
            ready_muscles = [
                name for name, v in mf.items()
                if isinstance(v, dict) and (v.get("readiness_pct") or 0) >= 70
            ][:2]

    score = 55.0
    reason_bits = []
    if days is None:
        subtitle = "Zeit für eine Einheit"
    else:
        subtitle = f"Letztes Training vor {days} Tag(en)"
        if days >= 2:
            score += 12
            reason_bits.append(f"{days} Tage kein Training")
    if capacity["prefer_physical"]:
        score += 15
        reason_bits.append("arbeitsfreier Tag mit Energie")
    if ready_muscles:
        subtitle = f"Erholt: {', '.join(ready_muscles)}"
        reason_bits.append("Muskeln erholt")
    if capacity["level"] == "geladen":
        score += 8
    reason = " · ".join(reason_bits) or "gute Gelegenheit für Bewegung"
    return _s(
        "sport", "Sport – Trainingseinheit",
        subtitle=subtitle, reason=reason, energy="hoch", duration_min=60,
        score=score, source="fitness",
        action={"type": "open_app", "app": "fitness"},
    )


def _einkauf_suggestion(signals: dict) -> dict | None:
    count = signals.get("shopping_open_count") or 0
    critical = signals.get("lager_critical_count") or 0
    if count <= 0 and critical <= 0:
        return None
    names = signals.get("shopping_names") or []
    if count > 0:
        title = f"Einkaufen – {count} Artikel offen"
        subtitle = ", ".join(names) if names else ""
        source = "mealprep"
    else:
        title = f"Nachkaufen – {critical} kritische Bestände"
        subtitle = ""
        source = "lager"
    score = 46.0 + min(count, 6) * 3 + min(critical, 4) * 4
    return _s(
        "einkauf", title, subtitle=subtitle,
        reason="offene Einkaufsliste / niedrige Bestände",
        energy="mittel", duration_min=45, score=score, source=source,
        action={"type": "open_app", "app": source},
    )


def _erholung_suggestion(capacity: dict) -> dict:
    level = capacity["level"]
    if level in ("geschont", "erschöpft"):
        score = 70.0 if level == "erschöpft" else 60.0
        title = "Runterfahren – heute lieber erholen"
        reason = capacity["reason"]
        subtitle = "Buch, Pause oder früh ins Bett statt Anstrengung"
    else:
        score = 32.0
        title = "Buch / Pause"
        reason = "bewusste Pause tut dem Kopf gut"
        subtitle = "kurze Auszeit"
    return _s(
        "erholung", title, subtitle=subtitle, reason=reason,
        energy="niedrig", duration_min=40, score=score, source="intern",
        action={"type": "note"},
    )


def _habit_suggestions(progress: dict, capacity: dict) -> list[dict]:
    out = []
    for h in progress["habits"]:
        if h["category"] == "lernen":
            continue  # Lernen wird bereits automatisch als Sessions geplant
        if h["remaining_minutes"] <= 0:
            continue
        endangered = h["endangered"]
        # Nur gefährdete oder deutlich unter Ziel liegende Hobbys aktiv vorschlagen.
        if not endangered and h["pct"] >= 60:
            continue
        energy = "niedrig" if h["category"] == "lesen" else "mittel"
        kind = "lesen" if h["category"] == "lesen" else "hobby"
        score = 40.0 + (25 if endangered else 0) + max(0, (60 - h["pct"]) * 0.3)
        streak = h["streak_days"]
        reason = "Wochenminimum gefährdet" if endangered else f"{h['pct']}% der Wochenquote"
        subtitle = f"noch {h['remaining_minutes']} Min diese Woche"
        if streak >= 2:
            subtitle += f" · {streak}-Tage-Serie"
            score += 5
        out.append(_s(
            kind, f"{h['name']}",
            subtitle=subtitle, reason=reason, energy=energy,
            duration_min=min(90, max(30, h["remaining_minutes"])),
            score=score, source="habit",
            action={"type": "schedule_habit", "habit_id": h["habit_id"]},
            sid=f"habit:{h['habit_id']}",
        ))
    return out


def _todo_suggestions(db: Session, day: date, capacity: dict) -> list[dict]:
    out = []
    overdue = (
        db.query(Todo)
        .filter(
            Todo.completed == False,  # noqa: E712
            Todo.status.notin_(["done", "cancelled"]),
            Todo.due_date.isnot(None),
            Todo.due_date < day,
        )
        .order_by(Todo.due_date)
        .limit(5)
        .all()
    )
    for t in overdue:
        out.append(_s(
            "todo", t.title,
            subtitle=f"überfällig seit {t.due_date.isoformat()}",
            reason="überfällige Aufgabe", energy=t.energy_required or "mittel",
            duration_min=t.estimated_minutes or DEFAULT_TODO_MINUTES,
            score=72.0, source="todo",
            action={"type": "todo", "todo_id": t.id}, sid=f"todo:{t.id}",
        ))
    due_today = (
        db.query(Todo)
        .filter(
            Todo.completed == False,  # noqa: E712
            Todo.status.notin_(["done", "cancelled", "scheduled"]),
            Todo.due_date == day,
        )
        .limit(5)
        .all()
    )
    for t in due_today:
        out.append(_s(
            "todo", t.title, subtitle="heute fällig",
            reason="heute fällig", energy=t.energy_required or "mittel",
            duration_min=t.estimated_minutes or DEFAULT_TODO_MINUTES,
            score=58.0, source="todo",
            action={"type": "todo", "todo_id": t.id}, sid=f"todo:{t.id}",
        ))
    return out


def build_suggestions(db: Session, day: date | None = None, now: datetime | None = None) -> dict:
    """Gerankte Vorschläge + optionale Entscheidungsfrage für einen Tag."""
    day = day or date.today()
    now = _now_berlin(now)
    capacity = compute_day_capacity(db, day)
    day_type = capacity["day_type"]

    free_minutes, next_free = _free_minutes_today(db, day, now)
    signals = cross_app.gather_signals()
    progress = build_progress(db)

    candidates: list[dict] = []

    # Immer relevant: Todos (fällig/überfällig) und Einkauf (Erledigung).
    candidates.extend(_todo_suggestions(db, day, capacity))
    einkauf = _einkauf_suggestion(signals)
    if einkauf:
        candidates.append(einkauf)

    # Diskretionäres nur, wenn überhaupt freie Zeit da ist.
    has_free = free_minutes >= 20
    if has_free:
        sport = _sport_suggestion(capacity, signals)
        if sport:
            candidates.append(sport)
        candidates.extend(_habit_suggestions(progress, capacity))
        candidates.append(_erholung_suggestion(capacity))
    elif capacity["level"] in ("geschont", "erschöpft"):
        # Kein freier Slot, aber angeschlagen → Erholung trotzdem anbieten.
        candidates.append(_erholung_suggestion(capacity))

    # Feedback-Lernen: „wie ging's mir danach?" + „passt die Tageszeit?" hebt/dämpft Vorschläge.
    _apply_feedback_adjustment(db, candidates, ref=day, now_hour=now.hour)

    # Energie-Filter: was die Tageskapazität nicht trägt, fliegt raus (nur mit Check-in
    # streng; ohne Check-in ist max_task_energy 'hoch' → nichts wird gefiltert).
    max_energy = capacity["max_task_energy"]
    filtered = [c for c in candidates if energy_fits(c["energy"], max_energy)]
    # Erholung nie wegfiltern (ist per Definition niedrig-Energie), Todos/Einkauf behalten.
    kept_ids = {c["id"] for c in filtered}
    for c in candidates:
        if c["id"] not in kept_ids and c["kind"] in ("erholung", "todo", "einkauf"):
            filtered.append(c)

    filtered.sort(key=lambda c: c["score"], reverse=True)

    # Entscheidungsmodus: konkurrieren >=2 diskretionäre Optionen eng um denselben Slot?
    choice = None
    if has_free:
        disc = [c for c in filtered if c["kind"] in _DISCRETIONARY]
        if len(disc) >= 2 and (disc[0]["score"] - disc[1]["score"]) <= 22:
            options = disc[:3]
            if capacity["level"] in ("geschont", "erschöpft"):
                prompt = "Anstrengender Tag, wozu hast du noch Kraft?"
            elif day_type in ("arbeit", "schule"):
                prompt = "Feierabend, wozu hast du heute Lust?"
            else:
                prompt = "Wozu hast du heute Lust?"
            choice = {
                "prompt": prompt,
                "context": capacity["reason"],
                "options": options,
            }

    return {
        "date": day.isoformat(),
        "generated_at": now.isoformat(),
        "day_type": day_type,
        "capacity": capacity,
        "free_minutes": free_minutes,
        "next_free": next_free.isoformat() if next_free else None,
        "signals_available": signals.get("available", False),
        "suggestions": filtered[:8],
        "choice": choice,
        "top": filtered[0] if filtered else None,
    }
