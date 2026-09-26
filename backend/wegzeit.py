"""Wegzeit zwischen zwei Punkten, ueber einen austauschbaren Anbieter.

Der Kalender rechnet nicht selbst. Er fragt den Dienst unter `ROUTING_URL`, und
`ROUTING_ART` sagt, welche Sprache dort gesprochen wird.

★★ Die Abfahrtszeit gehoert in die Signatur, obwohl Valhalla sie ignoriert.
   Bei Fuss und Rad ist die Wegzeit um 5 Uhr dieselbe wie um 17 Uhr, beim
   Nahverkehr ist sie es nicht: dort entscheidet der Fahrplan, und zwischen dem
   letzten Bus um 23:10 und dem naechsten um 5:40 liegen sieben Stunden. Wer die
   Zeit erst einbaut, wenn der Fahrplan da ist, muss jeden Aufrufer anfassen und
   jede gespeicherte Wegzeit neu bewerten. Sie kostet hier nichts und macht den
   spaeteren Anbieter zu einer neuen Funktion statt zu einem Umbau.

★ Fail-soft: kein Anbieter, kein Netz, keine Koordinaten heisst `None`, nie eine
  Ausnahme nach aussen. Ein Termin ohne Wegzeit ist ein Termin; ein Termin, der
  wegen eines Routing-Timeouts nicht gespeichert wird, ist ein Datenverlust.

Was es NICHT gibt: Nahverkehr. Valhalla rechnet Fuss, Rad und Auto aus
OpenStreetMap-Geometrie, aber ein Bus faehrt nach Fahrplan, und Fahrplaene stehen
nicht in OSM. Dafuer braucht es GTFS-Daten und einen Anbieter, der sie liest
(OpenTripPlanner). `ROUTING_ART=otp` ist dafuer vorgesehen und meldet bis dahin
klar, dass es ihn noch nicht gibt, statt still eine Fusszeit zu liefern.
"""

import json
import logging
from datetime import datetime
from urllib import error, request

from backend.config import settings

logger = logging.getLogger(__name__)

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
    return bool(settings.ROUTING_URL)


def mittel_moeglich(mittel: str) -> bool:
    """Kann der eingestellte Anbieter dieses Verkehrsmittel?

    Getrennt von `verfuegbar()`, weil die Antwort verschieden ist: ohne Anbieter
    gibt es gar nichts, mit Valhalla gibt es alles ausser Nahverkehr. Wer beides
    in einen Schalter presst, kann dem Menschen nicht sagen, was ihm fehlt.
    """
    if not verfuegbar():
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
        if settings.ROUTING_ART == "valhalla":
            return _valhalla(start, ziel, mittel)
        if settings.ROUTING_ART == "otp":
            raise WegzeitFehlt(
                "ROUTING_ART=otp ist vorgesehen, aber der Dienst ist noch nicht "
                "gebaut. Bis dahin ROUTING_ART=valhalla setzen (Fuss, Rad, Auto)."
            )
        logger.warning("Unbekannte ROUTING_ART %r", settings.ROUTING_ART)
        return None
    except WegzeitFehlt:
        raise
    except (error.URLError, TimeoutError, ValueError, OSError, KeyError) as e:
        logger.warning("Wegzeit nicht berechenbar (%s -> %s, %s): %s",
                       start, ziel, mittel, e)
        return None
