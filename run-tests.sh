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
# ★ Die Untergrenze wird hier DURCHGESETZT, nicht nur beschrieben. Vorher stand
# eine Erwartungszahl als Kommentar, und zwar an vier Stellen mit drei
# verschiedenen Werten (253, 267, und ein "253+" in zwei READMEs), waehrend
# tatsaechlich 371 liefen. Ein Kommentar kann nicht veralten, ohne dass es
# jemandem auffaellt, weil ihn nichts liest. Diese Zahl liest das Skript.
#
# Steigt die Testzahl dauerhaft, wird MINDEST_TESTS hier hochgesetzt. Sonst
# nirgends: die READMEs nennen bewusst keine Zahl mehr.
set -euo pipefail

cd "$(dirname "$0")"

MINDEST_TESTS=371

lauf(){
  docker compose run --rm \
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
}

# Ein gezielter Teillauf (`./run-tests.sh tests.test_daytype`) laeuft per
# Definition mit weniger Tests. Die Schranke gilt nur fuer den vollen Lauf,
# sonst meldete jeder Einzeltest einen Fehlalarm.
if [ "$#" -gt 0 ]; then
  exec_rc=0
  lauf "$@" || exec_rc=$?
  exit "$exec_rc"
fi

protokoll="$(mktemp)"
trap 'rm -f "$protokoll"' EXIT

# unittest schreibt "Ran N tests" nach stderr, deshalb 2>&1 vor dem tee.
rc=0
lauf 2>&1 | tee "$protokoll" || true
rc="${PIPESTATUS[0]}"

gelaufen="$(sed -n 's/^Ran \([0-9]\{1,\}\) tests\{0,1\}.*/\1/p' "$protokoll" | tail -1)"

if [ -z "$gelaufen" ]; then
  echo "FEHLER: keine Zeile 'Ran N tests' gefunden. Der Lauf ist nicht ausgewertet." >&2
  exit 1
fi

if [ "$gelaufen" -lt "$MINDEST_TESTS" ]; then
  echo >&2
  echo "FEHLER: nur $gelaufen Tests gelaufen, erwartet mindestens $MINDEST_TESTS." >&2
  echo "        Das ist KEIN gruener Lauf. Wahrscheinlichste Ursache ist eine fehlende" >&2
  echo "        Testabhaengigkeit: dann laedt unittest ganze Module stillschweigend nicht" >&2
  echo "        (genau so fehlten hier einmal die Mandanten-Isolationstests)." >&2
  exit 1
fi

exit "$rc"
