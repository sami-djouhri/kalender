"""Sicherheits-Helfer: Passwort-Hashing (pbkdf2, stdlib) + einfacher Rate-Limiter.

Kein externes Dependency (bcrypt/passlib): pbkdf2_hmac aus hashlib ist für ein
einzelnes App-Passwort ausreichend stark. Format:
``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``.
"""

import hashlib
import hmac
import secrets
import threading
import time

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Erzeugt einen gesalzenen pbkdf2-Hash."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def is_hashed(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_ALGO + "$")


def verify_password(password: str, stored: str) -> bool:
    """Prüft ein Passwort gegen einen gespeicherten Wert.

    Unterstützt sowohl den pbkdf2-Hash als auch Legacy-Klartext (für die einmalige
    Migration bestehender Installationen): beides in konstanter Zeit verglichen.
    """
    if not stored:
        return False
    if is_hashed(stored):
        try:
            _algo, iters, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
            )
            return hmac.compare_digest(dk.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    # Legacy-Klartext
    return hmac.compare_digest(password, stored)


class RateLimiter:
    """Sliding-Window-Rate-Limiter (in-memory, thread-safe).

    Für eine Single-Instance-App ausreichend: begrenzt Fehlversuche pro Schlüssel
    (z.B. Client-IP) und liefert die Restsperre in Sekunden.
    """

    def __init__(self, max_attempts: int = 8, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """(erlaubt?, retry_after_seconds). Zählt den Versuch mit."""
        now = now if now is not None else time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.max_attempts:
                retry = int(self.window - (now - hits[0])) + 1
                self._hits[key] = hits
                return False, max(1, retry)
            hits.append(now)
            self._hits[key] = hits
            # Gelegentliches Aufräumen alter Schlüssel
            if len(self._hits) > 1024:
                self._hits = {
                    k: [t for t in v if now - t < self.window]
                    for k, v in self._hits.items()
                    if any(now - t < self.window for t in v)
                }
            return True, 0

    def reset(self, key: str) -> None:
        """Nach erfolgreichem Login den Zähler leeren."""
        with self._lock:
            self._hits.pop(key, None)
