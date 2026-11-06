"""Startet den Dienst gegen eine Datenbank mit `zeit_ist` im Stand vom 2026-09-14.

Zweiter Alt-Schema-Test, und er ist eine eigene Datei aus einem ausdruecklichen
Grund: `tests/test_migration_alt.py` verbietet in seinem Kopf, sein `_ALT_SCHEMA`
zu erweitern. Jene Datei beschreibt absichtlich den Stand vor der Orte-Erweiterung
vom 2026-09-06. Wer sie fortschreibt, verliert genau den alten Stand, den sie
pruefen soll.

Die Falle, um die es geht, ist dieselbe und hat hier schon einmal einen CORE-
Dienst umgelegt: sobald ein ORM-Modell eine Spalte kennt, selektiert **jede** Query
sie mit, auch die in den Migrationsschritten selbst. Steht die anlegende Migration
in `on_startup()` weiter unten als die erste ORM-Query, stirbt der Start auf jeder
gewachsenen Datenbank mit „no such column", bevor sie an die Reihe kommt. Am
2026-09-06 gemessen gegen eine Kopie der Produktionsdatenbank. An diesem Dienst
haengt `/api/day-type/today` und damit das 05:35-Weckfenster.

Die uebrige Suite sieht das NICHT: dort legt `Base.metadata.create_all` jede
Spalte sofort an. Nur ein von Hand gebautes Alt-Schema trifft den Fall.

Der Start laeuft in einem EIGENEN Prozess, und das ist nicht Vorsicht, sondern
notwendig: `backend/database.py` baut `engine` beim Import aus
`settings.DATABASE_URL`, und unter `unittest discover` werden vorher alle
alphabetisch frueheren Testmodule importiert. Der Motor haengt dann an DEREN
frischer Datenbank, der Start wandert ueber die falsche Datei, und dieser Test
sieht in seiner unberuehrten Alt-Datei nach. Er meldete dann eine fehlende Spalte
und klaenge wie ein Migrationsfehler, obwohl die Migration nie ueber das
Alt-Schema gelaufen ist. Ausfuehrlich steht das in `test_migration_alt.py`.
"""

import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

DB = "/tmp/kalender-unittest-altschema-zeitmessung.db"

_WURZEL = Path(__file__).resolve().parents[1]

_START = """
import os
from backend.main import on_startup
from backend.session_notifications import stop_session_notification_runner
on_startup()
stop_session_notification_runner()
"""


def _start_gegen(db_pfad: str) -> subprocess.CompletedProcess:
    """`on_startup()` gegen genau diese Datenbank laufen lassen."""
    umgebung = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_pfad}",
        "KALENDER_PASSWORD": "test-password",
        "SECRET_KEY": "test-secret",
    }
    return subprocess.run(
        [sys.executable, "-c", _START],
        cwd=str(_WURZEL), env=umgebung,
        capture_output=True, text=True, timeout=180,
    )


# Stand vor der Zeitmessung vom 2026-10-25: `zeit_ist` existiert (seit dem
# 2026-09-14), aber ohne `taetigkeit`, `verweis`, `bestaetigt` und `genau`, und
# `habit_quellen` gibt es noch nicht.
#
# ⚠️ Diese Datei beschreibt einen alten Stand. Wer kuenftig Spalten ergaenzt,
# erweitert sie NICHT, sondern legt eine dritte Datei an - aus demselben Grund,
# aus dem es diese hier neben `test_migration_alt.py` gibt.
_ALT_SCHEMA = """
CREATE TABLE settings (key VARCHAR(100) PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE calendars (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(200) NOT NULL,
    color VARCHAR(7) NOT NULL DEFAULT '#3788d8', description TEXT,
    is_system BOOLEAN DEFAULT 0, owner_sub VARCHAR(128),
    created_at DATETIME, updated_at DATETIME);
CREATE TABLE habits (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(200) NOT NULL,
    color VARCHAR(7) NOT NULL DEFAULT '#4a9eff', owner_sub VARCHAR(128) NOT NULL DEFAULT '',
    project_id VARCHAR(36), target_hours_per_week FLOAT NOT NULL DEFAULT 6.0,
    session_duration_minutes INTEGER NOT NULL DEFAULT 90,
    weekday_start VARCHAR(5) NOT NULL DEFAULT '17:00',
    weekday_end VARCHAR(5) NOT NULL DEFAULT '20:00',
    weekend_start VARCHAR(5) NOT NULL DEFAULT '10:00',
    weekend_end VARCHAR(5) NOT NULL DEFAULT '18:00',
    active BOOLEAN DEFAULT 1, learning_mode BOOLEAN DEFAULT 0,
    focus_block_minutes INTEGER NOT NULL DEFAULT 25,
    break_minutes INTEGER NOT NULL DEFAULT 5,
    category VARCHAR(20) NOT NULL DEFAULT 'sonstige',
    weekday_target_ratio FLOAT NOT NULL DEFAULT 0.3,
    max_consecutive_days INTEGER NOT NULL DEFAULT 3,
    max_session_minutes INTEGER NOT NULL DEFAULT 120,
    can_be_overridden_by_goals BOOLEAN NOT NULL DEFAULT 1,
    can_be_split BOOLEAN NOT NULL DEFAULT 1,
    weekly_minimum_hours FLOAT, preferred_days TEXT,
    created_at DATETIME, updated_at DATETIME);
CREATE TABLE zeit_ist (
    id VARCHAR(36) PRIMARY KEY, owner_sub VARCHAR(128) NOT NULL DEFAULT '',
    date DATE NOT NULL, start DATETIME NOT NULL, "end" DATETIME NOT NULL,
    art VARCHAR(20) NOT NULL, titel VARCHAR(300), quelle VARCHAR(30) NOT NULL,
    extern_id VARCHAR(200) NOT NULL, roh_typ VARCHAR(100),
    puls_schnitt INTEGER, schritte INTEGER, kalorien INTEGER,
    created_at DATETIME, updated_at DATETIME);
"""


class TestStartAufAltemZeitSchema(unittest.TestCase):
    def setUp(self):
        Path(DB).unlink(missing_ok=True)
        for endung in ("-wal", "-shm"):
            Path(DB + endung).unlink(missing_ok=True)
        c = sqlite3.connect(DB)
        c.executescript(_ALT_SCHEMA)
        c.execute("INSERT INTO habits (id, name, owner_sub, category, created_at, "
                  "updated_at) VALUES ('alt-h1', 'Bestandsziel', 'wer-auch-immer', "
                  "'hobby', '2026-01-01', '2026-01-01')")
        c.execute("INSERT INTO zeit_ist (id, owner_sub, date, start, \"end\", art, "
                  "titel, quelle, extern_id, created_at, updated_at) VALUES "
                  "('alt-z1', 'wer-auch-immer', '2026-09-20', "
                  "'2026-09-20 18:00:00', '2026-09-20 18:40:00', 'training', "
                  "'Bestandsmessung', 'uhr', 'uhr-1', '2026-09-20', '2026-09-20')")
        c.commit()
        c.close()

    def test_startup_laeuft_und_ergaenzt_zeitmessung(self):
        lauf = _start_gegen(DB)
        self.assertEqual(
            lauf.returncode, 0,
            f"on_startup() auf altem zeit_ist-Schema abgebrochen:\n{lauf.stderr[-2000:]}")

        c = sqlite3.connect(DB)
        try:
            zeit_ist = {r[1] for r in c.execute("PRAGMA table_info(zeit_ist)")}
            for spalte in ("taetigkeit", "verweis", "bestaetigt", "genau"):
                self.assertIn(spalte, zeit_ist, f"zeit_ist.{spalte} fehlt nach dem Start")

            quellen = {r[1] for r in c.execute("PRAGMA table_info(habit_quellen)")}
            self.assertTrue(quellen, "Tabelle habit_quellen wurde nicht angelegt")
            for spalte in ("habit_id", "dienst", "kennung", "owner_sub"):
                self.assertIn(spalte, quellen)

            # Bestandsdaten unangetastet: eine Migration, die Zeilen verliert, ist
            # schlimmer als eine, die abbricht.
            zeile = c.execute(
                "SELECT titel, quelle, bestaetigt, genau FROM zeit_ist WHERE id = 'alt-z1'"
            ).fetchone()
            self.assertEqual(zeile[0], "Bestandsmessung")
            self.assertEqual(zeile[1], "uhr")
            # Bestandszeilen gelten als 'automatisch': von Hand eingetragene gab es
            # vor dieser Migration nicht, sie stammen per Definition von einem Melder.
            # Ein NULL hier waere schlimmer als ein falscher Stand, denn die Bilanz
            # filtert auf die Staende und saehe die Zeile dann ueberhaupt nicht.
            self.assertEqual(zeile[2], "automatisch")
            self.assertEqual(zeile[3], 0)
            self.assertEqual(
                c.execute("SELECT name FROM habits WHERE id = 'alt-h1'").fetchone()[0],
                "Bestandsziel")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
