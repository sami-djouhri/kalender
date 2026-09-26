"""Den Mandanten fuer headerlose Aufrufe aus den vorhandenen Daten ableiten.

WOZU
``DEFAULT_OWNER_SUB`` traegt die Kennung eines konkreten Menschen. Seit dem
2026-09-06 steht sie nicht mehr im Quelltext, sondern in der Konfiguration, weil
dieses Repo veroeffentlicht werden soll. Damit entstand ein neues Problem: wer
den Dienst baut, bevor die Konfiguration den Wert traegt, bekommt einen Dienst,
der headerlosen Aufrufern leere Antworten gibt.

★ HIER WIEGT DAS SCHWERER ALS BEI LAGER, MEALPREP UND FITNESS. Am headerlosen
Pfad haengt die CORE-Kette: Home Assistant fragt ``/api/day-type/today`` ab, und
am Tagestyp haengt das 05:35-Weckfenster. Ein leerer Mandant liefert dort keinen
Fehler, sondern einen Tagestyp ohne Datengrundlage. Leer sieht aus wie "frei".

DIE ABLEITUNG
Traegt die Datenbank genau **einen** Mandanten, dann ist er es. Das ist der Fall
einer bestehenden Ein-Personen-Installation, und genau dort waere die Luecke
schaedlich. Bei mehreren Mandanten wird **nicht geraten**: dann ist "headerlos"
schlicht nicht mehr eindeutig, und die richtige Antwort ist, nichts zu liefern
und es zu sagen. Bei leerer Datenbank gibt es nichts abzuleiten, das ist eine
frische Installation.

★ GEFRAGT WIRD UEBER ALLE MANDANTEN-MODELLE, nicht ueber ein ausgesuchtes.
``backend/secretary.tenants_with_data`` gibt es schon, fragt aber nur Todo,
DailyGoal und Habit ab, weil es fuer die Tagesplanung geschrieben ist. Wer
Termine und Kontakte pflegt, aber keine Gewohnheiten, kaeme dort auf null. Ein
einzelnes Modell zu waehlen heisst, die Ableitung an einer Eigenheit
aufzuhaengen, die man beim Lesen des Codes nicht sieht.

★★ ZWEI SORTEN VON ZEILEN GEHOEREN KEINEM MANDANTEN, und beide kommen in diesem
Dienst wirklich vor. Sie zu uebersehen heisst nicht, ein bisschen daneben zu
liegen, sondern die Ableitung genau dann zu verlieren, wenn sie gebraucht wird:

* ``owner_sub IS NULL`` ist Absicht und bedeutet "fuer alle": die sieben
  System-Kalender (jeder Mandant braucht die Huellen) und die Feiertags-Events.
  Am 2026-09-06 waren das hier 7 von 7 Kalendern und 22 von 159 Events.
* ``SYSTEM_RUN_SUB`` ist ein erfundener sub fuer den systemweiten
  Retention-Lauf, weil ``SecretaryRun.owner_sub`` NOT NULL ist. Gemessen am
  selben Tag: 58 solche Zeilen neben 318 echten. Ohne diese Ausnahme haette die
  Ableitung **zwei** Mandanten gezaehlt, sich verweigert, und der Wecker haette
  seinen Tagestyp aus einer leeren Sicht bezogen.

WAS SIE NICHT IST
Kein Ersatz fuer die Konfiguration. Sie schreibt nichts fest, laeuft bei jedem
Start neu und protokolliert, was sie getan hat. Sobald der Wert konfiguriert
ist, greift sie gar nicht mehr.
"""

import logging

from sqlalchemy import select

from backend.database import SessionLocal
from backend.tenant import SYSTEM_RUN_SUB
from backend.tenant import TENANT_MODELS as MODELLE

logger = logging.getLogger(__name__)

# Subs, die keinem Menschen gehoeren. NULL faellt schon ueber die Wahrheitspruefung
# heraus, dies hier sind die erfundenen.
KEINE_MANDANTEN = frozenset({SYSTEM_RUN_SUB})


def einzigen_mandanten_ableiten() -> str | None:
    """Genau ein Mandant ueber alle Mandanten-Tabellen? Dann seine Kennung."""
    gefunden: set[str] = set()
    try:
        with SessionLocal() as db:
            for modell in MODELLE:
                # skip_tenant: die Abfrage soll ueber ALLE Mandanten sehen. Ohne
                # das filtert das globale Scoping sie auf den aktuellen (leeren)
                # sub und liefert immer null Zeilen. Das waere eine Ableitung,
                # die sich selbst blind macht.
                subs = db.execute(
                    select(modell.owner_sub).distinct().execution_options(skip_tenant=True)
                ).scalars().all()
                gefunden.update(s for s in subs if s and s not in KEINE_MANDANTEN)
                if len(gefunden) > 1:
                    break
    except Exception as exc:  # DB noch nicht migriert, Spalte fehlt, o.ae.
        logger.warning("Mandant nicht ableitbar (%s)", exc)
        return None

    if len(gefunden) == 1:
        return next(iter(gefunden))
    if len(gefunden) > 1:
        logger.error(
            "DEFAULT_OWNER_SUB ist leer und die Daten tragen mehrere Mandanten. "
            "Es wird nicht geraten: headerlose Aufrufe sehen nichts, und daran "
            "haengt der Tagestyp fuer Home Assistant. "
            "Wert setzen mit saganta/scripts/owner-kennung-eintragen.sh"
        )
    return None
