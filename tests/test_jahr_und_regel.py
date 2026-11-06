"""Tagestyp ueber ein ganzes Jahr (`GET /api/day-type/bereich`) und die
RRULE-Pruefung beim Anlegen/Aendern eines Termins.

Zur Regel: eine RRULE, an der dateutil scheitert, speicherte der Dienst
bis 2026-11-03 klaglos, und `expand_event` zeigte sie wortlos als
Einzeltermin. Haeufigster Fall `UNTIL=20261231` ohne Uhrzeit und Z.
"""

import os
import unittest

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-jahr-regel.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.database import Base, engine
from backend.main import app, create_jwt_token

from backend.system_calendars import TERMINE_CAL_ID as TERMINE


class JahrUndRegelTest(unittest.TestCase):
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

    def _termin(self, regel):
        return self.client.post("/api/events", headers=self.h, json={
            "calendar_id": TERMINE, "title": "Probe",
            "start": "2026-11-03T10:00:00", "end": "2026-11-03T11:00:00",
            "recurrence_rule": regel,
        })

    def test_bereich_ein_jahr_gleich_der_woche(self):
        self.client.post("/api/day-type/set?date=2027-03-02&type=urlaub", headers=self.h)
        r = self.client.get("/api/day-type/bereich?start=2027-01-01&tage=365", headers=self.h)
        self.assertEqual(r.status_code, 200)
        tage = {t["date"]: t["type"] for t in r.json()}
        self.assertEqual(len(tage), 365)
        self.assertEqual(tage["2027-01-01"], "feiertag")
        self.assertEqual(tage["2027-03-02"], "urlaub")
        woche = self.client.get("/api/day-type/week?week_start=2027-03-01", headers=self.h).json()
        for eintrag in woche:
            self.assertEqual(tage[eintrag["date"]], eintrag["type"])

    def test_bereich_grenze(self):
        r = self.client.get("/api/day-type/bereich?start=2027-01-01&tage=401", headers=self.h)
        self.assertEqual(r.status_code, 422)

    def test_bereich_braucht_anmeldung(self):
        r = self.client.get("/api/day-type/bereich?start=2027-01-01&tage=7")
        self.assertIn(r.status_code, (401, 403))

    def test_until_ohne_uhrzeit_wird_abgelehnt(self):
        r = self._termin("FREQ=WEEKLY;BYDAY=TU;UNTIL=20261231")
        self.assertEqual(r.status_code, 422)
        self.assertIn("UNTIL", r.text)

    def test_unsinn_wird_abgelehnt(self):
        self.assertEqual(self._termin("FREQ=SOMETIMES").status_code, 422)

    def test_gueltige_regeln_gehen_durch(self):
        for regel in (
            "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;UNTIL=20261231T235959Z",
            "FREQ=MONTHLY;COUNT=3",
            "FREQ=MONTHLY;BYDAY=1MO",
            None,
        ):
            self.assertEqual(self._termin(regel).status_code, 201, regel)

    def test_aendern_prueft_ebenso(self):
        ev = self._termin("FREQ=WEEKLY").json()
        r = self.client.put(f"/api/events/{ev['id']}", headers=self.h,
                            json={"recurrence_rule": "FREQ=DAILY;UNTIL=20270101"})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
