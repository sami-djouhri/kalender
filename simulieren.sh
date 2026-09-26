#!/usr/bin/env bash
# Kalender-Simulation im Dienst-Image: Testkonto, erfundenes Quartal, Sonden.
#
# Gegenstueck zu run-tests.sh. Die Testsuite prueft Einzelteile, dieser Lauf
# prueft das Zusammenspiel ueber Wochen: Tagestyp, Serien ueber die Zeit-
# umstellung, Mandantentrennung, iCal, Planer.
#
# Die Datenbank liegt zwingend unter /tmp (tmpfs im read_only-Container); das
# Skript selbst bricht ab, wenn DATABASE_URL woanders hin zeigt. Produktions-
# daten werden nie angefasst.
#
# httpx wird zur Laufzeit nach /tmp installiert: gleiche Begruendung wie in
# run-tests.sh: starlettes TestClient braucht es, die Produktions-Deps sollen
# es nicht tragen.
set -euo pipefail

cd "$(dirname "$0")"

exec docker compose run --rm \
  -e DATABASE_URL=sqlite:////tmp/kalender-simulation.db \
  -e KALENDER_PASSWORD=sim-password \
  -e SECRET_KEY=sim-secret \
  -e CROSS_APP_ENABLED=0 \
  -e ASSISTANT_NUDGES_ENABLED=0 \
  -e PYTHONPATH=/tmp/testdeps:/work \
  -v "$(pwd):/work" -w /work \
  --entrypoint sh kalender \
  -c 'pip install -q --target /tmp/testdeps httpx >/dev/null 2>&1 || {
        echo "ABBRUCH: httpx liess sich nicht installieren, ohne TestClient keine Simulation" >&2
        exit 1
      }
      python scripts/simulieren.py "$@"' -- "$@"
