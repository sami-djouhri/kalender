"""Fensterschnitt und Zeitrechnung, die Befunde der Simulation vom 2026-08-24.

Diese Datei haelt fest, was `scripts/simulieren.py` in der Sonde „Fensterschnitt"
gemessen hat: ein Termin um 23:30 fehlte an seinem eigenen Tag und tauchte
stattdessen am Folgetag auf.

Die Ursache steht ausfuehrlich in `backend/wanduhr.py`. Kurz: das Frontend schickt
die Fenstergrenzen als `Date.toISOString()`, also `…T22:00:00.000Z` fuer Mitternacht
Berliner Zeit. Termine liegen als Berliner Wanduhr in der Datenbank. SQLite kennt
keine Zonen: SQLAlchemy formatiert die Felder und wirft `tzinfo` weg. Verglichen
wurde damit `22:00` gegen Wanduhrzeiten: das Fenster lag um den UTC-Versatz daneben.

Praktische Folge vor dem Fix: in der Monatsansicht fielen Termine nach 22:00 am
**letzten** Tag des geladenen Fensters heraus. Sie waren nicht geloescht, nur
unsichtbar, die Klasse Fehler, die niemand meldet.
"""
import os
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-zeitfenster.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.database import Base, engine
from backend.main import app, create_jwt_token
from backend.system_calendars import TERMINE_CAL_ID
from backend.wanduhr import BERLIN, als_wanduhr, tagesgrenzen

UTC = ZoneInfo("UTC")


def wie_das_frontend(wanduhr: datetime) -> str:
    """Baut den Fensterrand woertlich so, wie `frontend/js/app.js` ihn schickt."""
    return wanduhr.replace(tzinfo=BERLIN).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class WanduhrEinheitTest(unittest.TestCase):
    def test_zonenlose_zeit_bleibt_unveraendert(self):
        d = datetime(2026, 5, 13, 23, 30)
        self.assertEqual(als_wanduhr(d), d)

    def test_utc_wird_auf_berliner_wanduhr_gerechnet(self):
        # Sommer: +2h
        self.assertEqual(
            als_wanduhr(datetime(2026, 5, 13, 22, 0, tzinfo=UTC)),
            datetime(2026, 5, 14, 0, 0),
        )
        # Winter: +1h
        self.assertEqual(
            als_wanduhr(datetime(2026, 1, 13, 23, 0, tzinfo=UTC)),
            datetime(2026, 1, 14, 0, 0),
        )

    def test_tagesgrenzen_sind_halboffen(self):
        beginn, ende = tagesgrenzen(datetime(2026, 5, 13).date())
        self.assertEqual(beginn, datetime(2026, 5, 13, 0, 0))
        self.assertEqual(ende, datetime(2026, 5, 14, 0, 0))

    def test_tagesgrenzen_ueber_die_zeitumstellung(self):
        """Der 25.10.2026 hat 25 Stunden, die Wanduhr-Grenzen bleiben 00:00/00:00."""
        beginn, ende = tagesgrenzen(datetime(2026, 10, 25).date())
        self.assertEqual((beginn.hour, ende.hour), (0, 0))
        self.assertEqual((ende - beginn), timedelta(days=1))


class FensterschnittTest(unittest.TestCase):
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

    def _anlegen(self, titel: str, start: str, ende: str) -> str:
        r = self.client.post("/api/events", json={
            "calendar_id": TERMINE_CAL_ID, "title": titel, "start": start, "end": ende,
        }, headers=self.h)
        self.assertIn(r.status_code, (200, 201), r.text)
        return r.json()["id"]

    def _titel_im_fenster(self, von: datetime, bis: datetime) -> list[str]:
        r = self.client.get("/api/events", params={
            "start": wie_das_frontend(von), "end": wie_das_frontend(bis),
        }, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return [e["title"] for e in r.json()]

    def test_spaeter_termin_liegt_an_seinem_eigenen_tag(self):
        self._anlegen("Spaet-Sommer", "2026-05-13T23:30:00", "2026-05-13T23:59:00")
        eigener = self._titel_im_fenster(datetime(2026, 5, 13), datetime(2026, 5, 14))
        folge = self._titel_im_fenster(datetime(2026, 5, 14), datetime(2026, 5, 15))
        self.assertIn("Spaet-Sommer", eigener)
        self.assertNotIn("Spaet-Sommer", folge)

    def test_spaeter_termin_auch_im_winter(self):
        """Im Winter ist der Versatz +1h, der Fehler war dort um eine Stunde kleiner
        und damit noch unauffaelliger."""
        self._anlegen("Spaet-Winter", "2026-01-13T23:30:00", "2026-01-13T23:59:00")
        eigener = self._titel_im_fenster(datetime(2026, 1, 13), datetime(2026, 1, 14))
        folge = self._titel_im_fenster(datetime(2026, 1, 14), datetime(2026, 1, 15))
        self.assertIn("Spaet-Winter", eigener)
        self.assertNotIn("Spaet-Winter", folge)

    def test_letzter_tag_der_monatsansicht_verliert_nichts(self):
        """Der Fall, der real weh tat: 42-Tage-Raster, Termin am letzten Abend."""
        self._anlegen("Am Rasterrand", "2026-06-14T22:30:00", "2026-06-14T23:30:00")
        raster = self._titel_im_fenster(datetime(2026, 5, 4), datetime(2026, 6, 15))
        self.assertIn("Am Rasterrand", raster)

    def test_serie_wird_am_fensterrand_nicht_verschoben(self):
        r = self.client.post("/api/events", json={
            "calendar_id": TERMINE_CAL_ID, "title": "Nachtserie",
            "start": "2026-05-04T23:15:00", "end": "2026-05-04T23:45:00",
            "recurrence_rule": "FREQ=DAILY;COUNT=5",
        }, headers=self.h)
        self.assertIn(r.status_code, (200, 201), r.text)
        r = self.client.get("/api/events", params={
            "start": wie_das_frontend(datetime(2026, 5, 4)),
            "end": wie_das_frontend(datetime(2026, 5, 9)),
        }, headers=self.h)
        tage = sorted(e["start"][:10] for e in r.json() if e["title"] == "Nachtserie")
        self.assertEqual(
            tage,
            ["2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07", "2026-05-08"],
        )

    def test_zonenlose_grenzen_funktionieren_weiter(self):
        """Die native App und die Tests schicken zonenlos, das muss so bleiben."""
        self._anlegen("Zonenlos", "2026-07-02T09:00:00", "2026-07-02T10:00:00")
        r = self.client.get("/api/events", params={
            "start": "2026-07-02T00:00:00", "end": "2026-07-03T00:00:00",
        }, headers=self.h)
        self.assertIn("Zonenlos", [e["title"] for e in r.json()])


if __name__ == "__main__":
    unittest.main()
