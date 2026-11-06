import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-daytype.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, engine, get_db
from backend.holidays_nrw import get_nrw_holidays
from backend.models import Calendar, Event
from backend.routers.daytype import (
    ARBEIT_TIMES,
    BERLIN,
    DAYTYPE_CALENDARS,
    FEIERTAG_CALENDAR_ID,
    _get_daytype,
    _get_daytype_display,
    _make_daytype_event,
)


class DaytypePriorityTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = next(get_db())
        # Systemkalender anlegen
        for cal_id in list(DAYTYPE_CALENDARS.values()) + [FEIERTAG_CALENDAR_ID]:
            self.db.add(Calendar(id=cal_id, name=cal_id[:8], is_system=True))
        self.db.commit()
        self.day = date(2027, 3, 9)  # Dienstag

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _add(self, cal_id, dtype=None):
        ev = _make_daytype_event(dtype or "urlaub", self.day)
        self.db.add(Event(calendar_id=cal_id, title="x", start=ev["start"], end=ev["end"], all_day=ev["all_day"]))
        self.db.commit()

    def test_default_is_frei(self):
        self.assertEqual(_get_daytype(self.db, self.day), "frei")
        self.assertEqual(_get_daytype_display(self.db, self.day), "frei")

    def test_weekend_display(self):
        sat = date(2027, 3, 13)
        self.assertEqual(_get_daytype_display(self.db, sat), "wochenende")
        self.assertEqual(_get_daytype(self.db, sat), "frei")

    def test_krank_maps_to_frei_for_ha_but_krank_for_display(self):
        self._add(DAYTYPE_CALENDARS["krank"], "krank")
        self.assertEqual(_get_daytype(self.db, self.day), "frei")
        self.assertEqual(_get_daytype_display(self.db, self.day), "krank")

    def test_arbeit_detected(self):
        self._add(DAYTYPE_CALENDARS["arbeit"], "arbeit")
        self.assertEqual(_get_daytype(self.db, self.day), "arbeit")

    def test_urlaub_beats_arbeit(self):
        self._add(DAYTYPE_CALENDARS["arbeit"], "arbeit")
        self._add(DAYTYPE_CALENDARS["urlaub"], "urlaub")
        self.assertEqual(_get_daytype(self.db, self.day), "urlaub")

    def test_feiertag_beats_everything(self):
        self._add(DAYTYPE_CALENDARS["arbeit"], "arbeit")
        self._add(DAYTYPE_CALENDARS["urlaub"], "urlaub")
        self._add(FEIERTAG_CALENDAR_ID, "urlaub")  # all-day Feiertag-Event
        self.assertEqual(_get_daytype(self.db, self.day), "feiertag")
        self.assertEqual(_get_daytype_display(self.db, self.day), "feiertag")

    def test_timed_arbeit_overlap_detected(self):
        # Arbeit ist getimt (7:00-15:45); Overlap-Erkennung muss greifen.
        self._add(DAYTYPE_CALENDARS["arbeit"], "arbeit")
        ev = self.db.query(Event).filter(Event.calendar_id == DAYTYPE_CALENDARS["arbeit"]).first()
        self.assertFalse(ev.all_day)
        self.assertEqual(_get_daytype(self.db, self.day), "arbeit")

    def test_arbeit_friday_shorter(self):
        """Freitags gilt der kuerzere Arbeitstag.

        Geprueft wird gegen ARBEIT_TIMES statt gegen eine fest eingetippte Stunde:
        der Test hing sonst an einer konkreten Uhrzeit und wurde rot, als die reale
        Arbeitszeit geaendert wurde (Fr endet seit Laengerem 13:00, der Test erwartete
        noch 12:00). Interessant ist die Verzweigung, nicht der Stundenwert.
        """
        fri = date(2027, 3, 12)
        mon = date(2027, 3, 8)
        self.assertEqual(fri.weekday(), 4)
        fri_ev = _make_daytype_event("arbeit", fri)
        mon_ev = _make_daytype_event("arbeit", mon)
        self.assertEqual(fri_ev["end"].astimezone(BERLIN).hour, ARBEIT_TIMES["friday"][2])
        self.assertEqual(mon_ev["end"].astimezone(BERLIN).hour, ARBEIT_TIMES["default"][2])
        self.assertLess(
            fri_ev["end"].astimezone(BERLIN), mon_ev["end"].astimezone(BERLIN).replace(day=12)
        )


class HolidayTest(unittest.TestCase):
    def test_known_nrw_holidays_2026(self):
        holidays = dict(get_nrw_holidays(2026))
        # Neujahr + Tag der Arbeit fix
        self.assertIn(date(2026, 1, 1), holidays)
        self.assertIn(date(2026, 5, 1), holidays)
        # Ostersonntag 2026 = 5. April (Gauss) → Karfreitag 3. April
        self.assertIn(date(2026, 4, 3), holidays)
        # Erster Weihnachtstag
        self.assertIn(date(2026, 12, 25), holidays)


if __name__ == "__main__":
    unittest.main()
