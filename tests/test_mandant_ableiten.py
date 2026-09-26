"""Ableitung des Mandanten fuer headerlose Aufrufe (backend/mandant_ableiten.py).

Warum das hier besonders scharf geprueft wird: am headerlosen Pfad haengt
/api/day-type/today und damit das 05:35-Weckfenster. Eine Ableitung, die sich
verweigert, liefert keinen Fehler, sondern einen Tagestyp ohne Datengrundlage.
"""
import os
import unittest
from datetime import date, datetime

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-ableiten.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, SessionLocal, engine
from backend.mandant_ableiten import einzigen_mandanten_ableiten
from backend.models import Calendar, Event, Habit, SecretaryRun
from backend.system_calendars import FEIERTAG_CALENDAR_ID, TERMINE_CAL_ID
from backend.tenant import SYSTEM_RUN_SUB

SUB_A = "tenant-a-sub"
SUB_B = "tenant-b-sub"


def _system_session():
    """Ungescopte Sitzung: owner_sub explizit None, damit nichts gestempelt wird."""
    db = SessionLocal()
    db.info["owner_sub"] = None
    return db


class MandantAbleitenTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def _anlegen(self, objekte):
        db = _system_session()
        try:
            for o in objekte:
                db.add(o)
            db.commit()
        finally:
            db.close()

    def test_leere_datenbank_leitet_nichts_ab(self):
        """Frische Installation: es gibt nichts abzuleiten, und geraten wird nicht."""
        self.assertIsNone(einzigen_mandanten_ableiten())

    def test_genau_ein_mandant(self):
        self._anlegen([Habit(name="Laufen", owner_sub=SUB_A)])
        self.assertEqual(einzigen_mandanten_ableiten(), SUB_A)

    def test_zwei_mandanten_werden_nicht_geraten(self):
        self._anlegen([
            Habit(name="Laufen", owner_sub=SUB_A),
            Habit(name="Lesen", owner_sub=SUB_B),
        ])
        self.assertIsNone(einzigen_mandanten_ableiten())

    def test_ueber_alle_modelle_nicht_nur_ein_ausgesuchtes(self):
        """Wer Kontakte und Termine pflegt, aber keine Gewohnheiten, wird gefunden.

        backend.secretary.tenants_with_data fragt nur Todo/DailyGoal/Habit ab,
        weil es fuer die Tagesplanung geschrieben ist. Wer es hier
        wiederverwendet, bekommt bei so einem Bestand None.
        """
        self._anlegen([
            # Die Huelle ist geteilt (owner_sub NULL), der Termin darin gehoert
            # SUB_A. Genau diese Kombination soll gefunden werden.
            Calendar(id=TERMINE_CAL_ID, name="Termine", owner_sub=None),
            # Zonenlose Berliner Wanduhrzeit, siehe backend/wanduhr.py.
            Event(
                calendar_id=TERMINE_CAL_ID,
                title="Zahnarzt",
                start=datetime(2026, 9, 10, 10, 0),
                end=datetime(2026, 9, 10, 11, 0),
                owner_sub=SUB_A,
            ),
        ])
        self.assertEqual(einzigen_mandanten_ableiten(), SUB_A)

    def test_globale_null_zeilen_zaehlen_nicht_als_mandant(self):
        """System-Kalender und Feiertags-Events tragen owner_sub NULL, mit Absicht.

        Ohne die Wahrheitspruefung waere None ein 'Mandant' und die Ableitung
        haette bei einer Ein-Personen-Installation zwei gezaehlt.
        """
        self._anlegen([
            Calendar(id=FEIERTAG_CALENDAR_ID, name="Feiertage", owner_sub=None),
            Event(
                calendar_id=FEIERTAG_CALENDAR_ID,
                title="Tag der Deutschen Einheit",
                start=datetime(2026, 10, 3, 0, 0),
                end=datetime(2026, 10, 3, 23, 59),
                owner_sub=None,
            ),
            Habit(name="Laufen", owner_sub=SUB_A),
        ])
        self.assertEqual(einzigen_mandanten_ableiten(), SUB_A)

    def test_nur_globale_zeilen_ergeben_keinen_mandanten(self):
        self._anlegen([
            Calendar(id=FEIERTAG_CALENDAR_ID, name="Feiertage", owner_sub=None),
        ])
        self.assertIsNone(einzigen_mandanten_ableiten())

    # --- Der eigentliche Grund fuer dieses Modul ---

    def test_system_marker_zaehlt_nicht_als_zweiter_mandant(self):
        """★★ Der Regressionstest.

        SecretaryRun traegt fuer den systemweiten Retention-Lauf einen
        erfundenen sub, weil die Spalte NOT NULL ist. Am 2026-09-06 lagen in
        der Live-Datenbank 58 solche Zeilen neben 318 echten. Wer das Muster
        aus lager/mealprep/fitness unveraendert uebernimmt, zaehlt hier zwei
        Mandanten, leitet nichts ab, und der Wecker bezieht seinen Tagestyp aus
        einer leeren Sicht.
        """
        self._anlegen([
            Habit(name="Laufen", owner_sub=SUB_A),
            SecretaryRun(owner_sub=SYSTEM_RUN_SUB, run_date=date(2026, 9, 6), kind="purge"),
            SecretaryRun(owner_sub=SUB_A, run_date=date(2026, 9, 6), kind="briefing"),
        ])
        self.assertEqual(einzigen_mandanten_ableiten(), SUB_A)

    def test_nur_system_marker_ergibt_keinen_mandanten(self):
        """Ein Bestand, der ausschliesslich aus Systemlaeufen besteht, hat keinen Owner."""
        self._anlegen([
            SecretaryRun(owner_sub=SYSTEM_RUN_SUB, run_date=date(2026, 9, 6), kind="purge"),
        ])
        self.assertIsNone(einzigen_mandanten_ableiten())

    def test_marker_in_secretary_stimmt_mit_der_konstante_ueberein(self):
        """Haelt beide Seiten aneinander fest.

        backend/secretary.py wiederholt den Platzhalter derzeit als Literal.
        Wird er dort umbenannt und hier nicht, zaehlt die Ableitung wieder
        einen Mandanten zu viel, und zwar lautlos: alle anderen Tests hier
        laufen weiter gruen, weil sie die Konstante benutzen.
        """
        import pathlib
        import re

        quelle = (
            pathlib.Path(__file__).resolve().parent.parent / "backend" / "secretary.py"
        ).read_text()
        # Beide Aufrufe, die den Platzhalter als Marker setzen bzw. lesen.
        marker = re.findall(r'_(?:already|mark)_ran\(\s*\w+\s*,\s*"([^"]+)"', quelle)
        self.assertTrue(marker, "Keine Marker-Aufrufe in secretary.py gefunden")
        for m in marker:
            self.assertEqual(
                m,
                SYSTEM_RUN_SUB,
                f"secretary.py setzt den Systemlauf-Marker auf '{m}', "
                f"backend/tenant.SYSTEM_RUN_SUB ist '{SYSTEM_RUN_SUB}'. "
                "Die Ableitung wuerde ihn als Mandanten zaehlen.",
            )


if __name__ == "__main__":
    unittest.main()
