"""Präferenz-bewusste Zeitplanung: „wann soll ich das am besten machen?".

Der smarte Kern der Vision: findet für eine flexible Aktivität den besten freien Slot
der nächsten Tage, gewichtet nach **gelernter Zeit-Präferenz** (`preferences.time_fit`)
+ **Tageskapazität** (`daily_energy`) + Tagestyp. Read-only: schlägt nur vor, plant
nichts fest (keine CORE-Mutation). Deterministisch und rückwärtskompatibel: ohne
Feedback fällt es sauber auf Kapazität/Wachfenster zurück.

Wiederverwendet bewusst die bestehende, geprüfte Slot-Findung `scheduler.compute_free_slots`
(zieht Events/Sessions/Ziele/geplante Todos + vergangene Tageszeit ab; Slots sind
Berlin-aware) statt sie zu duplizieren.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from backend.daily_energy import compute_day_capacity
from backend.preferences import bucket_for_hour, time_fit
from backend.routers.daytype import _get_daytype_display
from backend.scheduler import _ensure_aware, compute_free_slots

BERLIN = ZoneInfo("Europe/Berlin")

# Kapazitäts-Level → Slot-Bonus (geladene/normale Tage bevorzugt, angeschlagene meiden).
_LEVEL_BONUS = {"geladen": 0.5, "normal": 0.2, "geschont": -0.3, "erschöpft": -0.8}


def suggest_best_time(
    db: Session,
    activity_type: str,
    from_day: date | None = None,
    horizon_days: int = 7,
    duration_min: int = 60,
    now: datetime | None = None,
) -> dict | None:
    """Bester freier Slot für eine flexible Aktivität in den nächsten `horizon_days` Tagen.

    Rückgabe: `{day, start, hour, bucket, score, day_type, reason, preference_score}`
    oder None, wenn nirgends genug Platz ist. Der Slot ist ein **Vorschlag**, der Owner
    entscheidet; nichts wird verbindlich gebucht.
    """
    if not activity_type:
        return None
    from_day = from_day or date.today()
    now = now or datetime.now(BERLIN)
    min_slot = max(15, min(duration_min, 30))

    best: dict | None = None
    for offset in range(max(1, horizon_days)):
        day = from_day + timedelta(days=offset)
        day_type = _get_daytype_display(db, day)
        if day_type == "krank":
            continue
        capacity = compute_day_capacity(db, day, now=now)

        for s, e in compute_free_slots(db, day, min_minutes=min_slot):
            s = _ensure_aware(s).astimezone(BERLIN)
            e = _ensure_aware(e).astimezone(BERLIN)
            if (e - s).total_seconds() / 60 < min_slot:
                continue
            hour = s.hour
            bucket = bucket_for_hour(hour)
            pref = time_fit(db, activity_type, hour, ref=day)  # None = keine Daten

            score = 0.0
            reasons: list[str] = []
            if pref is not None:
                score += pref * 2.0
                if pref >= 0.4:
                    reasons.append(f"{bucket} läuft dir gut")
                elif pref <= -0.4:
                    reasons.append(f"{bucket} ist bei dir eher schwach")

            score += _LEVEL_BONUS.get(capacity["level"], 0.0)

            # Körperlich Forderndes (Sport) lieber an Tagen mit Kraft dafür.
            if activity_type == "sport":
                if capacity.get("prefer_physical"):
                    score += 0.6
                    reasons.append("guter Tag für Bewegung")
                elif not capacity.get("allow_physical"):
                    score -= 0.8

            # Kleiner „früher ist besser"-Tiebreak, damit es nicht ewig aufschiebt.
            score -= offset * 0.05

            cand = {
                "activity_type": activity_type,
                "day": day.isoformat(),
                "start": s.isoformat(),
                "hour": hour,
                "bucket": bucket,
                "day_type": day_type,
                "preference_score": pref,
                "score": round(score, 3),
                "reason": ", ".join(reasons) or "freier Slot",
            }
            if best is None or cand["score"] > best["score"]:
                best = cand

    return best
