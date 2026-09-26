#!/usr/bin/env bash
# Testlauf des Kalenders im Dienst-Image.
#
# WARUM DIESES SKRIPT: `python -m unittest discover -s tests` allein lief lange
# scheinbar gruen, hat aber neun Testmodule stillschweigend gar nicht geladen:
# starlettes TestClient braucht `httpx`, und das steckt bewusst nicht in den
# Produktions-Abhaengigkeiten. Ergebnis: 147 statt 192 Tests, und ausgerechnet die
# Mandanten-Isolationstests (test_multitenant) gehoerten zu den stummen.
#
# Loesung ohne neue Produktions-Dependency: httpx zur Laufzeit nach /tmp (tmpfs,
# im read_only-Container beschreibbar) installieren und per PYTHONPATH einbinden.
#
# Erwartung: mindestens "Ran 267 tests ... OK" (Stand 2026-09-06). Laeuft eine
# kleinere Zahl durch, fehlt eine
# Testabhaengigkeit, dann NICHT ignorieren.
#
# Gegenstueck: ./simulieren.sh prueft das Zusammenspiel ueber Wochen (Testkonto,
# erfundener Alltag, Sonden auf Tagestyp/Zeitzone/Mandanten/iCal/Planer).
set -euo pipefail

cd "$(dirname "$0")"

exec docker compose run --rm \
  -e DATABASE_URL=sqlite:////tmp/kalender-unittest.db \
  -e KALENDER_PASSWORD=test-password \
  -e SECRET_KEY=test-secret \
  -e PYTHONPATH=/tmp/testdeps \
  -v "$(pwd):/work" -w /work \
  --entrypoint sh kalender \
  -c 'pip install -q --target /tmp/testdeps httpx >/dev/null 2>&1 || {
        echo "WARNUNG: httpx-Installation fehlgeschlagen. TestClient-Module werden uebersprungen" >&2
      }
      python -m unittest discover -s tests "$@"' -- "$@"
