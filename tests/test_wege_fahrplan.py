"""Tests fuer Bus und Bahn, Heimweg, Arbeitsweg und den Einkaufs-Vorschlag.

Auftrag 2026-10-31.

Wie in `test_orte_wege.py` faellt kein Test ins Netz: ersetzt werden genau die
Funktionen, die hinausgehen (`wege.verbindung`, `wege.wegzeit`, die
Einkaufsliste), und fuer den OTP-Anbieter selbst `urlopen` mit einer echten
Antwortform.
"""

import io
import json
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

try:  # setzt DATABASE_URL; Name je nach Aufruf (discover -s tests oder tests.<modul>)
    from test_orte_wege import ARBEIT, TAG, ZUHAUSE, OrteBasis
except ImportError:
    from tests.test_orte_wege import ARBEIT, TAG, ZUHAUSE, OrteBasis

from backend import wege, wegzeit
from backend.config import settings
from backend.models import Calendar, Event
from backend.routers import places as places_router
from backend.schemas import PlaceCreate
from backend.system_calendars import DAYTYPE_CALENDARS, WEGE_CALENDAR_ID
from backend.wegzeit import BERLIN

STADT = (51.2562, 7.1508)       # Wuppertal
MARKT = (50.9400, 6.9700)       # ein Markt, erfunden


def um(stunde, minute=0, tag=TAG):
    return datetime.combine(tag, datetime.min.time()).replace(hour=stunde, minute=minute)


class FahrplanBasis(OrteBasis):
    def setUp(self):
        super().setUp()
        self.db.add(Calendar(id=DAYTYPE_CALENDARS["arbeit"], name="Arbeit", is_system=True))
        self.db.commit()
        settings.ROUTING_URL = "http://valhalla.invalid"
        settings.OTP_URL = "http://otp.invalid"
        settings.WEG_STANDARD_MITTEL = "transit"
        places_router.ort_anlegen(
            PlaceCreate(name="Daheim", kind="zuhause", lat=ZUHAUSE[0], lon=ZUHAUSE[1]), self.db)
        self.anfragen = []
        self._fahrzeit = 30
        self._einkauf = mock.patch("backend.cross_app.shopping_open_count", return_value=(0, []))
        self._einkauf.start()

        def gefaelscht(start, ziel, ankunft=None, abfahrt=None):
            """Ein Fahrplan mit festem Takt: Abfahrt immer zur vollen oder halben Stunde."""
            self.anfragen.append((start, ziel, ankunft, abfahrt))
            dauer = timedelta(minutes=self._fahrzeit)
            if ankunft is not None:
                spaetest = ankunft - dauer
                ab = spaetest.replace(minute=0 if spaetest.minute < 30 else 30, second=0)
            else:
                ab = abfahrt + timedelta(minutes=(30 - abfahrt.minute % 30) % 30)
            return {"abfahrt": ab.replace(tzinfo=BERLIN), "ankunft": (ab + dauer).replace(tzinfo=BERLIN),
                    "text": f"Bus 42 Start {ab:%H:%M} → Ziel {(ab + dauer):%H:%M}"}

        wege.verbindung = gefaelscht

    def tearDown(self):
        self._einkauf.stop()
        super().tearDown()

    def heimwege(self):
        return self.db.query(Event).filter(Event.travel_kind == wege.HEIM).all()


class TestBusUndBahn(FahrplanBasis):
    def test_block_beginnt_bei_der_abfahrt(self):
        """Termin 9:00, Puffer 5, Fahrt 30 min: der Bus um 8:00 kommt 8:30 an.

        Der Block beginnt um 8:00 und nicht um 8:25: die Wartezeit am Ziel
        verbringt man nicht zu Hause.
        """
        termin = self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1])
        weg = wege.weg_aktualisieren(self.db, termin)
        self.assertEqual(weg.start, um(8, 0))
        self.assertEqual(weg.end, um(8, 55))
        self.assertEqual(weg.travel_mode, "transit")
        self.assertIn("Bus 42", weg.description)
        self.assertIsNone(weg.travel_kind)

    def test_ohne_verbindung_kein_weg_und_kein_fehler(self):
        wege.verbindung = lambda *a, **k: None
        termin = self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1])
        self.assertIsNone(wege.weg_aktualisieren(self.db, termin))

    def test_serie_traegt_die_laengste_tagesart(self):
        """Samstags faehrt der Bus seltener: der Serienweg nimmt die laengere Zeit."""
        wochentage = []

        def je_tag(start, ziel, ankunft=None, abfahrt=None):
            wochentage.append(ankunft.weekday())
            dauer = 50 if ankunft.weekday() == 5 else 20
            ab = ankunft - timedelta(minutes=dauer)
            return {"abfahrt": ab.replace(tzinfo=BERLIN), "ankunft": ankunft.replace(tzinfo=BERLIN),
                    "text": "Bus"}

        wege.verbindung = je_tag
        termin = self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1],
                              recurrence_rule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA")
        weg = wege.weg_aktualisieren(self.db, termin)
        self.assertEqual(weg.end - weg.start, timedelta(minutes=50))
        self.assertIn("samstags 50 min", weg.description)
        self.assertIn("werktags 20 min", weg.description)
        # je Tagesart genau eine Anfrage, nicht eine je Vorkommen
        self.assertEqual(len(wochentage), 2)


class TestHeimweg(FahrplanBasis):
    def test_nach_dem_letzten_termin_nach_hause(self):
        self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1], title="Arzt")
        self._termin(stunde=14, lat=STADT[0], lon=STADT[1], title="Treffen")
        self.db.commit()
        ergebnis = wege.tag_planen(self.db, TAG)
        self.db.commit()
        heim = self.heimwege()
        self.assertEqual(len(heim), 1)
        self.assertEqual(heim[0].start, um(15, 0))     # Ende Treffen, Bus zur vollen Stunde
        self.assertEqual(heim[0].end, um(15, 30))
        self.assertTrue(heim[0].title.startswith("Weg nach Hause"))
        self.assertEqual(ergebnis["heimweg"]["von"], "Treffen")
        self.assertEqual(self.anfragen[-1][1], ZUHAUSE)

    def test_heimweg_wandert_zum_neuen_letzten_termin(self):
        self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1], title="Arzt")
        wege.tag_planen(self.db, TAG)
        self.db.commit()
        spaet = self._termin(stunde=17, lat=STADT[0], lon=STADT[1], title="Kino")
        wege.tag_planen(self.db, TAG)
        self.db.commit()
        heim = self.heimwege()
        self.assertEqual(len(heim), 1)
        self.assertEqual(heim[0].travel_for_event_id, spaet.id)

    def test_letzter_termin_zu_hause_kein_heimweg(self):
        self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1], title="Arzt")
        self._termin(stunde=18, lat=ZUHAUSE[0], lon=ZUHAUSE[1], title="Handwerker")
        wege.tag_planen(self.db, TAG)
        self.db.commit()
        # der Arzt ist der letzte Aufenthalt AUSSER Haus; danach geht es heim
        heim = self.heimwege()
        self.assertEqual(len(heim), 1)
        self.assertEqual(heim[0].start, um(10, 0))

    def test_leerer_tag_raeumt_alten_heimweg_ab(self):
        termin = self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1])
        wege.tag_planen(self.db, TAG)
        self.db.commit()
        wege.weg_entfernen(self.db, termin.id)
        self.db.delete(termin)
        self.db.commit()
        self.assertEqual(self.heimwege(), [])

    def test_hin_und_heimweg_sind_getrennt_auffindbar(self):
        """Beide zeigen auf denselben Termin; der Hinweg darf den Heimweg nie ueberschreiben."""
        termin = self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1])
        wege.weg_aktualisieren(self.db, termin)
        wege.tag_planen(self.db, TAG)
        self.db.commit()
        wege.weg_aktualisieren(self.db, termin)
        self.db.commit()
        alle = self.db.query(Event).filter(Event.travel_for_event_id == termin.id).all()
        self.assertEqual(sorted(w.travel_kind or "hin" for w in alle), ["heim", "hin"])

    def test_zu_fuss_ohne_fahrplan(self):
        settings.WEG_STANDARD_MITTEL = "pedestrian"
        wege.wegzeit = lambda start, ziel, mittel=None, ankunft=None: 25
        self._termin(stunde=9, lat=ARBEIT[0], lon=ARBEIT[1])
        wege.tag_planen(self.db, TAG)
        self.db.commit()
        heim = self.heimwege()
        self.assertEqual((heim[0].start, heim[0].end), (um(10, 0), um(10, 25)))


class TestArbeitstag(FahrplanBasis):
    def setUp(self):
        super().setUp()
        places_router.ort_anlegen(
            PlaceCreate(name="Firma", kind="arbeit", lat=ARBEIT[0], lon=ARBEIT[1]), self.db)
        self.arbeit = Event(calendar_id=DAYTYPE_CALENDARS["arbeit"], title="Arbeit",
                            start=um(7), end=um(16))
        self.db.add(self.arbeit)
        self.db.commit()

    def test_arbeitstag_bekommt_hin_und_heimweg(self):
        ergebnis = wege.tag_planen(self.db, TAG)
        self.db.commit()
        self.assertEqual(ergebnis["hinwege"], 1)
        hin = self.db.query(Event).filter(Event.travel_for_event_id == self.arbeit.id,
                                          Event.travel_kind.is_(None)).one()
        self.assertEqual(hin.end, um(6, 55))
        self.assertEqual(hin.calendar_id, WEGE_CALENDAR_ID)
        heim = self.heimwege()
        self.assertEqual(heim[0].start, um(16, 0))

    def test_abendtermin_startet_an_der_arbeit(self):
        termin = self._termin(stunde=18, lat=STADT[0], lon=STADT[1], title="Sport")
        start, name = wege.startort(self.db, termin)
        self.assertEqual(start, ARBEIT)
        self.assertEqual(name, "Firma")

    def test_zwei_arbeitsorte_kein_geratener_weg(self):
        places_router.ort_anlegen(
            PlaceCreate(name="Zweitfirma", kind="arbeit", lat=STADT[0], lon=STADT[1]), self.db)
        ergebnis = wege.tag_planen(self.db, TAG)
        self.assertEqual(ergebnis["hinwege"], 0)

    def test_tagestyp_selbst_bleibt_unberuehrt(self):
        """CORE: der Wege-Lauf liest den Tagestyp, er schreibt ihn nie."""
        vorher = (self.arbeit.start, self.arbeit.end, self.arbeit.calendar_id, self.arbeit.title)
        wege.tag_planen(self.db, TAG)
        self.db.commit()
        self.db.refresh(self.arbeit)
        self.assertEqual(vorher, (self.arbeit.start, self.arbeit.end,
                                  self.arbeit.calendar_id, self.arbeit.title))
        self.assertEqual(self.db.query(Event).filter(
            Event.calendar_id == DAYTYPE_CALENDARS["arbeit"]).count(), 1)


class TestEinkauf(FahrplanBasis):
    def test_offene_liste_wird_genannt_nicht_eingeplant(self):
        self._einkauf.stop()
        self._einkauf = mock.patch("backend.cross_app.shopping_open_count",
                                   return_value=(3, ["Milch", "Brot"]))
        self._einkauf.start()
        places_router.ort_anlegen(
            PlaceCreate(name="Edeka", kind="einkauf", lat=MARKT[0], lon=MARKT[1]), self.db)
        self._termin(stunde=14, lat=STADT[0], lon=STADT[1], title="Treffen")
        wege.tag_planen(self.db, TAG)
        self.db.commit()
        heim = self.heimwege()
        self.assertEqual(len(heim), 1)
        self.assertIn("Einkauf steht an (3 Artikel, z. B. Milch, Brot)", heim[0].description)
        self.assertIn("Über Edeka", heim[0].description)
        # Vorschlag, kein Plan: kein Markt-Termin, der Heimweg bleibt der direkte
        self.assertEqual(heim[0].end, um(15, 30))
        self.assertEqual(self.db.query(Event).filter(Event.title.like("%Edeka%")).count(), 0)

    def test_leere_liste_kein_satz(self):
        self._termin(stunde=14, lat=STADT[0], lon=STADT[1])
        wege.tag_planen(self.db, TAG)
        self.db.commit()
        self.assertNotIn("Einkauf", self.heimwege()[0].description)

    def test_ausfall_der_einkaufsliste_kostet_den_heimweg_nicht(self):
        self._einkauf.stop()
        self._einkauf = mock.patch("backend.cross_app.shopping_open_count",
                                   side_effect=OSError("mealprep weg"))
        self._einkauf.start()
        self._termin(stunde=14, lat=STADT[0], lon=STADT[1])
        wege.tag_planen(self.db, TAG)
        self.db.commit()
        self.assertEqual(len(self.heimwege()), 1)


class TestOtpAnbieter(unittest.TestCase):
    """Der echte `verbindung()`-Weg gegen eine Antwort in der Form von OTP 2."""

    def setUp(self):
        self._alt = settings.OTP_URL
        settings.OTP_URL = "http://otp.invalid"
        wegzeit._otp_gesperrt_bis = 0.0

    def tearDown(self):
        settings.OTP_URL = self._alt

    def antwort(self, *fahrten):
        kanten = []
        for ab, an, legs in fahrten:
            kanten.append({"node": {"start": ab, "end": an, "legs": legs}})
        roh = json.dumps({"data": {"planConnection": {"edges": kanten}}}).encode()
        r = mock.MagicMock()
        r.__enter__.return_value = io.BytesIO(roh)
        return r

    @staticmethod
    def bein(modus, linie, von, nach, ab, an):
        return {"mode": modus, "route": {"shortName": linie} if linie else None,
                "from": {"name": von}, "to": {"name": nach},
                "start": {"scheduledTime": ab}, "end": {"scheduledTime": an}}

    def test_spaeteste_abfahrt_die_rechtzeitig_ankommt(self):
        legs = [self.bein("WALK", None, "Start", "Rathaus", "2027-03-09T07:28+01:00", "2027-03-09T07:32+01:00"),
                self.bein("BUS", "42", "Musterstadt Rathaus", "Nachbarort ZOB",
                          "2027-03-09T07:32+01:00", "2027-03-09T07:51+01:00")]
        frueh = ("2027-03-09T07:00+01:00", "2027-03-09T07:30+01:00", legs)
        passt = ("2027-03-09T07:28+01:00", "2027-03-09T07:51+01:00", legs)
        zu_spaet = ("2027-03-09T07:58+01:00", "2027-03-09T08:21+01:00", legs)
        with mock.patch("backend.wegzeit.request.urlopen",
                        return_value=self.antwort(frueh, passt, zu_spaet)) as uo:
            v = wegzeit.verbindung(ZUHAUSE, ARBEIT, ankunft=um(7, 55))
        self.assertEqual(v["abfahrt"], datetime(2027, 3, 9, 7, 28, tzinfo=BERLIN))
        self.assertEqual(v["text"], "zu Fuß 4 min, Bus 42 Musterstadt Rathaus 07:32 → Nachbarort ZOB 07:51")
        anfrage = json.loads(uo.call_args[0][0].data)
        self.assertEqual(anfrage["variables"]["zeit"], {"latestArrival": "2027-03-09T07:55:00+01:00"})

    def test_sommerzeit_wird_mitgeschickt(self):
        """Die DB haelt Wanduhr ohne Zone; im Juli ist das +02:00, nicht +01:00."""
        with mock.patch("backend.wegzeit.request.urlopen", return_value=self.antwort()) as uo:
            self.assertIsNone(wegzeit.verbindung(ZUHAUSE, ARBEIT, abfahrt=datetime(2027, 7, 3, 16, 0)))
        anfrage = json.loads(uo.call_args[0][0].data)
        self.assertEqual(anfrage["variables"]["zeit"], {"earliestDeparture": "2027-07-03T16:00:00+02:00"})

    def test_suchfenster_ist_fest(self):
        """Ohne festes Fenster fand OTP bei „spaetestens an 19:08" keine Fahrt
        und gab einen Fussweg von 3,5 Stunden zurueck (gemessen 2026-11-01)."""
        with mock.patch("backend.wegzeit.request.urlopen", return_value=self.antwort()) as uo:
            wegzeit.verbindung(ZUHAUSE, ARBEIT, ankunft=datetime(2027, 3, 9, 19, 8))
        self.assertIn('searchWindow: "PT2H"', json.loads(uo.call_args[0][0].data)["query"])

    def test_zuglinie_ohne_doppelte_art(self):
        legs = [self.bein("RAIL", "RE6", "Essen Hbf", "Düsseldorf Hbf",
                          "2027-03-09T08:00+01:00", "2027-03-09T08:25+01:00")]
        with mock.patch("backend.wegzeit.request.urlopen",
                        return_value=self.antwort(("2027-03-09T08:00+01:00", "2027-03-09T08:25+01:00", legs))):
            v = wegzeit.verbindung(ZUHAUSE, ARBEIT, abfahrt=um(7, 50))
        self.assertTrue(v["text"].startswith("RE6 Essen Hbf 08:00"))

    def test_fehlerantwort_ist_keine_verbindung(self):
        r = mock.MagicMock()
        r.__enter__.return_value = io.BytesIO(json.dumps({"errors": [{"message": "x"}]}).encode())
        with mock.patch("backend.wegzeit.request.urlopen", return_value=r):
            self.assertIsNone(wegzeit.verbindung(ZUHAUSE, ARBEIT, abfahrt=um(8)))

    def test_nach_netzfehler_eine_weile_nicht_fragen(self):
        """Totes OTP darf nicht jedes Speichern eines Termins aufhalten."""
        from urllib import error as urlerror
        with mock.patch("backend.wegzeit.request.urlopen",
                        side_effect=urlerror.URLError("refused")) as uo:
            self.assertIsNone(wegzeit.verbindung(ZUHAUSE, ARBEIT, abfahrt=um(8)))
            self.assertIsNone(wegzeit.verbindung(ZUHAUSE, ARBEIT, abfahrt=um(9)))
        self.assertEqual(uo.call_count, 1)

    def test_ohne_otp_url_klar_benannt(self):
        settings.OTP_URL = ""
        with self.assertRaises(wegzeit.WegzeitFehlt):
            wegzeit.verbindung(ZUHAUSE, ARBEIT, abfahrt=um(8))


class TestWegeTick(FahrplanBasis):
    def test_einmal_am_tag_und_nicht_vor_vier(self):
        # Die Session muss denselben Mandanten tragen wie der Lauf, sonst sieht
        # das Tenant-Scoping die eigene SecretaryRun-Marke nicht wieder.
        self.db.info["owner_sub"] = "test-owner"
        with mock.patch("backend.assistant._owner_session", return_value=self.db), \
             mock.patch.object(self.db, "close"), \
             mock.patch.object(settings, "DEFAULT_OWNER_SUB", "test-owner"), \
             mock.patch("backend.wege.tage_planen", return_value=[]) as plan:
            nachts = datetime(2027, 3, 9, 3, 30, tzinfo=BERLIN)
            self.assertIsNone(wege.run_wege_tick(nachts))
            morgens = datetime(2027, 3, 9, 5, 0, tzinfo=BERLIN)
            self.assertIsNotNone(wege.run_wege_tick(morgens))
            self.assertIsNone(wege.run_wege_tick(morgens + timedelta(hours=3)))
        self.assertEqual(plan.call_count, 1)
        self.assertEqual(plan.call_args[0][1], date(2027, 3, 9))


if __name__ == "__main__":
    unittest.main()
