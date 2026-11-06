"""Wegzeit zwischen zwei Punkten, ueber einen austauschbaren Anbieter.

Der Kalender rechnet nicht selbst. Er fragt den Dienst unter `ROUTING_URL`, und
`ROUTING_ART` sagt, welche Sprache dort gesprochen wird.

Die Abfahrtszeit gehoert in die Signatur, obwohl Valhalla sie ignoriert.
   Bei Fuss und Rad ist die Wegzeit um 5 Uhr dieselbe wie um 17 Uhr, beim
   Nahverkehr ist sie es nicht: dort entscheidet der Fahrplan, und zwischen dem
   letzten Bus um 23:10 und dem naechsten um 5:40 liegen sieben Stunden. Wer die
   Zeit erst einbaut, wenn der Fahrplan da ist, muss jeden Aufrufer anfassen und
   jede gespeicherte Wegzeit neu bewerten. Sie kostet hier nichts und macht den
   spaeteren Anbieter zu einer neuen Funktion statt zu einem Umbau.

Fail-soft: kein Anbieter, kein Netz, keine Koordinaten heisst `None`, nie eine
  Ausnahme nach aussen. Ein Termin ohne Wegzeit ist ein Termin; ein Termin, der
  wegen eines Routing-Timeouts nicht gespeichert wird, ist ein Datenverlust.

Nahverkehr (`transit`) kommt seit dem 2026-10-31 aus OpenTripPlanner unter
`OTP_URL` (GTFS von gtfs.de, 50-km-Kreis, Rezept in `homelab/karten` unter
`otp/`). Valhalla rechnet weiter Fuss, Rad und Auto; ein Bus faehrt nach
Fahrplan, und Fahrplaene stehen nicht in OSM. Ohne `OTP_URL` meldet `transit`
klar `WegzeitFehlt`, statt still eine Fusszeit zu liefern.

Beim Nahverkehr ist die Wegzeit die Spanne von der ABFAHRT bis zur
   gewuenschten Ankunft, nicht die Fahrdauer. Faehrt der Bus um 8:10 und ist um
   8:42 da, der Termin aber um 9:00, dann muss man um 8:10 los: der Block
   beginnt dort. Die Wartezeit am Ziel gehoert in den Block, weil man sie nicht
   zu Hause verbringt.
Die Zeit entscheidet, also auch der Wochentag. OTP bekommt das Datum mit und
   rechnet damit den Samstags- und Sonntagsfahrplan von selbst.
"""
import re
from zoneinfo import ZoneInfo

import json
import logging
import time
from datetime import datetime
from urllib import error, request

from backend.config import settings

logger = logging.getLogger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")

# Was der Kalender kennt, und wie es beim jeweiligen Anbieter heisst.
MITTEL = ("pedestrian", "bicycle", "auto", "transit")
_VALHALLA_COSTING = {"pedestrian": "pedestrian", "bicycle": "bicycle", "auto": "auto"}

BESCHRIFTUNG = {
    "pedestrian": "zu Fuß",
    "bicycle": "mit dem Rad",
    "auto": "mit dem Auto",
    "transit": "mit Bus und Bahn",
}


class WegzeitFehlt(Exception):
    """Der Anbieter kann dieses Verkehrsmittel nicht. Sichtbar, nicht still."""


def verfuegbar() -> bool:
    return bool(settings.ROUTING_URL or settings.OTP_URL)


def mittel_moeglich(mittel: str) -> bool:
    """Kann der eingestellte Anbieter dieses Verkehrsmittel?

    Getrennt von `verfuegbar()`, weil die Antwort verschieden ist: ohne Anbieter
    gibt es gar nichts, mit Valhalla gibt es alles ausser Nahverkehr. Wer beides
    in einen Schalter presst, kann dem Menschen nicht sagen, was ihm fehlt.
    """
    if mittel == "transit" and settings.OTP_URL:
        return True
    if not settings.ROUTING_URL:
        return False
    if settings.ROUTING_ART == "valhalla":
        return mittel in _VALHALLA_COSTING
    if settings.ROUTING_ART == "otp":
        return mittel in MITTEL
    return False


def _valhalla(start: tuple[float, float], ziel: tuple[float, float],
              mittel: str) -> int | None:
    costing = _VALHALLA_COSTING.get(mittel)
    if not costing:
        raise WegzeitFehlt(
            f"Valhalla rechnet keine Route {BESCHRIFTUNG.get(mittel, mittel)}. "
            f"Dafuer braucht es Fahrplandaten und ROUTING_ART=otp."
        )
    last = json.dumps({
        "locations": [{"lat": start[0], "lon": start[1]},
                      {"lat": ziel[0], "lon": ziel[1]}],
        "costing": costing,
        # Ohne diese Angabe faellt Valhalla auf Meilen zurueck. Die Zeit waere
        # davon unberuehrt, die Strecke aber nicht, und die steht spaeter im
        # Termin.
        "directions_options": {"units": "kilometers"},
    }).encode("utf-8")
    anfrage = request.Request(
        f"{settings.ROUTING_URL}/route", data=last,
        headers={"Content-Type": "application/json"})
    with request.urlopen(anfrage, timeout=settings.ROUTING_TIMEOUT) as r:
        antwort = json.loads(r.read().decode("utf-8"))
    fahrt = (antwort or {}).get("trip") or {}
    zusammenfassung = fahrt.get("summary") or {}
    sekunden = zusammenfassung.get("time")
    if sekunden is None:
        return None
    return max(1, round(sekunden / 60))


# --- OpenTripPlanner ---------------------------------------------------------

# `searchWindow` fest auf zwei Stunden. OTP rechnet sein Fenster sonst selbst
#   aus, und das war zu kurz: „spaetestens an 19:08" (Nachbarort -> Nachbarstadt,
#   2026-11-01) fand KEINE Fahrt im Fenster (`NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW`)
#   und lieferte statt der Busse 16:14-18:09 einen reinen Fussweg von 3,5 Stunden.
#   Bei 18:00 oder 20:00 kamen Busse, der Fehler hing also an der Uhrzeit.
_PLAN = """
query ($von: PlanLabeledLocationInput!, $nach: PlanLabeledLocationInput!,
       $zeit: PlanDateTimeInput!) {
  planConnection(origin: $von, destination: $nach, dateTime: $zeit, first: 4,
                 searchWindow: "PT2H") {
    edges { node {
      start end
      legs { mode route { shortName } from { name } to { name }
             start { scheduledTime } end { scheduledTime } }
    } }
  }
}"""

# Nach einem Netzfehler fragt der Kalender OTP fuenf Minuten lang nicht. Der
#   Heimweg wird beim Speichern eines Termins nachgezogen; ohne diese Sperre
#   wartete bei totem OTP jedes Speichern auf mehrere Zeitablaeufe hintereinander.
#   Der Termin selbst ist dann laengst gespeichert, aber die Oberflaeche haengt.
_OTP_SPERRE_S = 300
_otp_gesperrt_bis = 0.0



def nahverkehr_gestoert() -> bool:
    """Liegt OTP gerade in der Netzfehler-Sperre? Fuer die Oberflaeche: ohne
    diese Auskunft sieht „0 Wege geplant" aus wie „nichts zu tun"."""
    return time.monotonic() < _otp_gesperrt_bis


_ART = {"BUS": "Bus", "TRAM": "Tram", "SUBWAY": "U", "RAIL": "Zug",
        "FERRY": "Fähre", "COACH": "Bus"}


def _lokal(zeit: datetime) -> datetime:
    """Ortszeit mit Zone. Der Kalender speichert lokal ohne Zone."""
    return zeit.replace(tzinfo=BERLIN) if zeit.tzinfo is None else zeit.astimezone(BERLIN)


def _zeit(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(BERLIN)


def _beschreibung(legs: list) -> str:
    """„Bus 42 Rathaus 7:32 → Nachbarstadt Mitte 7:51, zu Fuß 4 min"."""
    teile = []
    for leg in legs:
        ab = _zeit(leg["start"]["scheduledTime"])
        an = _zeit(leg["end"]["scheduledTime"])
        if leg.get("mode") == "WALK":
            minuten = max(1, round((an - ab).total_seconds() / 60))
            teile.append(f"zu Fuß {minuten} min")
            continue
        linie = (leg.get("route") or {}).get("shortName") or ""
        art = _ART.get(leg.get("mode"), leg.get("mode", "").title())
        name = f"{art} {linie}".strip() if not re.match(r"^[A-Z]{1,3}\s?\d", linie) else linie
        teile.append(f"{name} {leg['from']['name']} {ab:%H:%M} → "
                     f"{leg['to']['name']} {an:%H:%M}")
    return ", ".join(teile)


def verbindung(start: tuple[float, float], ziel: tuple[float, float],
               ankunft: datetime | None = None,
               abfahrt: datetime | None = None) -> dict | None:
    """Die passende Verbindung mit Bus und Bahn, oder None.

    Genau eines von `ankunft` (spaetestens da sein) und `abfahrt` (fruehestens
    los). Gewaehlt wird bei `ankunft` die SPAETESTE Abfahrt, die rechtzeitig
    ankommt, bei `abfahrt` die FRUEHESTE Ankunft: beides heisst, so wenig Zeit
    wie moeglich unterwegs oder wartend zu verbringen.

    Rueckgabe `{abfahrt, ankunft, text}` mit Zeiten in Ortszeit samt Zone.
    `WegzeitFehlt` ohne `OTP_URL`; Netzfehler ergeben None (fail-soft).
    """
    if not settings.OTP_URL:
        raise WegzeitFehlt("Fuer Bus und Bahn ist OTP_URL nicht gesetzt.")
    if (ankunft is None) == (abfahrt is None):
        raise ValueError("genau eines von ankunft und abfahrt")
    global _otp_gesperrt_bis
    if time.monotonic() < _otp_gesperrt_bis:
        return None
    if ankunft is not None:
        zeit = {"latestArrival": _lokal(ankunft).isoformat()}
    else:
        zeit = {"earliestDeparture": _lokal(abfahrt).isoformat()}

    def ort(p):
        return {"location": {"coordinate": {"latitude": p[0], "longitude": p[1]}}}

    last = json.dumps({"query": _PLAN, "variables": {
        "von": ort(start), "nach": ort(ziel), "zeit": zeit}}).encode("utf-8")
    anfrage = request.Request(
        f"{settings.OTP_URL}/otp/gtfs/v1", data=last,
        headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(anfrage, timeout=settings.ROUTING_TIMEOUT) as r:
            antwort = json.loads(r.read().decode("utf-8"))
    except (error.URLError, TimeoutError, OSError) as e:
        _otp_gesperrt_bis = time.monotonic() + _OTP_SPERRE_S
        logger.warning("OTP nicht erreichbar, %d s Pause: %s", _OTP_SPERRE_S, e)
        return None
    except ValueError as e:
        logger.warning("OTP-Antwort unlesbar: %s", e)
        return None
    if antwort.get("errors"):
        logger.warning("OTP meldet Fehler: %s", antwort["errors"])
        return None
    kanten = (((antwort.get("data") or {}).get("planConnection") or {})
              .get("edges") or [])
    kandidaten = []
    for k in kanten:
        n = k.get("node") or {}
        try:
            kandidaten.append((_zeit(n["start"]), _zeit(n["end"]), n.get("legs") or []))
        except (KeyError, TypeError, ValueError):
            continue
    if ankunft is not None:
        grenze = _lokal(ankunft)
        passend = [c for c in kandidaten if c[1] <= grenze]
        if not passend:
            return None
        ab, an, legs = max(passend, key=lambda c: c[0])
    else:
        grenze = _lokal(abfahrt)
        passend = [c for c in kandidaten if c[0] >= grenze]
        if not passend:
            return None
        ab, an, legs = min(passend, key=lambda c: c[1])
    return {"abfahrt": ab, "ankunft": an, "text": _beschreibung(legs)}


def wegzeit(start: tuple[float, float] | None,
            ziel: tuple[float, float] | None,
            mittel: str | None = None,
            ankunft: datetime | None = None) -> int | None:
    """Minuten von `start` nach `ziel`, oder None.

    `ankunft` ist die Zeit, zu der man da sein will. Valhalla ignoriert sie,
    ein Fahrplan-Anbieter braucht sie (siehe Kopf).
    """
    if not start or not ziel or not verfuegbar():
        return None
    if start[0] is None or start[1] is None or ziel[0] is None or ziel[1] is None:
        return None
    mittel = mittel or settings.WEG_STANDARD_MITTEL
    try:
        if mittel == "transit" and settings.OTP_URL:
            if ankunft is None:
                raise WegzeitFehlt("Bus und Bahn brauchen eine Ankunftszeit.")
            v = verbindung(start, ziel, ankunft=ankunft)
            if v is None:
                return None
            return max(1, round((_lokal(ankunft) - v["abfahrt"]).total_seconds() / 60))
        if not settings.ROUTING_URL:
            return None
        if settings.ROUTING_ART == "valhalla":
            return _valhalla(start, ziel, mittel)
        if settings.ROUTING_ART == "otp":
            raise WegzeitFehlt(
                "ROUTING_ART=otp ist nicht mehr der Weg zum Nahverkehr: OTP_URL "
                "setzen und ROUTING_ART=valhalla fuer Fuss, Rad und Auto lassen."
            )
        logger.warning("Unbekannte ROUTING_ART %r", settings.ROUTING_ART)
        return None
    except WegzeitFehlt:
        raise
    except (error.URLError, TimeoutError, ValueError, OSError, KeyError) as e:
        logger.warning("Wegzeit nicht berechenbar (%s -> %s, %s): %s",
                       start, ziel, mittel, e)
        return None
