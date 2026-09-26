"""Präferenz-Lernen: „wann fühlt sich welche Aktivität am besten an?".

Aggregiert `ActivityFeedback` zu einem **deterministischen** Präferenz-Score je
`(activity_type × Tageszeit-Bucket)`, **exponentiell nach Alter gewichtet** (Halbwertszeit
~4 Wochen), damit sich ändernde Gewohnheiten nachziehen und die Präferenz nicht verharrt.
Basis für sichtbare Insights („Sport läuft morgens am besten") + präferenz-bewusste
Vorschläge. Kein LLM.

Zeitkonvention: `ActivityFeedback.scheduled_start` ist naive Berlin-Wanduhr (wie im
Feedback-Upsert gesetzt) → `.hour` ist direkt die Berlin-Stunde.
"""

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from backend.models import ActivityFeedback

HALF_LIFE_DAYS = 28.0
MIN_SAMPLES = 2  # unter so wenig Datenpunkten je Bucket keine Aussage (Rausch-Schutz)

_ENERGY_SCORE = {"energetisiert": 1.0, "ok": 0.0, "erschöpft": -1.0}
_SATISFACTION_SCORE = {"gut": 0.5, "mittel": 0.0, "schlecht": -0.5}

# Tageszeit-Buckets (Berlin-Wanduhr-Stunde) → Label.
_BUCKETS = [
    (5, 11, "morgens"),
    (11, 14, "mittags"),
    (14, 18, "nachmittags"),
    (18, 24, "abends"),
]

_TYPE_LABEL = {
    "lernen": "Lernen",
    "sport": "Sport",
    "lesen": "Lesen",
    "hobby": "Hobby",
    "sonstige": "Aktivität",
}


def bucket_for_hour(hour: int) -> str:
    for lo, hi, label in _BUCKETS:
        if lo <= hour < hi:
            return label
    return "nachts"


def _feedback_value(fb: ActivityFeedback) -> float | None:
    """Wohlgefühl-Wert einer Feedback-Zeile (energy + satisfaction), oder None ohne Signal."""
    parts = []
    if fb.energy_after in _ENERGY_SCORE:
        parts.append(_ENERGY_SCORE[fb.energy_after])
    if fb.satisfaction in _SATISFACTION_SCORE:
        parts.append(_SATISFACTION_SCORE[fb.satisfaction])
    if not parts:
        return None
    return sum(parts)


def _hour_of(fb: ActivityFeedback) -> int | None:
    if fb.scheduled_start is None:
        return None
    return fb.scheduled_start.hour


def compute_preferences(
    db: Session,
    activity_types: list[str] | None = None,
    ref: date | None = None,
    lookback_days: int = 120,
) -> dict:
    """Präferenz-Aggregat je activity_type → {buckets: {bucket: {score, count}}, count}.

    Exponentiell alters-gewichtet. Nur `took_place=True` zählt (wie war's, wenn es lief).
    """
    ref = ref or date.today()
    since = ref - timedelta(days=lookback_days)
    q = db.query(ActivityFeedback).filter(
        ActivityFeedback.took_place == True,  # noqa: E712
        ActivityFeedback.occurrence_date >= since,
        ActivityFeedback.activity_type.isnot(None),
    )
    if activity_types:
        q = q.filter(ActivityFeedback.activity_type.in_(activity_types))

    # {activity_type: {bucket: [sum_w_val, sum_w, count]}}
    acc: dict[str, dict[str, list]] = {}
    for fb in q.all():
        val = _feedback_value(fb)
        hour = _hour_of(fb)
        if val is None or hour is None:
            continue
        age = max(0, (ref - fb.occurrence_date).days)
        w = 0.5 ** (age / HALF_LIFE_DAYS)
        bucket = bucket_for_hour(hour)
        b = acc.setdefault(fb.activity_type, {}).setdefault(bucket, [0.0, 0.0, 0])
        b[0] += w * val
        b[1] += w
        b[2] += 1

    out: dict[str, dict] = {}
    for at, buckets in acc.items():
        bucket_scores = {}
        total = 0
        for bucket, (sw, w, count) in buckets.items():
            bucket_scores[bucket] = {"score": round(sw / w, 3) if w else 0.0, "count": count}
            total += count
        out[at] = {"buckets": bucket_scores, "count": total}
    return out


def time_fit(
    db: Session, activity_type: str, at_hour: int, ref: date | None = None
) -> float | None:
    """Präferenz-Score für activity_type zur Stunde `at_hour` → float [-1.5..1.5] oder None.

    None = zu wenig Daten für diesen Bucket → kein Einfluss (rückwärtskompatibel).
    """
    if not activity_type:
        return None
    data = compute_preferences(db, activity_types=[activity_type], ref=ref).get(activity_type)
    if not data:
        return None
    b = data["buckets"].get(bucket_for_hour(at_hour))
    if not b or b["count"] < MIN_SAMPLES:
        return None
    return b["score"]


def generic_time_curve(db: Session, ref: date | None = None) -> dict[str, float]:
    """Genereller „wann bin ich gut drauf?"-Score je Tageszeit-Bucket über ALLE Aktivitäten.

    Mittelt die gelernten bucket-Scores count-gewichtet. Für Aufgaben (Todos), die keinen
    `activity_type` tragen, ist das der beste verfügbare Tageszeit-Proxy. Buckets mit zu
    wenig Daten fehlen im Ergebnis → der Planer fällt dort sauber auf eine Heuristik zurück.
    Leeres dict = noch keine Präferenzdaten (rückwärtskompatibel).
    """
    prefs = compute_preferences(db, ref=ref)
    agg: dict[str, list] = {}  # bucket -> [sum(score*count), sum(count)]
    for data in prefs.values():
        for bucket, b in data["buckets"].items():
            if b["count"] < MIN_SAMPLES:
                continue
            a = agg.setdefault(bucket, [0.0, 0.0])
            a[0] += b["score"] * b["count"]
            a[1] += b["count"]
    return {bucket: round(s / c, 3) for bucket, (s, c) in agg.items() if c > 0}


def build_insights(db: Session, ref: date | None = None) -> list[dict]:
    """Menschenlesbare Erkenntnisse je Aktivität (beste/schlechteste Tageszeit).

    Gibt nur Aussagen zurück, die auf genug Datenpunkten beruhen UND eine erkennbare
    Präferenz zeigen, sonst lieber schweigen als raten.
    """
    prefs = compute_preferences(db, ref=ref)
    insights: list[dict] = []
    for at, data in prefs.items():
        buckets = {b: v for b, v in data["buckets"].items() if v["count"] >= MIN_SAMPLES}
        if not buckets:
            continue
        best = max(buckets.items(), key=lambda kv: kv[1]["score"])
        worst = min(buckets.items(), key=lambda kv: kv[1]["score"])
        label = _TYPE_LABEL.get(at, at)
        text = None
        if best[1]["score"] >= 0.4:
            text = f"{label} läuft {best[0]} bei dir am besten."
        if worst[0] != best[0] and worst[1]["score"] <= -0.4:
            neg = f"{label} {worst[0]} macht dich oft platt."
            text = f"{text} {neg}" if text else neg
        if not text:
            continue
        insights.append({
            "activity_type": at,
            "label": label,
            "best_bucket": best[0],
            "best_score": best[1]["score"],
            "worst_bucket": worst[0],
            "worst_score": worst[1]["score"],
            "count": data["count"],
            "text": text,
        })
    insights.sort(key=lambda i: -i["count"])
    return insights
