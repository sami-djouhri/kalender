"""Unbekannte Query-Parameter sichtbar machen, statt sie zu verschlucken.

DAS PROBLEM
-----------
FastAPI ignoriert Query-Parameter, die eine Route nicht deklariert: kommentarlos,
mit HTTP 200. Ein Aufrufer, der sich im Namen vertut, bekommt deshalb nicht etwa
einen Fehler, sondern **eine plausible Antwort auf eine andere Frage**. Genau diese
Klasse Fehler hat hier schon mehrfach Zeit gekostet:

* ``host-router`` rief ``/api/events?date=…`` auf. Der Parameter existiert nicht;
  der Sprachassistent war dadurch dauerhaft kalenderblind, ohne dass irgendwo ein
  Fehler auftauchte. (Vermerkt im API-Vertrag vom 2026-08-16.)
* ``POST /api/habits/schedule`` nimmt ``week`` (ISO-Woche), nicht ``week_start``.
  Ein Aufruf mit ``week_start`` plant kommentarlos die *laufende* Woche.
* ``GET /api/habits/sessions`` kennt weder ``start`` noch ``end``, mit diesen
  Parametern liefert es alle Einheiten aller Wochen statt der gefragten.

Die beiden letzten Faelle sind der Simulation am 2026-08-24 selbst passiert und
haetten dort beinahe als Produktbefund gegolten.

DIE STUFEN
----------
Bewusst dasselbe dreistufige Muster wie bei der Mandanten-Header-Pruefung
(``backend/tenant_auth.py``), ein Vertrag mit lebenden Konsumenten wird nicht
ueber Nacht scharf geschaltet:

1. **Standard:** unbekannte Parameter werden **protokolliert** (WARNING, einmal je
   Route+Parameter, damit ein Poller kein Log flutet) und ansonsten wie bisher
   ignoriert. Nichts bricht.
2. ``STRICT_QUERY_PARAMS=1``: unbekannte Parameter geben **400** mit Klartext.

Die Wache haengt nur an den geschuetzten Routen (Ebene 3 des API-Vertrags). Die
eingefrorene Ebene 1 (Home Assistant, iCal, dev-portal) bleibt unberuehrt.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from fastapi.dependencies.utils import get_flat_dependant

from backend.config import settings

logger = logging.getLogger(__name__)

# Ueberall geduldet: Cache-Aufbrecher der Browser und das Feed-Token, das an
# geschuetzten Routen zwar nicht deklariert, aber ein bekanntes Anhaengsel ist.
IMMER_ERLAUBT = frozenset({"_", "t", "token", "cachebust", "nocache"})

_bekannt: dict[int, frozenset[str]] | None = None
_gemeldet: set[tuple[str, str]] = set()


def _index_aufbauen(app) -> dict[int, frozenset[str]]:
    """Einmal je Prozess: Endpunkt-Funktion → erlaubte Parameternamen.

    Geschluesselt wird ueber die Endpunkt-Funktion und nicht ueber
    ``request.scope["route"]``: letzteres setzt Starlette je nach Version
    unterschiedlich, ``scope["endpoint"]`` dagegen immer.
    """
    index: dict[int, frozenset[str]] = {}
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        endpoint = getattr(route, "endpoint", None)
        if dependant is None or endpoint is None:
            continue
        flach = get_flat_dependant(dependant, skip_repeats=True)
        index[id(endpoint)] = frozenset(p.alias for p in flach.query_params)
    return index


def parameter_wache(request: Request) -> None:
    """FastAPI-Abhaengigkeit: meldet (oder verweigert) unbekannte Query-Parameter."""
    global _bekannt

    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return
    if _bekannt is None:
        _bekannt = _index_aufbauen(request.app)
    erlaubt = _bekannt.get(id(endpoint))
    if erlaubt is None:
        return

    unbekannt = sorted(set(request.query_params) - erlaubt - IMMER_ERLAUBT)
    if not unbekannt:
        return

    pfad = request.scope.get("path", request.url.path)
    if settings.STRICT_QUERY_PARAMS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unbekannte Parameter: {', '.join(unbekannt)}. "
                f"Erlaubt sind: {', '.join(sorted(erlaubt)) or '(keine)'}"
            ),
        )

    for name in unbekannt:
        schluessel = (pfad, name)
        if schluessel in _gemeldet:
            continue
        _gemeldet.add(schluessel)
        logger.warning(
            "Unbekannter Query-Parameter '%s' an %s: wird ignoriert, der Aufruf "
            "liefert damit eine Antwort auf eine andere Frage. Erlaubt: %s",
            name, pfad, ", ".join(sorted(erlaubt)) or "(keine)",
        )
