"""Schutz der Steuer- und der globalen Daten: Befunde der Simulation vom 2026-08-24.

Drei Loecher, alle in `scripts/simulieren.py` gemessen:

1. **Feiertage waren fuer jeden Mandanten aenderbar.** Anlegen im Feiertagskalender
   war mit 403 gesperrt, Aendern und Loeschen nicht, die Sperre sass allein im
   Erzeugungspfad. Feiertags-Events sind aber die einzigen Zeilen ohne Besitzer und
   damit fuer *alle* sichtbar: ein beliebiger Mandant konnte den 25.12. fuer alle
   loeschen. Der Tagestyp dieses Tages faellt damit von `feiertag` auf `arbeit`
   zurueck, und daran haengt das Weckfenster um 05:35.

2. **Tagestyp-Kalender waren freie Ablage.** Ein Termin, den jemand von Hand in
   `daytype-urlaub` legte, machte den Tag zum Urlaubstag. Schlimmer: der Tag trug
   dann zwei Tagestyp-Ereignisse, und der Umschalter raeumte nur eines ab.

3. **Der Umschalter log.** Er suchte das bestehende Ereignis in Wörterbuch-Reihenfolge
   statt nach Prioritaet, loeschte genau eines und meldete „frei": waehrend das
   zweite liegen blieb und Home Assistant weiter „urlaub" las.
"""
import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-steuerdaten.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.database import Base, SessionLocal, engine
from backend.main import app, create_jwt_token
from backend.models import Event, Setting
from backend.system_calendars import (
    DAYTYPE_CALENDARS,
    FEIERTAG_CALENDAR_ID,
    GEBURTSTAGE_CALENDAR_ID,
    TERMINE_CAL_ID,
)

FREMD = "fremder-mandant-sub"


class SteuerdatenSchutzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        auth = {"Authorization": f"Bearer {create_jwt_token()}"}
        cls.owner = auth
        cls.fremd = {**auth, "X-Saganta-Sub": FREMD}
        db = SessionLocal()
        db.info["owner_sub"] = None
        try:
            cls.token = db.query(Setting).filter(Setting.key == "feed_token").first().value
        finally:
            db.close()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def _ein_feiertag(self) -> dict:
        jahr = date.today().year
        r = self.client.get("/api/events", params={
            "start": f"{jahr}-01-01T00:00:00", "end": f"{jahr}-12-31T23:59:59",
        }, headers=self.fremd)
        feiertage = [e for e in r.json() if e["calendar_id"] == FEIERTAG_CALENDAR_ID]
        self.assertTrue(feiertage, "Startup-Seed hat keine Feiertage angelegt")
        return feiertage[0]

    # --- 1. Globale Feiertage ---------------------------------------------

    def test_fremder_mandant_kann_feiertag_nicht_umbenennen(self):
        feiertag = self._ein_feiertag()
        r = self.client.put(f"/api/events/{feiertag['id']}",
                            json={"title": "Gekapert"}, headers=self.fremd)
        self.assertEqual(r.status_code, 403, r.text)

        weiter = self.client.get(f"/api/events/{feiertag['id']}", headers=self.owner)
        self.assertEqual(weiter.json()["title"], feiertag["title"])

    def test_fremder_mandant_kann_feiertag_nicht_loeschen(self):
        feiertag = self._ein_feiertag()
        r = self.client.delete(f"/api/events/{feiertag['id']}", headers=self.fremd)
        self.assertEqual(r.status_code, 403, r.text)

        tag = feiertag["start"][:10]
        typ = self.client.get("/api/day-type",
                              params={"date": tag, "token": self.token}).json()["type"]
        self.assertEqual(typ, "feiertag", "Tagestyp des Feiertags ist gekippt")

    def test_auch_der_owner_fasst_erzeugte_kalender_nicht_an(self):
        """Nicht nur eine Mandantenfrage: erzeugte Eintraege sind beim naechsten
        Start ohnehin wieder ueberschrieben: ehrlich 403 statt stiller Verlust."""
        feiertag = self._ein_feiertag()
        self.assertEqual(
            self.client.delete(f"/api/events/{feiertag['id']}", headers=self.owner).status_code,
            403,
        )

    def test_verschieben_in_einen_gesperrten_kalender_ist_dicht(self):
        """Der Schleichweg: harmlos anlegen, dann per PUT umhaengen."""
        r = self.client.post("/api/events", json={
            "calendar_id": TERMINE_CAL_ID, "title": "Trojaner",
            "start": "2026-11-02T09:00:00", "end": "2026-11-02T10:00:00",
        }, headers=self.fremd)
        self.assertIn(r.status_code, (200, 201), r.text)
        eid = r.json()["id"]

        for ziel in (FEIERTAG_CALENDAR_ID, GEBURTSTAGE_CALENDAR_ID,
                     DAYTYPE_CALENDARS["urlaub"]):
            antwort = self.client.put(f"/api/events/{eid}", json={"calendar_id": ziel},
                                      headers=self.fremd)
            self.assertEqual(antwort.status_code, 403, f"{ziel}: {antwort.text}")

    # --- 2. Tagestyp-Kalender sind Steuerdaten ----------------------------

    def test_direktes_schreiben_in_tagestyp_kalender_ist_gesperrt(self):
        for typ, cal_id in DAYTYPE_CALENDARS.items():
            r = self.client.post("/api/events", json={
                "calendar_id": cal_id, "title": f"Handeintrag {typ}",
                "start": "2026-11-03T00:00:00", "end": "2026-11-04T00:00:00",
                "all_day": True,
            }, headers=self.owner)
            self.assertEqual(r.status_code, 403, f"{typ}: {r.text}")

    def test_der_vorgesehene_weg_funktioniert_weiter(self):
        r = self.client.post("/api/day-type/set",
                             params={"date": "2026-11-03", "type": "urlaub"},
                             headers=self.owner)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["type"], "urlaub")

    # --- 3. Der Umschalter bleibt eindeutig -------------------------------

    def test_umschalter_raeumt_einen_mehrdeutigen_tag_auf(self):
        """Selbstheilung: ein Tag mit zwei Tagestyp-Ereignissen wird wieder eindeutig.

        Solche Tage koennen aus der Zeit vor der Sperre stammen. Der Umschalter
        loeschte frueher nur eines und meldete „frei", waehrend das zweite blieb.
        """
        tag = date(2026, 11, 10)
        db = SessionLocal()
        try:
            for typ in ("arbeit", "urlaub"):
                db.add(Event(
                    calendar_id=DAYTYPE_CALENDARS[typ], title=typ.capitalize(),
                    start=datetime(2026, 11, 10, 0, 0),
                    end=datetime(2026, 11, 11, 0, 0), all_day=True,
                ))
            db.commit()
        finally:
            db.close()

        # Vorher mehrdeutig: die Prioritaet entscheidet, urlaub schlaegt arbeit
        vorher = self.client.get("/api/day-type", params={
            "date": tag.isoformat(), "token": self.token}).json()["type"]
        self.assertEqual(vorher, "urlaub")

        # Umschalten auf urlaub -> der Tag muss 'frei' werden UND es bleibt nichts liegen
        r = self.client.post("/api/day-type/set",
                             params={"date": tag.isoformat(), "type": "urlaub"},
                             headers=self.owner)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["type"], "frei")

        nachher = self.client.get("/api/day-type", params={
            "date": tag.isoformat(), "token": self.token}).json()["type"]
        self.assertEqual(
            nachher, "frei",
            "Der Umschalter meldet 'frei', Home Assistant liest etwas anderes",
        )

    def test_beide_setz_endpunkte_verhalten_sich_gleich(self):
        """`/set` (Oberflaeche, JWT) und `/set-external` (dev-portal, Feed-Token)
        teilen sich seit 2026-08-24 einen Kern. Vorher hatten sie zwei Kopien,
        und nur eine kannte halbe Urlaubstage."""
        tag = "2026-11-17"
        extern = self.client.post("/api/day-type/set-external",
                                  params={"date": tag, "type": "schule", "token": self.token})
        self.assertEqual(extern.json()["type"], "schule")

        # Umschalten ueber den anderen Endpunkt muss denselben Tag aufloesen
        intern = self.client.post("/api/day-type/set",
                                  params={"date": tag, "type": "schule"}, headers=self.owner)
        self.assertEqual(intern.json()["type"], "frei")
        self.assertEqual(
            self.client.get("/api/day-type",
                            params={"date": tag, "token": self.token}).json()["type"],
            "frei",
        )

    def test_halber_urlaubstag_bleibt_unterscheidbar(self):
        tag = "2026-11-18"
        self.client.post("/api/day-type/set",
                         params={"date": tag, "type": "urlaub", "half": "true"},
                         headers=self.owner)
        # Voller Urlaub ueber denselben Endpunkt ist ein Wechsel, kein Umschalten
        r = self.client.post("/api/day-type/set",
                             params={"date": tag, "type": "urlaub"}, headers=self.owner)
        self.assertEqual(r.json()["type"], "urlaub")

        events = self.client.get("/api/events", params={
            "start": f"{tag}T00:00:00",
            "end": (date.fromisoformat(tag) + timedelta(days=1)).isoformat() + "T00:00:00",
        }, headers=self.owner).json()
        urlaube = [e for e in events if e["calendar_id"] == DAYTYPE_CALENDARS["urlaub"]]
        self.assertEqual(len(urlaube), 1, f"Tag traegt {len(urlaube)} Urlaubs-Ereignisse")
        self.assertEqual(urlaube[0]["title"], "Urlaub")


if __name__ == "__main__":
    unittest.main()
