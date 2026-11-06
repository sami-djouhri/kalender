"""Block planen: „morgen 2h Keller aufräumen“ (Auftrag 2026-11-01).

Drei Dinge, die vorher falsch waren:
1. Die Schnelleingabe las „2h“ als Uhrzeit 02:00, „2 Std“ machte eine Aufgabe
   ohne Dauer, „zuhause“ blieb im Titel.
2. Habit-Planer und freie Slots sahen Todos mit Zeitblock nicht (nur die
   Slot-Rechnung zählte sie) und Serientermine nur in der Woche ihres Beginns.
3. Ohne Uhrzeit gab es keine Vorschläge; jetzt bis zu drei, angelegt wird erst
   beim Antippen.
"""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-block.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.capture import BERLIN, parse_capture
from backend.database import Base, engine, get_db
from backend.main import app, create_jwt_token
from backend.models import Event, HabitSession, Todo
from backend.scheduler import _get_occupied_slots, block_vorschlaege, compute_free_slots
from backend.system_calendars import DAYTYPE_CALENDARS, TERMINE_CAL_ID

# Montag, 2026-07-13, 09:00 Berlin
NOW = datetime(2026, 7, 13, 9, 0, tzinfo=BERLIN)
# Ein Dienstag weit in der Zukunft, damit „heute ist schon vorbei“ nie greift.
TAG = date(2027, 3, 9)


def p(text):
    return parse_capture(text, now=NOW)


class DauerStattUhrzeit(unittest.TestCase):
    def test_2h_ist_dauer(self):
        c = p("morgen 2h Keller aufräumen zuhause")
        self.assertIsNone(c.start_time)
        self.assertEqual(c.dauer_min, 120)
        self.assertEqual(c.date, "2026-07-14")
        self.assertEqual(c.title, "Keller aufräumen")
        self.assertEqual(c.ort, "zuhause")
        self.assertEqual(c.type, "event")

    def test_std_ist_dauer_und_kein_todo(self):
        c = p("morgen 2 Std Keller")
        self.assertEqual(c.dauer_min, 120)
        self.assertEqual(c.type, "event")
        self.assertEqual(c.title, "Keller")

    def test_18h_bleibt_uhrzeit(self):
        c = p("Sport 18h")
        self.assertEqual(c.start_time, "18:00")
        self.assertIsNone(c.dauer_min)

    def test_um_7h_ist_uhrzeit(self):
        self.assertEqual(p("Training um 7h").start_time, "07:00")

    def test_uhrzeit_und_dauer(self):
        c = p("Zahnarzt morgen 15 Uhr 2h")
        self.assertEqual((c.start_time, c.end_time, c.dauer_min), ("15:00", "17:00", 120))

    def test_komma_dauer(self):
        """„1,5h“ wurde vorher als 5 Stunden gelesen."""
        self.assertEqual(p("1,5h lesen").dauer_min, 90)

    def test_dauer_in_worten(self):
        self.assertEqual(p("eine halbe Stunde Yoga").dauer_min, 30)
        self.assertEqual(p("anderthalb Stunden Garten").dauer_min, 90)

    def test_aufgabe_behaelt_dauer(self):
        c = p("morgen 1h einkaufen")
        self.assertEqual(c.type, "todo")
        self.assertEqual(c.dauer_min, 60)

    def test_block_ohne_datum_bleibt_ohne_datum(self):
        """Die Vorschläge suchen dann über mehrere Tage, nicht nur heute."""
        c = p("2h Keller")
        self.assertIsNone(c.date)
        self.assertEqual(c.confidence, "mittel")


class BelegungBasis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.h = {"Authorization": f"Bearer {create_jwt_token()}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def setUp(self):
        self.db = next(get_db())
        self.db.query(Event).filter(
            Event.calendar_id.in_([TERMINE_CAL_ID, DAYTYPE_CALENDARS["arbeit"]])
        ).delete(synchronize_session=False)
        self.db.query(Todo).delete(synchronize_session=False)
        self.db.query(HabitSession).delete(synchronize_session=False)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _termin(self, tag, von, bis, kalender=TERMINE_CAL_ID, **felder):
        e = Event(calendar_id=kalender, title=felder.pop("title", "Termin"),
                  start=datetime.combine(tag, datetime.min.time()).replace(hour=von),
                  end=datetime.combine(tag, datetime.min.time()).replace(hour=bis),
                  **felder)
        self.db.add(e)
        self.db.commit()
        return e

    def _belegt(self, tag, stunde):
        punkt = datetime.combine(tag, datetime.min.time()).replace(hour=stunde, minute=30, tzinfo=BERLIN)
        return any(s <= punkt < e for s, e in _get_occupied_slots(self.db, tag, tag))


class BelegungTest(BelegungBasis):
    def test_serientermin_aus_der_vergangenheit_belegt(self):
        """Gemessen 2026-11-01: alle 8 Serien begannen im Juli und waren unsichtbar."""
        anfang = TAG - timedelta(weeks=30)
        self._termin(anfang, 17, 19, recurrence_rule="FREQ=WEEKLY;BYDAY=TU")
        self.assertTrue(self._belegt(TAG, 17))
        self.assertFalse(self._belegt(TAG, 15))

    def test_ausnahmetag_der_serie_ist_frei(self):
        anfang = TAG - timedelta(weeks=3)
        self._termin(anfang, 17, 19, recurrence_rule="FREQ=WEEKLY;BYDAY=TU",
                     recurrence_exdates=TAG.isoformat())
        self.assertFalse(self._belegt(TAG, 17))

    def test_todo_mit_zeitblock_belegt_auch_fuer_habits(self):
        start = datetime.combine(TAG, datetime.min.time()).replace(hour=10)
        self.db.add(Todo(title="Keller", scheduled_start=start,
                         scheduled_end=start + timedelta(hours=2),
                         scheduling_mode="fixed_slot", status="scheduled"))
        self.db.commit()
        self.assertTrue(self._belegt(TAG, 11))

    def test_erledigtes_todo_gibt_den_block_frei(self):
        start = datetime.combine(TAG, datetime.min.time()).replace(hour=10)
        self.db.add(Todo(title="Keller", scheduled_start=start,
                         scheduled_end=start + timedelta(hours=2),
                         status="done", completed=True))
        self.db.commit()
        self.assertFalse(self._belegt(TAG, 11))

    def test_arbeitszeit_ist_kein_freier_slot(self):
        self._termin(TAG, 7, 16, kalender=DAYTYPE_CALENDARS["arbeit"], title="Arbeit")
        frei = compute_free_slots(self.db, TAG)
        self.assertEqual(frei[0][0].hour, 16)
        # Für Habits bleibt Arbeit unsichtbar (eigene Zeitfenster je Tagestyp).
        self.assertFalse(self._belegt(TAG, 10))


class VorschlagTest(BelegungBasis):
    def test_drei_vorschlaege_je_tagesabschnitt(self):
        v = block_vorschlaege(self.db, 120, TAG)
        self.assertEqual([x["abschnitt"] for x in v], ["Vormittag", "Nachmittag", "Abend"])
        self.assertEqual((v[0]["start_time"], v[0]["end_time"]), ("08:00", "10:00"))

    def test_vorschlag_meidet_termine(self):
        self._termin(TAG, 8, 12)
        self._termin(TAG, 13, 22)
        v = block_vorschlaege(self.db, 60, TAG)
        self.assertEqual([x["start_time"] for x in v], ["12:00"])

    def test_zu_lang_fuer_den_tag(self):
        self._termin(TAG, 9, 21)
        self.assertEqual(block_vorschlaege(self.db, 120, TAG), [])


class CaptureApiTest(BelegungBasis):
    def test_parse_liefert_vorschlaege(self):
        r = self.client.post("/api/capture", json={"text": f"{TAG:%d.%m.%Y} 2h Keller", "allow_llm": False},
                             headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["dauer_min"], 120)
        self.assertEqual(len(body["vorschlaege"]), 3)

    def test_block_ohne_zeit_wird_nicht_ganztags(self):
        r = self.client.post("/api/capture/commit", headers=self.h, json={
            "type": "event", "title": "Keller", "date": TAG.isoformat(), "dauer_min": 120})
        self.assertEqual(r.status_code, 422)

    def test_gewaehlter_vorschlag_wird_termin(self):
        r = self.client.post("/api/capture/commit", headers=self.h, json={
            "type": "event", "title": "Keller", "date": TAG.isoformat(),
            "start_time": "10:00", "end_time": "12:00", "dauer_min": 120})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue(self._belegt(TAG, 11))

    def test_aufgabe_mit_vorschlag_wird_fester_block(self):
        r = self.client.post("/api/capture/commit", headers=self.h, json={
            "type": "todo", "title": "einkaufen", "date": TAG.isoformat(),
            "start_time": "17:00", "end_time": "18:00", "dauer_min": 60})
        self.assertEqual(r.status_code, 201, r.text)
        t = self.db.query(Todo).filter(Todo.id == r.json()["id"]).one()
        self.assertEqual((t.scheduling_mode, t.estimated_minutes), ("fixed_slot", 60))
        self.assertTrue(self._belegt(TAG, 17))


if __name__ == "__main__":
    unittest.main()
