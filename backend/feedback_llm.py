"""LLM-Auswertung von Aktivitäts-Randnotizen (opt-in slow-lane, rein lokal).

Hinterlässt der Owner nach einer Aktivität eine Freitext-Notiz („war platt, zu spät
gegessen" / „Kapitel 3 war zäh, nochmal wiederholen"), wertet ein lokales LLM sie
**asynchron** aus: knappe Zusammenfassung, Stimmung und optionale Vertiefungs-Aufgaben.

Design (bewusst konservativ):
* **Opt-in**: läuft nur bei `FEEDBACK_LLM_ENABLED` + konfiguriertem `LLM_GATEWAY_URL`
  (lokaler node2-Gemma). Sonst No-op, das strukturierte Feedback (energy/satisfaction)
  funktioniert komplett unabhängig weiter. Nichts verlässt das Heimnetz.
* **Nicht blockierend**: die Verarbeitung läuft als Batch im 60s-Runner, nicht im
  `POST /api/feedback` → der Request bleibt schnell.
* **Idempotent**: `llm_processed_at` markiert erledigte Notizen; kein Doppel-Lauf.
* **Fail-soft**: Netz-/Gateway-Fehler → Notiz bleibt unverarbeitet (späterer Retry);
  „LLM lieferte Müll" → als verarbeitet markiert (leeres Ergebnis), kein Endlos-Retry.

Wiederverwendet bewusst das erprobte LLM-Muster aus `capture.py` (urllib→Gateway,
`/v1/chat/completions`, robustes JSON-Extrahieren).
"""

import json
import logging
import re
from datetime import datetime
from urllib import error, request
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import ActivityFeedback

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")

# Kein f-string → die escaped ASCII-Quotes im JSON-Beispiel sind unkritisch (vgl. capture.py).
_SYSTEM = (
    "Du wertest die kurze Randnotiz zu einer gerade erledigten Aktivität aus. "
    "Antworte NUR mit JSON in genau dieser Form: "
    "{\"summary\":\"ein knapper Satz\",\"sentiment\":\"positiv|neutral|negativ\","
    "\"followups\":[\"kurze konkrete Aufgabe\"]}. "
    "followups NUR bei klarem Bedarf (hoechstens 3), sonst leere Liste. "
    "Kein Fliesstext, keine Erklaerung, nur das JSON."
)

_SENTIMENTS = {"positiv", "neutral", "negativ"}


def _configured() -> bool:
    return bool(getattr(settings, "LLM_GATEWAY_URL", "") and getattr(settings, "FEEDBACK_LLM_ENABLED", False))


def _extract_json(content: str) -> dict | None:
    """Robustes JSON aus einer LLM-Antwort ziehen (auch mit Text drumherum)."""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _call_llm(note: str, activity_type: str | None, title: str | None) -> dict | None:
    """Fragt das lokale LLM. Rückgabe: geparstes dict (evtl. leer `{}`), wenn der Call
    durchkam; `None` NUR bei Netz-/HTTP-Fehler (→ später erneut versuchen)."""
    if not _configured():
        return None
    context = f"Aktivität: {title or activity_type or 'unbekannt'}. Notiz: {note}"
    body = {
        "model": getattr(settings, "LLM_MODEL", "gemma"),
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": context},
        ],
        "temperature": 0,
        "max_tokens": 220,
    }
    headers = {"Content-Type": "application/json"}
    if getattr(settings, "LLM_API_KEY", ""):
        headers["Authorization"] = f"Bearer {settings.LLM_API_KEY}"
    url = settings.LLM_GATEWAY_URL.rstrip("/") + "/v1/chat/completions"
    try:
        req = request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with request.urlopen(req, timeout=getattr(settings, "LLM_TIMEOUT", 60)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        return _extract_json(content) or {}  # {} = durchgekommen, aber kein brauchbares JSON
    except (error.URLError, TimeoutError, OSError, KeyError, ValueError, json.JSONDecodeError):
        logger.info("Feedback-LLM nicht verfügbar/fehlgeschlagen", exc_info=True)
        return None


def apply_llm_result(db: Session, fb: ActivityFeedback, parsed: dict) -> list[str]:
    """Übernimmt ein LLM-Ergebnis in die Feedback-Zeile (+ optional Vertiefungs-Todos).

    Setzt IMMER `llm_processed_at` (auch bei leerem `parsed`) → Idempotenz. Gibt die
    bereinigten Follow-up-Texte zurück (für Tests/Logging). Committet NICHT selbst.
    """
    summary = parsed.get("summary")
    fb.llm_summary = summary.strip()[:500] if isinstance(summary, str) and summary.strip() else None
    sentiment = parsed.get("sentiment")
    fb.llm_sentiment = sentiment if sentiment in _SENTIMENTS else None
    raw = parsed.get("followups")
    followups = [str(f).strip()[:200] for f in raw if str(f).strip()][:3] if isinstance(raw, list) else []
    fb.llm_followups = json.dumps(followups, ensure_ascii=False) if followups else None
    fb.llm_processed_at = datetime.now(BERLIN)

    # Vertiefungs-Todos nur hinter separatem Flag (sonst flutet der Backlog).
    if followups and getattr(settings, "FEEDBACK_LLM_FOLLOWUP_TODOS", False):
        from backend.models import Todo

        for text in followups:
            db.add(Todo(
                title=text,
                owner_sub=fb.owner_sub,
                scheduling_mode="pool",
                priority="niedrig",
                estimated_minutes=20,
            ))
    return followups


def process_pending_feedback_notes(limit: int = 5) -> int:
    """Verarbeitet offene Notizen (`note` gesetzt, `llm_processed_at` NULL) als Batch.

    Läuft owner-gescopt im 60s-Runner, gedrosselt (`limit` pro Tick). No-op wenn nicht
    konfiguriert. Gibt die Zahl verarbeiteter Notizen zurück. Bricht bei einem
    Gateway-/Netzfehler ab (Retry im nächsten Tick), verarbeitet sonst der Reihe nach.
    """
    if not _configured():
        return 0
    from backend.database import SessionLocal

    db = SessionLocal()
    db.info["owner_sub"] = settings.DEFAULT_OWNER_SUB
    processed = 0
    try:
        pending = (
            db.query(ActivityFeedback)
            .filter(
                ActivityFeedback.note.isnot(None),
                ActivityFeedback.note != "",
                ActivityFeedback.llm_processed_at.is_(None),
            )
            .order_by(ActivityFeedback.created_at.desc())
            .limit(limit)
            .all()
        )
        for fb in pending:
            parsed = _call_llm(fb.note, fb.activity_type, fb.title)
            if parsed is None:
                break  # Gateway-/Netzfehler → nächster Tick erneut
            apply_llm_result(db, fb, parsed)
            db.commit()
            processed += 1
    except Exception:
        logger.exception("Feedback-LLM-Batch fehlgeschlagen")
        db.rollback()
    finally:
        db.close()
    return processed
