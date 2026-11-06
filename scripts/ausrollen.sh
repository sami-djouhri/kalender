#!/usr/bin/env bash
# Rollt den Kalender (CORE, Weckkette) nach dem Verfahren aus, das die Projektregeln
# verlangt, und nimmt auf Wunsch den kalender-bff mit.
#
#   bash scripts/ausrollen.sh <kurzname> [--mit-bff]
#
# Ablauf:
#   1. Tagestyp heute/morgen VORHER, ueber HTTP im Container (das Feed-Token
#      verlaesst den Container nicht und wird nicht ausgegeben)
#   2. benannte DB-Kopie mit sqlite3.backup() (WAL-Modus, `cp` waere unvollstaendig)
#   3. laufendes Image als kalender-rollback:vor-<kurzname> markieren
#   4. bauen, starten, auf healthy warten
#   5. Tagestyp NACHHER; weicht er ab, Rueckbau auf das markierte Image
#
# Warum ein Skript statt einer Anleitung: der Vergleich in Schritt 1 und 5 braucht
# das Feed-Token, und eine Anleitung, die man von Hand abtippt, laesst genau diesen
# Schritt aus, wenn es spaet ist. Entstanden am 2026-11-01, als ein Deploy nachts
# daran hing.
#
# ⚠️ Rueckbau nimmt nur das Image zurueck. Hat der neue Stand beim Start die
# Datenbank migriert, liegt die Kopie aus Schritt 2 daneben; sie wird NICHT von
# selbst zurueckgespielt, das entscheidet ein Mensch.
set -euo pipefail

cd "$(dirname "$0")/.."
KURZ="${1:?kurzname fehlt, z. B. handzeit}"
MIT_BFF="${2:-}"
STEMPEL="$(date +%Y%m%d-%H%M)"

tagestyp() {
  docker exec -i -w /app kalender python3 - <<'PY'
import json, urllib.request
from backend.database import SessionLocal
from backend.models import Setting
db = SessionLocal()
tok = db.query(Setting).filter(Setting.key == "feed_token").first().value
db.close()
for pfad in ("today", "tomorrow"):
    with urllib.request.urlopen(f"http://127.0.0.1:8085/api/day-type/{pfad}?token={tok}", timeout=15) as r:
        d = json.load(r)
    print(pfad, d.get("date"), d.get("type"), d.get("start"), d.get("end"))
PY
}

warte_gesund() {
  local name="$1"
  for _ in $(seq 1 60); do
    zustand="$(docker inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null || echo fehlt)"
    [ "$zustand" = healthy ] && return 0
    sleep 5
  done
  echo "FEHLER: $name nach 5 min nicht healthy (zuletzt: $zustand)" >&2
  return 1
}

echo "== 1. Tagestyp vorher"
vorher="$(tagestyp)"
echo "$vorher"

echo "== 2. DB-Kopie"
docker exec -i -w /app kalender python3 - "$KURZ" "$STEMPEL" <<'PY'
import sqlite3, sys
quelle = sqlite3.connect("data/kalender.db")
ziel_name = f"data/kalender.db.bak-vor-{sys.argv[1]}-{sys.argv[2]}"
ziel = sqlite3.connect(ziel_name)
quelle.backup(ziel)
ziel.close(); quelle.close()
print("  ", ziel_name)
PY

echo "== 3. Rueckfall-Image"
# Das Image, das der Container FAEHRT, nicht `:latest`. Am 2026-11-01 baute
# eine parallele Sitzung den Kalender in derselben Minute neu; `:latest` war damit
# schon der neue Stand, und der Rueckweg zeigte auf genau das, wovon er wegfuehren
# sollte. Bemerkt erst danach, an gleicher Image-ID von Rueckfall und Betrieb.
docker tag "$(docker inspect --format '{{.Image}}' kalender)" "kalender-rollback:vor-$KURZ"
echo "   kalender-rollback:vor-$KURZ"

echo "== 4. Bauen und starten"
docker compose up -d --build kalender
warte_gesund kalender

echo "== 5. Tagestyp nachher"
nachher="$(tagestyp)"
echo "$nachher"
if [ "$vorher" != "$nachher" ]; then
  echo "!! Tagestyp weicht ab. Rueckbau auf kalender-rollback:vor-$KURZ" >&2
  docker tag "kalender-rollback:vor-$KURZ" kalender-kalender:latest
  docker compose up -d --no-build kalender
  warte_gesund kalender
  echo "!! Zurueckgebaut. Bitte ansehen, bevor erneut ausgerollt wird." >&2
  exit 1
fi
echo "   gleich, in Ordnung"

if [ "$MIT_BFF" = "--mit-bff" ]; then
  echo "== 6. kalender-bff"
  ( cd ../saganta/infra && docker compose -f docker-compose.yml -f docker-compose.override.lan.yml up -d --build kalender-bff )
  warte_gesund saganta-kalender-bff
fi

echo "== Fertig: Kalender $(git rev-parse --short HEAD) laeuft"
