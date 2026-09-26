import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-refresh.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

import jwt
from fastapi.testclient import TestClient

from backend.config import settings
from backend.database import Base, engine
from backend.main import app


def _mint(iat: datetime, exp: datetime) -> str:
    return jwt.encode(
        {"sub": "admin", "iat": iat, "exp": exp},
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


class AuthRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=engine)

    def test_refresh_with_fresh_bearer_returns_new_token(self):
        now = datetime.now(timezone.utc)
        token = _mint(now, now + timedelta(hours=1))
        r = self.client.post(
            "/api/auth/refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["token_type"], "bearer")
        self.assertTrue(body["access_token"])
        self.assertNotEqual(body["access_token"], token)
        decoded = jwt.decode(
            body["access_token"],
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        self.assertEqual(decoded["sub"], "admin")

    def test_refresh_with_expired_but_within_grace_succeeds(self):
        now = datetime.now(timezone.utc)
        iat = now - timedelta(days=5)
        token = _mint(iat, now - timedelta(hours=1))
        r = self.client.post(
            "/api/auth/refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200)

    def test_refresh_with_token_older_than_grace_window_is_rejected(self):
        now = datetime.now(timezone.utc)
        iat = now - timedelta(days=settings.JWT_REFRESH_GRACE_DAYS + 1)
        token = _mint(iat, now - timedelta(days=1))
        r = self.client.post(
            "/api/auth/refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 401)

    def test_refresh_with_invalid_signature_is_rejected(self):
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {"sub": "admin", "iat": now, "exp": now + timedelta(hours=1)},
            "wrong-secret",
            algorithm=settings.JWT_ALGORITHM,
        )
        r = self.client.post(
            "/api/auth/refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 401)

    def test_refresh_without_token_is_rejected(self):
        with TestClient(app) as fresh:
            r = fresh.post("/api/auth/refresh")
            self.assertEqual(r.status_code, 401)

    def test_refresh_via_cookie_works(self):
        now = datetime.now(timezone.utc)
        token = _mint(now, now + timedelta(hours=1))
        r = self.client.post(
            "/api/auth/refresh",
            cookies={"session_token": token},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("session_token", r.cookies)


if __name__ == "__main__":
    unittest.main()
