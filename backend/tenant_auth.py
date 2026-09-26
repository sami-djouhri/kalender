"""Echtheitsprüfung des ``X-Saganta-Sub``-Headers.

**Das Problem, das hier adressiert wird:** Der Header kam bis 2026-08 ungeprüft aus
dem Request (``database.get_db``). Die gesamte Mandantentrennung hängt damit an der
Annahme, dass der ``kalender-bff`` der einzige Weg zum Dienst ist. Wer ``:8085``
direkt erreicht, kann sich mit einem beliebigen ``X-Saganta-Sub`` als fremder
Mandant ausgeben, das ORM-Scoping in ``tenant.py`` glaubt ihm.

**Die Lösung** ist bewusst klein und stufenweise, nach dem bereits erprobten Muster
in ``lager/app/backend_auth.py`` (stdlib-HMAC, kein zusätzliches Paket):

Der Absender schickt zusätzlich ``X-Saganta-Sub-Sig: hex(HMAC-SHA256(secret, sub))``.
Die Signatur ist an genau diesen ``sub`` gebunden, eine abgefangene Signatur lässt
sich nicht auf einen fremden ``sub`` umhängen. Das Geheimnis selbst wandert nie über
die Leitung (anders als bei einem statischen Shared-Header).

Drei Zustände, gesteuert über ``KALENDER_TENANT_SECRET`` und ``TENANT_HEADER_ENFORCE``:

1. **Kein Secret konfiguriert** (Auslieferungszustand): verhält sich **exakt wie
   vorher**. Kein Zwang, keine Warnung, sonst wäre der Betrieb ab dem ersten Deploy
   voller Rauschen, und Rauschen ist genau das, was in diesem System schon einmal
   einen echten Ausfall verdeckt hat.
2. **Secret gesetzt, ``TENANT_HEADER_ENFORCE=0``** (Beobachtungsphase): unsignierte
   oder falsch signierte Header werden **akzeptiert und protokolliert**. So sieht
   man vor dem Scharfschalten, welche Absender noch nachziehen müssen.
3. **Secret gesetzt, ``TENANT_HEADER_ENFORCE=1``**: alles Unsignierte gibt 401,
   kein stiller Rückfall auf den Owner.

**Unberührt in allen drei Zuständen:** Aufrufe *ohne* ``X-Saganta-Sub``. Das sind
die CORE-Pfade (Home Assistant über ``/api/day-type/*``, iCal, Postfach,
Knowledge-Gateway, natives Frontend), sie laufen weiter auf ``DEFAULT_OWNER_SUB``.
Das ist kein Übergangszustand, sondern der Vertrag: das Homelab *ist* der Owner.
"""

import hashlib
import hmac
import logging

from fastapi import HTTPException, status

from backend.config import settings

logger = logging.getLogger(__name__)

SUB_HEADER = "x-saganta-sub"
SIG_HEADER = "x-saganta-sub-sig"


def expected_signature(sub: str, secret: str) -> str:
    """Die Signatur, die ein Absender für diesen ``sub`` mitschicken muss."""
    return hmac.new(secret.encode("utf-8"), sub.encode("utf-8"), hashlib.sha256).hexdigest()


def resolve_owner_sub(request) -> str:
    """Mandant für diesen Request. Wirft 401 nur im Erzwingen-Modus."""
    if request is None:
        return settings.DEFAULT_OWNER_SUB

    sub = request.headers.get(SUB_HEADER)
    if not sub:
        # Headerloser Pfad: CORE. Unverändert.
        return settings.DEFAULT_OWNER_SUB

    secret = settings.KALENDER_TENANT_SECRET
    if not secret:
        # Noch kein Geheimnis vergeben: wie bisher, ohne Rauschen.
        return sub

    provided = request.headers.get(SIG_HEADER, "")
    if provided and hmac.compare_digest(provided, expected_signature(sub, secret)):
        return sub

    grund = "ohne Signatur" if not provided else "mit falscher Signatur"
    if settings.TENANT_HEADER_ENFORCE:
        logger.warning("Mandanten-Header %s abgelehnt (sub=%s…)", grund, sub[:8])
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "X-Saganta-Sub ohne gültige Signatur",
        )
    logger.warning(
        "Mandanten-Header %s akzeptiert (Beobachtungsphase, sub=%s…): Absender nachziehen, "
        "bevor TENANT_HEADER_ENFORCE=1 gesetzt wird",
        grund,
        sub[:8],
    )
    return sub
