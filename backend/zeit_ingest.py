"""Eingang für gemessene Zeit: was ein Gerät mitgeschrieben hat.

Vorgesehen für eine Smartwatch, bewusst ohne Festlegung auf ein Fabrikat. Der
Dienst nimmt ein neutrales Format entgegen (Beginn, Ende, Art, dazu optional
Puls, Schritte, Kalorien); was eine bestimmte Uhr daraus macht, ist Sache eines
Adapters, der außerhalb sitzt und nur übersetzt. Damit kostet ein Wechsel des
Fabrikats einen Adapter und nicht den halben Dienst.

Warum der Pfad einen eigenen Token braucht
------------------------------------------
Ein Gerät kann kein JWT halten und keinen signierten Mandanten-Header erzeugen.
Es bliebe also der headerlose Weg, der auf `DEFAULT_OWNER_SUB` zurückfällt. Genau
diese Bauart ist im Haus schon einmal teuer geworden: die Fitness-App hatte einen
solchen Pfad, geschützt allein durch die Bindung an 127.0.0.1, und hätte mit dem
ersten LAN-Vhost Trainings- und Körperdaten offengelegt (2026-09-14). Ein
Schreibpfad, den jemand ohne Nachweis erreicht, ist hier zusätzlich heikel, weil
er Bewegungsprofile entgegennimmt.

Deshalb: ohne gesetzten `ZEIT_INGEST_TOKEN` ist dieser Eingang **aus**. Nicht
offen, nicht auf den Owner zurückfallend, sondern abgelehnt. Wer ihn benutzen
will, vergibt ein Geheimnis; wer ihn nicht benutzt, hat keinen offenen Weg.
"""
from __future__ import annotations

import hmac
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import ZeitIst
from backend.tagesdecke import ARTEN
from backend.wanduhr import als_wanduhr

# Was gängige Geräte melden, übersetzt in das Vokabular der Decke (tagesdecke.ARTEN).
# Bewusst klein gehalten: ein Adapter, der mehr weiß, soll `art` gleich richtig
# setzen. Diese Tabelle ist nur die Rettung für den Fall, dass er es nicht tut.
ROH_TYP_MAP = {
    "sleep": "schlaf",
    "deep_sleep": "schlaf",
    "light_sleep": "schlaf",
    "rem": "schlaf",
    "nap": "erholung",
    "rest": "erholung",
    "meditation": "erholung",
    "workout": "training",
    "strength_training": "training",
    "running": "training",
    "cycling": "training",
    "swimming": "training",
    "walking": "grundlast",
    "housework": "grundlast",
    "meal": "grundlast",
    "commute": "grundlast",
    "work": "fix",
}


class IngestFehler(ValueError):
    """Eingabe unbrauchbar. Trägt eine Meldung, die dem Absender hilft."""


def _als_zeitpunkt(wert, feldname: str) -> datetime:
    """Nimmt einen Zeitpunkt entgegen, auch als ISO-Zeichenkette.

    ★ Über HTTP kommt JSON, und JSON kennt keinen Zeittyp: `start` ist dort
    immer eine Zeichenkette. Die erste Fassung verlangte ein `datetime` und
    lehnte deshalb **jeden** echten Aufruf ab, während sie mit 200 und
    `aufgenommen: 0` antwortete. Das sah wie ein leerer Tag aus, nicht wie ein
    Fehler, und wäre an einem Gerät, das nachts unbeobachtet liefert, wochenlang
    unbemerkt geblieben.
    """
    if isinstance(wert, datetime):
        return wert
    if isinstance(wert, str) and wert.strip():
        text = wert.strip()
        # datetime.fromisoformat kennt das abschliessende Z erst ab Python 3.11.
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            raise IngestFehler(
                f"'{feldname}' ist kein lesbarer Zeitpunkt: {wert!r} "
                "(erwartet ISO-8601, z. B. 2026-09-16T18:00:00+02:00)"
            )
    raise IngestFehler(f"'{feldname}' fehlt oder ist kein Zeitpunkt")


def ingest_erlaubt() -> bool:
    return bool(settings.ZEIT_INGEST_TOKEN)


def pruefe_token(token: str | None) -> bool:
    """Vergleicht den mitgelieferten Token, ohne über die Laufzeit zu plaudern."""
    if not ingest_erlaubt():
        return False
    if not token:
        return False
    return hmac.compare_digest(token, settings.ZEIT_INGEST_TOKEN)


def _art_bestimmen(art: str | None, roh_typ: str | None) -> str:
    if art and art in ARTEN:
        return art
    if roh_typ:
        gefunden = ROH_TYP_MAP.get(roh_typ.strip().lower())
        if gefunden:
            return gefunden
    if art:
        raise IngestFehler(
            f"Unbekannte Art '{art}'. Erlaubt: {', '.join(sorted(ARTEN))}"
        )
    raise IngestFehler("Weder 'art' noch ein bekannter 'roh_typ' angegeben")


def _tagesstuecke(start: datetime, ende: datetime) -> list[tuple[date, datetime, datetime]]:
    """Ein Intervall an Mitternacht zerlegen, je Kalendertag ein Stück.

    ★ Nötig, weil `ZeitIst.date` die Auswertung trägt. Ein Schlaf von 23:10 bis
    06:40 gehört zu zwei Tagen; ungeteilt abgelegt, fehlte er der Tagesbilanz des
    Folgetags vollständig, und zwar unauffällig: die Zeile existiert ja, sie
    hängt nur am falschen Datum.
    """
    stuecke: list[tuple[date, datetime, datetime]] = []
    cursor = start
    while cursor < ende:
        tagesende = datetime.combine(cursor.date(), datetime.min.time()) + timedelta(days=1)
        schnitt = min(ende, tagesende)
        stuecke.append((cursor.date(), cursor, schnitt))
        cursor = schnitt
    return stuecke


def eintrag_aufnehmen(db: Session, roh: dict) -> tuple[ZeitIst | None, str]:
    """Einen gemessenen Zeitraum aufnehmen. Gibt (Zeile, Vorgang) zurück.

    Vorgang ist `neu`, `aktualisiert` oder `uebersprungen`. Idempotent über
    (`quelle`, `extern_id`): dieselbe Messung zweimal geschickt erzeugt kein
    Doppel. Ohne das hätte ein Tag nach einem Verbindungsabbruch mehr als 24
    Stunden Ist, und die Auswertung wäre still falsch.
    """
    quelle = (roh.get("quelle") or "").strip()
    if not quelle:
        raise IngestFehler("'quelle' fehlt (z. B. 'uhr' oder der Geraetename)")

    start = _als_zeitpunkt(roh.get("start"), "start")
    ende = _als_zeitpunkt(roh.get("ende"), "ende")
    start = als_wanduhr(start)
    ende = als_wanduhr(ende)
    if ende <= start:
        raise IngestFehler("'ende' muss nach 'start' liegen")
    if (ende - start) > timedelta(days=2):
        raise IngestFehler("Zeitraum laenger als zwei Tage, das ist keine Aktivitaet")

    art = _art_bestimmen(roh.get("art"), roh.get("roh_typ"))
    extern_id = (roh.get("extern_id") or "").strip()
    if not extern_id:
        # Ohne Kennung des Absenders bleibt nur der Beginn als Identität. Das ist
        # schwächer (eine korrigierte Messung legt eine zweite Zeile an), aber
        # besser als gar keine Idempotenz.
        extern_id = f"auto-{start.isoformat()}"

    stuecke = _tagesstuecke(start, ende)
    letzte: ZeitIst | None = None
    vorgang = "uebersprungen"
    for tag, s_start, s_ende in stuecke:
        # Bei mehreren Tagesstücken braucht jedes eine eigene Kennung, sonst
        # überschreiben sie sich gegenseitig über die Eindeutigkeitsbedingung.
        kennung = extern_id if len(stuecke) == 1 else f"{extern_id}#{tag.isoformat()}"
        vorhanden = (
            db.query(ZeitIst)
            .filter(ZeitIst.quelle == quelle, ZeitIst.extern_id == kennung)
            .first()
        )
        if vorhanden is None:
            zeile = ZeitIst(
                date=tag, start=s_start, end=s_ende, art=art,
                titel=roh.get("titel"), quelle=quelle, extern_id=kennung,
                roh_typ=roh.get("roh_typ"), puls_schnitt=roh.get("puls_schnitt"),
                schritte=roh.get("schritte"), kalorien=roh.get("kalorien"),
            )
            db.add(zeile)
            letzte, vorgang = zeile, "neu"
        else:
            vorhanden.date = tag
            vorhanden.start = s_start
            vorhanden.end = s_ende
            vorhanden.art = art
            vorhanden.titel = roh.get("titel")
            vorhanden.roh_typ = roh.get("roh_typ")
            vorhanden.puls_schnitt = roh.get("puls_schnitt")
            vorhanden.schritte = roh.get("schritte")
            vorhanden.kalorien = roh.get("kalorien")
            letzte, vorgang = vorhanden, "aktualisiert"
    return letzte, vorgang


def stapel_aufnehmen(db: Session, eintraege: list[dict]) -> dict:
    """Mehrere Messungen auf einmal. Fehlerhafte werden benannt, nicht verschluckt.

    Ein Gerät liefert einen Tag am Stück. Bräche der ganze Stapel an einem
    unbrauchbaren Eintrag, verlöre man die 19 guten mit. Umgekehrt wäre stilles
    Überspringen schlimmer: man hielte die Bilanz für vollständig. Also beides
    melden und nur die guten schreiben.
    """
    neu = aktualisiert = 0
    fehler: list[dict] = []
    for index, roh in enumerate(eintraege):
        try:
            _, vorgang = eintrag_aufnehmen(db, roh)
            if vorgang == "neu":
                neu += 1
            elif vorgang == "aktualisiert":
                aktualisiert += 1
        except IngestFehler as problem:
            fehler.append({"index": index, "meldung": str(problem)})
    db.commit()
    return {
        "aufgenommen": neu,
        "aktualisiert": aktualisiert,
        "abgelehnt": len(fehler),
        "fehler": fehler,
    }
