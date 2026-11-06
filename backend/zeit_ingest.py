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


# Die drei Stände von `ZeitIst.bestaetigt`, in der Reihenfolge, in der sie
# entstehen: ein Melder meldet `automatisch`, etwas Unsicheres kommt als `offen`
# herein und wird zu `manuell`, wenn jemand es bestätigt oder selbst einträgt.
#
# ⚠️ Welche davon auf ein Wochenziel zählen, steht NICHT hier, sondern an der
# einen Stelle, die es angeht (`wochenbilanz.ZAEHLENDE_STAENDE`). Die Namen sagen
# die Herkunft und nicht die Geltung; wer das an zwei Orten entscheidet, bekommt
# zwei Antworten.
BESTAETIGT_STAENDE = ("automatisch", "offen", "manuell")


class IngestFehler(ValueError):
    """Eingabe unbrauchbar. Trägt eine Meldung, die dem Absender hilft."""


def _als_zeitpunkt(wert, feldname: str) -> datetime:
    """Nimmt einen Zeitpunkt entgegen, auch als ISO-Zeichenkette.

    Über HTTP kommt JSON, und JSON kennt keinen Zeittyp: `start` ist dort
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


def melder_tokens() -> dict[str, str]:
    """Die Token je Melder aus `ZEIT_INGEST_TOKENS`, Format `name:geheimnis,...`.

    Unbrauchbare Teile werden stillschweigend übersprungen. Das ist hier richtig:
    ein Tippfehler in einem Eintrag soll nicht die übrigen Melder aussperren, und
    der betroffene merkt es an seiner eigenen 401.
    """
    paare: dict[str, str] = {}
    for teil in settings.ZEIT_INGEST_TOKENS.split(","):
        name, _, geheimnis = teil.partition(":")
        name, geheimnis = name.strip(), geheimnis.strip()
        if name and geheimnis:
            paare[name] = geheimnis
    return paare


def ingest_erlaubt() -> bool:
    return bool(melder_tokens()) or bool(settings.ZEIT_INGEST_TOKEN)


def token_bekannt(token: str | None) -> bool:
    """Ist das überhaupt einer unserer Token, unabhängig von einer Quelle?

    Zwei Fragen, zwei Funktionen, und das ist hier nicht Förmlichkeit: „gehört
    dieser Token zum Haus" ist die Authentisierung, „darf er diese Quelle
    schreiben" die Berechtigung (`pruefe_token`). Die erste muss **immer** laufen,
    auch bei einem leeren Stapel.

    Woran das auffiel: eine Prüfung allein je Eintrag lief bei `eintraege: []`
    überhaupt nicht und antwortete mit 200. Ein Absender mit falschem Token hält
    sich danach für berechtigt, und wer die Gültigkeit eines Tokens mit einem
    leeren Stapel probiert, bekommt eine falsche Zusage. Gefunden am 2026-10-25
    von `test_tagesdecke_api.test_eingang_lehnt_falschen_token_ab`, der genau das
    tut.
    """
    if not token:
        return False
    tokens = melder_tokens()
    if tokens:
        return any(hmac.compare_digest(token, wert) for wert in tokens.values())
    if settings.ZEIT_INGEST_TOKEN:
        return hmac.compare_digest(token, settings.ZEIT_INGEST_TOKEN)
    return False


def pruefe_token(token: str | None, quelle: str | None = None) -> bool:
    """Prüft den Token, und zwar **gegen die Quelle**, in deren Namen er schreibt.

    Die Bindung an die Quelle ist der Zweck dieser Funktion, nicht ein Extra:
    ein Melder-Token darf genau seine eigene Quelle schreiben. Ohne das hätte jeder
    Melder Schreibrecht auf die Messungen aller anderen, und die Melder laufen auf
    verschiedenen Wirten mit verschiedener Angriffsfläche.

    Rückwärtsgang: ist `ZEIT_INGEST_TOKENS` nicht eingerichtet, gilt
    `ZEIT_INGEST_TOKEN` für jede Quelle. Das ist der Stand vor dem 2026-10-25 und
    bleibt gültig, damit das Einrichten der Melder-Token ein eigener Schritt sein
    kann und nicht in derselben Minute passieren muss wie das Ausrollen.

    `hmac.compare_digest` statt `==`, damit die Laufzeit nichts über den Token sagt.
    """
    if not token:
        return False
    tokens = melder_tokens()
    if tokens:
        erwartet = tokens.get((quelle or "").strip())
        if erwartet:
            return hmac.compare_digest(token, erwartet)
        # Kein Eintrag für diese Quelle: abgelehnt. Bewusst kein Rückfall auf den
        # alten Sammel-Token, sonst wäre die Bindung mit einem gesetzten Alt-Wert
        # wieder aufgehoben, und das stillschweigend.
        return False
    if settings.ZEIT_INGEST_TOKEN:
        return hmac.compare_digest(token, settings.ZEIT_INGEST_TOKEN)
    return False


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

    Nötig, weil `ZeitIst.date` die Auswertung trägt. Ein Schlaf von 23:10 bis
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
    # Die Kennung innerhalb der Quelle. Leerstring statt None, weil die Zuordnung
    # gegen `HabitQuelle.kennung` laeuft und die Spalte dort NOT NULL ist: zwei
    # verschiedene Schreibweisen fuer „keine Unterteilung" wuerden dieselbe Quelle
    # in zwei zerlegen, und eine davon traefe nie ein Ziel.
    taetigkeit = (roh.get("taetigkeit") or "").strip()
    bestaetigt = (roh.get("bestaetigt") or "automatisch").strip()
    if bestaetigt not in BESTAETIGT_STAENDE:
        raise IngestFehler(
            f"Unbekannter Stand '{bestaetigt}'. Erlaubt: {', '.join(BESTAETIGT_STAENDE)}"
        )
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
                taetigkeit=taetigkeit, verweis=roh.get("verweis"),
                bestaetigt=bestaetigt, genau=bool(roh.get("genau", False)),
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
            vorhanden.taetigkeit = taetigkeit
            vorhanden.verweis = roh.get("verweis")
            vorhanden.bestaetigt = bestaetigt
            vorhanden.genau = bool(roh.get("genau", False))
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
