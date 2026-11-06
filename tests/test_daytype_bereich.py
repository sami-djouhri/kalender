"""Tagestyp ueber einen Zeitraum setzen (`POST /api/day-type/set-range`).

Der Anlass ist eine Frage. Bis hierher: ja, einzeln.
Drei Wochen Urlaub waren einundzwanzig Aufrufe.

Geprueft wird vor allem das, was einen Zeitraum von einer Schleife ueber
`/set` unterscheidet: kein Umschalten, keine Feiertage, einmal neu planen je
Woche statt je Tag.
"""

import os
import unittest
from datetime import date, datetime

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-daytype-bereich.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, engine, get_db
from backend.models import Calendar, Event
from backend.routers.daytype import (
    DAYTYPE_CALENDARS,
    FEIERTAG_CALENDAR_ID,
    _get_daytype,
    _get_daytype_display,
    get_urlaub_bilanz,
    set_day_type,
    set_day_type_range,
)


class TagestypBereichTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = next(get_db())
        for cal_id in list(DAYTYPE_CALENDARS.values()) + [FEIERTAG_CALENDAR_ID]:
            self.db.add(Calendar(id=cal_id, name=cal_id[:8], is_system=True))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _bereich(self, von, bis, typ, **kwargs):
        return set_day_type_range(
            von=von, bis=bis, dtype=typ,
            half=kwargs.get("half", False),
            nur_wochentage=kwargs.get("nur_wochentage", True),
            db=self.db,
        )

    def test_zwei_wochen_urlaub_in_einem_aufruf(self):
        # Mo 2027-03-01 bis Fr 2027-03-12, zwei volle Arbeitswochen.
        ergebnis = self._bereich("2027-03-01", "2027-03-12", "urlaub")
        self.assertEqual(len(ergebnis["gesetzt"]), 10)
        self.assertEqual(_get_daytype(self.db, date(2027, 3, 1)), "urlaub")
        self.assertEqual(_get_daytype(self.db, date(2027, 3, 12)), "urlaub")

    def test_wochenenden_bleiben_aussen_vor(self):
        ergebnis = self._bereich("2027-03-01", "2027-03-07", "urlaub")
        self.assertEqual(len(ergebnis["gesetzt"]), 5)
        gruende = {e["grund"] for e in ergebnis["uebersprungen"]}
        self.assertEqual(gruende, {"wochenende"})
        # Samstag bleibt, was er war.
        self.assertEqual(_get_daytype_display(self.db, date(2027, 3, 6)), "wochenende")

    def test_wochenenden_auf_wunsch_mit(self):
        ergebnis = self._bereich("2027-03-01", "2027-03-07", "urlaub", nur_wochentage=False)
        self.assertEqual(len(ergebnis["gesetzt"]), 7)
        self.assertEqual(_get_daytype(self.db, date(2027, 3, 6)), "urlaub")

    def test_schaltet_nicht_um(self):
        """Der entscheidende Unterschied zu einer Schleife ueber /set.

        Ein Tag, der den Zieltyp schon traegt, muss ihn behalten. Ueber `/set`
        wuerde er `frei` - und genau dort faellt es niemandem auf, weil die
        uebrigen zwanzig Tage richtig aussehen.
        """
        set_day_type(date_str="2027-03-03", dtype="urlaub", half=False, db=self.db)
        self.assertEqual(_get_daytype(self.db, date(2027, 3, 3)), "urlaub")

        self._bereich("2027-03-01", "2027-03-05", "urlaub")
        self.assertEqual(_get_daytype(self.db, date(2027, 3, 3)), "urlaub")
        self.assertEqual(_get_daytype(self.db, date(2027, 3, 1)), "urlaub")

    def test_feiertag_wird_ausgelassen(self):
        karfreitag = date(2027, 3, 26)
        self.db.add(Event(
            calendar_id=FEIERTAG_CALENDAR_ID,
            title="Testfeiertag",
            start=datetime(2027, 3, 26, 0, 0),
            end=datetime(2027, 3, 26, 23, 59),
            all_day=True,
        ))
        self.db.commit()

        ergebnis = self._bereich("2027-03-22", "2027-03-26", "urlaub")
        self.assertEqual(len(ergebnis["gesetzt"]), 4)
        self.assertIn(
            {"date": karfreitag.isoformat(), "grund": "feiertag"},
            ergebnis["uebersprungen"],
        )
        # Kein Urlaubsereignis am Feiertag, also auch keines im Urlaubskonto.
        bilanz = get_urlaub_bilanz(jahr=2027, db=self.db)
        self.assertNotIn(karfreitag.isoformat(), bilanz["tage"])

    def test_frei_raeumt_den_bereich(self):
        self._bereich("2027-03-01", "2027-03-05", "urlaub")
        self._bereich("2027-03-01", "2027-03-05", "frei")
        self.assertEqual(_get_daytype(self.db, date(2027, 3, 3)), "frei")

    def test_ersetzt_einen_anderen_typ(self):
        self._bereich("2027-03-01", "2027-03-05", "arbeit")
        self._bereich("2027-03-03", "2027-03-04", "urlaub")
        self.assertEqual(_get_daytype(self.db, date(2027, 3, 2)), "arbeit")
        self.assertEqual(_get_daytype(self.db, date(2027, 3, 3)), "urlaub")

    def test_einmal_neu_planen_je_woche(self):
        """Zwei Kalenderwochen, zehn Tage, zwei Planungslaeufe."""
        ergebnis = self._bereich("2027-03-01", "2027-03-12", "urlaub")
        self.assertEqual(ergebnis["wochen_neu_geplant"], 2)

    def test_umgekehrter_bereich_wird_abgewiesen(self):
        with self.assertRaises(Exception):
            self._bereich("2027-03-10", "2027-03-01", "urlaub")

    def test_zu_langer_bereich_wird_abgewiesen(self):
        with self.assertRaises(Exception):
            self._bereich("2027-01-01", "2028-06-01", "urlaub")


class UrlaubBilanzTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = next(get_db())
        for cal_id in list(DAYTYPE_CALENDARS.values()) + [FEIERTAG_CALENDAR_ID]:
            self.db.add(Calendar(id=cal_id, name=cal_id[:8], is_system=True))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_leeres_jahr(self):
        bilanz = get_urlaub_bilanz(jahr=2027, db=self.db)
        self.assertEqual(bilanz["verbraucht"], 0)
        self.assertEqual(bilanz["tage"], [])

    def test_halbe_tage_zaehlen_halb(self):
        set_day_type(date_str="2027-03-01", dtype="urlaub", half=False, db=self.db)
        set_day_type(date_str="2027-03-02", dtype="urlaub", half=True, db=self.db)
        bilanz = get_urlaub_bilanz(jahr=2027, db=self.db)
        self.assertEqual(bilanz["ganze_tage"], 1)
        self.assertEqual(bilanz["halbe_tage"], 1)
        self.assertEqual(bilanz["verbraucht"], 1.5)

    def test_nachbarjahr_zaehlt_nicht_mit(self):
        set_day_type(date_str="2027-12-31", dtype="urlaub", half=False, db=self.db)
        set_day_type(date_str="2028-01-03", dtype="urlaub", half=False, db=self.db)
        self.assertEqual(get_urlaub_bilanz(jahr=2027, db=self.db)["verbraucht"], 1)
        self.assertEqual(get_urlaub_bilanz(jahr=2028, db=self.db)["verbraucht"], 1)


if __name__ == "__main__":
    unittest.main()
