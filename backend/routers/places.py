"""Orte, Adresssuche und Wegzeit-Auskunft.

Drei Dinge unter einem Dach, weil sie dieselbe Frage beantworten: wo ist etwas,
und wie lange braucht man dahin.

* `/api/places` verwaltet benannte Orte (Zuhause, Arbeit, Sportverein).
* `/api/places/adressen/*` reicht die dreistufige Adresssuche durch, damit das
  Frontend nicht selbst wissen muss, wo der Adressdienst steht (und damit es ihn
  ueber die gleiche Anmeldung erreicht wie alles andere).
* `/api/places/konfiguration` sagt dem Frontend, was ueberhaupt geht. Ohne diese
  Auskunft muesste es raten und wuerde eine Kartenflaeche zeigen, die leer bleibt.
"""

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend import orte as adressen
from backend import wege, wegzeit
from backend.config import settings
from backend.database import get_db
from backend.models import Place
from backend.schemas import PlaceCreate, PlaceResponse, PlaceUpdate

router = APIRouter(prefix="/api/places", tags=["places"])


@router.get("/konfiguration")
def konfiguration():
    """Was diese Instanz kann. Bewusst ohne Anmeldung nutzbare Fakten, keine Daten.

    Das Frontend fragt hier, bevor es eine Karte oder ein Wegzeit-Feld zeigt.
    Ohne diese Auskunft entstuende der haeufigste Fehler dieser Bauart: eine
    Oberflaeche, die etwas anbietet, was der Server nicht kann, und dann schweigt.
    """
    return {
        "adressen": adressen.verfuegbar(),
        "karte": settings.KARTEN_URL or None,
        "wegzeit": wegzeit.verfuegbar(),
        "verkehrsmittel": [
            {"id": m, "name": wegzeit.BESCHRIFTUNG[m],
             "moeglich": wegzeit.mittel_moeglich(m)}
            for m in wegzeit.MITTEL
        ],
        "standard_mittel": settings.WEG_STANDARD_MITTEL,
        "puffer_minuten": settings.WEG_PUFFER_MINUTEN,
        "vorlauf_minuten": settings.WEG_VORLAUF_MINUTEN,
    }


# --- Adresssuche -------------------------------------------------------------

@router.get("/adressen/suche")
def adresse_suchen(q: str = Query(..., min_length=2)):
    """Freitext zu Adressvorschlaegen mit Koordinaten.

    Jeder Treffer nennt seine `genauigkeit` (hausnummer, strasse, ort). Das ist
    keine Zierde: eine Wegzeit zum Ortsmittelpunkt kann um Kilometer danebenliegen,
    und der Unterschied muss sichtbar bleiben statt in einer Zahl zu verschwinden.
    """
    return {"treffer": adressen.freitext(q), "dienst": adressen.verfuegbar()}


@router.get("/adressen/orte")
def adressen_orte(q: str = Query(..., min_length=2)):
    return adressen.orte(q)


@router.get("/adressen/strassen")
def adressen_strassen(q: str = Query(..., min_length=2), ort: int | None = None):
    return adressen.strassen(q, ort)


@router.get("/adressen/nummern")
def adressen_nummern(strasse: int = Query(...), q: str = ""):
    return adressen.nummern(strasse, q)


# --- Wegzeit-Auskunft --------------------------------------------------------

@router.get("/aufstehzeit")
def aufstehzeit(datum: date_type = Query(...), db: Session = Depends(get_db)):
    """Wann man aufstehen muesste, damit der erste Termin klappt.

    ⚠️ Reine Auskunft. Sie weckt niemanden und aendert die feste Weckzeit je
    Tagestyp nicht. Erst wenn die Zahl ueber Wochen gestimmt hat, ist die Frage
    sinnvoll, ob sie etwas ausloesen darf.
    """
    ergebnis = wege.aufstehzeit(db, datum)
    if ergebnis is None:
        return {"datum": datum.isoformat(), "aufstehen": None,
                "grund": "kein Termin mit Uhrzeit an diesem Tag"}
    return ergebnis


# --- Orte --------------------------------------------------------------------

@router.get("", response_model=list[PlaceResponse])
def orte_auflisten(db: Session = Depends(get_db)):
    return db.query(Place).order_by(Place.kind, Place.name).all()


def _zuhause_eindeutig(db: Session, kind: str, ausser_id: str | None = None) -> None:
    """Es darf nur ein Zuhause geben, sonst ist der Startort mehrdeutig.

    Fail-loud statt still das zweite umzubiegen: wer ein zweites Zuhause anlegt,
    hat sich entweder vertan oder ist umgezogen, und beides will man merken.
    """
    if kind != "zuhause":
        return
    frage = db.query(Place).filter(Place.kind == "zuhause")
    if ausser_id:
        frage = frage.filter(Place.id != ausser_id)
    vorhanden = frage.first()
    if vorhanden is not None:
        raise HTTPException(
            status_code=409,
            detail=f"„{vorhanden.name}“ ist bereits als Zuhause eingetragen. "
                   f"Erst dort die Art ändern oder den Ort löschen.",
        )


@router.post("", response_model=PlaceResponse, status_code=201)
def ort_anlegen(data: PlaceCreate, db: Session = Depends(get_db)):
    _zuhause_eindeutig(db, data.kind)
    ort = Place(**data.model_dump())
    db.add(ort)
    db.commit()
    db.refresh(ort)
    return ort


@router.get("/{place_id}", response_model=PlaceResponse)
def ort_holen(place_id: str, db: Session = Depends(get_db)):
    ort = db.query(Place).filter(Place.id == place_id).first()
    if not ort:
        raise HTTPException(status_code=404, detail="Ort nicht gefunden")
    return ort


@router.put("/{place_id}", response_model=PlaceResponse)
def ort_aendern(place_id: str, data: PlaceUpdate, db: Session = Depends(get_db)):
    ort = db.query(Place).filter(Place.id == place_id).first()
    if not ort:
        raise HTTPException(status_code=404, detail="Ort nicht gefunden")
    felder = data.model_dump(exclude_unset=True)
    if felder.get("kind"):
        _zuhause_eindeutig(db, felder["kind"], ausser_id=place_id)
    for schluessel, wert in felder.items():
        setattr(ort, schluessel, wert)
    db.commit()
    db.refresh(ort)
    return ort


@router.delete("/{place_id}", status_code=204)
def ort_loeschen(place_id: str, db: Session = Depends(get_db)):
    ort = db.query(Place).filter(Place.id == place_id).first()
    if not ort:
        raise HTTPException(status_code=404, detail="Ort nicht gefunden")
    db.delete(ort)
    db.commit()
