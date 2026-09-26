"""API-Tests der Tagesdecke: Zugangswege, Korrektur über HTTP, iCal-Abo.

Der Kern der Decke ist in `test_tagesdecke.py` geprüft. Hier geht es um die
Schicht darüber, wo die Fehler anders aussehen:

* der Zeit-Eingang muss ohne Token **ablehnen**, nicht durchlassen,
* der iCal-Feed muss ohne gültigen Feed-Token schweigen und mit Token echte
  Einträge liefern (ein Import-Fehler dort fiele sonst erst auf dem Handy auf),
* die Ebene-1-Route `/api/day-type/today` muss unberührt bleiben, weil an ihr
  das 05:35-Weckfenster hängt.
"""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-decke-api.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.config import settings
from backend.database import Base, engine
from backend.main import app, create_jwt_token
from backend.models import Setting


class DeckeApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.auth = {"Authorization": f"Bearer {create_jwt_token()}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def setUp(self):
        self._token_vorher = settings.ZEIT_INGEST_TOKEN

    def tearDown(self):
        settings.ZEIT_INGEST_TOKEN = self._token_vorher

    def _feed_token(self) -> str:
        from backend.database import SessionLocal
        db = SessionLocal()
        try:
            zeile = db.query(Setting).filter(Setting.key == "feed_token").first()
            return zeile.value
        finally:
            db.close()

    # --- Lesen ----------------------------------------------------------

    def test_decke_braucht_anmeldung(self):
        antwort = self.client.get("/api/tagesdecke", params={"datum": "2027-03-09"})
        self.assertIn(antwort.status_code, (401, 403))

    def test_decke_liefert_lueckenlose_bloecke(self):
        antwort = self.client.get(
            "/api/tagesdecke", params={"datum": "2027-03-09"}, headers=self.auth
        )
        self.assertEqual(antwort.status_code, 200)
        daten = antwort.json()
        self.assertTrue(daten["bloecke"])
        self.assertEqual(daten["offen_minuten"], 0)

    def test_arten_nennen_die_gespiegelten(self):
        antwort = self.client.get("/api/tagesdecke/arten", headers=self.auth)
        self.assertEqual(antwort.status_code, 200)
        arten = {a["schluessel"]: a for a in antwort.json()["arten"]}
        self.assertTrue(arten["fix"]["gespiegelt"])
        self.assertFalse(arten["erholung"]["gespiegelt"])

    def test_rueckblick_lehnt_verdrehten_zeitraum_ab(self):
        antwort = self.client.get(
            "/api/tagesdecke/rueckblick",
            params={"von": "2027-03-09", "bis": "2027-03-01"},
            headers=self.auth,
        )
        self.assertEqual(antwort.status_code, 400)

    # --- Korrigieren ----------------------------------------------------

    def test_festschreiben_und_block_verwerfen(self):
        tag = "2027-04-06"
        antwort = self.client.post(
            "/api/tagesdecke/festschreiben", params={"datum": tag}, headers=self.auth
        )
        self.assertEqual(antwort.status_code, 200)
        bloecke = antwort.json()["bloecke"]
        self.assertTrue(all(b["id"] for b in bloecke), "Festgeschriebene Blöcke ohne ID")

        ziel = bloecke[0]
        weg = self.client.delete(
            f"/api/tagesdecke/block/{ziel['id']}", headers=self.auth
        )
        self.assertEqual(weg.status_code, 200)
        self.assertEqual(weg.json()["status"], "verworfen")

        # ★ Verworfenes bleibt als Zeile bestehen: es ist das Lernsignal.
        erneut = self.client.get(
            "/api/tagesdecke", params={"datum": tag}, headers=self.auth
        )
        ids = [b["id"] for b in erneut.json()["bloecke"]]
        self.assertIn(ziel["id"], ids)

    def test_block_verschieben_ueber_http(self):
        tag = "2027-04-07"
        self.client.post(
            "/api/tagesdecke/festschreiben", params={"datum": tag}, headers=self.auth
        )
        decke = self.client.get(
            "/api/tagesdecke", params={"datum": tag}, headers=self.auth
        ).json()
        ziel = [b for b in decke["bloecke"] if b["art"] == "erholung"][0]
        antwort = self.client.patch(
            f"/api/tagesdecke/block/{ziel['id']}",
            json={"start": f"{tag}T20:00:00", "ende": f"{tag}T21:00:00"},
            headers=self.auth,
        )
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json()["quelle"], "manuell")
        self.assertTrue(antwort.json()["start"].startswith(f"{tag}T20:00"))

    def test_unbekannte_art_wird_abgelehnt(self):
        antwort = self.client.post(
            "/api/tagesdecke/block",
            json={
                "datum": "2027-04-08", "start": "2027-04-08T10:00:00",
                "ende": "2027-04-08T11:00:00", "art": "unfug", "titel": "X",
            },
            headers=self.auth,
        )
        self.assertEqual(antwort.status_code, 400)

    # --- Zeit-Eingang ---------------------------------------------------

    def test_eingang_ohne_eingerichteten_token_ist_zu(self):
        """Fail-closed: nicht eingerichtet heißt abgelehnt, nicht offen."""
        settings.ZEIT_INGEST_TOKEN = ""
        antwort = self.client.post(
            "/api/zeit/ingest",
            json={"eintraege": [{
                "quelle": "uhr", "art": "training",
                "start": "2027-04-09T18:00:00", "ende": "2027-04-09T19:00:00",
            }]},
        )
        self.assertEqual(antwort.status_code, 403)

    def test_eingang_lehnt_falschen_token_ab(self):
        settings.ZEIT_INGEST_TOKEN = "richtig"
        antwort = self.client.post(
            "/api/zeit/ingest",
            json={"eintraege": []},
            headers={"X-Zeit-Token": "falsch"},
        )
        self.assertEqual(antwort.status_code, 401)

    def test_eingang_nimmt_mit_token_an(self):
        settings.ZEIT_INGEST_TOKEN = "richtig"
        antwort = self.client.post(
            "/api/zeit/ingest",
            json={"eintraege": [{
                "quelle": "uhr", "art": "training", "extern_id": "api-1",
                "start": "2027-04-09T18:00:00", "ende": "2027-04-09T19:00:00",
            }]},
            headers={"X-Zeit-Token": "richtig"},
        )
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json()["aufgenommen"], 1)

    def test_eingang_braucht_kein_jwt(self):
        """Ein Messgerät kann kein JWT halten. Der Token ist der ganze Nachweis."""
        settings.ZEIT_INGEST_TOKEN = "richtig"
        antwort = self.client.post(
            "/api/zeit/ingest",
            json={"eintraege": [{
                "quelle": "uhr", "art": "erholung", "extern_id": "api-2",
                "start": "2027-04-09T20:00:00", "ende": "2027-04-09T21:00:00",
            }]},
            headers={"X-Zeit-Token": "richtig"},
        )
        self.assertEqual(antwort.status_code, 200)

    # --- iCal-Abo -------------------------------------------------------

    def test_ical_ohne_token_abgelehnt(self):
        antwort = self.client.get("/api/tagesdecke/ical", params={"token": "falsch"})
        self.assertEqual(antwort.status_code, 403)

    def test_ical_liefert_eintraege(self):
        antwort = self.client.get(
            "/api/tagesdecke/ical",
            params={"token": self._feed_token(), "tage_zurueck": 0, "tage_voraus": 1},
        )
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("text/calendar", antwort.headers["content-type"])
        inhalt = antwort.text
        self.assertIn("BEGIN:VCALENDAR", inhalt)
        self.assertIn("BEGIN:VEVENT", inhalt)
        self.assertIn("Tagesdecke", inhalt)

    # --- Die Zusage, die nicht brechen darf -----------------------------

    def test_daytype_bleibt_unberuehrt(self):
        """★ An `/api/day-type/today` hängt das 05:35-Weckfenster.

        Die Decke ist eine additive Schicht. Wenn dieser Test bricht, ist der
        Eingriff zu tief gegangen.
        """
        antwort = self.client.get(
            "/api/day-type/today", params={"token": self._feed_token()}
        )
        self.assertEqual(antwort.status_code, 200)
        # Der Vertrag heißt `type`, nicht `day_type`. Home Assistant liest genau
        # dieses Feld; ein Umbenennen wäre ein Bruch der eingefrorenen Ebene 1.
        daten = antwort.json()
        self.assertIn("type", daten)
        self.assertIn("date", daten)


if __name__ == "__main__":
    unittest.main()
