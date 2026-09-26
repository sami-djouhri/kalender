"""Kapazitäts-Modell, wie viel „Tag" heute realistisch ist.

Aus dem Morgen-Check-in (`DailyCheckIn`) + Day-Type + rollierendem Strain wird ein
**Kapazitäts-Level** abgeleitet, das die adaptive Planung und die Vorschläge steuert:

    geladen: viel Energie / Off-Day → körperliche Aktivität bevorzugt, höherer Cap
    normal: Standard (auch der Zustand OHNE Check-in → rückwärtskompatibel)
    geschont: müde / schlecht geschlafen → Tag nicht randvoll, nichts Hartes
    erschöpft: sehr müde / krank → nur das Nötigste, Erholung explizit vorschlagen

**Rückwärtskompatibilität ist Absicht:** ohne Check-in UND ohne hohen Strain ist das
Level `normal` mit factor 1.0 → die Planung verhält sich exakt wie vor dieser Epoche.
Erst ein expliziter Check-in (oder kritischer Strain) dämpft/hebt.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from backend.models import DailyCheckIn
from backend.routers.daytype import _get_daytype_display

BERLIN = ZoneInfo("Europe/Berlin")

_ENERGY_ORDER = {"niedrig": 0, "mittel": 1, "hoch": 2}

# Diskretionäre Basis-Minuten je Day-Type (Zeit für Hobbys/Todos NACH den Pflichten).
_BASE_DISCRETIONARY_MINUTES = {
    "arbeit": 150,
    "schule": 180,
    "frei": 300,
    "wochenende": 360,
    "urlaub": 360,
    "feiertag": 360,
    "krank": 60,
}

_LEVEL_FACTOR = {"geladen": 1.15, "normal": 1.0, "geschont": 0.6, "erschöpft": 0.35}
_LEVEL_MAX_ENERGY = {"geladen": "hoch", "normal": "hoch", "geschont": "mittel", "erschöpft": "niedrig"}


def get_checkin(db: Session, day: date) -> DailyCheckIn | None:
    return db.query(DailyCheckIn).filter(DailyCheckIn.date == day).first()


def upsert_checkin(db: Session, day: date, **fields) -> DailyCheckIn:
    """Check-in für einen Tag anlegen/aktualisieren (nur gesetzte Felder ändern)."""
    row = get_checkin(db, day)
    if row is None:
        row = DailyCheckIn(date=day)
        db.add(row)
    for key in ("sleep_quality", "energy", "mood", "physical_ready", "note", "source"):
        if key in fields and fields[key] is not None:
            setattr(row, key, fields[key])
    db.commit()
    db.refresh(row)
    return row


def _strain(db: Session, day: date) -> float:
    """Rollierender Strain (lazy import → kein Zyklus scheduler↔daily_energy)."""
    try:
        from backend.scheduler import calculate_strain
        return calculate_strain(db, day)
    except Exception:
        return 0.0


def _strain_level(strain: float) -> str:
    if strain < 0.3:
        return "niedrig"
    if strain < 0.5:
        return "mittel"
    if strain < 0.7:
        return "hoch"
    return "kritisch"


def compute_day_capacity(db: Session, day: date, now: datetime | None = None) -> dict:
    """Kapazitäts-Objekt für einen Tag. Deterministisch, kein LLM.

    Score 0..100 aus Day-Type-Basis + Check-in-Signalen − Strain → Level → factor/caps.
    """
    day_type = _get_daytype_display(db, day)
    checkin = get_checkin(db, day)
    strain = _strain(db, day)

    is_offday = day_type in ("frei", "wochenende", "urlaub", "feiertag")

    # 1. Day-Type-Basis (diskretionäre Kapazität nach den Pflichten)
    base = {
        "arbeit": 45, "schule": 50, "krank": 15,
        "frei": 70, "wochenende": 72, "urlaub": 72, "feiertag": 72,
    }.get(day_type, 60)
    score = float(base)
    reasons: list[str] = []

    # 2. Check-in-Signale
    physical_blocked = False
    if checkin:
        if checkin.energy == "hoch":
            score += 25; reasons.append("viel Energie")
        elif checkin.energy == "niedrig":
            score -= 30; reasons.append("wenig Energie")
        if checkin.sleep_quality == "gut":
            score += 10; reasons.append("gut geschlafen")
        elif checkin.sleep_quality == "schlecht":
            score -= 20; reasons.append("schlecht geschlafen")
        if checkin.mood == "gut":
            score += 5
        elif checkin.mood == "mies":
            score -= 10; reasons.append("gedrückte Stimmung")
        if checkin.physical_ready is True:
            score += 5
        elif checkin.physical_ready is False:
            score -= 15
            physical_blocked = True
            reasons.append("körperlich angeschlagen")

    # 3. Strain dämpft (0..1 → 0..30 Punkte Abzug)
    if strain > 0.3:
        score -= (strain - 0.3) * 42
        if strain >= 0.7:
            reasons.append("hohe Belastung der letzten Tage")

    score = max(0.0, min(100.0, score))

    # 4. Level
    if score >= 75:
        level = "geladen"
    elif score >= 50:
        level = "normal"
    elif score >= 25:
        level = "geschont"
    else:
        level = "erschöpft"

    factor = _LEVEL_FACTOR[level]
    max_task_energy = _LEVEL_MAX_ENERGY[level]

    # 5. Körperliche Aktivität erlaubt?
    allow_physical = (
        day_type != "krank"
        and level in ("geladen", "normal")
        and not physical_blocked
    )
    # Off-Days mit guter Kapazität sind DER Platz für Anstrengendes (Owner-Wunsch:
    # körperlich Fordendes lieber an arbeitsfreien Tagen).
    prefer_physical = is_offday and level in ("geladen", "normal") and not physical_blocked

    cap_minutes = int(_BASE_DISCRETIONARY_MINUTES.get(day_type, 240) * factor)

    if not reasons:
        reasons.append("normaler Tag" if not checkin else "ausgeglichen")

    return {
        "date": day.isoformat(),
        "day_type": day_type,
        "level": level,
        "score": round(score),
        "factor": factor,
        "cap_minutes": cap_minutes,
        "allow_physical": allow_physical,
        "prefer_physical": prefer_physical,
        "max_task_energy": max_task_energy,
        "has_checkin": checkin is not None,
        "strain": round(strain, 2),
        "strain_level": _strain_level(strain),
        "reason": ", ".join(reasons),
        "checkin": None if not checkin else {
            "sleep_quality": checkin.sleep_quality,
            "energy": checkin.energy,
            "mood": checkin.mood,
            "physical_ready": checkin.physical_ready,
            "note": checkin.note,
        },
    }


def energy_fits(task_energy: str | None, max_task_energy: str) -> bool:
    """Passt eine Aufgaben-Energie (hoch/mittel/niedrig) in die Tageskapazität?"""
    if not task_energy:
        return True  # ohne Angabe immer planbar (wie bisher)
    return _ENERGY_ORDER.get(task_energy, 1) <= _ENERGY_ORDER.get(max_task_energy, 2)
