"""Tests der Tagesdecke: lückenlose Tagesplanung, Korrektur und Zeit-Eingang.

Geprüft wird vor allem das, was still falsch sein könnte:

* **Lückenlosigkeit** ist die Kernzusage. Ein Test, der nur schaut, ob Blöcke
  entstehen, würde eine Decke mit Löchern durchwinken.
* **Der Tagesrand kommt aus `sleep_schedule`**, nicht mehr aus dem festen Fenster
  08:00 bis 22:00. An einem Arbeitstag muss die Decke um 05:30 beginnen, sonst ist
  genau der Fehler zurück, der diese Epoche ausgelöst hat.
* **Die Minutensumme darf den wachen Tag nicht überschreiten.** Überlappende
  Termine sind im Kalender erlaubt; eine Decke, die sie doppelt zählt, meldet
  26 Stunden Tag und niemand rechnet nach.
* **`events` bleibt unberührt.** Daran hängt der Tagestyp und damit die
  Weckkette. Ein Test hält das fest, damit es niemand später bequemer macht.
* **Der Zeit-Eingang ist ohne Token zu.** Fail-closed, nicht fail-open.
"""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-tagesdecke.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.config import settings
from backend.database import Base, engine, get_db
import backend.tenant  # noqa: F401 (registriert das Mandanten-Scoping)
from backend.models import Calendar, Event, Habit, HabitSession, TimeBlock, ZeitIst
from backend.system_calendars import DAYTYPE_CALENDARS
from backend.tagesdecke import (
    ARTEN,
    abgleich,
    baue_decke,
    block_aendern,
    block_anlegen,
    festschreiben,
    ist_festgeschrieben,
    neu_ordnen,
    rueckblick,
    wach_fenster,
)
from backend import zeit_ingest
from backend.zeit_ingest import IngestFehler, eintrag_aufnehmen, stapel_aufnehmen

TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"
# Ein Dienstag ohne Feiertag. Ohne Tagestyp-Eintrag gilt 'frei' (wach 08:00 bis 24:00).
FREI_DI = date(2027, 3, 9)
# Derselbe Wochentag, sobald ein Arbeits-Event darauf liegt: wach ab 05:30, Bett 21:30.
ARBEIT_DI = date(2027, 3, 16)


def _wanduhr(tag: date, stunde: int, minute: int = 0) -> datetime:
    """Zonenlose Berliner Wanduhr, so wie die Werte in der Datenbank liegen."""
    return datetime(tag.year, tag.month, tag.day, stunde, minute)


class DeckeBasis(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self._ingest_vorher = settings.ZEIT_INGEST_TOKEN
        self.db = next(get_db())
        self.db.add(Calendar(id=TERMINE_CAL_ID, name="Termine", is_system=True))
        for schluessel, kennung in DAYTYPE_CALENDARS.items():
            self.db.add(Calendar(id=kennung, name=schluessel, is_system=True))
        self.db.commit()

    def tearDown(self):
        settings.ZEIT_INGEST_TOKEN = self._ingest_vorher
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _termin(self, tag, von, bis, titel="Termin", kalender=TERMINE_CAL_ID, **kw):
        ereignis = Event(
            calendar_id=kalender, title=titel,
            start=_wanduhr(tag, *von), end=_wanduhr(tag, *bis), **kw
        )
        self.db.add(ereignis)
        self.db.commit()
        self.db.refresh(ereignis)
        return ereignis

    def _arbeitstag(self, tag):
        """Macht den Tag zum Arbeitstag, wie es `day-type/set` täte."""
        return self._termin(
            tag, (7, 0), (16, 0), titel="Arbeit",
            kalender=DAYTYPE_CALENDARS["arbeit"],
        )


class WachFensterTest(DeckeBasis):
    def test_freier_tag_beginnt_um_acht(self):
        von, bis = wach_fenster(self.db, FREI_DI)
        self.assertEqual(von.hour, 8)
        self.assertEqual(von.minute, 0)

    def test_arbeitstag_beginnt_halb_sechs_nicht_um_acht(self):
        """Der eigentliche Grund für diese Epoche.

        `scheduler.DAY_PLAN_WINDOW_START` stand auf 08:00, während der Wecker
        längst um 05:30 klingelte. Die zweieinhalb Stunden waren für die Planung
        unsichtbar.
        """
        self._arbeitstag(ARBEIT_DI)
        von, bis = wach_fenster(self.db, ARBEIT_DI)
        self.assertEqual((von.hour, von.minute), (5, 30))
        self.assertEqual((bis.hour, bis.minute), (21, 30))

    def test_fenster_endet_spaetestens_mitternacht(self):
        """Krank sieht 01:00 als Bettzeit vor. Ein Block darf nicht in den
        Folgetag laufen, sonst blockiert er dessen Decke ganztägig."""
        self._termin(
            date(2027, 3, 23), (0, 0), (23, 59), titel="Krank",
            kalender=DAYTYPE_CALENDARS["krank"], all_day=True,
        )
        von, bis = wach_fenster(self.db, date(2027, 3, 23))
        self.assertEqual(bis.date(), date(2027, 3, 24))
        self.assertEqual((bis.hour, bis.minute), (0, 0))


class LueckenlosigkeitTest(DeckeBasis):
    def _pruefe_lueckenlos(self, decke):
        """Kernzusage: zwischen den Blöcken darf nichts offen bleiben."""
        bloecke = decke["bloecke"]
        self.assertTrue(bloecke, "Decke ist leer")
        self.assertEqual(bloecke[0]["start"], decke["wach_von"],
                         "Decke beginnt nicht beim Aufstehen")
        self.assertEqual(bloecke[-1]["ende"], decke["wach_bis"],
                         "Decke endet nicht beim Schlafengehen")
        for vorher, nachher in zip(bloecke, bloecke[1:]):
            self.assertEqual(
                vorher["ende"], nachher["start"],
                f"Loch zwischen {vorher['titel']} ({vorher['ende']}) "
                f"und {nachher['titel']} ({nachher['start']})",
            )

    def test_leerer_freier_tag_ist_lueckenlos(self):
        decke = baue_decke(self.db, FREI_DI)
        self._pruefe_lueckenlos(decke)

    def test_tag_mit_terminen_ist_lueckenlos(self):
        self._termin(FREI_DI, (10, 0), (11, 30), titel="Zahnarzt")
        self._termin(FREI_DI, (15, 0), (16, 0), titel="Telefonat")
        decke = baue_decke(self.db, FREI_DI)
        self._pruefe_lueckenlos(decke)
        titel = [b["titel"] for b in decke["bloecke"]]
        self.assertIn("Zahnarzt", titel)
        self.assertIn("Telefonat", titel)

    def test_arbeitstag_ist_lueckenlos(self):
        self._arbeitstag(ARBEIT_DI)
        decke = baue_decke(self.db, ARBEIT_DI)
        self._pruefe_lueckenlos(decke)
        arbeit = [b for b in decke["bloecke"] if b["titel"] == "Arbeit"]
        self.assertEqual(len(arbeit), 1, "Arbeitsblock fehlt in der Decke")
        self.assertEqual(arbeit[0]["art"], "fix")

    def test_minutensumme_passt_zum_wachen_tag(self):
        """Überlappende Termine dürfen die Bilanz nicht über den Tag hinaus treiben."""
        self._termin(FREI_DI, (10, 0), (12, 0), titel="Doppelbuchung A")
        self._termin(FREI_DI, (11, 0), (13, 0), titel="Doppelbuchung B")
        decke = baue_decke(self.db, FREI_DI)
        self._pruefe_lueckenlos(decke)
        self.assertEqual(
            decke["verplant_minuten"], decke["wach_minuten"],
            "Summe der Blockminuten weicht vom wachen Tag ab",
        )
        self.assertEqual(decke["offen_minuten"], 0)


class ErholungUndGrundlastTest(DeckeBasis):
    def test_erholung_wird_geplant_nicht_nur_vorgeschlagen(self):
        """Owner-Vorgabe: Erholung ist Teil der Planung, kein Rest."""
        decke = baue_decke(self.db, FREI_DI)
        erholung = [b for b in decke["bloecke"] if b["art"] == "erholung"]
        self.assertTrue(erholung, "Kein Erholungsblock in der Decke")
        self.assertTrue(all(b["begruendung"] for b in erholung),
                        "Erholungsblock ohne Begründung")

    def test_mahlzeiten_sind_eingeplant(self):
        decke = baue_decke(self.db, FREI_DI)
        titel = [b["titel"] for b in decke["bloecke"]]
        self.assertIn("Mittagessen", titel)
        self.assertIn("Abendessen", titel)

    def test_ausklang_vor_dem_schlafen(self):
        decke = baue_decke(self.db, FREI_DI)
        letzter = decke["bloecke"][-1]
        self.assertEqual(letzter["art"], "erholung")

    def test_jeder_block_hat_eine_bekannte_art(self):
        decke = baue_decke(self.db, FREI_DI)
        for block in decke["bloecke"]:
            self.assertIn(block["art"], ARTEN, f"Unbekannte Art: {block['art']}")

    def test_mahlzeiten_liegen_zur_mahlzeit(self):
        """★ Der Fund aus der Klartext-Ausgabe (scripts/decke-zeigen.py).

        Die erste Fassung prüfte nur, ob die Lücke das Mahlzeitfenster
        schneidet, und legte den Block dann an den Anfang der Lücke. An einem
        freien Tag mit einer einzigen großen Lücke ab 08:00 ergab das
        Mittagessen um 08:40 und Abendessen um 09:20. Jeder einzelne Test war
        grün: es gab Blöcke, sie hießen richtig, die Decke war lückenlos.
        """
        decke = baue_decke(self.db, FREI_DI)
        nach_titel = {b["titel"]: b for b in decke["bloecke"]}
        mittag = nach_titel["Mittagessen"]["start"]
        abend = nach_titel["Abendessen"]["start"]
        self.assertGreaterEqual(mittag.hour, 11, "Mittagessen liegt vormittags")
        self.assertLessEqual(mittag.hour, 14)
        self.assertGreaterEqual(abend.hour, 17, "Abendessen liegt vor dem Nachmittag")
        self.assertLessEqual(abend.hour, 20)
        self.assertLess(mittag, abend, "Abendessen vor dem Mittagessen")

    def test_keine_endlosen_erholungsbloecke(self):
        """Ein Block über elf Stunden ist keine Planung, sondern ein Loch mit Namen."""
        decke = baue_decke(self.db, FREI_DI)
        for block in decke["bloecke"]:
            if block["art"] == "erholung":
                self.assertLessEqual(
                    block["minuten"], 5 * 60,
                    f"Erholungsblock über {block['minuten']} min: {block['titel']}",
                )

    def test_kurze_reste_heissen_puffer_nicht_freie_zeit(self):
        """Zehn Minuten zwischen zwei Terminen sind ein Übergang, keine Erholung."""
        self._termin(FREI_DI, (10, 0), (11, 0), titel="Erster")
        self._termin(FREI_DI, (11, 10), (12, 0), titel="Zweiter")
        decke = baue_decke(self.db, FREI_DI)
        for block in decke["bloecke"]:
            if block["minuten"] < 20:
                self.assertEqual(
                    block["art"], "puffer",
                    f"{block['minuten']} min heißen '{block['titel']}'",
                )


class TrainingTest(DeckeBasis):
    def test_training_wird_eingeplant(self):
        """Der Kalender reserviert die Zeit. Was trainiert wird, sagt die Fitness-App."""
        decke = baue_decke(self.db, FREI_DI)
        training = [b for b in decke["bloecke"] if b["art"] == "training"]
        self.assertEqual(len(training), 1)
        self.assertTrue(training[0]["begruendung"])

    def test_training_auch_am_arbeitstag(self):
        """★★ Der unangenehmste Fund aus der Klartext-Ausgabe.

        Der Ausschluss hing an `capacity.allow_physical`, das Level `geladen`
        oder `normal` verlangt. Ein Arbeitstag ohne jeden Check-in landet über
        die Tagestyp-Basis bei `geschont` und fiel damit durch: an keinem
        Arbeitstag wurde je Training geplant, begründet mit „heute körperlich
        nicht belastbar". Eine Behauptung über den Körper des Nutzers, erfunden
        aus dem Umstand, dass er arbeitet.
        """
        self._arbeitstag(ARBEIT_DI)
        decke = baue_decke(self.db, ARBEIT_DI)
        training = [b for b in decke["bloecke"] if b["art"] == "training"]
        self.assertEqual(len(training), 1, "Am Arbeitstag wird kein Training geplant")
        self.assertNotIn("belastbar", training[0]["begruendung"] or "")

    def test_kein_training_wenn_der_checkin_es_sagt(self):
        """Verzichtet wird auf Angabe, nicht auf Vermutung."""
        from backend.daily_energy import upsert_checkin
        upsert_checkin(self.db, FREI_DI, physical_ready=False)
        decke = baue_decke(self.db, FREI_DI)
        self.assertEqual([b for b in decke["bloecke"] if b["art"] == "training"], [])
        self.assertTrue(decke["hinweise"])
        self.assertIn("angegeben", decke["hinweise"][0])

    def test_gewohnheit_ersetzt_kein_training(self):
        """Ob eine Gewohnheit körperlich ist, steht nirgends. Sie dafür zu halten
        ist geraten, und der geratene Verzicht fällt nicht auf."""
        gewohnheit = Habit(name="Gitarre", weekly_minimum_hours=2)
        self.db.add(gewohnheit)
        self.db.commit()
        self.db.refresh(gewohnheit)
        self.db.add(HabitSession(
            habit_id=gewohnheit.id, week_iso="2027-W10", status="accepted",
            start=_wanduhr(FREI_DI, 17, 30), end=_wanduhr(FREI_DI, 18, 15),
        ))
        self.db.commit()
        decke = baue_decke(self.db, FREI_DI)
        self.assertTrue([b for b in decke["bloecke"] if b["art"] == "training"])

    def test_kein_training_an_krankem_tag(self):
        krank_tag = date(2027, 3, 23)
        self._termin(
            krank_tag, (0, 0), (23, 59), titel="Krank",
            kalender=DAYTYPE_CALENDARS["krank"], all_day=True,
        )
        decke = baue_decke(self.db, krank_tag)
        training = [b for b in decke["bloecke"] if b["art"] == "training"]
        self.assertEqual(training, [], "An einem Krankheitstag wird Training geplant")
        self.assertTrue(decke["hinweise"], "Der Verzicht wird nicht begründet")


class EventsUnberuehrtTest(DeckeBasis):
    def test_planen_schreibt_nicht_in_events(self):
        """★ Die Schutzzusage dieser Epoche.

        An `events` hängen die Tagestyp-Kalender und damit die 05:35-Weckkette.
        Weder das Bauen noch das Festschreiben der Decke darf dort eine Zeile
        anlegen.
        """
        self._arbeitstag(ARBEIT_DI)
        vorher = self.db.query(Event).count()
        baue_decke(self.db, ARBEIT_DI)
        festschreiben(self.db, ARBEIT_DI)
        self.assertEqual(self.db.query(Event).count(), vorher)

    def test_ganztaegiges_belegt_den_tag_nicht(self):
        """Urlaub beschreibt den Tag. Als Block würde er ihn zudecken."""
        self._termin(
            FREI_DI, (0, 0), (23, 59), titel="Urlaub",
            kalender=DAYTYPE_CALENDARS["urlaub"], all_day=True,
        )
        decke = baue_decke(self.db, FREI_DI)
        self.assertNotIn("Urlaub", [b["titel"] for b in decke["bloecke"]])
        self.assertTrue(decke["bloecke"])


class FestschreibenUndKorrigierenTest(DeckeBasis):
    def test_offener_tag_folgt_verschobenem_termin(self):
        ereignis = self._termin(FREI_DI, (10, 0), (11, 0), titel="Beweglich")
        ereignis.start = _wanduhr(FREI_DI, 14)
        ereignis.end = _wanduhr(FREI_DI, 15)
        self.db.commit()
        decke = baue_decke(self.db, FREI_DI)
        block = [b for b in decke["bloecke"] if b["titel"] == "Beweglich"][0]
        self.assertEqual(block["start"].hour, 14)

    def test_festgeschriebener_tag_folgt_nicht_mehr(self):
        """Ein gelebter Tag darf sich nicht ändern, weil ein Termin wandert."""
        ereignis = self._termin(FREI_DI, (10, 0), (11, 0), titel="Beweglich")
        festschreiben(self.db, FREI_DI)
        ereignis.start = _wanduhr(FREI_DI, 14)
        ereignis.end = _wanduhr(FREI_DI, 15)
        self.db.commit()
        decke = baue_decke(self.db, FREI_DI)
        block = [b for b in decke["bloecke"] if b["titel"] == "Beweglich"][0]
        self.assertEqual(block["start"].hour, 10, "Protokoll ist nachträglich gewandert")

    def test_festschreiben_ist_idempotent(self):
        self._termin(FREI_DI, (10, 0), (11, 0), titel="Termin")
        erste = festschreiben(self.db, FREI_DI)
        anzahl = self.db.query(TimeBlock).count()
        zweite = festschreiben(self.db, FREI_DI)
        self.assertEqual(self.db.query(TimeBlock).count(), anzahl)
        self.assertEqual(len(erste["bloecke"]), len(zweite["bloecke"]))

    def test_festschreiben_ueberschreibt_korrektur_nicht(self):
        festschreiben(self.db, FREI_DI)
        block = self.db.query(TimeBlock).filter(TimeBlock.art == "erholung").first()
        block_aendern(self.db, block.id, titel="Doch Hausputz", art="grundlast")
        festschreiben(self.db, FREI_DI)
        self.db.refresh(block)
        self.assertEqual(block.titel, "Doch Hausputz")
        self.assertEqual(block.art, "grundlast")

    def test_verschieben_macht_block_manuell(self):
        festschreiben(self.db, FREI_DI)
        block = self.db.query(TimeBlock).filter(TimeBlock.art == "erholung").first()
        neu = block_aendern(
            self.db, block.id,
            start=_wanduhr(FREI_DI, 20), ende=_wanduhr(FREI_DI, 21),
        )
        self.assertEqual(neu.quelle, "manuell")
        self.assertEqual(neu.start.hour, 20)

    def test_ende_vor_beginn_wird_abgelehnt(self):
        festschreiben(self.db, FREI_DI)
        block = self.db.query(TimeBlock).first()
        with self.assertRaises(ValueError):
            block_aendern(
                self.db, block.id,
                start=_wanduhr(FREI_DI, 20), ende=_wanduhr(FREI_DI, 19),
            )

    def test_verworfener_block_bleibt_erhalten(self):
        """★ Was geplant war und nicht stattfand, ist das Lernsignal.

        Ein echtes Löschen würde es jeden Abend wegwerfen.
        """
        festschreiben(self.db, FREI_DI)
        block = self.db.query(TimeBlock).filter(TimeBlock.art == "training").first()
        block_aendern(self.db, block.id, status="verworfen")
        self.db.refresh(block)
        self.assertEqual(block.status, "verworfen")
        self.assertIsNotNone(
            self.db.query(TimeBlock).filter(TimeBlock.id == block.id).first()
        )

    def test_verworfenes_zaehlt_nicht_zur_bilanz(self):
        festschreiben(self.db, FREI_DI)
        vorher = baue_decke(self.db, FREI_DI)["verplant_minuten"]
        block = self.db.query(TimeBlock).filter(TimeBlock.art == "training").first()
        block_aendern(self.db, block.id, status="verworfen")
        nachher = baue_decke(self.db, FREI_DI)["verplant_minuten"]
        self.assertLess(nachher, vorher)

    def test_rueckwirkend_fuer_einen_vergangenen_tag(self):
        """Verpasste Tage müssen nachtragbar sein, auch Wochen später."""
        vergangen = date(2026, 1, 13)
        decke = festschreiben(self.db, vergangen)
        self.assertTrue(decke["festgeschrieben"])
        self.assertTrue(ist_festgeschrieben(self.db, vergangen))
        zeile = block_anlegen(
            self.db, vergangen,
            _wanduhr(vergangen, 19), _wanduhr(vergangen, 20, 30),
            "erholung", "War beim Konzert",
        )
        self.assertEqual(zeile.status, "bestaetigt")
        self.assertEqual(zeile.quelle, "manuell")

    def test_manueller_block_ueberlebt_neuplanung_eines_offenen_tages(self):
        block_anlegen(
            self.db, FREI_DI,
            _wanduhr(FREI_DI, 16), _wanduhr(FREI_DI, 17),
            "grundlast", "Wäsche",
        )
        decke = baue_decke(self.db, FREI_DI)
        self.assertIn("Wäsche", [b["titel"] for b in decke["bloecke"]])
        # Und die Decke bleibt trotz des eingeschobenen Blocks lückenlos.
        for vorher, nachher in zip(decke["bloecke"], decke["bloecke"][1:]):
            self.assertEqual(vorher["ende"], nachher["start"])

    def test_unbekannte_art_wird_abgelehnt(self):
        with self.assertRaises(ValueError):
            block_anlegen(
                self.db, FREI_DI,
                _wanduhr(FREI_DI, 16), _wanduhr(FREI_DI, 17),
                "quatsch", "Unsinn",
            )


class NeuOrdnenTest(DeckeBasis):
    def _ids(self, tag=FREI_DI):
        return [b["id"] for b in baue_decke(self.db, tag)["bloecke"]]

    def _lueckenlos(self, tag=FREI_DI):
        decke = baue_decke(self.db, tag)
        for vorher, nachher in zip(decke["bloecke"], decke["bloecke"][1:]):
            self.assertEqual(
                vorher["ende"], nachher["start"],
                f"Loch zwischen {vorher['titel']} und {nachher['titel']}",
            )
        self.assertEqual(decke["bloecke"][0]["start"], decke["wach_von"])

    def test_reihenfolge_wird_uebernommen(self):
        festschreiben(self.db, FREI_DI)
        vorher = self._ids()
        gedreht = list(reversed(vorher))
        neu_ordnen(self.db, FREI_DI, gedreht)
        self.assertEqual(self._ids(), gedreht)

    def test_decke_bleibt_nach_dem_ordnen_lueckenlos(self):
        festschreiben(self.db, FREI_DI)
        neu_ordnen(self.db, FREI_DI, list(reversed(self._ids())))
        self._lueckenlos()

    def test_dauern_bleiben_erhalten(self):
        festschreiben(self.db, FREI_DI)
        vorher = {b["id"]: b["minuten"] for b in baue_decke(self.db, FREI_DI)["bloecke"]}
        neu_ordnen(self.db, FREI_DI, list(reversed(list(vorher))))
        nachher = {b["id"]: b["minuten"] for b in baue_decke(self.db, FREI_DI)["bloecke"]}
        self.assertEqual(vorher, nachher)

    def test_verworfener_block_reisst_kein_loch(self):
        """★ Der Fehler, den der erste Entwurf hatte.

        Verworfene Blöcke zählen nicht zur Bilanz. Sie deshalb beim Umsortieren
        zu überspringen liegt nahe, risse aber genau dort ein Loch in die Decke,
        wo etwas geplant war, und die Lückenlosigkeit ist die Kernzusage.
        """
        festschreiben(self.db, FREI_DI)
        ids = self._ids()
        mitte = ids[len(ids) // 2]
        block_aendern(self.db, mitte, status="verworfen")
        neu_ordnen(self.db, FREI_DI, list(reversed(ids)))
        self._lueckenlos()

    def test_teilliste_laesst_den_rest_hinten_stehen(self):
        festschreiben(self.db, FREI_DI)
        ids = self._ids()
        neu_ordnen(self.db, FREI_DI, [ids[-1]])
        self.assertEqual(self._ids()[0], ids[-1])
        self._lueckenlos()

    def test_offener_tag_wird_abgelehnt(self):
        """An einem offenen Tag kämen gespiegelte Blöcke wieder aus ihrer Quelle.
        Ein Umsortieren wäre dort ein lautloses Zurückspringen."""
        with self.assertRaises(ValueError):
            neu_ordnen(self.db, FREI_DI, [])

    def test_unbekannte_kennung_wird_abgelehnt(self):
        festschreiben(self.db, FREI_DI)
        with self.assertRaises(ValueError):
            neu_ordnen(self.db, FREI_DI, ["gibt-es-nicht"])

    def test_fremder_tag_wird_nicht_angefasst(self):
        """Eine Kennung von gestern darf den heutigen Tag nicht umbauen."""
        festschreiben(self.db, FREI_DI)
        anderer = FREI_DI + timedelta(days=1)
        festschreiben(self.db, anderer)
        fremde = self._ids(anderer)[0]
        with self.assertRaises(ValueError):
            neu_ordnen(self.db, FREI_DI, [fremde])


class ZeitEingangTest(DeckeBasis):
    def _messung(self, tag, von, bis, art="training", **kw):
        roh = {
            "quelle": "uhr", "art": art,
            "start": _wanduhr(tag, *von), "ende": _wanduhr(tag, *bis),
        }
        roh.update(kw)
        return roh

    def test_ohne_token_ist_der_eingang_zu(self):
        """Fail-closed. Ein offener Schreibpfad nimmt Bewegungsprofile entgegen."""
        settings.ZEIT_INGEST_TOKEN = ""
        self.assertFalse(zeit_ingest.ingest_erlaubt())
        self.assertFalse(zeit_ingest.pruefe_token("irgendwas"))
        self.assertFalse(zeit_ingest.pruefe_token(""))
        self.assertFalse(zeit_ingest.pruefe_token(None))

    def test_token_wird_geprueft(self):
        settings.ZEIT_INGEST_TOKEN = "geheim"
        self.assertTrue(zeit_ingest.pruefe_token("geheim"))
        self.assertFalse(zeit_ingest.pruefe_token("falsch"))
        self.assertFalse(zeit_ingest.pruefe_token(None))

    def test_messung_wird_aufgenommen(self):
        _, vorgang = eintrag_aufnehmen(
            self.db, self._messung(FREI_DI, (18, 0), (19, 0), extern_id="a1")
        )
        self.db.commit()
        self.assertEqual(vorgang, "neu")
        self.assertEqual(self.db.query(ZeitIst).count(), 1)

    def test_gleiche_messung_zweimal_erzeugt_kein_doppel(self):
        """Ohne Idempotenz hätte ein Tag nach einem Verbindungsabbruch mehr als
        24 Stunden Ist, und die Bilanz wäre still falsch."""
        roh = self._messung(FREI_DI, (18, 0), (19, 0), extern_id="a1")
        eintrag_aufnehmen(self.db, roh)
        self.db.commit()
        _, vorgang = eintrag_aufnehmen(self.db, dict(roh))
        self.db.commit()
        self.assertEqual(vorgang, "aktualisiert")
        self.assertEqual(self.db.query(ZeitIst).count(), 1)

    def test_zeitraum_ueber_mitternacht_wird_geteilt(self):
        """Ein Schlaf von 23:10 bis 06:40 gehört zu zwei Tagen. Ungeteilt fehlte
        er der Bilanz des Folgetags vollständig."""
        roh = {
            "quelle": "uhr", "art": "schlaf", "extern_id": "nacht-1",
            "start": _wanduhr(FREI_DI, 23, 10),
            "ende": _wanduhr(FREI_DI + timedelta(days=1), 6, 40),
        }
        eintrag_aufnehmen(self.db, roh)
        self.db.commit()
        zeilen = self.db.query(ZeitIst).order_by(ZeitIst.start).all()
        self.assertEqual(len(zeilen), 2)
        self.assertEqual(zeilen[0].date, FREI_DI)
        self.assertEqual(zeilen[1].date, FREI_DI + timedelta(days=1))
        # Und die Teilung bleibt idempotent.
        eintrag_aufnehmen(self.db, dict(roh))
        self.db.commit()
        self.assertEqual(self.db.query(ZeitIst).count(), 2)

    def test_zonenbehaftete_zeit_wird_auf_wanduhr_gebracht(self):
        """★ Eine Uhr liefert ISO-8601 mit Zone. Ohne Umrechnung läge im Sommer
        jeder Eintrag zwei Stunden daneben, plausibel aussehend."""
        from zoneinfo import ZoneInfo
        roh = {
            "quelle": "uhr", "art": "training", "extern_id": "tz-1",
            # 16:00 UTC im Sommer ist 18:00 Berliner Wanduhr.
            "start": datetime(2027, 7, 6, 16, 0, tzinfo=ZoneInfo("UTC")),
            "ende": datetime(2027, 7, 6, 17, 0, tzinfo=ZoneInfo("UTC")),
        }
        eintrag_aufnehmen(self.db, roh)
        self.db.commit()
        zeile = self.db.query(ZeitIst).first()
        self.assertEqual(zeile.start.hour, 18)
        self.assertIsNone(zeile.start.tzinfo)

    def test_iso_zeichenkette_wird_angenommen(self):
        """★ Über HTTP kommt JSON, und JSON kennt keinen Zeittyp.

        Die erste Fassung verlangte ein `datetime` und lehnte damit jeden echten
        Aufruf ab, während sie mit 200 und `aufgenommen: 0` antwortete. Das sah
        aus wie ein Tag ohne Aktivität, nicht wie ein Fehler.
        """
        for start, ende, hinweis in [
            ("2027-03-09T18:00:00", "2027-03-09T19:00:00", "ohne Zone"),
            ("2027-07-06T16:00:00Z", "2027-07-06T17:00:00Z", "mit Z"),
            ("2027-07-06T18:00:00+02:00", "2027-07-06T19:00:00+02:00", "mit Versatz"),
        ]:
            with self.subTest(hinweis=hinweis):
                zeile, vorgang = eintrag_aufnehmen(self.db, {
                    "quelle": "uhr", "art": "training", "extern_id": f"iso-{hinweis}",
                    "start": start, "ende": ende,
                })
                self.db.commit()
                self.assertEqual(vorgang, "neu")
                self.assertIsNone(zeile.start.tzinfo, "Zone nicht abgestreift")
        # Beide Sommer-Angaben meinen denselben Moment: 18:00 Berliner Wanduhr.
        sommer = self.db.query(ZeitIst).filter(ZeitIst.date == date(2027, 7, 6)).all()
        self.assertEqual({z.start.hour for z in sommer}, {18})

    def test_unlesbare_zeichenkette_wird_benannt(self):
        with self.assertRaises(IngestFehler) as fehler:
            eintrag_aufnehmen(self.db, {
                "quelle": "uhr", "art": "training",
                "start": "gestern abend", "ende": "2027-03-09T19:00:00",
            })
        self.assertIn("start", str(fehler.exception))

    def test_roh_typ_wird_uebersetzt(self):
        roh = {
            "quelle": "uhr", "roh_typ": "strength_training", "extern_id": "r1",
            "start": _wanduhr(FREI_DI, 18), "ende": _wanduhr(FREI_DI, 19),
        }
        eintrag_aufnehmen(self.db, roh)
        self.db.commit()
        self.assertEqual(self.db.query(ZeitIst).first().art, "training")

    def test_unbrauchbare_eingaben_werden_benannt(self):
        for roh, hinweis in [
            ({"art": "training", "start": _wanduhr(FREI_DI, 1), "ende": _wanduhr(FREI_DI, 2)},
             "fehlende Quelle"),
            ({"quelle": "uhr", "art": "training",
              "start": _wanduhr(FREI_DI, 5), "ende": _wanduhr(FREI_DI, 4)},
             "Ende vor Beginn"),
            ({"quelle": "uhr", "art": "schwimmen-im-datensee",
              "start": _wanduhr(FREI_DI, 1), "ende": _wanduhr(FREI_DI, 2)},
             "unbekannte Art"),
        ]:
            with self.subTest(hinweis=hinweis):
                with self.assertRaises(IngestFehler):
                    eintrag_aufnehmen(self.db, roh)

    def test_stapel_meldet_gute_und_schlechte(self):
        """Ein unbrauchbarer Eintrag darf die guten nicht mitreißen, aber er darf
        auch nicht stillschweigend verschwinden."""
        ergebnis = stapel_aufnehmen(self.db, [
            self._messung(FREI_DI, (18, 0), (19, 0), extern_id="g1"),
            {"quelle": "uhr"},  # unbrauchbar
            self._messung(FREI_DI, (20, 0), (21, 0), extern_id="g2"),
        ])
        self.assertEqual(ergebnis["aufgenommen"], 2)
        self.assertEqual(ergebnis["abgelehnt"], 1)
        self.assertEqual(ergebnis["fehler"][0]["index"], 1)


class AbgleichTest(DeckeBasis):
    def test_ohne_messungen_meldet_der_abgleich_das_ehrlich(self):
        ergebnis = abgleich(self.db, FREI_DI)
        self.assertFalse(ergebnis["hat_messungen"])
        self.assertEqual(ergebnis["ist_je_art"], {})

    def test_messung_wird_dem_passenden_block_zugeordnet(self):
        festschreiben(self.db, FREI_DI)
        training = self.db.query(TimeBlock).filter(TimeBlock.art == "training").first()
        eintrag_aufnehmen(self.db, {
            "quelle": "uhr", "art": "training", "extern_id": "t1",
            "start": training.start, "ende": training.end,
        })
        self.db.commit()
        ergebnis = abgleich(self.db, FREI_DI)
        self.assertTrue(ergebnis["hat_messungen"])
        treffer = [z for z in ergebnis["zuordnung"] if z["block_id"] == training.id]
        self.assertEqual(len(treffer), 1)
        self.assertTrue(treffer[0]["passt"])

    def test_abweichung_wird_beziffert(self):
        festschreiben(self.db, FREI_DI)
        eintrag_aufnehmen(self.db, {
            "quelle": "uhr", "art": "training", "extern_id": "t2",
            "start": _wanduhr(FREI_DI, 9), "ende": _wanduhr(FREI_DI, 9, 30),
        })
        self.db.commit()
        ergebnis = abgleich(self.db, FREI_DI)
        self.assertIn("training", ergebnis["abweichung_je_art"])

    def test_abgleich_aendert_keine_zeile(self):
        """Eine Auswertung, die Daten verändert, macht aus einem Denkfehler einen
        Datenfehler."""
        festschreiben(self.db, FREI_DI)
        vorher = {(b.id, b.start, b.art) for b in self.db.query(TimeBlock).all()}
        abgleich(self.db, FREI_DI)
        nachher = {(b.id, b.start, b.art) for b in self.db.query(TimeBlock).all()}
        self.assertEqual(vorher, nachher)


class RueckblickTest(DeckeBasis):
    def test_ausgefallenes_wird_getrennt_gezaehlt(self):
        festschreiben(self.db, FREI_DI)
        training = self.db.query(TimeBlock).filter(TimeBlock.art == "training").first()
        block_aendern(self.db, training.id, status="verworfen")
        ergebnis = rueckblick(self.db, FREI_DI, FREI_DI)
        self.assertIn("training", ergebnis["ausgefallen_je_art"])
        self.assertNotIn("training", ergebnis["gelebt_je_art"])

    def test_schnitt_je_tag_ueber_mehrere_tage(self):
        festschreiben(self.db, FREI_DI)
        festschreiben(self.db, FREI_DI + timedelta(days=1))
        ergebnis = rueckblick(self.db, FREI_DI, FREI_DI + timedelta(days=1))
        self.assertEqual(ergebnis["tage"], 2)
        self.assertEqual(ergebnis["festgeschriebene_tage"], 2)
        self.assertTrue(ergebnis["schnitt_je_tag"])


class TrainingAnFitnessTest(DeckeBasis):
    """Die Übergabe an die Fitness-App: Zeit von hier, Inhalt von dort.

    Geprüft wird die Grenze, nicht die Fitness-App. Entscheidend ist, dass die
    **Dauer des Blocks** hinübergeht: ohne sie rät die andere Seite, und ein
    geratener Zuschnitt sieht genauso aus wie ein gemessener.
    """

    def test_dauer_des_blocks_geht_hinueber(self):
        from unittest import mock

        from backend import cross_app

        decke = baue_decke(self.db, FREI_DI)
        block = next(b for b in decke["bloecke"] if b["art"] == "training")

        with mock.patch.object(cross_app, "_get_json", return_value={
            "minuten": block["minuten"], "minuten_quelle": "angefragt",
            "titel": "Drücken", "uebungen": [],
        }) as geholt:
            ergebnis = cross_app.get_training_jetzt(block["minuten"])

        url = geholt.call_args[0][0]
        self.assertIn(f"minuten={block['minuten']}", url)
        # ★ Der Kalender weiß als einziger, dass die Zahl gemessen ist.
        self.assertEqual(ergebnis["minuten_quelle"], "kalender")

    def test_ohne_fitness_bleibt_die_zeit_stehen(self):
        """Fällt die Nachbar-App aus, ist die reservierte Zeit trotzdem gültig.

        Sie ist die Zusage dieses Dienstes; der Inhalt ist die der anderen
        Seite. Ein Ausfall dort darf den Block hier nicht verschwinden lassen.
        """
        from unittest import mock

        from backend import cross_app

        with mock.patch.object(cross_app, "get_training_jetzt", return_value=None):
            from backend.routers.tagesdecke import training_jetzt

            antwort = training_jetzt(datum=FREI_DI, db=self.db)

        self.assertIsNotNone(antwort["block"])
        self.assertGreater(antwort["block"]["minuten"], 0)
        self.assertIsNone(antwort["training"])
        self.assertIn("nicht erreichbar", antwort["hinweis"])

    def test_ohne_trainingsblock_wird_nicht_gefragt(self):
        """Kein Training geplant heißt: die Fitness-App gar nicht erst stören."""
        from unittest import mock

        from backend import cross_app
        from backend.routers.tagesdecke import training_jetzt

        decke = baue_decke(self.db, FREI_DI)
        training = next(b for b in decke["bloecke"] if b["art"] == "training")
        festschreiben(self.db, FREI_DI)
        zeile = (
            self.db.query(TimeBlock)
            .filter(TimeBlock.date == FREI_DI, TimeBlock.art == "training")
            .first()
        )
        block_aendern(self.db, zeile.id, status="verworfen")

        with mock.patch.object(cross_app, "get_training_jetzt") as gefragt:
            antwort = training_jetzt(datum=FREI_DI, db=self.db)

        gefragt.assert_not_called()
        self.assertIsNone(antwort["block"])
        self.assertIn("keine Trainingszeit", antwort["hinweis"])
        self.assertTrue(training)  # es gab ihn vor dem Verwerfen


class MandantenTrennungTest(DeckeBasis):
    def test_neue_modelle_sind_streng_gescoped(self):
        """Ein neues Modell, das nicht in STRICT_TENANT_MODELS steht, ist für
        alle Mandanten sichtbar. Dieser Test hält die Registrierung fest."""
        from backend.tenant import STRICT_TENANT_MODELS
        self.assertIn(TimeBlock, STRICT_TENANT_MODELS)
        self.assertIn(ZeitIst, STRICT_TENANT_MODELS)


if __name__ == "__main__":
    unittest.main()
