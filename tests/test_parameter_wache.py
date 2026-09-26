"""Unbekannte Query-Parameter: sichtbar statt verschluckt.

Hintergrund in `backend/parameter_wache.py`. Der Anlass in Kurzform: FastAPI
ignoriert nicht deklarierte Query-Parameter kommentarlos. Ein Aufrufer, der sich
vertippt, bekommt deshalb keinen Fehler, sondern eine plausible Antwort auf eine
andere Frage, und merkt es unter Umstaenden monatelang nicht.

Der erste Fund der Wache war ausgerechnet ein **Test dieses Projekts**:
`test_multitenant.test_fremder_mandant_sieht_owner_tagestyp_nicht` rief
`/api/day-type/week?start=…` statt `?week_start=…` auf. Der Pflichtparameter
fehlte damit, die Antwort war 422, und der ganze Vergleich lag hinter einem
`if status == 200`. Der Test war gruen und hat nie etwas geprueft.
"""
import importlib
import os
import unittest

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-paramwache.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend import parameter_wache as wache
from backend.config import settings
from backend.database import Base, engine
from backend.main import app, create_jwt_token


class ParameterWacheTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.h = {"Authorization": f"Bearer {create_jwt_token()}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def setUp(self):
        self._strikt = settings.STRICT_QUERY_PARAMS
        wache._gemeldet.clear()

    def tearDown(self):
        settings.STRICT_QUERY_PARAMS = self._strikt

    # --- Beobachtungsphase (Standard) -------------------------------------

    def test_unbekannter_parameter_wird_gemeldet_aber_nicht_abgelehnt(self):
        settings.STRICT_QUERY_PARAMS = False
        with self.assertLogs("backend.parameter_wache", level="WARNING") as protokoll:
            r = self.client.get("/api/events", params={"week_start": "2026-09-21"},
                                headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(any("week_start" in z for z in protokoll.output), protokoll.output)

    def test_jede_route_und_parameter_kombination_nur_einmal_melden(self):
        """Ein Poller darf das Protokoll nicht fluten."""
        settings.STRICT_QUERY_PARAMS = False
        with self.assertLogs("backend.parameter_wache", level="WARNING") as protokoll:
            for _ in range(5):
                self.client.get("/api/events", params={"quatsch": "1"}, headers=self.h)
        self.assertEqual(len([z for z in protokoll.output if "quatsch" in z]), 1,
                         protokoll.output)

    def test_bekannte_parameter_erzeugen_keine_meldung(self):
        settings.STRICT_QUERY_PARAMS = False
        with self.assertNoLogs("backend.parameter_wache", level="WARNING"):
            r = self.client.get("/api/events",
                                params={"start": "2026-09-21T00:00:00",
                                        "end": "2026-09-22T00:00:00"},
                                headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)

    def test_cache_aufbrecher_der_browser_sind_geduldet(self):
        settings.STRICT_QUERY_PARAMS = False
        with self.assertNoLogs("backend.parameter_wache", level="WARNING"):
            self.client.get("/api/calendars", params={"_": "1724500000"}, headers=self.h)

    # --- Scharf geschaltet -------------------------------------------------

    def test_strikt_lehnt_mit_klartext_ab(self):
        settings.STRICT_QUERY_PARAMS = True
        r = self.client.get("/api/events", params={"week_start": "2026-09-21"},
                            headers=self.h)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("week_start", r.json()["detail"])
        # Die Antwort muss sagen, was stattdessen erlaubt ist
        self.assertIn("calendar_id", r.json()["detail"])

    def test_strikt_laesst_richtige_aufrufe_durch(self):
        settings.STRICT_QUERY_PARAMS = True
        r = self.client.get("/api/events",
                            params={"start": "2026-09-21T00:00:00",
                                    "end": "2026-09-22T00:00:00"},
                            headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)

    # --- Ebene 1 bleibt unberuehrt ----------------------------------------

    def test_eingefrorene_ha_routen_haengen_nicht_an_der_wache(self):
        """Home Assistant, iCal und dev-portal duerfen sich nie an einer neuen
        Pruefung stossen, auch dann nicht, wenn sie scharf geschaltet ist."""
        settings.STRICT_QUERY_PARAMS = True
        from backend.database import SessionLocal
        from backend.models import Setting

        db = SessionLocal()
        db.info["owner_sub"] = None
        try:
            token = db.query(Setting).filter(Setting.key == "feed_token").first().value
        finally:
            db.close()

        for pfad in ("/api/day-type/today", "/api/ical"):
            r = self.client.get(pfad, params={"token": token, "unbekannt": "x"})
            self.assertEqual(r.status_code, 200, f"{pfad}: {r.text[:200]}")


if __name__ == "__main__":
    unittest.main()
