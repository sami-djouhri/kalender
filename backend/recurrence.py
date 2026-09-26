"""Event-Recurrence-Expansion.

`Event.recurrence_rule` speichert eine iCal-RRULE relativ zu `Event.start`
(z.B. ``FREQ=WEEKLY;BYDAY=MO,WE``). Diese Datei expandiert eine wiederkehrende
Basis-Event-Reihe in konkrete virtuelle Instanzen innerhalb eines Zeitfensters.

- Der iCal-Feed (`routers/ical.py`) expandiert NICHT, er reicht RRULE+EXDATE
  durch, abonnierende Clients (HA/Apple/Google) expandieren selbst.
- Die JSON-API (`/api/events`) rendert roh im Frontend und expandiert daher hier.

Instanz-IDs: ``{base_id}::{YYYY-MM-DD}``. `series_id` zeigt auf die Basis-Reihe,
`is_recurring_instance` markiert generierte Instanzen. Einzeltermin-Ausnahmen
(gelöscht/verschoben) liegen als CSV-ISO-Dates in `Event.recurrence_exdates`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from dateutil.rrule import rrulestr

from backend.models import Event
from backend.wanduhr import als_wanduhr


def _rechenzeit(dt: datetime) -> datetime:
    """Bringt einen Zeitpunkt in den Rechenraum der Wiederholungs-Expansion.

    Der Rechenraum ist die **Berliner Wanduhr, formal als UTC etikettiert**. Das
    klingt nach einer Notluege und ist eine bewusste Entscheidung:

    * ``dateutil`` verlangt, dass ``dtstart``, ``UNTIL`` und die ``between()``-
      Grenzen alle zonenlos **oder** alle zonenbehaftet sind, sonst wirft es
      ``ValueError``. Da ``set_until`` das RFC-Format mit ``Z`` schreibt, muessen
      alle drei zonenbehaftet sein.
    * Gerechnet werden muss trotzdem in Wanduhrzeit, sonst wandert eine
      woechentliche Reihe zweimal im Jahr um eine Stunde (siehe backend/wanduhr.py).

    Etikett UTC + Werte in Wanduhr erfuellt beides. Entscheidend ist nur, dass
    **jeder** Wert durch diese Funktion geht. Vorher tat das nur, was schon
    zonenlos hereinkam: eine zonenbehaftete Fenstergrenze (das Frontend schickt
    ``…T22:00:00.000Z``) wurde echt nach UTC gerechnet und lag damit um den
    Versatz neben den Termindaten.
    """
    return als_wanduhr(dt).replace(tzinfo=timezone.utc)


def parse_exdates(raw: str | None) -> set[str]:
    """CSV von ISO-Dates → Menge von 'YYYY-MM-DD'-Strings (leer-tolerant)."""
    if not raw:
        return set()
    return {part.strip()[:10] for part in raw.split(",") if part.strip()}


def add_exdate(raw: str | None, iso_date: str) -> str:
    """Fügt ein EXDATE additiv hinzu (sortiert, dedupliziert)."""
    dates = parse_exdates(raw)
    dates.add(iso_date[:10])
    return ",".join(sorted(dates))


def set_until(rule_str: str, until_dt: datetime) -> str:
    """Setzt/ersetzt UNTIL in einer RRULE (UTC-Basic-Format), für 'diese & folgende'."""
    until_utc = _rechenzeit(until_dt).strftime("%Y%m%dT%H%M%SZ")
    parts = [p for p in rule_str.split(";") if p and not p.upper().startswith(("UNTIL=", "COUNT="))]
    parts.append(f"UNTIL={until_utc}")
    return ";".join(parts)


def _match_naiveness(occ: datetime, reference: datetime) -> datetime:
    """Bringt eine rrule-Occurrence auf dieselbe tz-Konvention wie das Basis-Event.

    Die DB haelt Berliner Wanduhr ohne Zone (backend/wanduhr.py); rrule liefert
    dieselben Werte mit UTC-Etikett zurueck (siehe `_rechenzeit`). Hier faellt das
    Etikett wieder ab, damit die Instanz aussieht wie ein gespeichertes Event.
    """
    if reference.tzinfo is None and occ.tzinfo is not None:
        return occ.astimezone(timezone.utc).replace(tzinfo=None)
    if reference.tzinfo is not None and occ.tzinfo is None:
        return occ.replace(tzinfo=reference.tzinfo)
    return occ


def _instance_dict(event: Event, occ_start: datetime) -> dict:
    """Baut eine virtuelle Event-Instanz als EventResponse-kompatibles dict."""
    duration = event.end - event.start
    base_start = event.start
    base_end = event.end
    new_start = _match_naiveness(occ_start, base_start)
    new_end = new_start + duration
    iso_day = new_start.date().isoformat()
    return {
        "id": f"{event.id}::{iso_day}",
        "calendar_id": event.calendar_id,
        "title": event.title,
        "description": event.description,
        "location": event.location,
        "start": new_start,
        "end": new_end,
        "all_day": event.all_day,
        "recurrence_rule": event.recurrence_rule,
        "recurrence_exdates": event.recurrence_exdates,
        "activity_type": event.activity_type,
        "is_recurring_instance": new_start != base_start or new_end != base_end,
        "series_id": event.id,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
    }


def expand_event(event: Event, window_start: datetime, window_end: datetime) -> list[dict]:
    """Expandiert ein (ggf. wiederkehrendes) Event in Instanzen im Fenster.

    EXDATE-Tage werden ausgelassen.

    ⚠️ Ein **nicht**-wiederkehrendes Event ergibt immer genau eine Instanz,
    ungeprueft, ob es das Fenster ueberhaupt beruehrt. Das ist Absicht und passt
    zum einzigen Aufrufer (`routers/events.list_events` filtert Einzeltermine
    bereits in der Datenbank vor); die frueher hier stehende Zusage „sofern sie
    das Fenster überlappen" war falsch. Wer diese Funktion neu aufruft, muss
    selbst vorfiltern.
    """
    if not event.recurrence_rule:
        return [_instance_dict(event, event.start)]

    duration = _rechenzeit(event.end) - _rechenzeit(event.start)
    try:
        # dtstart aware (UTC), damit between() gegen aware Grenzen vergleichbar bleibt.
        rule = rrulestr(event.recurrence_rule, dtstart=_rechenzeit(event.start))
    except (ValueError, TypeError):
        # Defekte RRULE → wie Einzeltermin behandeln, statt 500 zu werfen.
        return [_instance_dict(event, event.start)]

    exdates = parse_exdates(event.recurrence_exdates)
    # Untere Grenze um die Dauer vorziehen, damit überlappende Vorkommen mitkommen.
    lower = _rechenzeit(window_start) - duration
    occurrences = rule.between(lower, _rechenzeit(window_end), inc=True)

    instances: list[dict] = []
    for occ in occurrences:
        # occ trägt dtstart.tzinfo; Vergleich gegen Wandzeit-Datum.
        if occ.date().isoformat() in exdates:
            continue
        instances.append(_instance_dict(event, occ))
    return instances


def expand_events(events: list[Event], window_start: datetime, window_end: datetime) -> list[dict]:
    """Expandiert eine Event-Liste und sortiert die Instanzen chronologisch."""
    out: list[dict] = []
    for event in events:
        out.extend(expand_event(event, window_start, window_end))
    out.sort(key=lambda e: _rechenzeit(e["start"]))
    return out
