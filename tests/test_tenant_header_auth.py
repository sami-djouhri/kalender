"""Echtheitsprüfung des X-Saganta-Sub-Headers (backend/tenant_auth.py).

Der wichtigste Test hier ist der letzte: die CORE-Pfade laufen headerlos und dürfen
sich in KEINEM Zustand ändern, sonst hängt am Ende der Wecker daran.
"""
import os
import unittest

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-tenantauth.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi import HTTPException  # noqa: E402

from backend import tenant_auth  # noqa: E402
from backend.config import settings  # noqa: E402

SECRET = "test-tenant-secret"
SUB_A = "tenant-a-sub"
SUB_B = "tenant-b-sub"

# Gemeinsamer Vektor mit der Absenderseite. Die Absender liegen in einem anderen
# Repo (saganta: kalender-bff, projectdeck-api, news-api, auth-proxy) und koennen
# diesen Pruefer nicht importieren. Statt eines Tests, der nur auf einem Host mit
# beiden Baeumen laeuft (und anderswo still gruen waere), prueft JEDE Seite gegen
# denselben bekannten Wert. Aendert eine Seite die Formel, bricht ihr eigener Test.
# Gegenstueck: saganta/services/kalender-bff/tests/test_tenant_sig.py
VEKTOR_SECRET = "saganta-kalender-interop-testvektor"
VEKTOR_SUB = "test-sub-0123456789"
VEKTOR_SIG = "93c03830288e7ce029d000f0ac8da629d8e6f45e7b1d1d17a6a253719abab49a"


class FakeRequest:
    """Minimaler Request-Ersatz: tenant_auth liest ausschliesslich Header."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


class TenantHeaderAuthTest(unittest.TestCase):
    def setUp(self):
        self._secret = settings.KALENDER_TENANT_SECRET
        self._enforce = settings.TENANT_HEADER_ENFORCE

    def tearDown(self):
        settings.KALENDER_TENANT_SECRET = self._secret
        settings.TENANT_HEADER_ENFORCE = self._enforce

    def _configure(self, secret: str = SECRET, enforce: bool = False):
        settings.KALENDER_TENANT_SECRET = secret
        settings.TENANT_HEADER_ENFORCE = enforce

    def test_ohne_secret_verhaelt_es_sich_wie_vorher(self):
        self._configure(secret="", enforce=False)
        req = FakeRequest({"X-Saganta-Sub": SUB_A})
        self.assertEqual(tenant_auth.resolve_owner_sub(req), SUB_A)

    def test_gueltige_signatur_wird_akzeptiert(self):
        self._configure(enforce=True)
        req = FakeRequest({
            "X-Saganta-Sub": SUB_A,
            "X-Saganta-Sub-Sig": tenant_auth.expected_signature(SUB_A, SECRET),
        })
        self.assertEqual(tenant_auth.resolve_owner_sub(req), SUB_A)

    def test_beobachtungsphase_akzeptiert_unsigniert(self):
        self._configure(enforce=False)
        req = FakeRequest({"X-Saganta-Sub": SUB_A})
        self.assertEqual(tenant_auth.resolve_owner_sub(req), SUB_A)

    def test_erzwingen_lehnt_unsigniert_ab(self):
        self._configure(enforce=True)
        req = FakeRequest({"X-Saganta-Sub": SUB_A})
        with self.assertRaises(HTTPException) as ctx:
            tenant_auth.resolve_owner_sub(req)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_erzwingen_lehnt_falsche_signatur_ab(self):
        self._configure(enforce=True)
        req = FakeRequest({"X-Saganta-Sub": SUB_A, "X-Saganta-Sub-Sig": "deadbeef"})
        with self.assertRaises(HTTPException):
            tenant_auth.resolve_owner_sub(req)

    def test_signatur_ist_an_den_sub_gebunden(self):
        """Eine abgefangene Signatur darf sich nicht auf einen fremden sub umhaengen lassen."""
        self._configure(enforce=True)
        req = FakeRequest({
            "X-Saganta-Sub": SUB_B,
            "X-Saganta-Sub-Sig": tenant_auth.expected_signature(SUB_A, SECRET),
        })
        with self.assertRaises(HTTPException):
            tenant_auth.resolve_owner_sub(req)

    def test_headerlos_bleibt_owner_in_jedem_zustand(self):
        """CORE: Home Assistant, iCal, Postfach, KG rufen ohne Header auf."""
        for secret, enforce in (("", False), (SECRET, False), (SECRET, True)):
            with self.subTest(secret=bool(secret), enforce=enforce):
                self._configure(secret=secret, enforce=enforce)
                self.assertEqual(
                    tenant_auth.resolve_owner_sub(FakeRequest()),
                    settings.DEFAULT_OWNER_SUB,
                )
                self.assertEqual(
                    tenant_auth.resolve_owner_sub(None),
                    settings.DEFAULT_OWNER_SUB,
                )


class InteropVektorTest(unittest.TestCase):
    """Haelt die Formel mit der Absenderseite (saganta) synchron."""

    def test_pruefer_erwartet_den_gemeinsamen_vektor(self):
        self.assertEqual(
            tenant_auth.expected_signature(VEKTOR_SUB, VEKTOR_SECRET),
            VEKTOR_SIG,
            "Signaturformel weicht vom gemeinsamen Vektor ab, die Absender in "
            "saganta (kalender-bff/projectdeck-api/news-api/auth-proxy) wuerden "
            "beim Erzwingen ausgesperrt.",
        )


if __name__ == "__main__":
    unittest.main()
