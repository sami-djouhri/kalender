"""Eigene Arbeitszeiten: Wochenvorgabe, Abweichung je Tag, Zeiten fuer Home Assistant.

Auftrag 2026-10-27. Entscheid zur
Datenhaltung: die Wahrheit liegt im Arbeits-Ereignis selbst, die Konstante ist
nur Vorgabe beim Anlegen, und Home Assistant FRAGT die Zeiten ab.

Drei Dinge, die hier ausdruecklich gesichert werden, weil sie lautlos kaputt
gehen koennen:

1. Ein Neustart darf eingetragene Zeiten nicht zuruecksetzen. Bis 2026-10-30
   schrieb `_migrate_timezone()` bei JEDEM Start alle Arbeitszeiten auf die
   Konstante zurueck.
2. Die Ebene-1-Antwort bleibt dieselbe, nur um `start`/`end` erweitert.
3. Uhrzeiten an einem Tag ohne Uhrzeit (Urlaub, Krank) sind ein 400, kein
   stilles Ignorieren.
"""
import os
import unittest
from datetime import date

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-arbeitszeiten.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.database import Base, SessionLocal, engine
from backend.main import _migrate_timezone, app, create_jwt_token
from backend.models import Setting

MONTAG = date(2027, 3, 8)
FREITAG = date(2027, 3, 12)


class ArbeitszeitenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.kopf = {"Authorization": f"Bearer {create_jwt_token()}"}
        db = SessionLocal()
        db.info["owner_sub"] = None
        try:
            cls.token = db.query(Setting).filter(Setting.key == "feed_token").first().value
        finally:
            db.close()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # Vorgabe zuruecksetzen, damit die Tests unabhaengig bleiben.
        self._plan({"arbeit": [], "schule": []})
        for tag in (MONTAG, FREITAG):
            self.client.post("/api/day-type/set-range", headers=self.kopf, params={
                "von": tag.isoformat(), "bis": tag.isoformat(), "type": "frei"})

    def _plan(self, eingabe):
        return self.client.put("/api/day-type/arbeitszeiten", headers=self.kopf, json=eingabe)

    def _setzen(self, tag, typ="arbeit", **extra):
        return self.client.post("/api/day-type/set", headers=self.kopf,
                                params={"date": tag.isoformat(), "type": typ, **extra})

    def _ha(self, tag):
        return self.client.get("/api/day-type",
                               params={"date": tag.isoformat(), "token": self.token}).json()

    # --- Ebene 1: Antwort fuer Home Assistant ---------------------------------

    def test_ha_antwort_traegt_die_zeiten_des_ereignisses(self):
        self._setzen(MONTAG)
        self.assertEqual(self._ha(MONTAG),
                         {"date": MONTAG.isoformat(), "type": "arbeit",
                          "start": "07:00", "end": "16:00"})

    def test_ha_antwort_ohne_zeiten_an_freiem_tag(self):
        antwort = self._ha(MONTAG)
        self.assertEqual(antwort["type"], "frei")
        self.assertIsNone(antwort["start"])
        self.assertIsNone(antwort["end"])

    def test_today_und_tomorrow_tragen_die_felder(self):
        for pfad in ("/api/day-type/today", "/api/day-type/tomorrow"):
            antwort = self.client.get(pfad, params={"token": self.token}).json()
            self.assertEqual(set(antwort), {"date", "type", "start", "end"}, pfad)

    # --- Abweichung je Tag ----------------------------------------------------

    def test_einzelner_tag_mit_eigener_zeit(self):
        antwort = self._setzen(MONTAG, von="09:30", bis="18:00")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json()["start"], "09:30")
        self.assertEqual(self._ha(MONTAG)["start"], "09:30")
        self.assertEqual(self._ha(MONTAG)["end"], "18:00")

    def test_zeit_aendern_schaltet_den_tag_nicht_frei(self):
        """Mit Uhrzeiten wird gesetzt, nie umgeschaltet, auch bei gleicher Zeit."""
        self._setzen(MONTAG, von="08:00", bis="16:00")
        self._setzen(MONTAG, von="08:00", bis="16:00")
        self.assertEqual(self._ha(MONTAG)["type"], "arbeit")

    def test_ohne_zeiten_schaltet_derselbe_typ_weiter_frei(self):
        """Das alte Umschalt-Verhalten der Oberflaeche bleibt."""
        self._setzen(MONTAG)
        self.assertEqual(self._setzen(MONTAG).json()["type"], "frei")
        self.assertEqual(self._ha(MONTAG)["type"], "frei")

    def test_uhrzeit_an_urlaub_ist_400(self):
        self.assertEqual(self._setzen(MONTAG, "urlaub", von="08:00", bis="12:00").status_code, 400)

    def test_kaputte_und_verdrehte_zeiten_sind_400(self):
        self.assertEqual(self._setzen(MONTAG, von="8 Uhr", bis="16:00").status_code, 400)
        self.assertEqual(self._setzen(MONTAG, von="16:00", bis="08:00").status_code, 400)
        self.assertEqual(self._setzen(MONTAG, von="08:00").status_code, 400)
        # Und nichts wurde dabei geschrieben.
        self.assertEqual(self._ha(MONTAG)["type"], "frei")

    # --- Wochenvorgabe --------------------------------------------------------

    def test_vorgabe_ohne_einstellung_ist_die_konstante(self):
        plan = self.client.get("/api/day-type/arbeitszeiten", headers=self.kopf).json()
        self.assertEqual(plan["arbeit"][0], {"wochentag": 0, "von": "07:00", "bis": "16:00", "eigen": False})
        self.assertEqual(plan["arbeit"][4]["bis"], "13:00")
        self.assertEqual(plan["schule"][2]["bis"], "14:40")

    def test_vorgabe_wirkt_beim_anlegen(self):
        self._plan({"arbeit": [{"wochentag": 0, "von": "06:00", "bis": "14:30"}]})
        self._setzen(MONTAG)
        self.assertEqual((self._ha(MONTAG)["start"], self._ha(MONTAG)["end"]), ("06:00", "14:30"))
        # Freitag hat keine eigene Angabe und bleibt bei der Konstante.
        self._setzen(FREITAG)
        self.assertEqual(self._ha(FREITAG)["end"], "13:00")

    def test_vorgabe_wirkt_im_zeitraum(self):
        self._plan({"arbeit": [{"wochentag": 0, "von": "06:00", "bis": "14:30"}]})
        self.client.post("/api/day-type/set-range", headers=self.kopf, params={
            "von": MONTAG.isoformat(), "bis": FREITAG.isoformat(), "type": "arbeit"})
        self.assertEqual(self._ha(MONTAG)["start"], "06:00")
        self.assertEqual(self._ha(FREITAG)["start"], "07:00")

    def test_zeitraum_mit_ausdruecklicher_zeit(self):
        antwort = self.client.post("/api/day-type/set-range", headers=self.kopf, params={
            "von": MONTAG.isoformat(), "bis": FREITAG.isoformat(), "type": "arbeit",
            "von_zeit": "10:00", "bis_zeit": "19:00"})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(self._ha(FREITAG)["start"], "10:00")

    def test_geaenderte_vorgabe_verschiebt_keinen_eingetragenen_tag(self):
        self._setzen(MONTAG)
        self._plan({"arbeit": [{"wochentag": 0, "von": "09:00", "bis": "17:00"}]})
        self.assertEqual(self._ha(MONTAG)["start"], "07:00")

    def test_vorgabe_gleich_konstante_wird_nicht_gespeichert(self):
        plan = self._plan({"arbeit": [{"wochentag": 0, "von": "07:00", "bis": "16:00"}]}).json()
        self.assertFalse(plan["arbeit"][0]["eigen"])

    def test_vorgabe_ungueltig_ist_400(self):
        self.assertEqual(self._plan({"urlaub": []}).status_code, 400)
        self.assertEqual(self._plan({"arbeit": [{"wochentag": 7, "von": "07:00", "bis": "16:00"}]}).status_code, 400)
        self.assertEqual(self._plan({"arbeit": [{"wochentag": 0, "von": "17:00", "bis": "16:00"}]}).status_code, 400)

    def test_vorgabe_braucht_anmeldung(self):
        self.assertIn(self.client.get("/api/day-type/arbeitszeiten").status_code, (401, 403))

    # --- Die Neustart-Falle ---------------------------------------------------

    def test_neustart_setzt_eigene_zeiten_nicht_zurueck(self):
        """Gegen den alten Code rot: `_migrate_timezone` schrieb bei jedem Start
        alle Arbeitszeiten auf 07:00 bis 16:00 zurueck."""
        self._setzen(MONTAG, von="09:30", bis="18:00")
        _migrate_timezone()
        self.assertEqual(self._ha(MONTAG)["start"], "09:30")
        self.assertEqual(self._ha(MONTAG)["end"], "18:00")

    def test_zwei_ganze_neustarts_lassen_tag_und_vorgabe_stehen(self):
        """Der ganze Startablauf (alle Migrationen, nicht nur eine), zweimal.

        Im Betrieb nicht messbar: am 2026-11-01 hatte noch kein Mandant eine
        eigene Zeit gespeichert, der Neustart um 11:10 bewies also nichts.
        """
        self.assertEqual(self._plan({"arbeit": [{"wochentag": 4, "von": "06:15", "bis": "14:45"}]}).status_code, 200)
        self._setzen(MONTAG, von="09:30", bis="18:00")
        for _ in range(2):
            self.client.__exit__(None, None, None)
            self.client.__enter__()
        self.assertEqual(self._ha(MONTAG)["start"], "09:30")
        self.assertEqual(self._ha(MONTAG)["end"], "18:00")
        # Die Vorgabe wirkt erst beim Anlegen: ein nach den Neustarts angelegter
        # Freitag muss sie noch tragen.
        self._setzen(FREITAG)
        self.assertEqual((self._ha(FREITAG)["start"], self._ha(FREITAG)["end"]), ("06:15", "14:45"))


if __name__ == "__main__":
    unittest.main()
