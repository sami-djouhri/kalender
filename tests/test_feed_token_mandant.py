"""Der Feed-Token je Mandant, und was dabei NICHT kaputtgehen darf.

Der Token lag ausschliesslich in der nicht mandantengetrennten Tabelle
``settings``, und ``/api/feed-token`` gab deshalb jedem den des Owners. Wer ihn
hat, oeffnet ueber ``token-login`` dessen ganzen Kalender und liest seine
iCal-Feeds.

★ Die Haelfte dieser Tests prueft die Gegenrichtung: der Owner-Token muss Wort
fuer Wort weiter funktionieren. An ihm haengt ``/api/day-type/today`` in Home
Assistant, also das 05:35-Weckfenster, und der Auto-Login des Vhosts.
"""
import os
import unittest

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-feedtoken.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret-feedtoken'

from fastapi.testclient import TestClient

from backend import mandant_einstellungen
from backend.config import settings
from backend.database import Base, SessionLocal, engine
from backend.main import app, create_jwt_token
from backend.models import MandantEinstellung, Setting

SUB_B = "feedtoken-gast"


def _system_session():
    db = SessionLocal()
    db.info["owner_sub"] = None
    return db


class FeedTokenJeMandantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def _owner_token(self):
        db = _system_session()
        try:
            return db.query(Setting).filter(Setting.key == "feed_token").first().value
        finally:
            db.close()

    # --- Der Owner bleibt, wo er ist -------------------------------------

    def test_owner_behaelt_seinen_token_in_settings(self):
        """Kein Umzug: der Wert des Owners bleibt in der globalen Tabelle."""
        db = _system_session()
        try:
            self.assertEqual(
                mandant_einstellungen.feed_token_fuer(db, settings.DEFAULT_OWNER_SUB),
                self._owner_token(),
            )
            # Und es entsteht KEIN zweiter Wert daneben.
            self.assertIsNone(
                db.query(MandantEinstellung)
                .filter(MandantEinstellung.owner_sub == settings.DEFAULT_OWNER_SUB)
                .first()
            )
        finally:
            db.close()

    def test_daytype_mit_owner_token_unveraendert(self):
        """Ebene 1, der eingefrorene Vertrag mit Home Assistant."""
        r = self.client.get(f"/api/day-type/today?token={self._owner_token()}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("type", r.json())

    def test_daytype_weist_mandanten_token_ab(self):
        """Ebene 1 gehoert dem Homelab, also dem Owner. Ein Gast-Token hat dort nichts zu suchen."""
        db = _system_session()
        try:
            gast = mandant_einstellungen.feed_token_fuer(db, SUB_B)
        finally:
            db.close()
        r = self.client.get(f"/api/day-type/today?token={gast}")
        self.assertEqual(r.status_code, 403, r.text)

    # --- Der Gast bekommt einen eigenen ---------------------------------

    def test_endpunkt_gibt_jedem_seinen_eigenen(self):
        owner = {"Authorization": f"Bearer {create_jwt_token()}"}
        gast = {"Authorization": f"Bearer {create_jwt_token(SUB_B)}"}

        t_owner = self.client.get("/api/feed-token", headers=owner).json()["feed_token"]
        t_gast = self.client.get("/api/feed-token", headers=gast).json()["feed_token"]

        self.assertTrue(t_owner and t_gast)
        self.assertNotEqual(t_owner, t_gast)
        self.assertEqual(t_owner, self._owner_token())

    def test_gast_token_ist_bestaendig(self):
        """Zweimal abfragen ergibt denselben Wert, nicht jedes Mal einen neuen."""
        gast = {"Authorization": f"Bearer {create_jwt_token(SUB_B)}"}
        erst = self.client.get("/api/feed-token", headers=gast).json()["feed_token"]
        dann = self.client.get("/api/feed-token", headers=gast).json()["feed_token"]
        self.assertEqual(erst, dann)

    def test_aufloesung_in_beide_richtungen(self):
        db = _system_session()
        try:
            gast = mandant_einstellungen.feed_token_fuer(db, SUB_B)
            self.assertEqual(mandant_einstellungen.mandant_fuer_feed_token(db, gast), SUB_B)
            self.assertEqual(
                mandant_einstellungen.mandant_fuer_feed_token(db, self._owner_token()),
                settings.DEFAULT_OWNER_SUB,
            )
            self.assertIsNone(mandant_einstellungen.mandant_fuer_feed_token(db, "erfunden"))
            self.assertIsNone(mandant_einstellungen.mandant_fuer_feed_token(db, ""))
        finally:
            db.close()

    # --- token-login loest auf, statt nur zu vergleichen ------------------

    def test_token_login_owner(self):
        from backend import sitzung

        r = self.client.get(
            f"/api/auth/token-login?token={self._owner_token()}", follow_redirects=False
        )
        self.assertEqual(r.status_code, 302, r.text)
        self.assertEqual(
            sitzung.sub_aus_token(r.cookies["session_token"]), settings.DEFAULT_OWNER_SUB
        )

    def test_token_login_gast_landet_bei_sich(self):
        """★ Der Fall, der vorher unmoeglich war und danach still falsch gewesen waere.

        Mit einer gescopten Session findet die Aufloesung den Gast-Token nicht
        (sie sieht nur Owner-Zeilen) und die Anmeldung landete beim Owner: ein
        funktionierender Login in einen fremden Kalender.
        """
        from backend import sitzung

        db = _system_session()
        try:
            gast = mandant_einstellungen.feed_token_fuer(db, SUB_B)
        finally:
            db.close()
        r = self.client.get(f"/api/auth/token-login?token={gast}", follow_redirects=False)
        self.assertEqual(r.status_code, 302, r.text)
        self.assertEqual(sitzung.sub_aus_token(r.cookies["session_token"]), SUB_B)

    def test_token_login_erfundener_token(self):
        r = self.client.get("/api/auth/token-login?token=nicht-echt", follow_redirects=False)
        self.assertEqual(r.status_code, 401)

    # --- iCal: jeder sieht seinen eigenen Kalender -----------------------

    def test_ical_derselbe_kalender_zwei_inhalte(self):
        """★★ Der scharfe Fall: EIN Kalender, zwei Token, zwei Inhalte.

        Die alte Fassung haette dem Gast schlicht 403 gegeben, sein Token war ja
        nicht der eine globale. Ein Test, der nur prueft, dass der Gast den
        Owner-Termin nicht sieht, waere deshalb auch vorher gruen gewesen und
        haette die Aenderung nicht beruehrt.

        Neu muss beides gelten: der Gast bekommt seinen Feed **und** darin steht
        ausschliesslich sein eigener Termin. Genau das haengt daran, dass
        ``verify_feed_token`` die Session auf den Mandanten des Tokens hebt; ohne
        diesen Schritt faellt sie auf den Owner zurueck und der Gast liest dessen
        Termine, mit HTTP 200 und ohne jede Spur im Protokoll.
        """
        from backend.system_calendars import TERMINE_CAL_ID

        owner = {"Authorization": f"Bearer {create_jwt_token()}"}
        gast_sitzung = {"Authorization": f"Bearer {create_jwt_token(SUB_B)}"}
        kalender_id = TERMINE_CAL_ID

        for kopf, titel in ((owner, "Owner-Termin-ical"), (gast_sitzung, "Gast-Termin-ical")):
            r = self.client.post(
                "/api/events",
                json={
                    "calendar_id": kalender_id,
                    "title": titel,
                    "start": "2026-10-01T10:00:00",
                    "end": "2026-10-01T11:00:00",
                },
                headers=kopf,
            )
            self.assertIn(r.status_code, (200, 201), r.text)

        db = _system_session()
        try:
            gast_token = mandant_einstellungen.feed_token_fuer(db, SUB_B)
        finally:
            db.close()

        feed_owner = self.client.get(
            f"/api/calendars/{kalender_id}/ical?token={self._owner_token()}"
        )
        self.assertEqual(feed_owner.status_code, 200, feed_owner.text)
        self.assertIn("Owner-Termin-ical", feed_owner.text)
        self.assertNotIn("Gast-Termin-ical", feed_owner.text)

        feed_gast = self.client.get(f"/api/calendars/{kalender_id}/ical?token={gast_token}")
        self.assertEqual(feed_gast.status_code, 200, feed_gast.text)
        self.assertIn("Gast-Termin-ical", feed_gast.text)
        self.assertNotIn("Owner-Termin-ical", feed_gast.text)


if __name__ == "__main__":
    unittest.main()
