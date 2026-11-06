#!/usr/bin/env bash
# Haelt den geteilten Freitext-Datensatz in beiden Baeumen gleich.
#
# Die Faelle in `tests/daten/freitext-faelle.json` sind der Vertrag zwischen
# zwei Umsetzungen derselben Regeln: `backend/capture.py` hier und
# `feature-kalender/domain/Freitext.kt` in `saganta-apps`. Dort liegt eine
# byte-identische Kopie, weil jenes Repo ohne diesen Baum baubar bleiben muss
# (gleiches Muster wie `app/konto_mechanik.py`).
#
# Ohne diesen Abgleich ist der Vertrag genau so lange gueltig, wie niemand
# nur eine Seite aendert. Beide Testsuiten blieben dabei gruen, und die
# Abweichung fiele erst auf, wenn derselbe Satz am Handy etwas anderes ergibt
# als im Web.
#
# Nutzung:
#   bash scripts/freitext-datensatz-abgleichen.sh              # nur pruefen
#   bash scripts/freitext-datensatz-abgleichen.sh --uebernehmen # Kopie nachziehen
set -euo pipefail

hier="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
quelle="$hier/tests/daten/freitext-faelle.json"
kopie="${SAGANTA_APPS:-$HOME/docker/saganta-apps}/feature-kalender/src/jvmCommonTest/resources/freitext-faelle.json"

# Fehlt eine Seite, ist das ein Fehler und kein "nichts zu tun": ein Abgleich,
# der ohne Gegenstueck gruen meldet, ist schlimmer als keiner.
if [[ ! -f "$quelle" ]]; then
    echo "FEHLER: Vorlage fehlt: $quelle" >&2
    exit 1
fi
if [[ ! -f "$kopie" ]]; then
    echo "FEHLER: Kopie fehlt: $kopie" >&2
    echo "  Liegt der saganta-apps-Baum anderswo? Dann SAGANTA_APPS=<pfad> setzen." >&2
    exit 1
fi

h_quelle="$(sha256sum "$quelle" | cut -d' ' -f1)"
h_kopie="$(sha256sum "$kopie" | cut -d' ' -f1)"

if [[ "$h_quelle" == "$h_kopie" ]]; then
    anzahl="$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))['faelle']))" "$quelle")"
    echo "ok: beide Seiten gleich ($anzahl Faelle, ${h_quelle:0:16})"
    exit 0
fi

# Den Unterschied festhalten und notfalls kopieren, BEVOR etwas ausgegeben
# wird. Stand das `cp` hinter der Ausgabe, brach ein Aufruf in einer Pipe
# (`... | head -3`) das Skript per SIGPIPE ab, bevor es etwas tat, und gab
# dabei die halbe Erfolgsmeldung aus. Selbst gemessen.
unterschied="$(diff -u "$kopie" "$quelle" | head -40 || true)"
uebernommen=0
if [[ "${1:-}" == "--uebernehmen" ]]; then
    cp "$quelle" "$kopie"
    uebernommen=1
fi

echo "ABWEICHUNG zwischen Vorlage und Kopie:"
echo "  Vorlage ${h_quelle:0:16}  $quelle"
echo "  Kopie   ${h_kopie:0:16}  $kopie"
echo
echo "$unterschied"
echo

if [[ "$uebernommen" -eq 1 ]]; then
    echo "Kopie nachgezogen. Jetzt BEIDE Testsuiten laufen lassen:"
    echo "  cd $hier && ./run-tests.sh"
    echo "  cd ${SAGANTA_APPS:-\$HOME/docker/saganta-apps} && bash build-remote.sh \\"
    echo "    \":feature-kalender:desktopTest --rerun-tasks --tests 'de.saganta.kalender.domain.FreitextTest'\""
    exit 0
fi

echo "Nichts geaendert. Zum Nachziehen der Kopie: --uebernehmen"
echo "⚠️ Die Vorlage ist kanonisch. Wurde die Kopie absichtlich geaendert,"
echo "   gehoert die Aenderung zuerst in die Vorlage."
exit 1
