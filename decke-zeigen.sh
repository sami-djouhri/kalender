#!/usr/bin/env bash
# Zeigt eine Tagesdecke im Klartext (siehe scripts/decke-zeigen.py).
#
# Gegenstueck zu run-tests.sh: die Tests pruefen Zusagen, dies zeigt das
# Ergebnis. Eine Decke kann jede Invariante erfuellen und trotzdem unbrauchbar
# sein; das sieht man erst, wenn man den Tag einmal untereinander liest.
#
# Die Datenbank liegt bewusst unter /tmp im Container. Produktionsdaten werden
# nie angefasst, das Python-Skript bricht sonst selbst ab.
set -euo pipefail

cd "$(dirname "$0")"

# ★ PYTHONPATH ist noetig: bei `python scripts/datei.py` legt Python das
# SKRIPT-Verzeichnis in den Suchpfad, nicht das Arbeitsverzeichnis. `backend`
# liegt daneben und waere sonst unauffindbar ("No module named 'backend'"),
# obwohl -w bereits stimmt.
exec docker compose run --rm \
  -e DATABASE_URL=sqlite:////tmp/decke-schau.db \
  -e KALENDER_PASSWORD=schau \
  -e SECRET_KEY=schau \
  -e DEFAULT_OWNER_SUB=schau-owner \
  -e PYTHONPATH=/work \
  -v "$(pwd)":/work -w /work \
  --entrypoint python kalender scripts/decke-zeigen.py
