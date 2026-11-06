"""Mandanten-Header fuer die Testsuite, signiert wie beim echten Absender.

**Warum es diese Datei gibt:** `run-tests.sh` laeuft im Dienst-Image, und das
Compose bindet `.env.tenant` als `env_file` ein. Der Testlauf erbt damit den
Betriebszustand `TENANT_HEADER_ENFORCE=1` samt Mandanten-Geheimnis. Ein
`X-Saganta-Sub` **ohne** Signatur gibt dort 401 (`backend/tenant_auth.py`), und
genau daran scheiterten `test_multitenant` und `test_steuerdaten_schutz`: sie
stempelten den Header von Hand, wie es vor dem Scharfschalten am 2026-09-27
ausreichte.

Die Tests bekommen hier ein **eigenes** Geheimnis, nicht das aus der Umgebung.
Zwei Gruende, und der zweite ist der wichtigere:

1. Kein Test haengt am Betriebswert. Wer `.env.tenant` dreht, dreht nicht die
   Testsuite mit.
2. Der Zustand ist **deterministisch scharf**. Ohne das Setzen liefe die Suite in
   einem Klon ohne `.env.tenant` (das ist `required: false`) im Alt-Verhalten
   durch: unsignierte Header werden dann akzeptiert, die Mandantentests waeren
   gruen, und zwar ohne den Pfad zu beruehren, den der Betrieb geht. Ein Test, der
   nur in der eigenen Umgebung das Richtige prueft, ist anderswo still gruen.

Die Signaturpruefung selbst gehoert nicht hierher, die steht in
`test_tenant_header_auth.py` (alle drei Zustaende) und `test_gate_anmeldung.py`
(fail-closed am Torwaechter). Hier geht es um das Gegenteil: dass ein *gueltig*
signierter Mandant arbeiten kann.
"""

from backend.config import settings
from backend.tenant_auth import expected_signature

# Bewusst nicht das Betriebsgeheimnis, siehe Modulkopf.
TEST_GEHEIMNIS = "kalender-test-mandanten-geheimnis"


def _zuruecknehmen(geheimnis: str, erzwingen: bool) -> None:
    settings.KALENDER_TENANT_SECRET = geheimnis
    settings.TENANT_HEADER_ENFORCE = erzwingen


def scharf_schalten(testklasse) -> None:
    """Erzwingen-Modus mit Test-Geheimnis, zurueckgenommen am Klassenende.

    Aufruf in ``setUpClass``. ``addClassCleanup`` statt ``tearDownClass``: die
    Ruecknahme ist damit registriert, **bevor** der restliche Aufbau laufen kann,
    und greift auch dann, wenn der danach fehlschlaegt. Sonst bliebe das
    Test-Geheimnis in ``settings`` stehen und die nachfolgenden Testmodule liefen
    in einem Zustand, den niemand gesetzt hat.
    """
    testklasse.addClassCleanup(
        _zuruecknehmen,
        settings.KALENDER_TENANT_SECRET,
        settings.TENANT_HEADER_ENFORCE,
    )
    settings.KALENDER_TENANT_SECRET = TEST_GEHEIMNIS
    settings.TENANT_HEADER_ENFORCE = True


def kopf(sub: str, basis: dict | None = None) -> dict:
    """Header fuer einen Mandanten: ``X-Saganta-Sub`` plus gueltige Signatur.

    ``basis`` wird nicht veraendert, sondern kopiert (die Aufrufer teilen sich ein
    gemeinsames ``Authorization``-Woerterbuch).
    """
    fertig = dict(basis or {})
    fertig["X-Saganta-Sub"] = sub
    fertig["X-Saganta-Sub-Sig"] = expected_signature(sub, settings.KALENDER_TENANT_SECRET)
    return fertig
