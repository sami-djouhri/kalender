"""Ein Tagestyp-Ereignis traegt immer einen Besitzer, sonst wird es nicht geschrieben.

SAGKAL-002 (Audit 2026-10-29): die bestehenden Tests pruefen das Verhalten
der Setz-Endpunkte, aber nie die Spalte `owner_sub` des erzeugten Ereignisses.
Genau an ihr hing der Vorfall ab dem 07.09.: 98 Arbeits- und Schultage entstanden
ohne Besitzer, `Event` ist STRICT, kein Mandant sah sie, und der Wecker las
`frei`. Rot wurde dabei nichts, ein freier Tag ist eine plausible Antwort.

Hier wird deshalb die Zeile selbst gelesen (ungescoped), und fuer den Fall ohne
Mandanten zweierlei verlangt: 503 statt eines unsichtbaren Ereignisses, und
dass vorher nichts abgeraeumt wurde. Im System-Kontext laeuft die Abfrage der
vorhandenen Tagestypen ungescoped, ein Abraeumen traefe also alle Mandanten.
"""
import os
import unittest
from datetime import date, datetime

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-tagestyp-besitz.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.config import settings
from backend.database import Base, SessionLocal, engine
from backend.main import app, create_jwt_token
from backend.models import Event, Setting
from backend.routers.daytype import _tagestyp_schreiben, _tagestyp_setzen
from backend.system_calendars import DAYTYPE_CALENDARS

FREMD = "fremder-mandant-sub"


def _tagestyp_zeilen(tag: date) -> list[Event]:
    """Alle Tagestyp-Ereignisse eines Tages, ohne jedes Mandanten-Scoping."""
    db = SessionLocal()
    db.info["owner_sub"] = None
    try:
        return (
            db.query(Event)
            .filter(Event.calendar_id.in_(list(DAYTYPE_CALENDARS.values())))
            .filter(Event.start < datetime(tag.year, tag.month, tag.day, 23, 59))
            .filter(Event.end > datetime(tag.year, tag.month, tag.day, 0, 0))
            .execution_options(skip_tenant=True)
            .all()
        )
    finally:
        db.close()


class TagestypBesitzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.owner = {"Authorization": f"Bearer {create_jwt_token()}"}
        db = SessionLocal()
        db.info["owner_sub"] = None
        try:
            cls.token = db.query(Setting).filter(Setting.key == "feed_token").first().value
        finally:
            db.close()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_set_schreibt_den_owner_in_die_zeile(self):
        tag = date(2026, 11, 23)
        antwort = self.client.post(
            "/api/day-type/set", params={"date": tag.isoformat(), "type": "arbeit"},
            headers=self.owner,
        )
        self.assertEqual(antwort.status_code, 200)
        zeilen = _tagestyp_zeilen(tag)
        self.assertEqual(len(zeilen), 1)
        self.assertEqual(zeilen[0].owner_sub, settings.DEFAULT_OWNER_SUB)
        # Und es kommt ueber den realen Owner-Feed als `arbeit` zurueck.
        self.assertEqual(
            self.client.get("/api/day-type",
                            params={"date": tag.isoformat(), "token": self.token}).json()["type"],
            "arbeit",
        )

    def test_set_external_schreibt_den_owner_in_die_zeile(self):
        tag = date(2026, 11, 24)
        antwort = self.client.post(
            "/api/day-type/set-external",
            params={"date": tag.isoformat(), "type": "schule", "token": self.token},
        )
        self.assertEqual(antwort.status_code, 200)
        zeilen = _tagestyp_zeilen(tag)
        self.assertEqual([z.owner_sub for z in zeilen], [settings.DEFAULT_OWNER_SUB])

    def test_set_range_schreibt_den_owner_in_jede_zeile(self):
        antwort = self.client.post(
            "/api/day-type/set-range",
            params={"von": "2026-11-30", "bis": "2026-12-02", "type": "arbeit"},
            headers=self.owner,
        )
        self.assertEqual(antwort.status_code, 200)
        for tag in (date(2026, 11, 30), date(2026, 12, 1), date(2026, 12, 2)):
            zeilen = _tagestyp_zeilen(tag)
            self.assertEqual([z.owner_sub for z in zeilen], [settings.DEFAULT_OWNER_SUB], tag)

    def test_ohne_mandant_kein_unsichtbares_ereignis_und_kein_abraeumen(self):
        tag = date(2026, 12, 8)
        # Ein fremder Mandant hat an diesem Tag Urlaub.
        fremd_db = SessionLocal()
        fremd_db.info["owner_sub"] = FREMD
        try:
            _tagestyp_schreiben(fremd_db, tag, "urlaub", False)
            fremd_db.commit()
        finally:
            fremd_db.close()
        self.assertEqual([z.owner_sub for z in _tagestyp_zeilen(tag)], [FREMD])

        system_db = SessionLocal()
        system_db.info["owner_sub"] = None
        try:
            for schreiber in (
                lambda: _tagestyp_setzen(system_db, tag, "arbeit"),
                lambda: _tagestyp_schreiben(system_db, tag, "arbeit", False),
            ):
                with self.assertRaises(HTTPException) as fehler:
                    schreiber()
                self.assertEqual(fehler.exception.status_code, 503)
            system_db.commit()
        finally:
            system_db.close()

        # Der fremde Urlaub steht noch, und es ist nichts ohne Besitzer dazugekommen.
        self.assertEqual([z.owner_sub for z in _tagestyp_zeilen(tag)], [FREMD])

    def test_leerer_default_owner_gibt_503_statt_frei(self):
        """Der Zustand vom 07.09.: DEFAULT_OWNER_SUB leer, der Request-Pfad
        traegt damit keinen Mandanten. Vorher entstand ein Ereignis, das kein
        Mandant sah; jetzt ist es ein lauter Fehler."""
        tag = date(2026, 12, 9)
        vorher = settings.DEFAULT_OWNER_SUB
        settings.DEFAULT_OWNER_SUB = ""
        try:
            antwort = self.client.post(
                "/api/day-type/set-external",
                params={"date": tag.isoformat(), "type": "arbeit", "token": self.token},
            )
        finally:
            settings.DEFAULT_OWNER_SUB = vorher
        self.assertEqual(antwort.status_code, 503)
        self.assertEqual(_tagestyp_zeilen(tag), [])

    def test_falscher_feed_token_wird_abgewiesen(self):
        for falsch in ("", "x", self.token[:-1], self.token + "x"):
            antwort = self.client.post(
                "/api/day-type/set-external",
                params={"date": "2026-12-10", "type": "arbeit", "token": falsch},
            )
            self.assertEqual(antwort.status_code, 403, falsch[:4])


if __name__ == "__main__":
    unittest.main()
