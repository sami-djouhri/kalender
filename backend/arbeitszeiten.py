"""Arbeits- und Schulzeiten: Vorgabe je Wochentag, je Mandant einstellbar.

**Wo die Wahrheit liegt.** Im Tagestyp-Ereignis selbst (``Event.start``/``.end``).
Diese Vorgabe entscheidet nur, mit welchen Uhrzeiten ein neues Arbeits- oder
Schul-Ereignis **angelegt** wird; wer einen einzelnen Tag abweichend eintraegt
(``/api/day-type/set?von=..&bis=..``), schreibt die Uhrzeit direkt ins Ereignis.
Home Assistant liest die Zeiten aus ``/api/day-type/today`` (Felder ``start``,
``end``), also aus dem Ereignis, nie aus dieser Einstellung. Eine geaenderte
Vorgabe verschiebt deshalb keinen schon eingetragenen Tag.

Bis 2026-10-30 gab es nur die Konstanten ``ARBEIT_TIMES``/``SCHULE_TIMES`` in
``routers/daytype.py``. Sie bleiben die Vorgabe, wenn ein Mandant nichts
eingestellt hat.

Gespeichert wird in ``mandant_einstellungen`` unter ``arbeitszeiten`` als JSON,
und nur die Abweichungen von der Konstante, damit eine spaeter geaenderte
Konstante nicht von einer kopierten Altvorgabe verdeckt wird:
``{"arbeit": {"0": "08:00-17:00"}, "schule": {}}`` (0 = Montag).
"""

from __future__ import annotations

import json
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.mandant_einstellungen import wert_lesen, wert_setzen

SCHLUESSEL = "arbeitszeiten"
ZEIT_TYPEN = ("arbeit", "schule")

# (start_h, start_m, ende_h, ende_m)
Zeiten = tuple[int, int, int, int]

_UHRZEIT = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def konstante(dtype: str, wochentag: int) -> Zeiten:
    """Die eingebaute Vorgabe, Quelle sind die Konstanten im Router."""
    from backend.routers.daytype import ARBEIT_TIMES, SCHULE_TIMES

    if dtype == "arbeit":
        return ARBEIT_TIMES["friday"] if wochentag == 4 else ARBEIT_TIMES["default"]
    return SCHULE_TIMES


def uhrzeit(text: str, feld: str) -> tuple[int, int]:
    """``"07:30"`` -> ``(7, 30)``. Alles andere ist ein 400, kein stilles Raten."""
    treffer = _UHRZEIT.match(text or "")
    if not treffer:
        raise HTTPException(400, f"{feld}: Uhrzeit als HH:MM erwartet, bekam {text!r}")
    return int(treffer.group(1)), int(treffer.group(2))


def zeiten_parsen(von: str | None, bis: str | None) -> Zeiten | None:
    """Beide oder keiner. ``bis`` muss nach ``von`` liegen (keine Nachtschicht)."""
    if von is None and bis is None:
        return None
    if von is None or bis is None:
        raise HTTPException(400, "von und bis nur gemeinsam angeben")
    vh, vm = uhrzeit(von, "von")
    bh, bm = uhrzeit(bis, "bis")
    if (bh, bm) <= (vh, vm):
        raise HTTPException(400, "bis muss nach von liegen")
    return vh, vm, bh, bm


def als_text(z: Zeiten) -> str:
    return f"{z[0]:02d}:{z[1]:02d}-{z[2]:02d}:{z[3]:02d}"


def _gespeichert(db: Session, sub: str) -> dict[str, dict[str, str]]:
    roh = wert_lesen(db, sub, SCHLUESSEL)
    if not roh:
        return {}
    try:
        daten = json.loads(roh)
    except ValueError:
        # Eine kaputte Zeile darf den Tagestyp nicht umwerfen: dann gilt die Konstante.
        return {}
    return daten if isinstance(daten, dict) else {}


def vorgabe(db: Session, sub: str | None, dtype: str, wochentag: int) -> Zeiten:
    """Mit welchen Uhrzeiten ein neuer Tag dieses Typs angelegt wird."""
    if sub and dtype in ZEIT_TYPEN:
        eintrag = _gespeichert(db, sub).get(dtype, {}).get(str(wochentag))
        if eintrag:
            try:
                von, bis = eintrag.split("-")
                return zeiten_parsen(von, bis)
            except (ValueError, HTTPException):
                pass
    return konstante(dtype, wochentag)


def wochenplan(db: Session, sub: str) -> dict:
    """Die volle Woche je Typ, mit Herkunft je Tag (eigene Angabe oder Vorgabe)."""
    gespeichert = _gespeichert(db, sub)
    ergebnis: dict = {}
    for dtype in ZEIT_TYPEN:
        tage = []
        for wt in range(7):
            z = vorgabe(db, sub, dtype, wt)
            tage.append({
                "wochentag": wt,
                "von": f"{z[0]:02d}:{z[1]:02d}",
                "bis": f"{z[2]:02d}:{z[3]:02d}",
                "eigen": str(wt) in gespeichert.get(dtype, {}),
            })
        ergebnis[dtype] = tage
    return ergebnis


def wochenplan_setzen(db: Session, sub: str, eingabe: dict) -> dict:
    """Uebernimmt ``{"arbeit": [{"wochentag": 0, "von": "08:00", "bis": "17:00"}, ...]}``.

    Ein Typ, der in der Eingabe fehlt, bleibt wie er ist. Ein Tag, dessen Zeiten
    der Konstante gleichen, wird **nicht** gespeichert (siehe Modulkopf).
    """
    gespeichert = _gespeichert(db, sub)
    for dtype, tage in eingabe.items():
        if dtype not in ZEIT_TYPEN:
            raise HTTPException(400, f"Unbekannter Typ {dtype!r}, erlaubt: {', '.join(ZEIT_TYPEN)}")
        if not isinstance(tage, list):
            raise HTTPException(400, f"{dtype}: Liste von Wochentagen erwartet")
        abweichend: dict[str, str] = {}
        for tag in tage:
            if not isinstance(tag, dict):
                raise HTTPException(400, f"{dtype}: Eintrag muss ein Objekt sein")
            wt = tag.get("wochentag")
            if not isinstance(wt, int) or isinstance(wt, bool) or not 0 <= wt <= 6:
                raise HTTPException(400, f"{dtype}: wochentag 0 (Montag) bis 6 erwartet")
            z = zeiten_parsen(tag.get("von"), tag.get("bis"))
            if z is None:
                continue
            if z != konstante(dtype, wt):
                abweichend[str(wt)] = als_text(z)
        gespeichert[dtype] = abweichend
    wert_setzen(db, sub, SCHLUESSEL, json.dumps(gespeichert, sort_keys=True))
    return wochenplan(db, sub)
