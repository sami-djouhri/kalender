"""Tests für Security-Härtung (backend/security_utils.py + Auth-Flow).

Passwort-Hashing (inkl. Legacy-Klartext-Migration beim Login), Rate-Limiting,
persistenter SECRET_KEY. unittest-Style.
"""

import os
import unittest

os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/kalender-unittest-security.db')
os.environ.setdefault('KALENDER_PASSWORD', 'test-password')
os.environ.setdefault('SECRET_KEY', 'test-secret')

from backend.security_utils import (
    RateLimiter,
    hash_password,
    is_hashed,
    verify_password,
)


class HashTest(unittest.TestCase):
    def test_hash_roundtrip(self):
        h = hash_password("geheim123")
        self.assertTrue(is_hashed(h))
        self.assertTrue(verify_password("geheim123", h))
        self.assertFalse(verify_password("falsch", h))

    def test_hash_is_salted(self):
        self.assertNotEqual(hash_password("x"), hash_password("x"))

    def test_legacy_plaintext_verify(self):
        # Bestehende Klartext-Passwörter müssen weiter funktionieren (Migration).
        self.assertTrue(verify_password("altpass", "altpass"))
        self.assertFalse(verify_password("altpass", "anders"))
        self.assertFalse(is_hashed("altpass"))

    def test_empty_stored(self):
        self.assertFalse(verify_password("x", ""))


class RateLimiterTest(unittest.TestCase):
    def test_blocks_after_max(self):
        rl = RateLimiter(max_attempts=3, window_seconds=100)
        t = 1000.0
        self.assertEqual(rl.check("ip", now=t)[0], True)
        self.assertEqual(rl.check("ip", now=t)[0], True)
        self.assertEqual(rl.check("ip", now=t)[0], True)
        allowed, retry = rl.check("ip", now=t)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)

    def test_window_slides(self):
        rl = RateLimiter(max_attempts=2, window_seconds=100)
        rl.check("ip", now=1000.0)
        rl.check("ip", now=1000.0)
        self.assertFalse(rl.check("ip", now=1050.0)[0])
        # nach Ablauf des Fensters wieder frei
        self.assertTrue(rl.check("ip", now=1200.0)[0])

    def test_reset(self):
        rl = RateLimiter(max_attempts=1, window_seconds=100)
        rl.check("ip", now=1000.0)
        self.assertFalse(rl.check("ip", now=1000.0)[0])
        rl.reset("ip")
        self.assertTrue(rl.check("ip", now=1000.0)[0])

    def test_keys_independent(self):
        rl = RateLimiter(max_attempts=1, window_seconds=100)
        rl.check("a", now=1000.0)
        self.assertTrue(rl.check("b", now=1000.0)[0])


class AuthFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from backend.database import Base, engine
        from backend.main import app
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        from backend.database import Base, engine
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def test_login_success_and_password_is_hashed(self):
        r = self.client.post("/api/auth/login", json={"password": "test-password"})
        self.assertEqual(r.status_code, 200, r.text)
        # In der DB darf kein Klartext mehr stehen
        from backend.database import SessionLocal
        from backend.models import Setting
        db = SessionLocal()
        try:
            row = db.query(Setting).filter(Setting.key == "kalender_password").first()
            self.assertTrue(is_hashed(row.value), "Passwort muss gehasht gespeichert sein")
        finally:
            db.close()

    def test_wrong_password_rejected(self):
        r = self.client.post("/api/auth/login", json={"password": "falsch"})
        self.assertEqual(r.status_code, 401)

    def test_secret_key_persisted_when_no_env(self):
        # Mit gesetztem SECRET_KEY-Env wird KEIN DB-Key erzeugt (env gewinnt).
        from backend.database import SessionLocal
        from backend.models import Setting
        db = SessionLocal()
        try:
            row = db.query(Setting).filter(Setting.key == "secret_key").first()
            # In dieser Testumgebung ist SECRET_KEY gesetzt -> kein DB-Row nötig
            self.assertIsNone(row)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
