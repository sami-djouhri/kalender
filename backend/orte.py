"""Adresssuche: Text zu Koordinaten, ueber einen austauschbaren Dienst.

Der Kalender loest Adressen nicht selbst auf. Er reicht die Suche an den Dienst
unter `ADRESS_URL` weiter, und der arbeitet dreistufig: Ort, dann Strasse im Ort,
dann Hausnummer in der Strasse. Diese Bauart ist keine Umstaendlichkeit, sondern
der Grund, warum es ohne 100 GB Nominatim-Datenbank geht: jede Stufe schraenkt
die naechste ein.

★ Ohne `ADRESS_URL` ist alles hier still aus. Eine Adresse bleibt dann der Text,
  den jemand getippt hat, und das ist ein gueltiger Zustand: der Kalender soll
  auch dort laufen, wo es keinen Adressdienst gibt. Deshalb liefert jede Funktion
  eine leere Liste statt eines Fehlers, und der Aufrufer prueft `verfuegbar()`,
  wenn er dem Menschen erklaeren will, warum nichts vorgeschlagen wird.

★ Fail-soft wie cross_app.py: ein Timeout darf niemals verhindern, dass ein
  Termin gespeichert wird. Ein Termin ohne Koordinaten ist ein Termin, ein nicht
  gespeicherter Termin ist ein Datenverlust.
"""

import json
import logging
import re
import threading
import time
from urllib import error, parse, request

from backend.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 4.0
_CACHE_SEKUNDEN = 300
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def verfuegbar() -> bool:
    return bool(settings.ADRESS_URL)


def _hole(pfad: str, **frage) -> list:
    """GET auf den Adressdienst; bei jedem Fehler eine leere Liste."""
    if not verfuegbar():
        return []
    url = f"{settings.ADRESS_URL}{pfad}?{parse.urlencode(frage)}"
    with _cache_lock:
        eintrag = _cache.get(url)
        if eintrag and eintrag[0] > time.monotonic():
            return eintrag[1]
    try:
        with request.urlopen(url, timeout=_TIMEOUT) as r:
            daten = json.loads(r.read().decode("utf-8"))
        if not isinstance(daten, list):
            logger.warning("Adressdienst lieferte kein Feld: %s", str(daten)[:200])
            return []
    except (error.URLError, TimeoutError, ValueError, OSError) as e:
        logger.warning("Adressdienst nicht erreichbar (%s): %s", pfad, e)
        return []
    with _cache_lock:
        _cache[url] = (time.monotonic() + _CACHE_SEKUNDEN, daten)
    return daten


def cache_leeren() -> None:
    with _cache_lock:
        _cache.clear()


def orte(text: str) -> list:
    return _hole("/orte", q=text)


def strassen(text: str, ort_id: int | None = None) -> list:
    frage = {"q": text}
    if ort_id is not None:
        frage["ort"] = ort_id
    return _hole("/strassen", **frage)


def nummern(strasse_id: int, text: str = "") -> list:
    return _hole("/nummern", strasse=strasse_id, q=text)


# --- Freitext ---------------------------------------------------------------
# Ein Mensch tippt „Velberter Str. 12, Heiligenhaus" und nicht drei Felder. Die
# Zerlegung hier ist bewusst eine HEURISTIK und wird auch so benannt: sie liefert
# Vorschlaege mit Koordinaten, nicht die Wahrheit. Wer sicher sein will, waehlt
# die drei Stufen einzeln. Deshalb gibt `freitext()` immer eine LISTE zurueck,
# auch bei einem einzigen Treffer, und das Frontend laesst waehlen.

_NUMMER = re.compile(r"\b(\d+\s*[a-zA-Z]?)\s*$")


def _teile(text: str) -> tuple[str, str, str]:
    """(strasse, hausnummer, ort) aus einem Freitext raten.

    Das Komma ist der einzige verlaessliche Trenner: davor steht die Strasse mit
    Nummer, dahinter der Ort. Ohne Komma wird der Ort nicht geraten, denn ein
    falsch geratener Ort ist schlimmer als gar keiner: er liefert Treffer, die
    plausibel aussehen und in der falschen Stadt liegen.
    """
    text = " ".join(text.split())
    ort = ""
    if "," in text:
        vorne, hinten = text.rsplit(",", 1)
        # Eine Postleitzahl vor dem Ortsnamen stoert die Namenssuche.
        ort = re.sub(r"^\s*\d{4,5}\s+", "", hinten).strip()
        text = vorne.strip()
    nummer = ""
    treffer = _NUMMER.search(text)
    if treffer:
        nummer = treffer.group(1).replace(" ", "")
        text = text[:treffer.start()].strip()
    return text.strip(), nummer, ort


def freitext(text: str, grenze: int = 8) -> list[dict]:
    """Adressvorschlaege zu einem getippten Text.

    Rueckgabe je Treffer: {"text", "lat", "lon", "genauigkeit"}.
    `genauigkeit` ist "hausnummer", "strasse" oder "ort" und sagt dem Aufrufer,
    wie weit die Aufloesung wirklich reicht. Das ist wichtig fuer die Wegzeit:
    eine Route zum Ortsmittelpunkt kann um Kilometer danebenliegen, und das soll
    man sehen koennen, statt es aus einer Zahl herauslesen zu muessen.
    """
    if not verfuegbar() or len(text.strip()) < 2:
        return []
    strasse_text, hausnummer, ort_text = _teile(text)

    ort_id, ort_name = None, ""
    if ort_text:
        gefunden = orte(ort_text)
        if gefunden:
            ort_id, ort_name = gefunden[0]["id"], gefunden[0]["name"]

    treffer: list[dict] = []
    if strasse_text:
        for s in strassen(strasse_text, ort_id)[:grenze]:
            beschriftung = f"{s['name']}, {s.get('ort') or ort_name}".strip(", ")
            if hausnummer:
                passend = [n for n in nummern(s["id"], hausnummer)
                           if n["nummer"].lower() == hausnummer.lower()]
                if passend:
                    treffer.append({
                        "text": f"{s['name']} {passend[0]['nummer']}, "
                                f"{s.get('ort') or ort_name}".strip(", "),
                        "lat": passend[0]["lat"], "lon": passend[0]["lon"],
                        "genauigkeit": "hausnummer",
                    })
                    continue
            treffer.append({"text": beschriftung, "lat": s["lat"], "lon": s["lon"],
                            "genauigkeit": "strasse"})

    # Kein Strassentreffer: dann wenigstens den Ort anbieten, aber ehrlich
    # beschriftet. Ein Ortsmittelpunkt als „Adresse" waere eine stille Luege.
    if not treffer:
        for o in orte(ort_text or strasse_text or text)[:grenze]:
            treffer.append({
                "text": o["name"] + (f" ({o['plz']})" if o.get("plz") else ""),
                "lat": o["lat"], "lon": o["lon"], "genauigkeit": "ort",
            })
    return treffer[:grenze]
