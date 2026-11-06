"""Zeit von Hand: Timer und Haken (`backend/handzeit.py`, `routers/zeit.py`).

Was hier geprueft wird und warum gerade das:

* **Ein laufender Timer zaehlt nicht**, erst der gestoppte. Er steht als Zeile mit
  dem Stand `laeuft` in derselben Tabelle wie jede Messung; liesse die Bilanz ihn
  zu, zaehlte ein vergessener Timer als erledigt.
* **Die naechste Taetigkeit beendet die vorige**, ohne Ueberlappung.
* **Ein Haken auf einer Sitzung zaehlt aufs Wochenziel**, ueber die Quelle
  `timer/<habit-id>`, die er selbst anlegt. Ohne sie laege die Messung in der
  Tabelle und zaehlte lautlos auf nichts.
* **Ein zweiter Haken korrigiert**, statt eine zweite Messung daneben zu legen.
* **Kappung**: ein ueber Nacht vergessener Timer belegt nicht 14 Stunden.
"""

import os
import unittest
from datetime import datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-handzeit.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend import handzeit
from backend.database import Base, SessionLocal, engine
from backend.main import app, create_jwt_token
from backend.models import Event, Habit, HabitQuelle, HabitSession, ZeitIst
from backend.wochenbilanz import gemessene_minuten


def _iso(dt: datetime) -> str:
    return dt.replace(second=0, microsecond=0).isoformat()


class HandzeitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.auth = {"Authorization": f"Bearer {create_jwt_token()}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def setUp(self):
        with SessionLocal() as db:
            for modell in (ZeitIst, HabitQuelle, HabitSession, Habit, Event):
                db.query(modell).delete()
            db.commit()
        self.jetzt = handzeit.jetzt()

    def _post(self, pfad, daten=None):
        return self.client.post(pfad, json=daten, headers=self.auth)

    def _habit_mit_sitzung(self, vor_stunden=3, dauer_min=60, status="pending"):
        with SessionLocal() as db:
            habit = Habit(name="Lesen", category="lesen", target_hours_per_week=3,
                          max_session_minutes=90)
            db.add(habit)
            db.flush()
            beginn = self.jetzt - timedelta(hours=vor_stunden)
            sitzung = HabitSession(
                habit_id=habit.id, start=beginn, end=beginn + timedelta(minutes=dauer_min),
                status=status, week_iso=beginn.strftime("%G-W%V"),
            )
            db.add(sitzung)
            db.commit()
            return habit.id, sitzung.id, beginn

    def _bilanz(self, habit_id, tag):
        with SessionLocal() as db:
            habit = db.get(Habit, habit_id)
            montag = tag - timedelta(days=tag.weekday())
            return gemessene_minuten(db, habit, montag)

    # ── Timer ────────────────────────────────────────────────────────────

    def test_timer_laeuft_und_ueberlebt_als_zeile(self):
        antwort = self._post("/api/zeit/timer/start", {"titel": "Keller aufräumen",
                                                        "seit": _iso(self.jetzt - timedelta(minutes=5))})
        self.assertEqual(antwort.status_code, 200, antwort.text)
        self.assertEqual(antwort.json()["laeuft"]["titel"], "Keller aufräumen")
        # Ein neuer Abruf (wie nach einem Neustart) findet ihn wieder.
        gelesen = self.client.get("/api/zeit/timer", headers=self.auth).json()
        self.assertEqual(gelesen["laeuft"]["seit"], _iso(self.jetzt - timedelta(minutes=5))[:16])

    def test_laufender_timer_zaehlt_nicht_erst_der_gestoppte(self):
        habit_id, sitzung_id, _ = self._habit_mit_sitzung(vor_stunden=0, dauer_min=60)
        self._post("/api/zeit/timer/start", {
            "titel": "Lesen", "bezug_typ": "session", "bezug_id": sitzung_id,
            "seit": _iso(self.jetzt - timedelta(minutes=40)),
        })
        # Die Quelle steht schon (der Start legt sie an), die Laufzeile zaehlt trotzdem nicht.
        tag = (self.jetzt - timedelta(minutes=40)).date()
        vorher = self._bilanz(habit_id, tag)
        self.assertEqual(vorher["quellen"], [f"timer/{habit_id}"])
        self.assertEqual(vorher["minuten"], 0)
        stopp = self._post("/api/zeit/timer/stopp", {})
        self.assertEqual(stopp.status_code, 200, stopp.text)
        self.assertGreaterEqual(stopp.json()["minuten"], 39)
        with SessionLocal() as db:
            self.assertEqual(db.get(HabitSession, sitzung_id).status, "completed")
            self.assertIsNone(handzeit.laufender(db))
            zeilen = db.query(ZeitIst).all()
            self.assertEqual([z.bestaetigt for z in zeilen], ["manuell"])
            self.assertEqual(zeilen[0].taetigkeit, habit_id)
        self.assertGreaterEqual(self._bilanz(habit_id, tag)["minuten"], 39)

    def test_naechste_taetigkeit_beendet_die_vorige(self):
        self._post("/api/zeit/timer/start", {"titel": "Eins", "seit": _iso(self.jetzt - timedelta(minutes=50))})
        self._post("/api/zeit/timer/start", {"titel": "Zwei", "seit": _iso(self.jetzt - timedelta(minutes=20))})
        with SessionLocal() as db:
            erledigt = db.query(ZeitIst).filter(ZeitIst.bestaetigt == "manuell").all()
            self.assertEqual(len(erledigt), 1)
            self.assertEqual(erledigt[0].titel, "Eins")
            self.assertEqual(int((erledigt[0].end - erledigt[0].start).total_seconds() // 60), 30)
            self.assertEqual(handzeit.laufender(db).titel, "Zwei")

    def test_vergessener_timer_wird_gekappt(self):
        habit_id, sitzung_id, _ = self._habit_mit_sitzung(vor_stunden=0)
        self._post("/api/zeit/timer/start", {
            "titel": "Lesen", "bezug_typ": "session", "bezug_id": sitzung_id,
            "seit": _iso(self.jetzt - timedelta(hours=5)),
        })
        stopp = self._post("/api/zeit/timer/stopp", {}).json()
        self.assertTrue(stopp["gekappt"])
        self.assertEqual(stopp["minuten"], 90)  # max_session_minutes des Habits

    def test_stopp_ohne_timer_ist_409(self):
        self.assertEqual(self._post("/api/zeit/timer/stopp", {}).status_code, 409)

    def test_timer_in_der_zukunft_wird_abgelehnt(self):
        antwort = self._post("/api/zeit/timer/start", {"titel": "x", "seit": _iso(self.jetzt + timedelta(hours=1))})
        self.assertEqual(antwort.status_code, 422)

    # ── Haken ────────────────────────────────────────────────────────────

    def test_haken_auf_sitzung_zaehlt_aufs_wochenziel(self):
        habit_id, sitzung_id, beginn = self._habit_mit_sitzung(vor_stunden=3, status="dismissed")
        antwort = self._post("/api/zeit/haken", {
            "titel": "Lesen", "start": _iso(beginn), "ende": _iso(beginn + timedelta(minutes=45)),
            "bezug_typ": "session", "bezug_id": sitzung_id,
        })
        self.assertEqual(antwort.status_code, 201, antwort.text)
        with SessionLocal() as db:
            self.assertEqual(db.get(HabitSession, sitzung_id).status, "completed")
            quelle = db.query(HabitQuelle).one()
            self.assertEqual((quelle.dienst, quelle.kennung), ("timer", habit_id))
        self.assertEqual(self._bilanz(habit_id, beginn.date())["minuten"], 45)

    def test_zweiter_haken_korrigiert_statt_zu_verdoppeln(self):
        with SessionLocal() as db:
            termin = Event(calendar_id="system-termine-0000-0000-000000000000", title="Zahnarzt",
                           start=self.jetzt - timedelta(hours=4), end=self.jetzt - timedelta(hours=3))
            db.add(termin)
            db.commit()
            termin_id = termin.id
        beginn = self.jetzt - timedelta(hours=4)
        for minuten in (60, 75):
            antwort = self._post("/api/zeit/haken", {
                "titel": "Zahnarzt", "start": _iso(beginn), "ende": _iso(beginn + timedelta(minutes=minuten)),
                "bezug_typ": "event", "bezug_id": termin_id,
            })
            self.assertEqual(antwort.status_code, 201, antwort.text)
        belege = self.client.get(
            "/api/zeit/belege",
            params={"von": str(beginn.date()), "bis": str(beginn.date())}, headers=self.auth,
        ).json()["belege"]
        self.assertEqual(len(belege), 1)
        self.assertEqual(belege[0]["verweis"], f"event:{termin_id}")
        self.assertEqual(belege[0]["ende"], _iso(beginn + timedelta(minutes=75))[:16])

    def test_serieninstanz_wird_ueber_ihre_serie_gefunden(self):
        with SessionLocal() as db:
            serie = Event(calendar_id="system-termine-0000-0000-000000000000", title="Training",
                          start=self.jetzt - timedelta(days=7, hours=2), end=self.jetzt - timedelta(days=7, hours=1),
                          recurrence_rule="FREQ=WEEKLY")
            db.add(serie)
            db.commit()
            instanz = f"{serie.id}::{(self.jetzt - timedelta(hours=2)).date()}"
        antwort = self._post("/api/zeit/haken", {
            "titel": "Training", "start": _iso(self.jetzt - timedelta(hours=2)),
            "ende": _iso(self.jetzt - timedelta(hours=1)), "bezug_typ": "event", "bezug_id": instanz,
        })
        self.assertEqual(antwort.status_code, 201, antwort.text)

    def test_haken_in_der_zukunft_wird_abgelehnt(self):
        antwort = self._post("/api/zeit/haken", {
            "titel": "x", "start": _iso(self.jetzt + timedelta(hours=1)),
            "ende": _iso(self.jetzt + timedelta(hours=2)),
        })
        self.assertEqual(antwort.status_code, 422)

    def test_unbekannter_bezug_ist_404(self):
        antwort = self._post("/api/zeit/haken", {
            "titel": "x", "start": _iso(self.jetzt - timedelta(hours=2)),
            "ende": _iso(self.jetzt - timedelta(hours=1)), "bezug_typ": "event", "bezug_id": "gibt-es-nicht",
        })
        self.assertEqual(antwort.status_code, 404)


if __name__ == "__main__":
    unittest.main()
