"""Fail-soft Cross-App-Konnektoren, der Kalender fragt Nachbar-Apps read-only ab.

Damit der Kalender Vorschläge aus dem ganzen Leben machen kann (Sport aus der
Fitness-App, Einkauf aus Lager/MealPrep), holt er sich hier fail-soft ein paar
Signale. Grundregeln:

* **Niemals blockierend für die Planung**: jeder Fehler (Timeout, 4xx/5xx, kaputtes
  JSON, Konnektor aus) → leeres/None-Ergebnis, kein Raise nach außen.
* **Read-only, je Mandant**: für den Owner (und den System-Kontext) headerlos,
  Fitness/MealPrep/Lager fallen dann auf ihren DEFAULT_OWNER_SUB zurück. Für
  jedes andere Konto mit signiertem ``X-Saganta-Sub``, Geheimnis je Dienst.
  Bis 2026-10-30 lief JEDE Frage headerlos, und Saganta hat drei Konten:
  jedes sah über ``/api/assistant/*`` Einkaufsliste, Training und Lagerstand
  des Owners (SAGKAL-012). Fehlt das Geheimnis eines Dienstes, bekommt ein
  fremdes Konto von dort **nichts**, statt die Daten des Owners.
* **Kurz gecacht** (CROSS_APP_CACHE_SECONDS): Einkaufsliste/Recovery ändern sich
  nicht im Sekundentakt; schützt die Nachbar-Apps vor Poll-Last.
* **Nur stdlib** (urllib): konsistent mit capture.py/session_notifications.py.
"""

import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import date, timedelta
from urllib import error, request

from backend.config import settings

logger = logging.getLogger(__name__)

# Einfacher thread-sicherer TTL-Cache: url -> (expires_monotonic, value)
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > time.monotonic():
            return entry[1]
    return None


def _cache_put(key: str, value: object):
    with _cache_lock:
        _cache[key] = (time.monotonic() + max(1, settings.CROSS_APP_CACHE_SECONDS), value)


def clear_cache() -> None:
    """Cache leeren (Tests / manueller Refresh)."""
    with _cache_lock:
        _cache.clear()


def _mandanten_kopf(sub: str | None, geheimnis: str) -> dict | None:
    """Kopfzeilen fuer eine Frage im Namen von ``sub``. None = nicht fragen.

    Owner und System-Kontext fragen headerlos (Vertrag der Nachbarn: headerlos
    ist der Owner). Ein anderes Konto signiert, und ohne Geheimnis wird gar nicht
    gefragt: die headerlose Antwort waere die des Owners.
    """
    if not sub or sub == settings.DEFAULT_OWNER_SUB:
        return {}
    if not geheimnis:
        return None
    sig = hmac.new(geheimnis.encode("utf-8"), sub.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"X-Saganta-Sub": sub, "X-Saganta-Sub-Sig": sig}


def _get_json(url: str, cache: bool = True, sub: str | None = None, geheimnis: str = ""):
    """Fail-soft GET → geparstes JSON oder None. Cacht Erfolge (nicht Fehler).

    Der Cache-Schluessel traegt den Mandanten. Nach URL allein bekaeme das
    zweite Konto innerhalb der Cache-Zeit die Antwort des ersten.
    """
    if not settings.CROSS_APP_ENABLED:
        return None
    kopf = _mandanten_kopf(sub, geheimnis)
    if kopf is None:
        return None
    schluessel = f"{sub if kopf else ''}|{url}"
    if cache:
        cached = _cache_get(schluessel)
        if cached is not None:
            return cached
    try:
        req = request.Request(url, headers={"Accept": "application/json", **kopf}, method="GET")
        with request.urlopen(req, timeout=settings.CROSS_APP_TIMEOUT) as resp:
            if resp.status >= 400:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except (error.URLError, error.HTTPError, TimeoutError, ValueError, OSError) as exc:
        logger.debug("Cross-App GET %s fehlgeschlagen: %s", url, exc)
        return None
    except Exception:  # pragma: no cover  (defensiv, nie die Planung reissen)
        logger.exception("Cross-App GET %s unerwartet fehlgeschlagen", url)
        return None
    if cache:
        _cache_put(schluessel, data)
    return data


# --- Fitness (:8094) ---

def get_muscle_freshness(sub: str | None = None) -> dict | None:
    """{'muscle_freshness': {...}, 'summary': {'avg_freshness', 'critical_muscles': [...]}}."""
    if not settings.FITNESS_URL:
        return None
    return _get_json(
        f"{settings.FITNESS_URL}/api/progress/muscle-freshness",
        sub=sub, geheimnis=settings.FITNESS_TENANT_SECRET,
    )


def get_recent_workouts(days_back: int = 10, sub: str | None = None) -> list | None:
    """Workouts der letzten `days_back` Tage (Liste von Dicts) oder None."""
    if not settings.FITNESS_URL:
        return None
    today = date.today()
    frm = (today - timedelta(days=days_back)).isoformat()
    to = today.isoformat()
    data = _get_json(
        f"{settings.FITNESS_URL}/api/workouts?date_from={frm}&date_to={to}&limit=50",
        sub=sub, geheimnis=settings.FITNESS_TENANT_SECRET,
    )
    return data if isinstance(data, list) else None


def get_training_jetzt(minuten: int | None = None, sub: str | None = None) -> dict | None:
    """Was die Fitness-App für ein Zeitfenster von ``minuten`` bereithält.

    Die Gegenrichtung der Arbeitsteilung aus `tagesdecke.py`: der Kalender
    entscheidet **wann** trainiert wird und wie lange, die Fitness-App **was**
    in diese Zeit gehört. Übergeben wird deshalb die Dauer des Trainingsblocks,
    und die App weist in ``minuten_quelle`` aus, ob sie damit gerechnet hat
    (``kalender``) oder mangels Angabe geraten hat (``vorgabe``).

    ⚠️ Bewusst **nicht** in `baue_decke` aufgerufen. Die Decke wird bei jedem
    Aufruf frisch gerechnet; ein HTTP-Aufruf darin läge auf dem Weg jeder
    Kalenderansicht und machte einen fremden Dienst zur Voraussetzung für das
    Anzeigen des eigenen Tages. Geholt wird der Inhalt erst, wenn jemand ihn
    sehen will.
    """
    if not settings.FITNESS_URL:
        return None
    pfad = "/api/training/jetzt"
    if minuten and minuten > 0:
        pfad += f"?minuten={int(minuten)}"
    daten = _get_json(
        f"{settings.FITNESS_URL}{pfad}", sub=sub, geheimnis=settings.FITNESS_TENANT_SECRET
    )
    if not isinstance(daten, dict):
        return None
    # Der Kalender kennt die Dauer, die App weiß nicht, woher sie kam. Erst
    # hier lässt sich "gemessen" von "geraten" unterscheiden.
    if minuten and minuten > 0 and daten.get("minuten_quelle") == "angefragt":
        daten["minuten_quelle"] = "kalender"
    return daten


def days_since_last_workout(sub: str | None = None) -> int | None:
    """Ganze Tage seit dem letzten Workout (0 = heute schon trainiert). None = unbekannt."""
    workouts = get_recent_workouts(days_back=14, sub=sub)
    if not workouts:
        return None
    latest = None
    for w in workouts:
        ts = w.get("started_at") or w.get("finished_at") or w.get("created_at")
        if not ts:
            continue
        day_str = str(ts)[:10]
        try:
            d = date.fromisoformat(day_str)
        except ValueError:
            continue
        if latest is None or d > latest:
            latest = d
    if latest is None:
        return None
    return max(0, (date.today() - latest).days)


# --- MealPrep (:8096) ---

def get_shopping_list(sub: str | None = None) -> dict | None:
    """Kurzstand der Einkaufsliste: ``{"offen": n, "namen": [...]}`` oder None.

    Bis 2026-11-01 kam das aus mealprep (``/shopping/current``). Die Liste
    ist seitdem ein eigener Dienst, und mealprep hat seine gespeicherte Liste
    abgebaut; der alte Weg antwortet dort nicht mehr. ``/api/kurz`` rechnet
    nicht neu und fragt keinen Nachbarn, er haelt also keinen Vorschlagslauf auf.
    """
    if not settings.EINKAUFSLISTE_URL:
        return None
    data = _get_json(
        f"{settings.EINKAUFSLISTE_URL}/api/kurz",
        sub=sub, geheimnis=settings.EINKAUFSLISTE_TENANT_SECRET,
    )
    if not isinstance(data, dict):
        return None
    return data


def shopping_open_count(sub: str | None = None) -> tuple[int, list[str]]:
    """(Anzahl offener Eintraege, bis zu 3 Beispielnamen). Leer bei Fehler."""
    data = get_shopping_list(sub)
    if not data:
        return 0, []
    try:
        anzahl = int(data.get("offen") or 0)
    except (TypeError, ValueError):
        return 0, []
    namen = [str(n).strip() for n in (data.get("namen") or []) if str(n).strip()]
    return anzahl, namen[:3]


# --- Lager (:8095) ---

def get_lager_critical(sub: str | None = None) -> list | None:
    """Kritische Bestände (list[dict]) oder None."""
    if not settings.LAGER_URL:
        return None
    data = _get_json(
        f"{settings.LAGER_URL}/api/critical", sub=sub, geheimnis=settings.LAGER_TENANT_SECRET
    )
    if isinstance(data, dict):
        return data.get("critical") or []
    return data if isinstance(data, list) else None


def get_low_stock(sub: str | None = None) -> list | None:
    """Niedrige Bestände (list[dict]) oder None."""
    if not settings.LAGER_URL:
        return None
    data = _get_json(
        f"{settings.LAGER_URL}/api/stats/low-stock?threshold_pct=100",
        sub=sub, geheimnis=settings.LAGER_TENANT_SECRET,
    )
    return data if isinstance(data, list) else None


# --- Aggregat ---

def gather_signals(sub: str | None = None) -> dict:
    """Alle Cross-App-Signale fuer ``sub`` in einem Dict, jedes Feld fail-soft.

    ``sub=None`` ist der System-Kontext und fragt wie der Owner.
    """
    signals: dict = {"available": settings.CROSS_APP_ENABLED}
    if not settings.CROSS_APP_ENABLED:
        return signals

    freshness = get_muscle_freshness(sub)
    signals["muscle_freshness"] = freshness
    signals["days_since_workout"] = days_since_last_workout(sub)

    shop_count, shop_names = shopping_open_count(sub)
    signals["shopping_open_count"] = shop_count
    signals["shopping_names"] = shop_names

    critical = get_lager_critical(sub)
    signals["lager_critical"] = critical or []
    signals["lager_critical_count"] = len(critical) if critical else 0

    return signals
