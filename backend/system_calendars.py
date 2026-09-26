"""IDs der System-Kalender: importfrei und ohne Seiteneffekte.

Diese Konstanten lagen bisher verstreut in `routers/daytype.py`, `birthday_utils.py`,
`main.py` und (dupliziert) `routers/postfach.py`. `tenant.py` braucht die
Feiertags-ID fuer das Event-Scoping; ein Import aus `routers.daytype` wuerde von
dort aus den kompletten Router-Stack (FastAPI -> database -> tenant) nachziehen und
einen Zyklus erzeugen. Deshalb liegen sie hier, in einem Modul ohne eigene Importe.

Die alten Fundstellen re-exportieren die Namen weiter, damit weder Tests noch
Konsumenten angefasst werden muessen.

Der Kalender fuehrt bewusst EINEN globalen Satz System-Kalender (`owner_sub IS NULL`),
den alle Mandanten sehen, die *Inhalte* sind pro Mandant gescoped. Einzige Ausnahme
sind Feiertage: die sind fuer alle gleich und bleiben deshalb global.
"""

DAYTYPE_CALENDARS = {
    "arbeit": "daytype-arbeit-0000-0000-000000000000",
    "schule": "daytype-schule-0000-0000-000000000000",
    "urlaub": "daytype-urlaub-0000-0000-000000000000",
    "krank": "daytype-krank-0000-0000-000000000000",
}

FEIERTAG_CALENDAR_ID = "daytype-feiertag-0000-0000-000000000000"
GEBURTSTAGE_CALENDAR_ID = "system-geburtstage-0000-0000-000000000000"
TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"
# Wege liegen in einem eigenen Kalender, damit man sie mit einem Griff ausblenden
# kann. Sie sind generiert wie Geburtstage: von Hand angelegte Wege wuerde der
# naechste Lauf ueberschreiben, deshalb steht der Kalender in GENERATED.
WEGE_CALENDAR_ID = "system-wege-0000-0000-000000000000"

DAYTYPE_IDS = set(DAYTYPE_CALENDARS.values()) | {FEIERTAG_CALENDAR_ID, GEBURTSTAGE_CALENDAR_ID}

# Kalender, die der Dienst selbst erzeugt und pflegt. In sie darf kein Mandant
# direkt schreiben (Feiertage/Geburtstage sind generiert).
GENERATED_CALENDAR_IDS = {FEIERTAG_CALENDAR_ID, GEBURTSTAGE_CALENDAR_ID, WEGE_CALENDAR_ID}

# Vollstaendiger Satz der System-Kalender.
SYSTEM_CALENDAR_IDS = set(DAYTYPE_IDS) | {TERMINE_CAL_ID, WEGE_CALENDAR_ID}
