"""Geheimnisse aus dem Zugriffsprotokoll heraushalten.

DAS PROBLEM
-----------
uvicorn protokolliert jede Anfrage samt vollstaendiger Adresse: **inklusive
Query-String**. Der Kalender nimmt seinen ``feed_token`` aber als Query-Parameter
entgegen, und dieser Token ist laut API-Vertrag zugleich das Auto-Login-Ticket
(``GET /api/auth/token-login``): wer ihn hat, ist Owner. Jede Zeile sah so aus::

    INFO: 10.210.3.6 - "GET /api/day-type?date=…&token=<klartext> HTTP/1.1" 200 OK

Gemessen am 2026-08-25: **509 von 644 Zeilen (79 %)**. Home Assistant allein fragt
mit zwei REST-Sensoren alle 300 s.

Die Zeilen bleiben nicht im Container: ``monitoring-promtail-1`` bindet
``/var/lib/docker/containers`` ein und scrapt per ``docker_sd_configs`` **alle**
Container nach Loki. Dessen Volume ``monitoring_loki-data`` liegt unter
``/var/lib/docker/volumes``, und das ist im restic-Satz. Das Geheimnis lag damit
in den Off-Site-Sicherungen, an einer Stelle, an der es niemand sucht, der seine
Secrets inventarisiert.

WARUM NICHT EINFACH EIN HEADER STATT QUERY
------------------------------------------
Weil iCal-Abos (Apple/Google/Thunderbird) keinen eigenen Header senden koennen,
*muss* die Abo-Adresse den Token tragen. Der Query-Parameter bleibt Teil des
Vertrags; abgestellt wird nur das Protokollieren seines Werts.

WARUM NICHT ``--no-access-log``
-------------------------------
Weil Beobachtbarkeit hier der falsche Preis waere. Stille hat in diesem System
schon einmal einen echten Ausfall verdeckt.

WAS STATTDESSEN PASSIERT
------------------------
Der **Name** des Parameters bleibt stehen, nur der Wert wird zu ``***``::

    INFO: 10.210.3.6 - "GET /api/day-type?date=…&token=*** HTTP/1.1" 200 OK

So bleibt erkennbar, dass der Aufruf token-authentifiziert war, und die
harmlosen Parameter bleiben lesbar. Ein gekuerztes Praefix des Geheimnisses waere
ausdruecklich **kein** gueltiger Kompromiss: Struktur zeigen, nie Werte.
"""
from __future__ import annotations

import logging
import re

#: Parameternamen, deren Wert nie protokolliert werden darf.
GEHEIME_PARAMETER = (
    "token",
    "feed_token",
    "access_token",
    "api_key",
    "apikey",
    "key",
    "secret",
    "password",
    "passwd",
    "pw",
    "sig",
    "signature",
)

ERSATZ = "***"

# Greift ab '?' oder '&' bis zum naechsten Trenner. Der Parametername wird als
# Gruppe erhalten, damit die Struktur der Adresse sichtbar bleibt.
_MUSTER = re.compile(
    r"(?i)([?&](?:" + "|".join(re.escape(p) for p in GEHEIME_PARAMETER) + r")=)[^&\s]*"
)


def geheimnisse_maskieren(adresse: str) -> str:
    """Ersetzt die Werte geheimer Query-Parameter durch ``***``.

    Idempotent: ein bereits maskierter String bleibt unveraendert.

    >>> geheimnisse_maskieren("/api/day-type?date=2026-08-31&token=s3hr-geheim")
    '/api/day-type?date=2026-08-31&token=***'
    """
    return _MUSTER.sub(r"\1" + ERSATZ, adresse)


class GeheimnisFilter(logging.Filter):
    """Maskiert die Adresse in uvicorns Zugriffs-Datensaetzen.

    uvicorn protokolliert (``uvicorn/protocols/http/h11_impl.py``) mit::

        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            client_addr, method, path_with_query, http_version, status,
        )

    ``record.args`` ist also ein **5-Tupel**, die Adresse steht auf Index 2. Die
    Stelligkeit muss erhalten bleiben: uvicorns ``AccessFormatter`` entpackt sie
    in genau fuenf Namen und wuerfe sonst einen Fehler beim Formatieren.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            maskiert = geheimnisse_maskieren(args[2])
            if maskiert != args[2]:
                record.args = (args[0], args[1], maskiert, args[3], args[4])
        return True


def filter_installieren() -> None:
    """Haengt den Filter an den Logger ``uvicorn.access``. Mehrfachaufruf ist harmlos.

    **Am Logger, nicht am Handler:** ``logging.config.dictConfig`` entfernt beim
    Konfigurieren die bestehenden *Handler* eines Loggers, aber **nicht** dessen
    Filter. Ein Filter am Logger ueberlebt damit ein erneutes
    ``uvicorn.Config.configure_logging()``: etwa im ``--reload``-Betrieb.

    Zum Zeitpunkt des Imports von ``backend.main`` ist der Logger bereits
    eingerichtet: ``uvicorn backend.main:app`` konfiguriert das Protokollieren in
    ``Config.__init__``, also **bevor** ``Config.load()` die App importiert.
    """
    logger = logging.getLogger("uvicorn.access")
    if any(isinstance(f, GeheimnisFilter) for f in logger.filters):
        return
    logger.addFilter(GeheimnisFilter())
