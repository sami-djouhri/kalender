#!/usr/bin/env python3
"""Kalender-Simulation: Testkonto anlegen, Nutzung erfinden, Verhalten messen.

WOZU DAS HIER EXISTIERT
-----------------------
Die Testsuite prueft Einzelteile gegen erwartete Rueckgabewerte. Sie sagt nichts
darueber, ob der Kalender ueber Wochen hinweg *als Ganzes* das Richtige tut:
Tagestyp, Serien ueber die Zeitumstellung, Sekretaer, Mandantentrennung und der
iCal-Feed greifen erst im Zusammenspiel ineinander. Genau da lagen die Befunde,
die diese Datei aufgedeckt hat.

Das Skript baut deshalb eine **komplette erfundene Nutzung** auf einer frischen
Datenbank auf (ein Testkonto neben dem Owner, ein Quartal Alltag) und laesst
danach **Sonden** darueber laufen. Jede Sonde prueft eine Zusage, die der Dienst
nach aussen macht, und meldet Klartext statt eines Rueckgabewerts.

WICHTIG, ES WIRD NIE PRODUKTIONSDATEN ANGEFASST
------------------------------------------------
`DATABASE_URL` zeigt zwingend auf eine Datei unter /tmp; das Skript bricht ab,
wenn jemand es auf `data/kalender.db` richtet. Aufruf ueber `./simulieren.sh`.

LESEN DER AUSGABE
-----------------
    ok      Zusage gehalten
    BEFUND  Zusage gebrochen, mit Beleg (Ist gegen Soll)
    info    Messwert ohne Wertung (Laufzeit, Anzahl)

Ein BEFUND ist kein Absturz: alle Sonden laufen zu Ende, damit ein Lauf das
vollstaendige Bild zeigt und nicht nur den ersten Fehler.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

# --- Schutzgurt: niemals gegen die Produktions-DB laufen -------------------
_DB = os.environ.get("DATABASE_URL", "")
if not _DB.startswith("sqlite:////tmp/"):
    sys.exit(
        "ABBRUCH: DATABASE_URL muss auf eine Datei unter /tmp zeigen "
        f"(ist: {_DB!r}). Aufruf ueber ./simulieren.sh."
    )
os.environ.setdefault("KALENDER_PASSWORD", "sim-password")
os.environ.setdefault("SECRET_KEY", "sim-secret")
# Cross-App-Konnektoren aus: die Simulation soll nichts im Netz anfassen.
os.environ.setdefault("CROSS_APP_ENABLED", "0")
os.environ.setdefault("ASSISTANT_NUDGES_ENABLED", "0")

_db_datei = _DB.replace("sqlite:////", "/")
for _rest in (_db_datei, _db_datei + "-wal", _db_datei + "-shm"):
    if os.path.exists(_rest):
        os.remove(_rest)

from fastapi.testclient import TestClient  # noqa: E402

from backend.database import Base, engine  # noqa: E402
from backend.main import app, create_jwt_token  # noqa: E402
from backend.system_calendars import (  # noqa: E402
    DAYTYPE_CALENDARS,
    FEIERTAG_CALENDAR_ID,
    TERMINE_CAL_ID,
)

TESTKONTO = "sim-testkonto-2026"


# ==========================================================================
# Berichtsgeruest
# ==========================================================================

class Bericht:
    def __init__(self) -> None:
        self.zeilen: list[tuple[str, str, str]] = []
        self.aktueller_block = ""

    def block(self, titel: str) -> None:
        self.aktueller_block = titel
        print(f"\n\033[1m── {titel} ─────────────────────────────────\033[0m")

    def ok(self, text: str) -> None:
        self.zeilen.append(("ok", self.aktueller_block, text))
        print(f"  \033[32mok\033[0m      {text}")

    def befund(self, text: str, beleg: str = "") -> None:
        self.zeilen.append(("BEFUND", self.aktueller_block, text))
        print(f"  \033[31mBEFUND\033[0m  {text}")
        if beleg:
            for zeile in beleg.splitlines():
                print(f"          \033[2m{zeile}\033[0m")

    def info(self, text: str) -> None:
        self.zeilen.append(("info", self.aktueller_block, text))
        print(f"  \033[2minfo    {text}\033[0m")

    def zusammenfassung(self) -> int:
        zaehler = Counter(art for art, _, _ in self.zeilen)
        print("\n" + "=" * 74)
        print(
            f"  {zaehler['ok']} Zusagen gehalten · "
            f"\033[31m{zaehler['BEFUND']} Befunde\033[0m · {zaehler['info']} Messwerte"
        )
        if zaehler["BEFUND"]:
            print("\n  Befunde nach Bereich:")
            for art, block, text in self.zeilen:
                if art == "BEFUND":
                    print(f"    · [{block}] {text}")
        print("=" * 74)
        return 1 if zaehler["BEFUND"] else 0


B = Bericht()


@contextmanager
def sonde(name: str):
    """Eine Sonde darf scheitern, ohne den Lauf zu beenden."""
    B.block(name)
    try:
        yield
    except Exception:
        B.befund(
            f"Sonde '{name}' ist abgestuerzt",
            traceback.format_exc(limit=6),
        )


# ==========================================================================
# Hilfen
# ==========================================================================

def iso_z(dt: datetime) -> str:
    """Wie das Frontend es schickt: Berlin-Wanduhr → UTC-ISO mit Z.

    `frontend/js/app.js` ruft `Date.toISOString()` auf. Genau dieser String
    landet als Fensterrand an /api/events, deshalb simuliert die Sonde ihn
    woertlich statt einer bequemen naiven Form.
    """
    return dt.replace(tzinfo=BERLIN).astimezone(ZoneInfo("UTC")).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def letzter_sonntag(jahr: int, monat: int) -> date:
    d = date(jahr, monat + 1, 1) - timedelta(days=1) if monat < 12 else date(jahr, 12, 31)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d


def titel(eintraege) -> list[str]:
    return [e["title"] for e in eintraege]


# ==========================================================================
# Welt aufbauen
# ==========================================================================

class Welt:
    """Ein Quartal erfundener Alltag fuer zwei Konten (Owner + Testkonto)."""

    def __init__(self, client: TestClient) -> None:
        self.c = client
        auth = {"Authorization": f"Bearer {create_jwt_token()}"}
        self.owner = auth
        self.test = {**auth, "X-Saganta-Sub": TESTKONTO}
        self.jahr = date.today().year
        self.fruehjahr_umstellung = letzter_sonntag(self.jahr, 3)   # 02:00 → 03:00
        self.herbst_umstellung = letzter_sonntag(self.jahr, 10)     # 03:00 → 02:00
        self.feed_token = self._feed_token()
        # Das Fenster liegt bewusst UM HEUTE HERUM: vier Wochen Vergangenheit,
        # acht Wochen Zukunft. Ein erster Entwurf lag komplett in der Vergangenheit
        #, dort plant der Gewohnheiten-Planer grundsaetzlich nichts
        # (scheduler.py: `if day < today: continue`), und die Sonde mass Leere
        # statt Verhalten.
        heute = date.today()
        self.start = heute - timedelta(days=heute.weekday()) - timedelta(weeks=4)
        self.tage = 84
        self.plan: dict[date, str] = {}

        # Feste Bezugspunkte, damit die Sonden nicht mit Offsets rechnen muessen.
        self.urlaub_ab = self.start + timedelta(weeks=1)            # Vergangenheit
        self.krank_vergangen = {self.start + timedelta(weeks=3, days=1),
                                self.start + timedelta(weeks=3, days=2)}
        # Krankheitstage AUCH in der Zukunft, sonst ist „an Krankheitstagen wird
        # nichts geplant" eine leere Zusage: dort plant der Planer ohnehin nichts.
        self.krank_zukunft = {self.start + timedelta(weeks=6, days=1),
                              self.start + timedelta(weeks=6, days=3)}
        self.planungs_woche = self.start + timedelta(weeks=6)       # Zukunft

    def _feed_token(self) -> str:
        from backend.database import SessionLocal
        from backend.models import Setting

        db = SessionLocal()
        db.info["owner_sub"] = None
        try:
            s = db.query(Setting).filter(Setting.key == "feed_token").first()
            return s.value if s else ""
        finally:
            db.close()

    # -- Tagestypen ---------------------------------------------------------

    def tagestypen_setzen(self) -> None:
        """Realistischer Rhythmus: Schicht, eine Urlaubswoche, zwei Krankheitstage.

        Der Plan wird mitgeschrieben (self.plan), die Sonde vergleicht spaeter
        gegen ihn, nicht gegen die Datenbank. Sonst prueft man den Dienst gegen
        sich selbst.
        """
        krank = self.krank_vergangen | self.krank_zukunft
        urlaub_ab = self.urlaub_ab

        for i in range(self.tage):
            tag = self.start + timedelta(days=i)
            if tag.weekday() >= 5:
                self.plan[tag] = "wochenende"
                continue
            if urlaub_ab <= tag < urlaub_ab + timedelta(days=5):
                self._setzen(tag, "urlaub")
                self.plan[tag] = "urlaub"
            elif tag in krank:
                self._setzen(tag, "krank")
                self.plan[tag] = "krank"
            else:
                self._setzen(tag, "arbeit")
                self.plan[tag] = "arbeit"

    def _setzen(self, tag: date, typ: str) -> None:
        r = self.c.post(
            "/api/day-type/set",
            params={"date": tag.isoformat(), "type": typ},
            headers=self.owner,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Tagestyp {typ} am {tag} abgelehnt: {r.status_code} {r.text}")

    # -- Termine ------------------------------------------------------------

    def termine_anlegen(self) -> None:
        """Serien und Einzeltermine, wie sie ein Mensch anlegt."""
        self.serien: dict[str, str] = {}

        def anlegen(headers, **daten) -> str:
            r = self.c.post("/api/events", json={"calendar_id": TERMINE_CAL_ID, **daten},
                            headers=headers)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Termin abgelehnt: {r.status_code} {r.text}")
            return r.json()["id"]

        # Woechentlich Sport, Mo+Mi 18:00, die Serie, an der Auswertung haengt.
        self.serien["sport"] = anlegen(
            self.owner,
            title="Sport",
            start=f"{self.start}T18:00:00",
            end=f"{self.start}T19:30:00",
            recurrence_rule="FREQ=WEEKLY;BYDAY=MO,WE",
            activity_type="sport",
        )
        # Taeglich 10:00 quer durch beide Zeitumstellungen.
        self.serien["standup"] = anlegen(
            self.owner,
            title="Standup",
            start=f"{self.fruehjahr_umstellung - timedelta(days=3)}T10:00:00",
            end=f"{self.fruehjahr_umstellung - timedelta(days=3)}T10:15:00",
            recurrence_rule="FREQ=DAILY",
        )
        # Spaeter Abendtermin: Randfall fuer den Fensterschnitt.
        self.spaet_tag = self.start + timedelta(days=9)
        self.serien["spaet"] = anlegen(
            self.owner,
            title="Spaeter Abendtermin",
            start=f"{self.spaet_tag}T23:30:00",
            end=f"{self.spaet_tag}T23:59:00",
        )
        # Ganztags ueber drei Tage.
        self.mehrtag = self.start + timedelta(days=15)
        self.serien["mehrtag"] = anlegen(
            self.owner,
            title="Messe",
            start=f"{self.mehrtag}T00:00:00",
            end=f"{self.mehrtag + timedelta(days=3)}T00:00:00",
            all_day=True,
        )
        # Testkonto: eigener Termin, gleicher Tag, gleicher Kalender.
        self.serien["fremd"] = anlegen(
            self.test,
            title="Testkonto-Privattermin",
            start=f"{self.spaet_tag}T23:30:00",
            end=f"{self.spaet_tag}T23:59:00",
        )

    # -- Kontakte, Gewohnheiten, Aufgaben ----------------------------------

    def stammdaten(self) -> None:
        # 29.02. bewusst dabei: der Schalttags-Geburtstag ist der Randfall,
        # an dem die Geburtstags-Erzeugung auf den 28.02. ausweichen muss.
        for name, geburtstag in (("Oma Sim", "1949-05-20"), ("Kind Sim", "2016-02-29")):
            r = self.c.post("/api/contacts", json={"name": name, "birthday": geburtstag},
                            headers=self.owner)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Kontakt {name}: {r.status_code} {r.text}")

        self.habits = {}
        for name, kategorie, dauer, stunden, lernmodus in (
            ("Spanisch", "lernen", 45, 4.0, True),
            ("Krafttraining", "sonstige", 60, 3.0, False),
            ("Lesen", "lesen", 30, 2.5, False),
        ):
            r = self.c.post("/api/habits", json={
                "name": name, "category": kategorie,
                "session_duration_minutes": dauer,
                "target_hours_per_week": stunden,
                "learning_mode": lernmodus,
            }, headers=self.owner)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Habit {name}: {r.status_code} {r.text}")
            self.habits[name] = r.json()["id"]

        self.todos = []
        for i in range(12):
            faellig = self.start + timedelta(days=i * 3)
            r = self.c.post("/api/todos", json={
                "title": f"Aufgabe {i + 1}",
                "due_date": faellig.isoformat(),
                "estimated_minutes": 30 + (i % 4) * 15,
                "energy_required": ("niedrig", "mittel", "hoch")[i % 3],
            }, headers=self.owner)
            if r.status_code in (200, 201):
                self.todos.append(r.json()["id"])

    def aufbauen(self) -> None:
        self.tagestypen_setzen()
        self.termine_anlegen()
        self.stammdaten()


# ==========================================================================
# Sonden
# ==========================================================================

def s_tagestyp_vertrag(w: Welt) -> None:
    """Die eine Zusage, an der der Wecker haengt (HA fragt /today + /tomorrow)."""
    with sonde("Tagestyp-Vertrag (HA-Sicht)"):
        erwartet_ha = {"wochenende": "frei", "krank": "frei", "urlaub": "urlaub",
                       "arbeit": "arbeit"}
        abweichungen = []
        for tag, geplant in w.plan.items():
            r = w.c.get("/api/day-type", params={"date": tag.isoformat(), "token": w.feed_token})
            ist = r.json()["type"]
            soll = erwartet_ha[geplant]
            # Feiertage schlagen alles, der Seed kennt sie, der Plan nicht.
            if ist == "feiertag":
                continue
            if ist != soll:
                abweichungen.append(f"{tag} ({geplant}): ist={ist} soll={soll}")
        if abweichungen:
            B.befund(f"{len(abweichungen)} Tage mit falschem Tagestyp",
                     "\n".join(abweichungen[:10]))
        else:
            B.ok(f"{len(w.plan)} Tage: Tagestyp entspricht durchgehend dem Plan")

        # Prioritaet: Feiertag schlaegt Urlaub, Krank meldet sich als 'frei'
        feiertag = date(w.jahr, 12, 25)
        w.c.post("/api/day-type/set", params={"date": feiertag.isoformat(), "type": "urlaub"},
                 headers=w.owner)
        typ = w.c.get("/api/day-type", params={"date": feiertag.isoformat(),
                                               "token": w.feed_token}).json()["type"]
        if typ == "feiertag":
            B.ok("Feiertag schlaegt Urlaub (25.12. mit gesetztem Urlaub bleibt feiertag)")
        else:
            B.befund(f"Feiertag verliert gegen Urlaub: 25.12. meldet '{typ}'")


def s_anzeige_tagestyp(w: Welt) -> None:
    with sonde("Tagestyp-Anzeige (Frontend-Sicht)"):
        woche = w.start + timedelta(weeks=3)   # Woche mit den zwei Krankheitstagen
        r = w.c.get("/api/day-type/week", params={"week_start": woche.isoformat()},
                    headers=w.owner)
        tage = {e["date"]: e["type"] for e in r.json()}
        krank_tage = [t for t, typ in tage.items() if typ == "krank"]
        if len(krank_tage) == 2:
            B.ok("Krankheitstage erscheinen in der Anzeige als 'krank' (HA sieht 'frei')")
        else:
            B.befund(f"Krankheitstage in der Anzeige: {krank_tage} (erwartet 2)")
        we = [t for t, typ in tage.items() if typ == "wochenende"]
        if len(we) == 2:
            B.ok("Wochenende wird als eigener Anzeigetyp gefuehrt")
        else:
            B.befund(f"Wochenendtage: {we} (erwartet 2)")


def s_fensterschnitt(w: Welt) -> None:
    """Der Fensterrand, den das Frontend woertlich so schickt."""
    with sonde("Fensterschnitt / Zeitzone"):
        tag = w.spaet_tag
        eigener_tag = w.c.get("/api/events", params={
            "start": iso_z(datetime(tag.year, tag.month, tag.day, 0, 0)),
            "end": iso_z(datetime(tag.year, tag.month, tag.day, 0, 0) + timedelta(days=1)),
        }, headers=w.owner).json()
        if "Spaeter Abendtermin" in titel(eigener_tag):
            B.ok("23:30-Termin erscheint an seinem eigenen Tag")
        else:
            B.befund("23:30-Termin fehlt am eigenen Tag",
                     f"Fenster {tag} 00:00–24:00 lieferte: {titel(eigener_tag)}")

        folgetag = datetime(tag.year, tag.month, tag.day) + timedelta(days=1)
        naechster = w.c.get("/api/events", params={
            "start": iso_z(folgetag),
            "end": iso_z(folgetag + timedelta(days=1)),
        }, headers=w.owner).json()
        if "Spaeter Abendtermin" in titel(naechster):
            B.befund(
                "23:30-Termin erscheint zusaetzlich am FOLGETAG",
                "Das Fenster kommt als UTC-ISO ('...T22:00:00.000Z') herein, die\n"
                "Termine liegen als Berliner Wanduhr in der Datenbank. Verglichen\n"
                "werden damit zwei verschiedene Zeitrechnungen, das Fenster ist um\n"
                "den UTC-Versatz verschoben.\n"
                f"Fenster {folgetag.date()} 00:00–24:00 lieferte: {titel(naechster)}",
            )
        else:
            B.ok("23:30-Termin erscheint NICHT am Folgetag")


def s_zeitumstellung(w: Welt) -> None:
    with sonde("Zeitumstellung"):
        for name, umstellung in (("Fruehjahr", w.fruehjahr_umstellung),
                                 ("Herbst", w.herbst_umstellung)):
            von = datetime(umstellung.year, umstellung.month, umstellung.day) - timedelta(days=2)
            bis = von + timedelta(days=5)
            events = w.c.get("/api/events", params={
                "start": von.isoformat(), "end": bis.isoformat(),
            }, headers=w.owner).json()
            standups = [e for e in events if e["title"] == "Standup"]
            zeiten = sorted({e["start"][11:16] for e in standups})
            if zeiten == ["10:00"]:
                B.ok(f"{name}sumstellung: Serie bleibt bei 10:00 Wanduhr "
                     f"({len(standups)} Instanzen)")
            else:
                B.befund(f"{name}sumstellung verschiebt die Serie",
                         f"Startzeiten im Fenster: {zeiten}")


def s_serien(w: Welt) -> None:
    with sonde("Serien: Ausnahme und Abschnitt"):
        von = datetime(w.start.year, w.start.month, w.start.day)
        bis = von + timedelta(days=28)
        p = {"start": von.isoformat(), "end": bis.isoformat()}

        vorher = [e for e in w.c.get("/api/events", params=p, headers=w.owner).json()
                  if e["title"] == "Sport"]
        B.info(f"Sport-Serie (Mo+Mi, 4 Wochen): {len(vorher)} Instanzen")

        # Eine Instanz herausnehmen
        opfer = vorher[2]["start"][:10]
        r = w.c.delete(f"/api/events/{w.serien['sport']}/instances/{opfer}", headers=w.owner)
        nachher = [e for e in w.c.get("/api/events", params=p, headers=w.owner).json()
                   if e["title"] == "Sport"]
        if r.status_code == 204 and len(nachher) == len(vorher) - 1:
            B.ok(f"'Nur dieser Termin' entfernt genau eine Instanz ({opfer})")
        else:
            B.befund(f"Ausnahme entfernte {len(vorher) - len(nachher)} Instanzen "
                     f"(erwartet 1), HTTP {r.status_code}")

        # Ab einem Datum abschneiden
        schnitt = nachher[len(nachher) // 2]["start"][:10]
        r = w.c.post(f"/api/events/{w.serien['sport']}/truncate",
                     params={"occ_date": schnitt}, headers=w.owner)
        rest = [e for e in w.c.get("/api/events", params=p, headers=w.owner).json()
                if e["title"] == "Sport"]
        zu_spaet = [e["start"][:10] for e in rest if e["start"][:10] >= schnitt]
        if r.status_code == 204 and not zu_spaet:
            B.ok(f"'Diese und folgende' schneidet ab {schnitt} sauber ab "
                 f"({len(rest)} Instanzen bleiben)")
        else:
            B.befund(f"Nach dem Abschneiden ab {schnitt} sind noch Instanzen da: {zu_spaet}")


def s_mandanten(w: Welt) -> None:
    with sonde("Mandantentrennung unter Last"):
        tag = w.spaet_tag
        p = {"start": f"{tag}T00:00:00", "end": f"{tag + timedelta(days=1)}T00:00:00"}
        owner_sicht = titel(w.c.get("/api/events", params=p, headers=w.owner).json())
        test_sicht = titel(w.c.get("/api/events", params=p, headers=w.test).json())

        if "Testkonto-Privattermin" not in owner_sicht:
            B.ok("Owner sieht den Termin des Testkontos nicht")
        else:
            B.befund("Owner sieht den Privattermin des Testkontos")
        if "Spaeter Abendtermin" not in test_sicht:
            B.ok("Testkonto sieht den Termin des Owners nicht")
        else:
            B.befund("Testkonto sieht den Termin des Owners")

        typ = w.c.get("/api/day-type/week",
                      params={"week_start": w.start.isoformat()},
                      headers=w.test).json()
        arbeitstage = [e for e in typ if e["type"] == "arbeit"]
        if not arbeitstage:
            B.ok("Testkonto erbt die Arbeitstage des Owners nicht")
        else:
            B.befund(f"Testkonto erbt {len(arbeitstage)} Arbeitstage des Owners")

        # Feiertage muessen fuer beide da sein, sonst kippt die Tagestyp-Kette
        p_fei = {"start": f"{w.jahr}-12-25T00:00:00", "end": f"{w.jahr}-12-26T00:00:00"}
        if any("Weihnacht" in t for t in
               titel(w.c.get("/api/events", params=p_fei, headers=w.test).json())):
            B.ok("Feiertage bleiben fuer jeden Mandanten sichtbar")
        else:
            B.befund("Testkonto sieht die Feiertage nicht: Tagestyp-Kette waere blind")


def s_globale_daten_schuetzen(w: Welt) -> None:
    """Feiertage sind die einzigen mandantenuebergreifenden Zeilen, wer sie
    aendern darf, aendert sie fuer alle."""
    with sonde("Schutz der globalen Feiertage"):
        p = {"start": f"{w.jahr}-12-25T00:00:00", "end": f"{w.jahr}-12-26T00:00:00"}
        feiertage = [e for e in w.c.get("/api/events", params=p, headers=w.test).json()
                     if e["calendar_id"] == FEIERTAG_CALENDAR_ID]
        if not feiertage:
            B.info("kein Feiertag im Pruefzeitraum gefunden: Sonde uebersprungen")
            return
        ziel = feiertage[0]["id"]

        r = w.c.put(f"/api/events/{ziel}", json={"title": "Vom Testkonto umbenannt"},
                    headers=w.test)
        if r.status_code in (403, 404):
            B.ok(f"Testkonto darf einen Feiertag nicht umbenennen (HTTP {r.status_code})")
        else:
            danach = titel(w.c.get("/api/events", params=p, headers=w.owner).json())
            B.befund(
                f"Testkonto hat einen globalen Feiertag umbenannt (HTTP {r.status_code})",
                "Der Feiertagskalender ist die einzige mandantenuebergreifend\n"
                "sichtbare Zeile. Anlegen ist mit 403 gesperrt, Aendern nicht:\n"
                "die Sperre sitzt nur im Erzeugungspfad.\n"
                f"Der Owner sieht jetzt: {danach}",
            )

        r = w.c.delete(f"/api/events/{ziel}", headers=w.test)
        if r.status_code in (403, 404):
            B.ok(f"Testkonto darf einen Feiertag nicht loeschen (HTTP {r.status_code})")
        else:
            danach = titel(w.c.get("/api/events", params=p, headers=w.owner).json())
            B.befund(
                f"Testkonto hat einen globalen Feiertag geloescht (HTTP {r.status_code})",
                f"Der Owner sieht am 25.12. jetzt: {danach or 'nichts'}\n"
                "Der Tagestyp dieses Tages faellt damit fuer ALLE von 'feiertag' zurueck.",
            )


def s_tagestyp_kalender_schreibbar(w: Welt) -> None:
    """Tagestyp-Kalender sind Steuerdaten, keine Ablage."""
    with sonde("Tagestyp-Kalender als Steuerdaten"):
        tag = w.start + timedelta(days=61)   # ein normaler Arbeitstag
        while tag.weekday() >= 5:
            tag += timedelta(days=1)

        r = w.c.post("/api/events", json={
            "calendar_id": DAYTYPE_CALENDARS["urlaub"],
            "title": "Von Hand in den Urlaubskalender",
            "start": f"{tag}T00:00:00", "end": f"{tag + timedelta(days=1)}T00:00:00",
            "all_day": True,
        }, headers=w.owner)

        if r.status_code == 403:
            B.ok("Direktes Schreiben in einen Tagestyp-Kalender ist gesperrt")
            _mehrdeutigen_tag_pruefen(w)
            return

        typ = w.c.get("/api/day-type", params={"date": tag.isoformat(),
                                               "token": w.feed_token}).json()["type"]
        # Steht jetzt Arbeit UND Urlaub am selben Tag? Dann kann der Umschalter
        # den Tag nicht mehr aufloesen.
        w.c.post("/api/day-type/set", params={"date": tag.isoformat(), "type": "urlaub"},
                 headers=w.owner)
        nach_toggle = w.c.get("/api/day-type", params={"date": tag.isoformat(),
                                                       "token": w.feed_token}).json()["type"]
        B.befund(
            f"Termine lassen sich frei in Tagestyp-Kalender schreiben (HTTP {r.status_code})",
            f"Der {tag} war 'arbeit', meldet nach dem Handeintrag '{typ}'.\n"
            f"Nach einem Umschalt-Versuch auf urlaub: '{nach_toggle}'.\n"
            "Der Umschalter entfernt immer nur EIN Tagestyp-Ereignis: bleiben zwei\n"
            "uebrig, laesst sich der Tag ueber die Oberflaeche nicht mehr aufloesen.",
        )


def _mehrdeutigen_tag_pruefen(w: Welt) -> None:
    """Selbstheilung: ein Tag mit zwei Tagestyp-Ereignissen muss aufloesbar bleiben.

    Solche Tage kann es aus der Zeit vor der Schreibsperre geben. Sie werden
    deshalb hier absichtlich an der API vorbei erzeugt.
    """
    from backend.database import SessionLocal
    from backend.models import Event

    tag = w.start + timedelta(days=68)
    while tag.weekday() >= 5:
        tag += timedelta(days=1)

    db = SessionLocal()
    try:
        for typ in ("arbeit", "urlaub"):
            db.add(Event(
                calendar_id=DAYTYPE_CALENDARS[typ], title=typ.capitalize(),
                start=datetime(tag.year, tag.month, tag.day),
                end=datetime(tag.year, tag.month, tag.day) + timedelta(days=1),
                all_day=True,
            ))
        db.commit()
    finally:
        db.close()

    gemeldet = w.c.post("/api/day-type/set",
                        params={"date": tag.isoformat(), "type": "urlaub"},
                        headers=w.owner).json()["type"]
    tatsaechlich = w.c.get("/api/day-type", params={"date": tag.isoformat(),
                                                    "token": w.feed_token}).json()["type"]
    if gemeldet == tatsaechlich:
        B.ok(f"Mehrdeutiger Tag wird beim Umschalten aufgeraeumt "
             f"(Oberflaeche und Home Assistant sagen beide '{gemeldet}')")
    else:
        B.befund(
            "Oberflaeche und Home Assistant sagen Verschiedenes",
            f"Der Umschalter meldet '{gemeldet}', /api/day-type liefert "
            f"'{tatsaechlich}'.\nAm HA-Wert haengt das Weckfenster.",
        )


def s_vertrag_eingefroren(w: Welt) -> None:
    """Ebene 1 des API-Vertrags ist eingefroren, nur additiv erweiterbar.

    Quelle: ~/homelab-work/KALENDER_API_VERTRAG_2026-08-16.md. Wer hier ein Feld
    umbenennt, aendert ueber sensor.kalender_tagestyp das Weckverhalten.
    """
    with sonde("Eingefrorener API-Vertrag (Ebene 1)"):
        pflicht = {
            "/api/day-type/today": {"date", "type"},
            "/api/day-type/tomorrow": {"date", "type"},
            "/api/day-type/events": {"date", "events"},
            "/api/day-type/goals": {"date", "goals"},
            "/api/day-type/sessions": {"date", "sessions"},
        }
        for pfad, felder in pflicht.items():
            r = w.c.get(pfad, params={"token": w.feed_token})
            if r.status_code != 200:
                B.befund(f"{pfad} antwortet mit HTTP {r.status_code}")
                continue
            fehlend = felder - set(r.json().keys())
            if fehlend:
                B.befund(f"{pfad} hat Pflichtfelder verloren: {sorted(fehlend)}")
            else:
                B.ok(f"{pfad} liefert {sorted(felder)}")

        # Falsches Token darf nie Daten herausgeben
        r = w.c.get("/api/day-type/today", params={"token": "falsch"})
        if r.status_code == 403:
            B.ok("Falsches Feed-Token wird mit 403 abgewiesen")
        else:
            B.befund(f"Falsches Feed-Token liefert HTTP {r.status_code}")

        # Tagestyp muss immer aus dem bekannten Wertevorrat kommen
        erlaubt = {"feiertag", "urlaub", "schule", "arbeit", "frei"}
        heute = w.c.get("/api/day-type/today", params={"token": w.feed_token}).json()["type"]
        if heute in erlaubt:
            B.ok(f"Tagestyp kommt aus dem vereinbarten Wertevorrat (heute: '{heute}')")
        else:
            B.befund(f"Unbekannter Tagestyp '{heute}': HA kennt nur {sorted(erlaubt)}")


def s_ical(w: Welt) -> None:
    with sonde("iCal-Feed"):
        r = w.c.get("/api/ical", params={"token": w.feed_token})
        if r.status_code != 200:
            B.befund(f"iCal-Feed antwortet mit HTTP {r.status_code}")
            return
        roh = r.text
        B.info(f"Feed: {len(roh)} Zeichen, {roh.count('BEGIN:VEVENT')} Ereignisse")

        try:
            from icalendar import Calendar as ICal
            kal = ICal.from_ical(roh)
        except Exception as exc:
            B.befund(f"Feed ist nicht parsebar: {exc}")
            return

        spaet = [k for k in kal.walk("VEVENT")
                 if str(k.get("summary")) == "Spaeter Abendtermin"]
        if not spaet:
            B.befund("Der 23:30-Termin fehlt im Feed")
        else:
            dt = spaet[0]["dtstart"].dt
            if dt.hour == 23 and dt.minute == 30:
                B.ok(f"Uhrzeiten stimmen mit der Oberflaeche ueberein ({dt})")
            else:
                B.befund(f"Uhrzeit im Feed weicht ab: {dt} (erwartet 23:30)")
            if dt.tzinfo is None:
                B.info("Zeiten stehen als 'schwebend' (ohne Zone) im Feed, ein Client "
                       "in einer anderen Zeitzone liest sie als seine Ortszeit")

        # Serien: RRULE muss durchgereicht werden, nicht expandiert
        sport = [k for k in kal.walk("VEVENT") if str(k.get("summary")) == "Sport"]
        if len(sport) == 1 and sport[0].get("rrule"):
            B.ok("Serien gehen als Regel durch (nicht expandiert)")
        else:
            B.befund(f"Sport-Serie liegt {len(sport)}x im Feed, RRULE vorhanden: "
                     f"{bool(sport and sport[0].get('rrule'))}")


def s_planer(w: Welt) -> None:
    with sonde("Gewohnheiten-Planer"):
        # ⚠️ Beide Endpunkte nehmen `week` als ISO-Woche ('2026-W26'), NICHT
        # `week_start` als Datum, und FastAPI verwirft unbekannte Parameter still.
        # Ein erster Entwurf dieser Sonde hat mit `week_start` kommentarlos die
        # laufende Woche geplant und die Einheiten aller Wochen gezaehlt (66 fuer
        # eine Woche). Seit 2026-08-24 meldet backend/parameter_wache.py so etwas.
        woche = w.planungs_woche               # Zukunft, mit zwei Krankheitstagen
        iso = woche.isocalendar()
        iso_woche = f"{iso[0]}-W{iso[1]:02d}"

        r = w.c.post("/api/habits/schedule", params={"week": iso_woche}, headers=w.owner)
        if r.status_code not in (200, 201):
            B.befund(f"Wochenplanung abgelehnt: HTTP {r.status_code} {r.text[:200]}")
            return

        sessions = w.c.get("/api/habits/sessions", params={"week": iso_woche},
                           headers=w.owner).json()
        alle = w.c.get("/api/habits/sessions", headers=w.owner).json()
        B.info(f"{len(sessions)} Einheiten in {iso_woche} (ab {woche}), "
               f"{len(alle)} ueber den ganzen Zeitraum")
        if not sessions:
            B.befund(f"Die Wochenplanung hat fuer {iso_woche} nichts erzeugt")
            return

        wochen_minuten = sum(
            (datetime.fromisoformat(s["end"]) - datetime.fromisoformat(s["start"])
             ).total_seconds() / 60 for s in sessions
        )
        B.info(f"Geplantes Volumen in {iso_woche}: {wochen_minuten / 60:.1f} h "
               f"(Ziel der drei Gewohnheiten: 9,5 h)")

        # Die folgenden Zusagen gelten fuer den gesamten Zeitraum, nicht nur die Woche.
        sessions = alle

        # Ueberschneidungen
        sortiert = sorted(sessions, key=lambda s: s["start"])
        kollisionen = [
            (a["start"], b["start"])
            for a, b in zip(sortiert, sortiert[1:])
            if b["start"] < a["end"]
        ]
        if kollisionen:
            B.befund(f"{len(kollisionen)} sich ueberschneidende Einheiten",
                     "\n".join(f"{a} ueberlappt {b}" for a, b in kollisionen[:5]))
        else:
            B.ok("Keine sich ueberschneidenden Einheiten")

        # Nichts an Krankheitstagen. Geprueft wird gegen die ZUKUENFTIGEN
        # Krankheitstage, an vergangenen plant der Planer ohnehin nichts, die
        # Zusage waere dort leer.
        krank_zukunft = {t.isoformat() for t in w.krank_zukunft}
        an_krank = [s for s in sessions if s["start"][:10] in krank_zukunft]
        if an_krank:
            B.befund(f"{len(an_krank)} Einheiten an kuenftigen Krankheitstagen geplant",
                     "\n".join(s["start"] for s in an_krank[:5]))
        else:
            B.ok(f"An Krankheitstagen wird nichts geplant "
                 f"(geprueft gegen {sorted(krank_zukunft)})")

        # Nie in die Vergangenheit (scheduler.py: `if day < today: continue`).
        # Bewusste Zusage, hier festgehalten, damit sie nicht als Fehler
        # missverstanden wird, wenn eine Sonde eine alte Woche plant.
        heute = date.today().isoformat()
        vergangen = [s for s in sessions
                     if s["start"][:10] < heute and s["status"] == "pending"]
        if vergangen:
            B.befund(f"{len(vergangen)} offene Einheiten liegen in der Vergangenheit",
                     "\n".join(f"{s['start']} ({s['status']})" for s in vergangen[:5]))
        else:
            B.ok("Es liegen keine offenen Einheiten in der Vergangenheit")

        # Nichts waehrend der Arbeitszeit
        arbeit = {t.isoformat() for t, typ in w.plan.items() if typ == "arbeit"}
        in_arbeitszeit = [
            s for s in sessions
            if s["start"][:10] in arbeit and "07:00" <= s["start"][11:16] < "16:00"
        ]
        if in_arbeitszeit:
            B.befund(f"{len(in_arbeitszeit)} Einheiten mitten in der Arbeitszeit",
                     "\n".join(s["start"] for s in in_arbeitszeit[:5]))
        else:
            B.ok("Keine Einheit faellt in den Arbeitsblock")


def s_dsgvo(w: Welt) -> None:
    with sonde("Auskunft und Loeschung (DSGVO)"):
        r = w.c.get("/api/account/export", headers=w.test)
        if r.status_code != 200:
            B.befund(f"Auskunft abgelehnt: HTTP {r.status_code}")
            return
        daten = r.json()
        as_text = str(daten)
        if "Testkonto-Privattermin" in as_text:
            B.ok("Auskunft enthaelt die eigenen Termine")
        else:
            B.befund("Auskunft enthaelt den eigenen Termin nicht")
        if "Spaeter Abendtermin" in as_text:
            B.befund("Auskunft des Testkontos enthaelt Termine des Owners")
        else:
            B.ok("Auskunft enthaelt keine fremden Termine")

        r = w.c.post("/api/account/delete", json={"confirm": "KONTO LOESCHEN"},
                     headers=w.test)
        if r.status_code not in (200, 204):
            B.befund(f"Loeschung abgelehnt: HTTP {r.status_code} {r.text[:200]}")
            return
        tag = w.spaet_tag
        p = {"start": f"{tag}T00:00:00", "end": f"{tag + timedelta(days=1)}T00:00:00"}
        rest = titel(w.c.get("/api/events", params=p, headers=w.test).json())
        if "Testkonto-Privattermin" not in rest:
            B.ok("Nach der Loeschung sind die eigenen Termine weg")
        else:
            B.befund("Termin ueberlebt die Kontoloeschung")
        owner_rest = titel(w.c.get("/api/events", params=p, headers=w.owner).json())
        if "Spaeter Abendtermin" in owner_rest:
            B.ok("Die Loeschung laesst die Daten des Owners unberuehrt")
        else:
            B.befund("Die Loeschung hat Daten des Owners mitgenommen")


def s_leistung(w: Welt) -> None:
    with sonde("Antwortzeiten"):
        messungen = {
            "Monatsansicht (/api/events, 6 Wochen)": lambda: w.c.get("/api/events", params={
                "start": iso_z(datetime(w.start.year, w.start.month, w.start.day)),
                "end": iso_z(datetime(w.start.year, w.start.month, w.start.day)
                             + timedelta(days=42)),
            }, headers=w.owner),
            "Tagestyp heute (HA-Pfad)": lambda: w.c.get(
                "/api/day-type/today", params={"token": w.feed_token}),
            "Wochenplan (/api/day-type/week-schedule)": lambda: w.c.get(
                "/api/day-type/week-schedule",
                params={"week_start": w.start.isoformat()}, headers=w.owner),
            "iCal-Gesamtfeed": lambda: w.c.get("/api/ical", params={"token": w.feed_token}),
        }
        for name, aufruf in messungen.items():
            t0 = time.perf_counter()
            for _ in range(5):
                aufruf()
            ms = (time.perf_counter() - t0) / 5 * 1000
            hinweis = "" if ms < 250 else "  ← langsam"
            B.info(f"{name}: {ms:6.1f} ms{hinweis}")


# ==========================================================================
# Lauf
# ==========================================================================

def main() -> int:
    print("\033[1mKalender-Simulation\033[0m")
    print(f"Datenbank: {_DB}")

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with TestClient(app) as client:
        t0 = time.perf_counter()
        welt = Welt(client)
        welt.aufbauen()
        print(f"\nTestkonto '{TESTKONTO}' und {welt.tage} Tage Alltag ab {welt.start} "
              f"aufgebaut ({time.perf_counter() - t0:.1f}s)")

        s_tagestyp_vertrag(welt)
        s_anzeige_tagestyp(welt)
        s_fensterschnitt(welt)
        s_zeitumstellung(welt)
        s_serien(welt)
        s_mandanten(welt)
        s_globale_daten_schuetzen(welt)
        s_tagestyp_kalender_schreibbar(welt)
        s_vertrag_eingefroren(welt)
        s_ical(welt)
        s_planer(welt)
        s_dsgvo(welt)
        s_leistung(welt)

    return B.zusammenfassung()


if __name__ == "__main__":
    sys.exit(main())
