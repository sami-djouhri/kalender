"""Tests fuer Orte, Adresszerlegung und Weg-Termine.

Kein Test hier faellt ins Netz. Adressdienst und Router sind Fremdsysteme; wer
sie in einer Testsuite anspricht, hat einen Test, der bei jedem Netzhaenger rot
wird und der auf einem fremden Rechner gar nicht laeuft. Stattdessen wird die
eine Funktion ersetzt, die hinausgeht (`wegzeit.wegzeit`), und der Rest echt
geprueft.

Der wichtigste Fall steht ganz unten: **ohne Anbieter darf nichts passieren.**
Das ist der Zustand jeder fremden Installation, und er muss lautlos funktionieren
statt Fehler zu werfen.
"""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-orte.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi import HTTPException

from backend import orte, wege, wegzeit
from backend.config import settings
from backend.database import Base, engine, get_db
import backend.tenant  # noqa: F401 (registriert das Tenant-Scoping)
from backend.models import Calendar, Event, Place
from backend.routers import places as places_router
from backend.schemas import PlaceCreate, PlaceUpdate
from backend.system_calendars import TERMINE_CAL_ID, WEGE_CALENDAR_ID

# Echte Koordinaten, damit die Zahlen beim Lesen einen Sinn ergeben.
ZUHAUSE = (51.3269, 7.0169)      # Heiligenhaus
ARBEIT = (51.3506, 7.1163)       # Velbert
TAG = date(2027, 3, 9)           # Dienstag, weit in der Zukunft


class OrteBasis(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = next(get_db())
        self.db.add(Calendar(id=TERMINE_CAL_ID, name="Termine", is_system=True))
        self.db.add(Calendar(id=WEGE_CALENDAR_ID, name="Wege", is_system=True))
        self.db.commit()
        self._alt_routing = settings.ROUTING_URL
        self._alt_wegzeit = wege.wegzeit
        self._alt_mittel = settings.WEG_STANDARD_MITTEL
        settings.WEG_STANDARD_MITTEL = "pedestrian"

    def tearDown(self):
        settings.ROUTING_URL = self._alt_routing
        settings.WEG_STANDARD_MITTEL = self._alt_mittel
        wege.wegzeit = self._alt_wegzeit
        self.db.close()

    def _routing(self, minuten=25):
        """Anbieter vortaeuschen und eine feste Wegzeit liefern."""
        settings.ROUTING_URL = "http://beispiel.invalid"
        self.aufrufe = []

        def gefaelscht(start, ziel, mittel=None, ankunft=None):
            self.aufrufe.append((start, ziel, mittel, ankunft))
            return minuten

        wege.wegzeit = gefaelscht

    def _termin(self, stunde=9, **felder):
        # ★ `owner_sub` wird bewusst NICHT gesetzt, sondern vom Tenant-Scoping
        #   gestempelt (backend/tenant.py, before_flush). Ein von Hand gesetzter
        #   Wert weicht vom sub der Session ab, und dann filtert der Loader die
        #   eigenen Testzeilen wieder weg: die Tests scheitern mit „nichts
        #   gefunden", obwohl die Zeile in der Datenbank steht.
        e = Event(
            calendar_id=TERMINE_CAL_ID,
            title=felder.pop("title", "Arbeit"),
            start=datetime.combine(TAG, datetime.min.time()).replace(hour=stunde),
            end=datetime.combine(TAG, datetime.min.time()).replace(hour=stunde + 1),
            **felder,
        )
        self.db.add(e)
        self.db.flush()
        return e


class TestOrteCrud(OrteBasis):
    def test_zuhause_nur_einmal(self):
        """Ein zweites Zuhause macht den Startort mehrdeutig und wird abgelehnt."""
        places_router.ort_anlegen(PlaceCreate(name="Daheim", kind="zuhause"), self.db)
        with self.assertRaises(HTTPException) as ctx:
            places_router.ort_anlegen(PlaceCreate(name="Noch daheim", kind="zuhause"), self.db)
        self.assertEqual(ctx.exception.status_code, 409)
        # Die Meldung muss den Namen des vorhandenen Ortes nennen, sonst weiss
        # niemand, welcher der beiden gemeint ist.
        self.assertIn("Daheim", ctx.exception.detail)

    def test_zuhause_umziehen_erlaubt(self):
        """Denselben Ort erneut als Zuhause speichern darf nicht an sich scheitern."""
        ort = places_router.ort_anlegen(PlaceCreate(name="Daheim", kind="zuhause"), self.db)
        geaendert = places_router.ort_aendern(
            ort.id, PlaceUpdate(kind="zuhause", address="Neue Strasse 1"), self.db)
        self.assertEqual(geaendert.address, "Neue Strasse 1")

    def test_arbeit_mehrfach_erlaubt(self):
        """Nur „zuhause“ ist eindeutig; zwei Arbeitsorte sind ein normaler Fall."""
        places_router.ort_anlegen(PlaceCreate(name="Buero", kind="arbeit"), self.db)
        places_router.ort_anlegen(PlaceCreate(name="Zweitjob", kind="arbeit"), self.db)
        self.assertEqual(len(places_router.orte_auflisten(self.db)), 2)


class TestAdressZerlegung(unittest.TestCase):
    """Reine Textzerlegung, ohne Dienst."""

    def test_komma_trennt_ort(self):
        self.assertEqual(orte._teile("Velberter Str. 12, Heiligenhaus"),
                         ("Velberter Str.", "12", "Heiligenhaus"))

    def test_plz_vor_dem_ort_faellt_weg(self):
        """Die Ortssuche kennt Namen, keine Postleitzahlen im Namensfeld."""
        self.assertEqual(orte._teile("Hauptstrasse 5, 42579 Heiligenhaus"),
                         ("Hauptstrasse", "5", "Heiligenhaus"))

    def test_hausnummer_mit_buchstabe(self):
        self.assertEqual(orte._teile("Ahornweg 24a, Velbert"),
                         ("Ahornweg", "24a", "Velbert"))

    def test_ohne_komma_wird_kein_ort_geraten(self):
        """Ein geratener Ort liefert plausible Treffer in der falschen Stadt.

        Deshalb bleibt das Ortsfeld leer, statt das letzte Wort zu nehmen.
        """
        strasse, nummer, ort = orte._teile("Bergische Universitaet Wuppertal")
        self.assertEqual(ort, "")
        self.assertEqual(nummer, "")

    def test_ohne_dienst_keine_treffer(self):
        alt = settings.ADRESS_URL
        settings.ADRESS_URL = ""
        try:
            self.assertFalse(orte.verfuegbar())
            self.assertEqual(orte.freitext("Hauptstrasse 1, Velbert"), [])
        finally:
            settings.ADRESS_URL = alt


class TestWegTermin(OrteBasis):
    def test_weg_entsteht_vor_dem_termin(self):
        self._routing(minuten=25)
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        termin = self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1])
        wege.weg_aktualisieren(self.db, termin)
        self.db.commit()

        weg = self.db.query(Event).filter(Event.travel_for_event_id == termin.id).one()
        self.assertEqual(weg.calendar_id, WEGE_CALENDAR_ID)
        # 25 min Weg, 5 min Puffer vor 9:00, also 8:30 bis 8:55
        self.assertEqual(weg.end, termin.start - timedelta(minutes=settings.WEG_PUFFER_MINUTEN))
        self.assertEqual((weg.end - weg.start), timedelta(minutes=25))
        self.assertIn("25 min", weg.description)
        self.assertIn("Daheim", weg.description)

    def test_zweiter_lauf_legt_keinen_zweiten_weg_an(self):
        """Wiedererkennung laeuft ueber travel_for_event_id, nicht ueber den Titel."""
        self._routing()
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        termin = self._termin(lat=ARBEIT[0], lon=ARBEIT[1])
        wege.weg_aktualisieren(self.db, termin)
        self.db.commit()
        # Titel von Hand aendern, wie es ein Mensch taete
        weg = self.db.query(Event).filter(Event.travel_for_event_id == termin.id).one()
        weg.title = "Mein Weg"
        self.db.commit()

        wege.weg_aktualisieren(self.db, termin)
        self.db.commit()
        self.assertEqual(
            self.db.query(Event).filter(Event.travel_for_event_id == termin.id).count(), 1)

    def test_termin_verschoben_weg_wandert_mit(self):
        self._routing(minuten=25)
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        termin = self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1])
        wege.weg_aktualisieren(self.db, termin)
        self.db.commit()

        termin.start = termin.start.replace(hour=14)
        termin.end = termin.end.replace(hour=15)
        wege.weg_aktualisieren(self.db, termin)
        self.db.commit()

        weg = self.db.query(Event).filter(Event.travel_for_event_id == termin.id).one()
        self.assertEqual(weg.end.hour, 13)
        self.assertEqual(weg.end.minute, 55)

    def test_ort_entfernt_weg_verschwindet(self):
        self._routing()
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        termin = self._termin(lat=ARBEIT[0], lon=ARBEIT[1])
        wege.weg_aktualisieren(self.db, termin)
        self.db.commit()

        termin.lat = termin.lon = None
        wege.weg_aktualisieren(self.db, termin)
        self.db.commit()
        self.assertEqual(
            self.db.query(Event).filter(Event.travel_for_event_id == termin.id).count(), 0)

    def test_kein_zuhause_kein_weg(self):
        """Ohne Startort gibt es nichts zu rechnen, und das ist kein Fehler."""
        self._routing()
        termin = self._termin(lat=ARBEIT[0], lon=ARBEIT[1])
        self.assertIsNone(wege.weg_aktualisieren(self.db, termin))

    def test_weg_zum_weg_gibt_es_nicht(self):
        """Sonst erzeugte jeder Weg einen Weg zu sich selbst, endlos."""
        self._routing()
        termin = self._termin(lat=ARBEIT[0], lon=ARBEIT[1],
                              travel_for_event_id="egal-welche-id")
        self.assertIsNone(wege.weg_aktualisieren(self.db, termin))

    def test_ganztaegig_bekommt_keinen_weg(self):
        """Ein ganztaegiger Termin hat keine Uhrzeit, an der man ankommen koennte."""
        self._routing()
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        termin = self._termin(lat=ARBEIT[0], lon=ARBEIT[1], all_day=True)
        self.assertIsNone(wege.weg_aktualisieren(self.db, termin))

    def test_startort_ist_der_vorherige_termin(self):
        """Wer schon unterwegs ist, laeuft nicht noch einmal von zu Hause los."""
        self._routing()
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        self._termin(stunde=8, title="Termin in Velbert",
                     lat=ARBEIT[0], lon=ARBEIT[1], location="Velbert")
        self.db.commit()
        spaeter = self._termin(stunde=11, title="Zweiter", lat=51.36, lon=7.12)

        start, name = wege.startort(self.db, spaeter)
        self.assertEqual(start, (ARBEIT[0], ARBEIT[1]))
        self.assertEqual(name, "Velbert")

    def test_serie_startet_immer_zu_hause(self):
        """Was letzten Dienstag vorher lag, sagt nichts ueber den naechsten."""
        self._routing()
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        self._termin(stunde=8, title="Einzeltermin davor", lat=ARBEIT[0], lon=ARBEIT[1])
        self.db.commit()
        serie = self._termin(stunde=11, title="Woechentlich", lat=51.36, lon=7.12,
                             recurrence_rule="FREQ=WEEKLY")
        start, name = wege.startort(self.db, serie)
        self.assertEqual(start, ZUHAUSE)
        self.assertEqual(name, "Daheim")

    def test_serie_vererbt_ihre_regel_an_den_weg(self):
        """Der Arbeitsweg wiederholt sich genau dann, wenn die Arbeit sich wiederholt."""
        self._routing()
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        serie = self._termin(lat=ARBEIT[0], lon=ARBEIT[1],
                             recurrence_rule="FREQ=WEEKLY;BYDAY=TU")
        wege.weg_aktualisieren(self.db, serie)
        self.db.commit()
        weg = self.db.query(Event).filter(Event.travel_for_event_id == serie.id).one()
        self.assertEqual(weg.recurrence_rule, "FREQ=WEEKLY;BYDAY=TU")

    def test_gleicher_ort_kein_weg(self):
        self._routing()
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        termin = self._termin(lat=ZUHAUSE[0], lon=ZUHAUSE[1])
        self.assertIsNone(wege.weg_aktualisieren(self.db, termin))

    def test_ankunftszeit_wird_durchgereicht(self):
        """Valhalla ignoriert sie, ein Fahrplan-Anbieter braucht sie.

        Der Test haelt die Signatur fest, damit der Nahverkehrs-Anbieter spaeter
        eine Erweiterung bleibt und kein Umbau wird.
        """
        self._routing()
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        termin = self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1])
        wege.weg_aktualisieren(self.db, termin)
        self.assertEqual(self.aufrufe[0][3], termin.start)


class TestOhneAnbieter(OrteBasis):
    """Der Zustand jeder fremden Installation: nichts konfiguriert."""

    def test_kein_routing_kein_weg_und_kein_fehler(self):
        settings.ROUTING_URL = ""
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        termin = self._termin(lat=ARBEIT[0], lon=ARBEIT[1])
        self.assertIsNone(wege.weg_aktualisieren(self.db, termin))
        self.db.commit()
        self.assertEqual(self.db.query(Event).filter(
            Event.travel_for_event_id.isnot(None)).count(), 0)

    def test_konfiguration_meldet_ehrlich_was_fehlt(self):
        settings.ROUTING_URL = ""
        alt_adress, alt_karte = settings.ADRESS_URL, settings.KARTEN_URL
        settings.ADRESS_URL = settings.KARTEN_URL = ""
        try:
            k = places_router.konfiguration()
            self.assertFalse(k["adressen"])
            self.assertFalse(k["wegzeit"])
            self.assertIsNone(k["karte"])
            self.assertTrue(all(not m["moeglich"] for m in k["verkehrsmittel"]))
        finally:
            settings.ADRESS_URL, settings.KARTEN_URL = alt_adress, alt_karte

    def test_wegzeit_ohne_koordinaten(self):
        self.assertIsNone(wegzeit.wegzeit(None, ARBEIT, "pedestrian"))
        self.assertIsNone(wegzeit.wegzeit(ZUHAUSE, None, "pedestrian"))


class TestVerkehrsmittel(unittest.TestCase):
    def setUp(self):
        self._alt = settings.ROUTING_URL, settings.ROUTING_ART
        settings.ROUTING_URL = "http://beispiel.invalid"

    def tearDown(self):
        settings.ROUTING_URL, settings.ROUTING_ART = self._alt

    def test_valhalla_kann_keinen_nahverkehr(self):
        """Der Kernpunkt: es wird gesagt, statt still eine Fusszeit zu liefern.

        Eine stille Fusszeit waere die gefaehrlichere Antwort. Sie sieht richtig
        aus, und man merkt den Fehler erst, wenn man zu spaet kommt.
        """
        settings.ROUTING_ART = "valhalla"
        self.assertTrue(wegzeit.mittel_moeglich("pedestrian"))
        self.assertTrue(wegzeit.mittel_moeglich("bicycle"))
        self.assertFalse(wegzeit.mittel_moeglich("transit"))
        with self.assertRaises(wegzeit.WegzeitFehlt):
            wegzeit.wegzeit(ZUHAUSE, ARBEIT, "transit")

    def test_otp_meldet_dass_es_ihn_noch_nicht_gibt(self):
        settings.ROUTING_ART = "otp"
        with self.assertRaises(wegzeit.WegzeitFehlt) as ctx:
            wegzeit.wegzeit(ZUHAUSE, ARBEIT, "transit")
        self.assertIn("noch nicht gebaut", str(ctx.exception))


class TestAufstehzeit(OrteBasis):
    def test_rechnet_vom_ersten_termin_zurueck(self):
        self._termin(stunde=9, title="Arbeit")
        self.db.commit()
        ergebnis = wege.aufstehzeit(self.db, TAG)
        erwartet = (datetime.combine(TAG, datetime.min.time()).replace(hour=9)
                    - timedelta(minutes=settings.WEG_VORLAUF_MINUTEN))
        self.assertEqual(ergebnis["aufstehen"], erwartet.isoformat())
        self.assertFalse(ergebnis["ist_weg"])

    def test_der_weg_zaehlt_als_erster_termin(self):
        """Wer um 8:30 losmuss, steht nicht nach dem Terminbeginn auf."""
        self._routing(minuten=25)
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]),
            self.db)
        termin = self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1])
        wege.weg_aktualisieren(self.db, termin)
        self.db.commit()

        ergebnis = wege.aufstehzeit(self.db, TAG)
        self.assertTrue(ergebnis["ist_weg"])
        self.assertTrue(ergebnis["aufstehen"] < termin.start.isoformat())

    def test_leerer_tag(self):
        self.assertIsNone(wege.aufstehzeit(self.db, TAG))


if __name__ == "__main__":
    unittest.main()
