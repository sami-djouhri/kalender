"""API der Tagesdecke: planen, korrigieren, abgleichen.

Zwei Zugangswege, und der Unterschied ist Absicht:

* Alles unter `/api/tagesdecke` läuft über JWT wie der übrige Kalender. Es ist
  die Bedienoberfläche.
* `/api/zeit/ingest` läuft über einen eigenen Token (`ZEIT_INGEST_TOKEN`), weil
  ein Messgerät kein JWT halten kann. Ohne gesetzten Token ist der Pfad aus.
  Begründung in `backend/zeit_ingest.py`.
* `/api/tagesdecke/trainingszeit` läuft über den Feed-Token, denselben Weg, den
  Home Assistant für den Tagestyp nimmt. Ein Nachbardienst hält auch kein JWT.
"""

from datetime import date as date_type
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import TimeBlock
from backend.tagesdecke import (
    ARTEN,
    GESPIEGELTE_ARTEN,
    abgleich,
    baue_decke,
    block_aendern,
    block_anlegen,
    festschreiben,
    neu_ordnen,
    rueckblick,
    wach_fenster,
)
from backend.zeit_ingest import (
    IngestFehler,
    ingest_erlaubt,
    pruefe_token,
    stapel_aufnehmen,
    token_bekannt,
)

router = APIRouter(prefix="/api/tagesdecke", tags=["tagesdecke"])
# Eigener Router ohne JWT-Abhängigkeit (wird in main.py ungeschützt eingehängt).
ingest_router = APIRouter(prefix="/api/zeit", tags=["tagesdecke"])
# Lesend, mit Feed-Token statt JWT, für Nachbardienste.
feed_router = APIRouter(prefix="/api/tagesdecke", tags=["tagesdecke"])


@feed_router.get("/trainingszeit")
def trainingszeit(
    datum: date_type = Query(...),
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    """Nur die reservierte Trainingszeit, ohne Inhalt.

    Das Gegenstück zu ``/training-jetzt`` weiter unten, und bewusst die
    kleinere Hälfte davon. Jener Endpunkt holt sich den **Inhalt** bei der
    Fitness-App; dieser liefert ihr die **Dauer**. Die Arbeitsteilung bleibt
    damit dieselbe wie überall: dieser Dienst sagt, wann und wie lange Zeit
    ist, die Fitness-App sagt, was hineingehört.

    ⚠️ Hier darf **niemals** ``cross_app.get_training_jetzt`` aufgerufen
    werden. Der Aufrufer ist die Fitness-App, und ein Rückruf dorthin ergäbe
    einen Kreis, der sich erst unter Last als Zeitüberschreitung zeigt.
    Deshalb steht diese Funktion hier und nicht als Zweig in ``training_jetzt``.

    Kein Trainingsblock heißt ``minuten: null`` und ist eine Antwort, keine
    Störung: an einem Tag ohne eingeplante Trainingszeit soll die Fitness-App
    ihre eigene Vorgabe nehmen und das auch so ausweisen.
    """
    from backend.routers.daytype import _verify_feed_token

    if not _verify_feed_token(token, db):
        raise HTTPException(status_code=403, detail="Invalid feed token")

    decke = baue_decke(db, datum)
    block = next(
        (
            b
            for b in decke["bloecke"]
            if b["art"] == "training" and b["status"] != "verworfen"
        ),
        None,
    )
    if block is None:
        return {"datum": datum, "minuten": None, "start": None, "ende": None}
    return {
        "datum": datum,
        "minuten": block["minuten"],
        "start": block["start"],
        "ende": block["ende"],
    }


@router.get("")
def decke_lesen(
    datum: date_type = Query(..., description="Tag, dessen Decke berechnet wird"),
    db: Session = Depends(get_db),
):
    """Die lückenlose Decke eines Tages. Read-only, schreibt nichts fest."""
    return baue_decke(db, datum)


@router.get("/arten")
def arten_lesen():
    """Das Vokabular der Blöcke, für die Auswahl in der Oberfläche.

    `gespiegelt` sagt der Oberfläche, dass der Block aus einer anderen Tabelle
    stammt. An einem noch offenen Tag hat das Verschieben eines solchen Blocks
    keinen Bestand: beim nächsten Aufruf kommt er wieder aus seiner Quelle. Wer
    das nicht weiß, baut ein Ziehen und Fallenlassen, das lautlos zurückspringt.
    """
    return {
        "arten": [
            {
                "schluessel": schluessel,
                "name": name,
                "gespiegelt": schluessel in GESPIEGELTE_ARTEN,
            }
            for schluessel, name in ARTEN.items()
        ]
    }


@router.get("/fenster")
def fenster_lesen(
    datum: date_type = Query(...),
    db: Session = Depends(get_db),
):
    """Von wann bis wann dieser Tag wach ist (aus `sleep_schedule` je Tagestyp)."""
    von, bis = wach_fenster(db, datum)
    return {
        "datum": datum,
        "wach_von": von,
        "wach_bis": bis,
        "wach_minuten": int((bis - von).total_seconds() // 60),
    }


@router.post("/festschreiben")
def decke_festschreiben(
    datum: date_type = Query(...),
    db: Session = Depends(get_db),
):
    """Die Decke zum Protokoll machen, damit sie korrigiert werden kann.

    Ab hier ändert ein später verschobener Termin den Tag nicht mehr. Idempotent:
    ein zweiter Aufruf überschreibt bereits vorgenommene Korrekturen nicht.
    """
    return festschreiben(db, datum)


@router.patch("/block/{block_id}")
def block_bearbeiten(
    block_id: str,
    start: datetime | None = Body(default=None),
    ende: datetime | None = Body(default=None),
    titel: str | None = Body(default=None),
    art: str | None = Body(default=None),
    status: str | None = Body(default=None),
    db: Session = Depends(get_db),
):
    """Einen Block verschieben, umbenennen, umwidmen oder verwerfen.

    Trägt das Ziehen und Fallenlassen in der Oberfläche. Jede Änderung macht den
    Block manuell und schützt ihn damit vor der nächsten automatischen Planung.
    """
    if art is not None and art not in ARTEN:
        raise HTTPException(status_code=400, detail=f"Unbekannte Art: {art}")
    if status is not None and status not in ("geplant", "bestaetigt", "verworfen"):
        raise HTTPException(status_code=400, detail=f"Unbekannter Status: {status}")
    try:
        zeile = block_aendern(
            db, block_id, start=start, ende=ende, titel=titel, art=art, status=status
        )
    except ValueError as problem:
        raise HTTPException(status_code=400, detail=str(problem))
    if zeile is None:
        raise HTTPException(status_code=404, detail="Block nicht gefunden")
    return _block_antwort(zeile)


@router.post("/block")
def block_hinzufuegen(
    datum: date_type = Body(...),
    start: datetime = Body(...),
    ende: datetime = Body(...),
    art: str = Body(...),
    titel: str = Body(...),
    begruendung: str | None = Body(default=None),
    db: Session = Depends(get_db),
):
    """Einen Block selbst setzen, etwa das, was man stattdessen getan hat."""
    try:
        zeile = block_anlegen(db, datum, start, ende, art, titel, begruendung)
    except ValueError as problem:
        raise HTTPException(status_code=400, detail=str(problem))
    return _block_antwort(zeile)


@router.delete("/block/{block_id}")
def block_verwerfen(
    block_id: str,
    endgueltig: bool = Query(
        False,
        description="Zeile wirklich löschen statt sie als nicht stattgefunden zu führen",
    ),
    db: Session = Depends(get_db),
):
    """Einen Block aus dem Tag nehmen.

    Vorgabe ist `status=verworfen` und **kein** Löschen. Was regelmäßig geplant
    und nie getan wird, ist das wertvollste Signal dieses Kalenders; ein echtes
    DELETE würde es jeden Abend wegwerfen. `endgueltig=true` gibt es für den
    Fall, dass ein Block gar nicht erst hätte entstehen dürfen.
    """
    zeile = db.query(TimeBlock).filter(TimeBlock.id == block_id).first()
    if zeile is None:
        raise HTTPException(status_code=404, detail="Block nicht gefunden")
    if endgueltig:
        db.delete(zeile)
        db.commit()
        return {"geloescht": True, "id": block_id}
    zeile.status = "verworfen"
    zeile.quelle = "manuell"
    db.commit()
    db.refresh(zeile)
    return _block_antwort(zeile)


@router.post("/neu-ordnen")
def decke_neu_ordnen(
    datum: date_type = Body(...),
    reihenfolge: list[str] = Body(...),
    db: Session = Depends(get_db),
):
    """Die Blöcke eines Tages in eine neue Reihenfolge bringen.

    Ein Aufruf statt vieler: die Decke ist lückenlos, ein Umsortieren verschiebt
    deshalb alles dahinter. Als Folge einzelner Verschiebungen könnte der Lauf
    mittendrin abbrechen und einen Tag mit Löchern oder Überlappungen
    hinterlassen.
    """
    try:
        return neu_ordnen(db, datum, reihenfolge)
    except ValueError as problem:
        raise HTTPException(status_code=400, detail=str(problem))


@router.get("/training-jetzt")
def training_jetzt(
    datum: date_type = Query(...),
    db: Session = Depends(get_db),
):
    """Der Trainingsblock des Tages und was die Fitness-App hineinlegt.

    Hier trifft sich die Arbeitsteilung aus `tagesdecke.py`: dieser Dienst
    hat die Zeit reserviert und begründet, warum sie dort liegt; den Inhalt
    liefert die Fitness-App, und zwar zugeschnitten auf genau diese Dauer.

    Fail-soft in beide Richtungen. Gibt es keinen Trainingsblock, wird das
    gesagt und die Fitness-App gar nicht erst gefragt. Antwortet sie nicht,
    bleibt der Block trotzdem stehen: die reservierte Zeit ist die Zusage
    dieses Dienstes, der Inhalt die der anderen Seite.
    """
    from backend import cross_app
    from backend.tenant import _session_sub

    decke = baue_decke(db, datum)
    block = next(
        (
            b
            for b in decke["bloecke"]
            if b["art"] == "training" and b["status"] != "verworfen"
        ),
        None,
    )
    if block is None:
        return {
            "datum": datum,
            "block": None,
            "training": None,
            "hinweis": "Für diesen Tag ist keine Trainingszeit eingeplant.",
        }

    training = cross_app.get_training_jetzt(block["minuten"], sub=_session_sub(db))
    return {
        "datum": datum,
        "block": {
            "id": block["id"],
            "start": block["start"],
            "ende": block["ende"],
            "minuten": block["minuten"],
            "begruendung": block["begruendung"],
        },
        "training": training,
        "hinweis": (
            None
            if training
            else "Die Zeit steht, die Fitness-App ist gerade nicht erreichbar."
        ),
    }


@router.get("/abgleich")
def abgleich_lesen(
    datum: date_type = Query(...),
    db: Session = Depends(get_db),
):
    """Soll gegen Ist für einen Tag. Wertet nur aus, ändert nichts."""
    return abgleich(db, datum)


@router.get("/rueckblick")
def rueckblick_lesen(
    von: date_type = Query(...),
    bis: date_type = Query(...),
    db: Session = Depends(get_db),
):
    """Wofür die Zeit über mehrere Tage ging, und was regelmäßig ausfällt."""
    if bis < von:
        raise HTTPException(status_code=400, detail="'bis' liegt vor 'von'")
    if (bis - von) > timedelta(days=370):
        raise HTTPException(status_code=400, detail="Zeitraum groesser als ein Jahr")
    return rueckblick(db, von, bis)


def _block_antwort(zeile: TimeBlock) -> dict:
    return {
        "id": zeile.id,
        "datum": zeile.date,
        "start": zeile.start,
        "ende": zeile.end,
        "minuten": int((zeile.end - zeile.start).total_seconds() // 60),
        "art": zeile.art,
        "titel": zeile.titel,
        "begruendung": zeile.begruendung,
        "status": zeile.status,
        "quelle": zeile.quelle,
        "herkunft_typ": zeile.herkunft_typ,
        "herkunft_id": zeile.herkunft_id,
    }


class ZeitIstEingang(BaseModel):
    """Ein gemessener Zeitraum, so wie ihn ein Gerät liefert.

    Typisiert an der Grenze, damit eine unbrauchbare Zeitangabe hier mit 422 und
    Klartext auffällt statt weiter unten als stille Ablehnung. `art` darf fehlen,
    wenn `roh_typ` gesetzt ist: dann übersetzt `zeit_ingest.ROH_TYP_MAP`.
    """

    quelle: str = Field(..., min_length=1, max_length=30)
    start: datetime
    ende: datetime
    art: str | None = Field(default=None, max_length=20)
    roh_typ: str | None = Field(default=None, max_length=100)
    titel: str | None = Field(default=None, max_length=300)
    extern_id: str | None = Field(default=None, max_length=200)
    # Die Kennung innerhalb der Quelle (`it-zertifikate`, `Minecraft`). Trägt die
    # Zuordnung zu einem Ziel über `HabitQuelle`.
    taetigkeit: str | None = Field(default=None, max_length=100)
    # Zurück zur Quelle (Partie, Buch, Trainingseinheit), für die Verlinkung im Tag.
    verweis: str | None = Field(default=None, max_length=300)
    # `automatisch` | `offen` | `manuell`, siehe `zeit_ingest.BESTAETIGT_STAENDE`.
    bestaetigt: str | None = Field(default=None, max_length=12)
    # Ob dieser Melder genau messen kann, und damit welche Mindestdauer beim
    # Zuschnitt gilt (1 statt 10 Minuten, `backend/zeitschnitt.py`). Der Melder
    # entscheidet das selbst, weil nur er weiß, ob er Zufallstreffer liefert: der
    # Laptop sieht ein Fenster im Vordergrund, eine Eigenapp sieht einen Zug.
    # Bewusst keine Liste der genauen Melder im Kalender, die veraltet still.
    genau: bool = False
    puls_schnitt: int | None = Field(default=None, ge=0, le=300)
    schritte: int | None = Field(default=None, ge=0)
    kalorien: int | None = Field(default=None, ge=0)


@ingest_router.post("/ingest")
def zeit_aufnehmen(
    eintraege: list[ZeitIstEingang] = Body(..., embed=True),
    x_zeit_token: str | None = Header(default=None, alias="X-Zeit-Token"),
    db: Session = Depends(get_db),
):
    """Gemessene Zeiträume entgegennehmen (Smartwatch über einen Adapter).

    Der Token geht als Header, nicht als Query-Parameter: uvicorn protokolliert
    jede Anfrage samt Query-String, und genau dadurch lag der `feed_token` dieses
    Dienstes über Monate im Klartext in Loki und damit in den Sicherungen
    (`backend/zugriffsprotokoll.py`). Ein Gerät kann im Gegensatz zu einem
    iCal-Abo eigene Header senden, also gibt es hier keinen Grund für den
    schwächeren Weg.
    """
    if not ingest_erlaubt():
        raise HTTPException(
            status_code=403,
            detail="Eingang ist nicht eingerichtet (ZEIT_INGEST_TOKENS nicht gesetzt)",
        )
    # Zuerst: ist das überhaupt einer unserer Token? Diese Prüfung ist
    # unabhängig von der Nutzlast und muss es sein. Eine Prüfung allein je Eintrag
    # lief bei einem **leeren** Stapel gar nicht und antwortete mit 200, und wer
    # die Gültigkeit seines Tokens mit einem leeren Stapel probiert, bekommt damit
    # eine falsche Zusage (am 2026-10-25 so gebaut und von einem Bestandstest
    # gefangen). Authentisierung und Berechtigung sind zwei Fragen.
    if not token_bekannt(x_zeit_token):
        raise HTTPException(status_code=401, detail="Token fehlt oder ist falsch")
    if len(eintraege) > 500:
        raise HTTPException(status_code=413, detail="Mehr als 500 Eintraege auf einmal")
    # Dann: geprüft wird JEDE Quelle des Stapels, nicht nur die erste.
    #
    # Der Token ist an seine Quelle gebunden (`zeit_ingest.pruefe_token`), und eine
    # Prüfung, die nur den ersten Eintrag ansieht, lässt den Rest des Stapels fremde
    # Quellen mitschreiben: die Bindung wäre dann nur ein Kommentar.
    #
    # Und der ganze Aufruf wird abgelehnt, nicht der einzelne Eintrag. Ein Melder,
    # der richtig arbeitet, schickt niemals eine fremde Quelle; tut er es doch, ist
    # sein Code falsch oder sein Token in fremder Hand. „19 geschrieben, 1
    # abgelehnt" sähe in beiden Fällen wie ein Datenproblem aus und nicht wie eines
    # mit den Rechten. Die Teilannahme in `stapel_aufnehmen` gilt für unbrauchbare
    # Eingaben, nicht für unerlaubte.
    for quelle in {e.quelle for e in eintraege}:
        if not pruefe_token(x_zeit_token, quelle):
            raise HTTPException(
                status_code=401,
                detail=f"Token fehlt oder gilt nicht fuer die Quelle '{quelle}'",
            )
    try:
        return stapel_aufnehmen(db, [e.model_dump() for e in eintraege])
    except IngestFehler as problem:
        raise HTTPException(status_code=400, detail=str(problem))
