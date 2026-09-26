"""Startet den Dienst gegen eine Datenbank im ALTEN Schema.

Warum es diesen Test gibt: die uebrige Suite laeuft auf einer frischen Datenbank,
und dort legt `Base.metadata.create_all` jede Spalte sofort an. Genau deshalb
sieht sie die haeufigste Migrationsfalle dieses Projekts NICHT.

★★ Die Falle: sobald ein ORM-Modell eine neue Spalte kennt, selektiert **jede**
Query sie mit, auch die Queries in den Migrationsschritten selbst. Steht die
anlegende Migration weiter unten in `on_startup()` als die erste ORM-Query,
stirbt der Start auf jeder gewachsenen Datenbank mit „no such column", bevor sie
an die Reihe kommt. Am 2026-09-06 gemessen gegen eine Kopie der Produktions-
datenbank: die Orte-Migration lag hinter `_migrate_activity_feedback()`, und der
Dienst kam nicht mehr hoch. Bei einem CORE-Dienst, an dem der Tagestyp und damit
das Weckfenster haengt, ist das kein Schoenheitsfehler.

Der Test baut deshalb ein Alt-Schema von Hand auf (nur die Spalten, die es vor
der jeweiligen Erweiterung gab), legt Daten hinein und laesst den echten
`on_startup()` darueberlaufen. Er prueft beides: dass der Start durchlaeuft und
dass die Bestandsdaten unangetastet bleiben.

Wer kuenftig Spalten ergaenzt, erweitert `_ALT_SCHEMA` NICHT: die Datei
beschreibt absichtlich einen alten Stand.
"""

import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

DB = "/tmp/kalender-unittest-altschema.db"

# ★★ Der Start laeuft in einem EIGENEN Prozess, und das ist der Kern dieser Datei.
#
# `backend/database.py` baut `engine` beim Import aus `settings.DATABASE_URL`. Ein
# `os.environ[...]` im Modulkopf wirkt also nur, wenn dieses Modul als erstes
# importiert wird. Unter `unittest discover` (dem kanonischen Weg, siehe
# run-tests.sh) werden vorher alle alphabetisch frueheren Testmodule importiert,
# `test_account` als erstes. Der Motor hing damit an DEREN Datenbank.
#
# Die Folge war kein Fehler, sondern etwas Schlimmeres: `on_startup()` wanderte
# ueber die fremde, frische Datenbank, wo `create_all` ohnehin jede Spalte anlegt,
# und dieser Test sah danach in seiner unberuehrten Alt-Datei nach. Er meldete
# „events.place_id fehlt" und klang wie ein Migrationsfehler, waehrend die
# Migration in Wahrheit nie ueber das Alt-Schema gelaufen war. Allein gestartet
# war er gruen, in der Suite rot, und beides aus demselben Grund.
#
# Ein Unterprozess bekommt sein `DATABASE_URL` vor jedem Import und ist gegen die
# Importreihenfolge dauerhaft unempfindlich. Er hat einen zweiten Vorzug: bricht
# der Start ab, kommt der echte Traceback als Text zurueck, statt diesen Testlauf
# mitzureissen.
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

# Stand vor der Orte-Erweiterung vom 2026-09-06.
_ALT_SCHEMA = """
CREATE TABLE settings (key VARCHAR(100) PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE calendars (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(200) NOT NULL,
    color VARCHAR(7) NOT NULL DEFAULT '#3788d8', description TEXT,
    is_system BOOLEAN DEFAULT 0, owner_sub VARCHAR(128),
    created_at DATETIME, updated_at DATETIME);
CREATE TABLE events (
    id VARCHAR(36) PRIMARY KEY, calendar_id VARCHAR(36) NOT NULL,
    owner_sub VARCHAR(128), title VARCHAR(500) NOT NULL, description TEXT,
    location VARCHAR(500), start DATETIME NOT NULL, "end" DATETIME NOT NULL,
    all_day BOOLEAN DEFAULT 0, recurrence_rule VARCHAR(500),
    recurrence_exdates TEXT, reminder_minutes INTEGER, activity_type VARCHAR(20),
    created_at DATETIME, updated_at DATETIME);
CREATE TABLE contacts (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(200) NOT NULL, birthday DATE,
    owner_sub VARCHAR(128) NOT NULL DEFAULT '', email VARCHAR(300),
    phone VARCHAR(50), notes TEXT, created_at DATETIME, updated_at DATETIME);
"""


class TestStartAufAltemSchema(unittest.TestCase):
    def setUp(self):
        Path(DB).unlink(missing_ok=True)
        for endung in ("-wal", "-shm"):
            Path(DB + endung).unlink(missing_ok=True)
        c = sqlite3.connect(DB)
        c.executescript(_ALT_SCHEMA)
        c.execute("INSERT INTO calendars (id, name, is_system, owner_sub, "
                  "created_at, updated_at) VALUES "
                  "('system-termine-0000-0000-000000000000', 'Termine', 1, NULL, "
                  "'2026-01-01', '2026-01-01')")
        c.execute("INSERT INTO events (id, calendar_id, owner_sub, title, start, "
                  "\"end\", all_day, created_at, updated_at) VALUES "
                  "('alt-1', 'system-termine-0000-0000-000000000000', 'wer-auch-immer', "
                  "'Bestandstermin', '2026-05-01 09:00:00', '2026-05-01 10:00:00', 0, "
                  "'2026-01-01', '2026-01-01')")
        c.execute("INSERT INTO contacts (id, name, owner_sub, created_at, updated_at) "
                  "VALUES ('alt-k1', 'Bestandskontakt', 'wer-auch-immer', "
                  "'2026-01-01', '2026-01-01')")
        c.commit()
        c.close()

    def test_startup_laeuft_und_ergaenzt_die_spalten(self):
        # Der eigentliche Prueffall: hier flog frueher „no such column: events.place_id".
        lauf = _start_gegen(DB)
        self.assertEqual(
            lauf.returncode, 0,
            f"on_startup() auf altem Schema abgebrochen:\n{lauf.stderr[-2000:]}")

        c = sqlite3.connect(DB)
        events = {r[1] for r in c.execute("PRAGMA table_info(events)")}
        contacts = {r[1] for r in c.execute("PRAGMA table_info(contacts)")}
        places = {r[1] for r in c.execute("PRAGMA table_info(places)")}

        for spalte in ("place_id", "lat", "lon", "travel_for_event_id", "travel_mode"):
            self.assertIn(spalte, events, f"events.{spalte} fehlt nach dem Start")
        for spalte in ("address", "lat", "lon"):
            self.assertIn(spalte, contacts, f"contacts.{spalte} fehlt nach dem Start")
        self.assertTrue(places, "Tabelle places wurde nicht angelegt")

        # Bestandsdaten unangetastet: eine Migration, die Zeilen verliert, waere
        # schlimmer als eine, die abbricht.
        self.assertEqual(
            c.execute("SELECT title FROM events WHERE id = 'alt-1'").fetchone()[0],
            "Bestandstermin")
        self.assertEqual(
            c.execute("SELECT name FROM contacts WHERE id = 'alt-k1'").fetchone()[0],
            "Bestandskontakt")
        # Der Wege-Kalender entsteht beim Start und ist ein System-Kalender.
        self.assertEqual(
            c.execute("SELECT is_system FROM calendars WHERE id LIKE 'system-wege%'")
            .fetchone()[0], 1)
        c.close()


if __name__ == "__main__":
    unittest.main()
