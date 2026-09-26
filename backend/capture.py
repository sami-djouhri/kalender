"""Quick-Capture: Freitext zu Termin/Aufgabe.

"Zahnarzt morgen 15 Uhr" -> {type: event, title: Zahnarzt, date: <morgen>, start: 15:00}

Zweistufig und privacy-first:
1. **Regelbasiert** (`parse_capture`): deutsches Datum/Zeit/Dauer-Parsing, rein lokal,
   deterministisch, keine externen Calls. Deckt den Alltag ab.
2. **LLM-Fallback** (`llm_parse`, optional): nur wenn die Regeln kein Datum/keine
   Zeit finden UND ein LLM-Gateway konfiguriert ist. Läuft über den lokalen
   node2-Gemma (Homelab): nichts verlässt das Netz. Fällt still auf das
   Regelergebnis zurück, wenn nicht konfiguriert/erreichbar.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from urllib import error, request
from zoneinfo import ZoneInfo

from backend.config import settings

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")

_WEEKDAYS = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonnabend": 5, "sonntag": 6,
}
_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
}
# Schlüsselwörter, die klar auf eine Aufgabe (statt Termin) deuten.
_TODO_HINTS = re.compile(
    r"\b(erinner\w*|aufgabe|todo|to-do|besorg\w*|kauf\w*|nicht vergessen|einkauf\w*|anrufen|mail\w*|schreib\w*)\b",
    re.IGNORECASE,
)
# Füllwörter, die nach dem Entfernen der Zeit-/Datums-Tokens noch am Titel kleben.
_FILLER = re.compile(
    r"^(um|am|den|der|die|das|einen|eine|ein|zum|zur|beim|bei|termin|erinnere?\s+mich\s+an|erinner\w*\s+an|erinner\w*|ich\s+muss|noch|bitte)\b[\s:,-]*",
    re.IGNORECASE,
)


@dataclass
class Capture:
    type: str            # "event" | "todo"
    title: str
    date: str | None = None        # ISO YYYY-MM-DD
    start_time: str | None = None  # HH:MM
    end_time: str | None = None    # HH:MM
    confidence: str = "niedrig"    # niedrig | mittel | hoch
    source: str = "rules"          # rules | llm

    def to_dict(self) -> dict:
        return asdict(self)


def _next_weekday(base: date, target_wd: int) -> date:
    """Nächstes Vorkommen eines Wochentags ab base (inkl. base selbst)."""
    delta = (target_wd - base.weekday()) % 7
    return base + timedelta(days=delta)


def _resolve_year(day: int, month: int, base: date) -> date:
    """Datum mit fehlendem Jahr: nimm dieses Jahr, sonst nächstes (nie Vergangenheit)."""
    try:
        candidate = date(base.year, month, day)
    except ValueError:
        return base  # ungültig -> fallback base (wird selten erreicht)
    if candidate < base:
        try:
            candidate = date(base.year + 1, month, day)
        except ValueError:
            pass
    return candidate


class _Extracted:
    """Sammelt gematchte Spans, um sie später aus dem Titel zu schneiden."""
    def __init__(self):
        self.spans: list[tuple[int, int]] = []

    def add(self, m: re.Match):
        self.spans.append((m.start(), m.end()))

    def strip(self, text: str) -> str:
        if not self.spans:
            out = text
        else:
            keep = []
            last = 0
            for s, e in sorted(self.spans):
                if s < last:
                    last = max(last, e)
                    continue
                keep.append(text[last:s])
                last = e
            keep.append(text[last:])
            out = " ".join(keep)
        out = re.sub(r"\s+", " ", out).strip(" ,;:-")
        # Führende Füllwörter iterativ entfernen
        while True:
            new = _FILLER.sub("", out).strip(" ,;:-")
            if new == out:
                break
            out = new
        return out.strip(" ,;:-")


def _parse_time_str(text: str, ex: _Extracted) -> str | None:
    """Findet die erste Zeitangabe. Gibt HH:MM zurück, markiert den Span."""
    # halb X (= X-1:30)
    m = re.search(r"\bhalb\s+(\d{1,2})\b", text, re.IGNORECASE)
    if m:
        h = int(m.group(1))
        hh = (h - 1) % 24
        ex.add(m)
        return f"{hh:02d}:30"
    # 15:30 / 15.30 Uhr
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(uhr|h)?\b", text, re.IGNORECASE)
    if m and 0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59:
        ex.add(m)
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    # "15 Uhr", "15h"
    m = re.search(r"\b(?:um\s+)?(\d{1,2})\s*(?:uhr|h)\b", text, re.IGNORECASE)
    if m and 0 <= int(m.group(1)) <= 23:
        ex.add(m)
        return f"{int(m.group(1)):02d}:00"
    # "um 14" (ohne Uhr-Suffix): negatives Lookahead schützt vor Datum (um 14.) / Zeit (um 14:30)
    m = re.search(r"\bum\s+(\d{1,2})(?![:.\d])", text, re.IGNORECASE)
    if m and 0 <= int(m.group(1)) <= 23:
        ex.add(m)
        return f"{int(m.group(1)):02d}:00"
    return None


def _parse_date_str(text: str, base: date, ex: _Extracted) -> str | None:
    """Findet die erste Datumsangabe. Gibt ISO-Date zurück, markiert den Span."""
    low = text.lower()
    # relative Tage
    for word, offset in (("übermorgen", 2), ("uebermorgen", 2), ("morgen", 1), ("heute", 0)):
        m = re.search(rf"\b{word}\b", low)
        if m:
            ex.add(m)
            return (base + timedelta(days=offset)).isoformat()
    # dd.mm.(yyyy), kein trailing \b (scheitert sonst am Schlusspunkt "24.12.")
    m = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{2,4})?", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if 1 <= day <= 31 and 1 <= month <= 12:
            ex.add(m)
            if m.group(3):
                year = int(m.group(3))
                if year < 100:
                    year += 2000
                try:
                    return date(year, month, day).isoformat()
                except ValueError:
                    return None
            return _resolve_year(day, month, base).isoformat()
    # "15. märz" / "15 maerz"
    m = re.search(r"\b(\d{1,2})\.?\s+(" + "|".join(_MONTHS) + r")\b", low)
    if m:
        day = int(m.group(1))
        month = _MONTHS[m.group(2)]
        if 1 <= day <= 31:
            ex.add(m)
            return _resolve_year(day, month, base).isoformat()
    # Wochentag
    for name, wd in _WEEKDAYS.items():
        m = re.search(rf"\b(?:am\s+|nächsten\s+|naechsten\s+)?{name}\b", low)
        if m:
            ex.add(m)
            return _next_weekday(base, wd).isoformat()
    return None


def _parse_duration_minutes(text: str, ex: _Extracted) -> int | None:
    m = re.search(r"\b(?:für\s+|fuer\s+)?(\d+)\s*(min\w*|std|stunden?|h)\b", text, re.IGNORECASE)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2).lower()
    ex.add(m)
    if unit.startswith("min"):
        return val
    return val * 60  # Stunden


def parse_capture(text: str, now: datetime | None = None) -> Capture:
    """Regelbasiertes Parsen eines Freitexts zu Termin/Aufgabe (rein lokal)."""
    now = now or datetime.now(BERLIN)
    base = now.date()
    raw = (text or "").strip()
    ex = _Extracted()

    start_time = _parse_time_str(raw, ex)
    date_iso = _parse_date_str(raw, base, ex)
    dur = _parse_duration_minutes(raw, ex)

    end_time = None
    if start_time and dur:
        h, m = map(int, start_time.split(":"))
        end_dt = datetime.combine(base, time(h, m)) + timedelta(minutes=dur)
        end_time = end_dt.strftime("%H:%M")

    title = ex.strip(raw) or raw

    # Typ-Heuristik: explizite Aufgaben-Hinweise gewinnen; sonst entscheidet die Zeit.
    is_todo = bool(_TODO_HINTS.search(raw))
    if start_time and not is_todo:
        typ = "event"
    else:
        typ = "todo"

    # Konfidenz
    if start_time and date_iso:
        conf = "hoch"
    elif start_time or date_iso:
        conf = "mittel"
    else:
        conf = "niedrig"

    # Ein Termin ohne Datum ist auf heute zu beziehen.
    if typ == "event" and not date_iso:
        date_iso = base.isoformat()

    return Capture(
        type=typ, title=title, date=date_iso,
        start_time=start_time, end_time=end_time,
        confidence=conf, source="rules",
    )


# --- Optionaler LLM-Fallback (lokaler Gemma über Gateway) ---

_LLM_SYSTEM = (
    "Du extrahierst aus einem deutschen Freitext genau einen Kalendereintrag. "
    "Antworte NUR mit JSON: {\"type\":\"event|todo\",\"title\":\"...\","
    "\"date\":\"YYYY-MM-DD oder null\",\"start_time\":\"HH:MM oder null\","
    "\"end_time\":\"HH:MM oder null\"}. Kein Fließtext, keine Erklärung."
)


def _llm_configured() -> bool:
    return bool(getattr(settings, "LLM_GATEWAY_URL", "") and getattr(settings, "CAPTURE_LLM_ENABLED", False))


def llm_parse(text: str, now: datetime | None = None) -> Capture | None:
    """Fragt den lokalen Gemma nach strukturierter Extraktion. None bei Fehler/aus."""
    if not _llm_configured():
        return None
    now = now or datetime.now(BERLIN)
    body = {
        "model": getattr(settings, "LLM_MODEL", "gemma"),
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": f"Heute ist {now.date().isoformat()} ({now.strftime('%A')}). Text: {text}"},
        ],
        "temperature": 0,
        "max_tokens": 200,
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
        parsed = _extract_json(content)
        if not parsed:
            return None
        return Capture(
            type=parsed.get("type") if parsed.get("type") in ("event", "todo") else "todo",
            title=(parsed.get("title") or text).strip(),
            date=parsed.get("date") or None,
            start_time=parsed.get("start_time") or None,
            end_time=parsed.get("end_time") or None,
            confidence="mittel",
            source="llm",
        )
    except (error.URLError, TimeoutError, OSError, KeyError, ValueError, json.JSONDecodeError):
        logger.info("LLM-Capture-Fallback nicht verfügbar/fehlgeschlagen", exc_info=True)
        return None


def _extract_json(content: str) -> dict | None:
    """Robustes JSON aus einer LLM-Antwort ziehen (auch wenn drumherum Text steht)."""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def capture(text: str, now: datetime | None = None, allow_llm: bool = True) -> Capture:
    """Parst Freitext regelbasiert; nutzt den LLM-Fallback nur bei niedriger Konfidenz."""
    result = parse_capture(text, now=now)
    if result.confidence == "niedrig" and allow_llm and _llm_configured():
        llm = llm_parse(text, now=now)
        if llm and (llm.date or llm.start_time):
            return llm
    return result
