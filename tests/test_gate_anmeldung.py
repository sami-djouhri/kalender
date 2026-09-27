"""Der Mandant im Sitzungs-Token, und die Anmeldung ueber einen Torwaechter.

Bis 2026-09-27 trug jedes Sitzungs-Token fest ``sub: "admin"``. Das native
Frontend lief damit fuer JEDEN Angemeldeten auf ``DEFAULT_OWNER_SUB``: ein
zweiter Nutzer sah die Daten des Owners, ohne dass die Mandantentrennung
irgendwo versagte, denn sie wurde nie nach ihm gefragt.

Die Tests hier pruefen beide Haelften:

* ``backend/sitzung.py`` -- Token tragen einen Mandanten, Altbestand gilt als Owner.
* ``/api/auth/gate-login`` -- ein signierter Mandant wird gegen ein Sitzungs-Token
  fuer genau diesen Mandanten getauscht, und zwar fail-closed.

★ Jeder Fall hier faellt gegen die alte Fassung. Das ist der Zweck: ein Test, der
auch vorher gruen war, prueft nicht die Aenderung.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-gate.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret-gate'

import jwt
from fastapi.testclient import TestClient

from backend import sitzung
from backend.config import settings
from backend.database import Base, engine
from backend.main import app, create_jwt_token
from backend.tenant_auth import expected_signature

GEHEIMNIS = "0123456789abcdef0123456789abcdef"
SUB_B = "gast-sub-zwei"


class MitLaufenderApp(unittest.TestCase):
    """Startet die App wie im Betrieb.

    ★ Der TestClient-Kontext ist hier kein Beiwerk: erst das Startup-Ereignis setzt
    den Sitzungs-Schluessel (``main._ensure_secret_key`` -> ``sitzung.schluessel_setzen``).
    Ohne ihn baut ``token_bauen`` absichtlich kein Token mehr. Die erste Fassung
    dieser Tests lief ohne Client und scheiterte deshalb achtmal an derselben
    Ursache -- was genau der Beweis dafuer ist, dass die Sperre greift.
    """

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


class SitzungsTokenTest(MitLaufenderApp):
    """backend/sitzung.py: wer steht im Token."""

    def test_ohne_schluessel_kein_token(self):
        """Die Gegenprobe zur Sperre in token_bauen."""
        vorher = sitzung.schluessel()
        sitzung.schluessel_setzen("")
        try:
            with self.assertRaises(RuntimeError):
                sitzung.token_bauen("irgendwer")
        finally:
            sitzung.schluessel_setzen(vorher)

    def test_neues_token_traegt_den_mandanten(self):
        token = create_jwt_token(SUB_B)
        self.assertEqual(sitzung.sub_aus_token(token), SUB_B)

    def test_ohne_angabe_gilt_der_owner(self):
        # Der native Passwort-Login gehoert dem Owner, nicht "irgendwem".
        self.assertEqual(sitzung.sub_aus_token(create_jwt_token()), settings.DEFAULT_OWNER_SUB)

    def test_altbestand_gilt_als_owner(self):
        """Ein vor der Umstellung ausgestelltes Token bleibt gueltig.

        Owner-Entscheid 2026-09-27: sonst waere mit dem Aufspielen jede offene
        Sitzung ungueltig geworden, auch die der Handy-App.
        """
        alt = jwt.encode(
            {
                "sub": sitzung.ALT_SUB,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
                "iat": datetime.now(timezone.utc),
            },
            sitzung.schluessel(),
            algorithm=settings.JWT_ALGORITHM,
        )
        self.assertEqual(sitzung.sub_aus_token(alt), settings.DEFAULT_OWNER_SUB)

    def test_abgelaufenes_token_liefert_keinen_mandanten(self):
        abgelaufen = jwt.encode(
            {
                "sub": SUB_B,
                "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
                "iat": datetime.now(timezone.utc) - timedelta(hours=2),
            },
            sitzung.schluessel(),
            algorithm=settings.JWT_ALGORITHM,
        )
        self.assertIsNone(sitzung.sub_aus_token(abgelaufen))
        # Nur der Refresh darf hineinsehen, und auch nur um die Nachfrist zu pruefen.
        self.assertEqual(sitzung.sub_aus_token(abgelaufen, ablauf_pruefen=False), SUB_B)

    def test_fremd_signiertes_token_zaehlt_nicht(self):
        fremd = jwt.encode(
            {"sub": SUB_B, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            "ein-anderer-schluessel",
            algorithm=settings.JWT_ALGORITHM,
        )
        self.assertIsNone(sitzung.sub_aus_token(fremd))


class GateAnmeldungTest(MitLaufenderApp):
    """/api/auth/gate-login: der Header IST hier der Zugang, also fail-closed."""

    def setUp(self):
        # Wert je Test setzen und zuruecknehmen: eine Zuweisung auf Modulebene
        # wuerde die anderen Testmodule mittreffen.
        self._vorher = settings.KALENDER_TENANT_SECRET
        settings.KALENDER_TENANT_SECRET = GEHEIMNIS

    def tearDown(self):
        settings.KALENDER_TENANT_SECRET = self._vorher

    def _kopf(self, sub, geheimnis=GEHEIMNIS):
        return {"X-Saganta-Sub": sub, "X-Saganta-Sub-Sig": expected_signature(sub, geheimnis)}

    def test_ohne_geheimnis_503(self):
        settings.KALENDER_TENANT_SECRET = ""
        r = self.client.get("/api/auth/gate-login", headers=self._kopf(SUB_B), follow_redirects=False)
        self.assertEqual(r.status_code, 503, r.text)
        self.assertNotIn("session_token", r.cookies)

    def test_ohne_signatur_401(self):
        r = self.client.get(
            "/api/auth/gate-login", headers={"X-Saganta-Sub": SUB_B}, follow_redirects=False
        )
        self.assertEqual(r.status_code, 401, r.text)

    def test_falsche_signatur_401(self):
        r = self.client.get(
            "/api/auth/gate-login",
            headers=self._kopf(SUB_B, geheimnis="falsches-geheimnis"),
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 401, r.text)

    def test_signatur_gilt_nur_fuer_ihren_mandanten(self):
        """Eine abgefangene Signatur laesst sich nicht auf einen fremden sub umhaengen."""
        kopf = self._kopf(SUB_B)
        kopf["X-Saganta-Sub"] = "ein-anderer-sub"
        r = self.client.get("/api/auth/gate-login", headers=kopf, follow_redirects=False)
        self.assertEqual(r.status_code, 401, r.text)

    def test_erzwingen_aus_macht_das_tor_nicht_weich(self):
        """Der Beobachtungsmodus gilt fuer die Datensicht, nicht fuer die Anmeldung.

        resolve_owner_sub laesst bei TENANT_HEADER_ENFORCE=0 einen unsignierten
        Header durch. Hier waere dasselbe ein Anmelde-Bypass.
        """
        vorher = settings.TENANT_HEADER_ENFORCE
        settings.TENANT_HEADER_ENFORCE = False
        try:
            r = self.client.get(
                "/api/auth/gate-login", headers={"X-Saganta-Sub": SUB_B}, follow_redirects=False
            )
            self.assertEqual(r.status_code, 401, r.text)
        finally:
            settings.TENANT_HEADER_ENFORCE = vorher

    def test_gueltige_signatur_setzt_token_fuer_diesen_mandanten(self):
        r = self.client.get(
            "/api/auth/gate-login", headers=self._kopf(SUB_B), follow_redirects=False
        )
        self.assertEqual(r.status_code, 302, r.text)
        token = r.cookies.get("session_token")
        self.assertIsNotNone(token)
        self.assertEqual(sitzung.sub_aus_token(token), SUB_B)

    def test_fremdes_ziel_wird_verworfen(self):
        for ziel in ("https://fremde.seite/x", "//fremde.seite/x"):
            with self.subTest(ziel=ziel):
                r = self.client.get(
                    f"/api/auth/gate-login?redirect={ziel}",
                    headers=self._kopf(SUB_B),
                    follow_redirects=False,
                )
                self.assertEqual(r.status_code, 302)
                self.assertEqual(r.headers["location"], "/")

    def test_eigenes_ziel_bleibt(self):
        r = self.client.get(
            "/api/auth/gate-login?redirect=/kontakte",
            headers=self._kopf(SUB_B),
            follow_redirects=False,
        )
        self.assertEqual(r.headers["location"], "/kontakte")


class TokenLoginZielTest(MitLaufenderApp):
    """Derselbe offene Weiterleiter steckte im Feed-Token-Login."""

    def _feed_token(self):
        from backend.database import SessionLocal
        from backend.models import Setting

        db = SessionLocal()
        db.info["owner_sub"] = None
        try:
            return db.query(Setting).filter(Setting.key == "feed_token").first().value
        finally:
            db.close()

    def test_fremdes_ziel_wird_verworfen(self):
        r = self.client.get(
            f"/api/auth/token-login?token={self._feed_token()}&redirect=https://fremde.seite",
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/")


class MandantAusDemTokenTest(MitLaufenderApp):
    """Die eigentliche Wirkung: das Sitzungs-Token entscheidet ueber die Datensicht."""

    def test_zwei_token_sehen_verschiedene_daten(self):
        owner = {"Authorization": f"Bearer {create_jwt_token()}"}
        gast = {"Authorization": f"Bearer {create_jwt_token(SUB_B)}"}

        r = self.client.post("/api/todos", json={"title": "nur-fuer-den-owner"}, headers=owner)
        self.assertIn(r.status_code, (200, 201), r.text)

        titel_gast = [t["title"] for t in self.client.get("/api/todos", headers=gast).json()]
        self.assertNotIn("nur-fuer-den-owner", titel_gast)
        titel_owner = [t["title"] for t in self.client.get("/api/todos", headers=owner).json()]
        self.assertIn("nur-fuer-den-owner", titel_owner)

    def test_signierter_header_gewinnt_ueber_das_token(self):
        """Der BFF muss im Namen eines Mandanten fragen koennen, trotz Cookie im Browser."""
        vorher = settings.KALENDER_TENANT_SECRET
        settings.KALENDER_TENANT_SECRET = GEHEIMNIS
        try:
            kopf = {
                "Authorization": f"Bearer {create_jwt_token()}",  # Token sagt: Owner
                "X-Saganta-Sub": SUB_B,                            # Header sagt: Gast
                "X-Saganta-Sub-Sig": expected_signature(SUB_B, GEHEIMNIS),
            }
            self.client.post("/api/todos", json={"title": "gast-hat-geschrieben"}, headers=kopf)
            titel_gast = [t["title"] for t in self.client.get("/api/todos", headers=kopf).json()]
            self.assertIn("gast-hat-geschrieben", titel_gast)

            nur_owner = {"Authorization": f"Bearer {create_jwt_token()}"}
            titel_owner = [t["title"] for t in self.client.get("/api/todos", headers=nur_owner).json()]
            self.assertNotIn("gast-hat-geschrieben", titel_owner)
        finally:
            settings.KALENDER_TENANT_SECRET = vorher

    def test_refresh_behaelt_den_mandanten(self):
        """★ Der scharfe Fall, und er faellt erst Stunden spaeter auf.

        Ein Refresh, der das neue Token ohne sub ausstellt, macht aus der Sitzung
        eines Gastes stillschweigend eine Owner-Sitzung. Sichtbar wird das nicht
        beim Anmelden, sondern wenn das Frontend irgendwann verlaengert.
        """
        gast = {"Authorization": f"Bearer {create_jwt_token(SUB_B)}"}
        r = self.client.post("/api/auth/refresh", headers=gast)
        self.assertEqual(r.status_code, 200, r.text)
        neu = r.json()["access_token"]
        self.assertEqual(sitzung.sub_aus_token(neu), SUB_B)

    def test_headerloser_pfad_bleibt_beim_owner(self):
        """CORE: Home Assistant und iCal rufen ohne Kopf und ohne Cookie."""
        from backend.database import SessionLocal
        from backend.models import Setting

        db = SessionLocal()
        db.info["owner_sub"] = None
        try:
            token = db.query(Setting).filter(Setting.key == "feed_token").first().value
        finally:
            db.close()
        r = self.client.get(f"/api/day-type/today?token={token}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("type", r.json())


if __name__ == "__main__":
    unittest.main()
