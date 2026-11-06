"""Gemessene Zeit belegt ein Wochenziel: Zuschnitt, Bilanz, Quellen, Token.

Was hier geprueft wird und warum gerade das:

* **Der Zuschnitt**, weil er die einzige Stelle ist, an der aus rohen Meldungen
  eine Zahl wird. Ein Fehler darin ist unsichtbar: die Minuten sind plausibel.
* **Dass `offen` nicht aufs Ziel zaehlt.** Diese Regel steht in keinem Feldnamen
  (`bestaetigt` nennt die Herkunft, nicht die Geltung), also braucht sie einen
  Test, dessen Name sie ausspricht.
* **Die Bindung des Melder-Tokens an seine Quelle**, und zwar im Stapel. Eine
  Pruefung, die nur den ersten Eintrag ansieht, laesst den Rest fremde Quellen
  schreiben und sieht dabei aus wie eine Pruefung.
* **Dass `/metrics` keine Taetigkeiten preisgibt.** Der Endpunkt ist ohne
  Anmeldung erreichbar, und der Laptop-Melder beruehrt spaeter Fenstertitel.
"""

import os
import unittest
from datetime import date, datetime, timedelta

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-zeitmessung.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.config import settings
from backend.database import Base, SessionLocal, engine
from backend.main import app, create_jwt_token
from backend.models import Habit, HabitQuelle, ZeitIst
from backend.wochenbilanz import gemessene_minuten
from backend.zeitschnitt import (
    LUECKE_MINUTEN,
    MINDEST_MINUTEN,
    MINDEST_MINUTEN_GENAU,
    mindestdauer,
    zuschneiden,
)


class _Messung:
    """Ein Stellvertreter fuer `ZeitIst`, so weit der Zuschnitt ihn braucht.

    Bewusst kein ORM-Objekt: der Zuschnitt soll ohne Datenbank pruefbar sein.
    Genau daran erkennt man, ob er wirklich eine reine Rechnung ist.
    """

    def __init__(self, von: str, bis: str, quelle="laptop", taetigkeit="Minecraft",
                 art="spiel", genau=False):
        self.start = datetime.fromisoformat(von)
        self.ende = datetime.fromisoformat(bis)
        self.quelle = quelle
        self.taetigkeit = taetigkeit
        self.art = art
        self.genau = genau


class ZuschnittTest(unittest.TestCase):
    def test_kurze_luecke_wird_verbunden(self):
        stuecke = zuschneiden([
            _Messung("2026-10-25T18:00", "2026-10-25T18:20"),
            _Messung("2026-10-25T18:30", "2026-10-25T18:50"),
        ])
        self.assertEqual(len(stuecke), 1)
        self.assertEqual(stuecke[0].minuten, 50)

    def test_lange_luecke_trennt(self):
        stuecke = zuschneiden([
            _Messung("2026-10-25T18:00", "2026-10-25T18:20"),
            _Messung("2026-10-25T19:00", "2026-10-25T19:20"),
        ])
        self.assertEqual([s.minuten for s in stuecke], [20, 20])

    def test_luecke_genau_auf_der_schwelle_verbindet_noch(self):
        """Die Grenze gehoert in einen Test, sonst verschiebt sie sich beim Umbau."""
        ende = datetime.fromisoformat("2026-10-25T18:20")
        weiter = ende + timedelta(minutes=LUECKE_MINUTEN)
        stuecke = zuschneiden([
            _Messung("2026-10-25T18:00", "2026-10-25T18:20"),
            _Messung(weiter.isoformat(), (weiter + timedelta(minutes=20)).isoformat()),
        ])
        self.assertEqual(len(stuecke), 1)

    def test_zu_kurzes_wird_verworfen(self):
        stuecke = zuschneiden([_Messung("2026-10-25T18:00", "2026-10-25T18:05")])
        self.assertEqual(stuecke, [])

    def test_verbinden_laeuft_vor_verwerfen(self):
        """Drei kurze Abschnitte mit kurzen Luecken sind ein langes Tun.

        Die Reihenfolge ist der Kern des Zuschnitts. Wer erst verwirft, loescht
        alle drei und meldet null, und zwar ohne Fehler: die Bilanz zeigt dann
        Nichtstun an einem Abend, an dem jemand eine Stunde gespielt hat.
        """
        stuecke = zuschneiden([
            _Messung("2026-10-25T18:00", "2026-10-25T18:04"),
            _Messung("2026-10-25T18:10", "2026-10-25T18:14"),
            _Messung("2026-10-25T18:20", "2026-10-25T18:24"),
        ])
        self.assertEqual(len(stuecke), 1)
        self.assertEqual(stuecke[0].minuten, 24)

    def test_genauer_melder_darf_eine_minute(self):
        kurz = _Messung("2026-10-25T18:00", "2026-10-25T18:02", quelle="schach",
                        taetigkeit="schach", genau=True)
        self.assertEqual(len(zuschneiden([kurz])), 1)
        kurz.genau = False
        self.assertEqual(zuschneiden([kurz]), [])

    def test_mindestdauer_hat_eine_stelle(self):
        self.assertEqual(mindestdauer(True), MINDEST_MINUTEN_GENAU)
        self.assertEqual(mindestdauer(False), MINDEST_MINUTEN)

    def test_verschiedene_taetigkeiten_verschmelzen_nicht(self):
        stuecke = zuschneiden([
            _Messung("2026-10-25T18:00", "2026-10-25T18:20", taetigkeit="Minecraft"),
            _Messung("2026-10-25T18:22", "2026-10-25T18:42", taetigkeit="Terraria"),
        ])
        self.assertEqual(len(stuecke), 2)

    def test_ueberlappung_zieht_das_ende_nicht_zurueck(self):
        """Eine enthaltene Messung darf das offene Stueck nicht verkuerzen.

        Tritt echt auf, sobald zwei Melder dasselbe sehen oder ein Melder einen
        Stand erneut schickt (Schach meldet den Gesamtstand, keinen Zuwachs).
        """
        stuecke = zuschneiden([
            _Messung("2026-10-25T18:00", "2026-10-25T19:00"),
            _Messung("2026-10-25T18:10", "2026-10-25T18:20"),
        ])
        self.assertEqual(len(stuecke), 1)
        self.assertEqual(stuecke[0].minuten, 60)


class BilanzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=engine)

    def setUp(self):
        self.db = SessionLocal()
        self.db.query(ZeitIst).delete()
        self.db.query(HabitQuelle).delete()
        self.db.query(Habit).delete()
        self.db.commit()
        self.habit = Habit(name="Schach", category="hobby", target_hours_per_week=0.5)
        self.db.add(self.habit)
        self.db.commit()
        # Ein Montag, damit das Wochenfenster nicht vom Testtag abhaengt.
        self.woche = date(2026, 10, 19)

    def tearDown(self):
        self.db.close()

    def _quelle(self, dienst="schach", kennung=""):
        q = HabitQuelle(habit_id=self.habit.id, dienst=dienst, kennung=kennung)
        self.db.add(q)
        self.db.commit()
        return q

    def _messung(self, minuten=40, tag=None, quelle="schach", taetigkeit="",
                 bestaetigt="automatisch", genau=True, stunde=18):
        tag = tag or self.woche
        start = datetime.combine(tag, datetime.min.time()).replace(hour=stunde)
        zeile = ZeitIst(
            date=tag, start=start, end=start + timedelta(minutes=minuten),
            art="spiel", quelle=quelle, taetigkeit=taetigkeit,
            extern_id=f"{quelle}-{tag}-{stunde}-{minuten}-{bestaetigt}",
            bestaetigt=bestaetigt, genau=genau,
        )
        self.db.add(zeile)
        self.db.commit()
        return zeile

    def test_ohne_quelle_keine_minuten_aber_auch_kein_fehler(self):
        """Ein Ziel ohne Melder ist 'nur Handarbeit', und das ist eine Aussage."""
        self._messung()
        bilanz = gemessene_minuten(self.db, self.habit, self.woche)
        self.assertEqual(bilanz["minuten"], 0)
        self.assertEqual(bilanz["quellen"], [])

    def test_messung_zaehlt_ueber_die_quelle_aufs_ziel(self):
        self._quelle()
        self._messung(minuten=40)
        bilanz = gemessene_minuten(self.db, self.habit, self.woche)
        self.assertEqual(bilanz["minuten"], 40)
        self.assertEqual(bilanz["quellen"], ["schach"])

    def test_offene_messung_zaehlt_nicht_aufs_ziel(self):
        """⚠️ Die Regel, die in keinem Feldnamen steht.

        `bestaetigt` sagt die Herkunft (`automatisch`/`offen`/`manuell`), nicht die
        Geltung. Dass `offen` nicht zaehlt, ist eine Entscheidung und steht an einer
        Stelle: `wochenbilanz.ZAEHLENDE_STAENDE`. Dieser Test ist ihr Gegenstueck.
        """
        self._quelle()
        self._messung(minuten=40, bestaetigt="offen")
        bilanz = gemessene_minuten(self.db, self.habit, self.woche)
        self.assertEqual(bilanz["minuten"], 0)
        self.assertEqual(bilanz["offene_minuten"], 40)

    def test_manuelle_messung_zaehlt(self):
        self._quelle()
        self._messung(minuten=30, bestaetigt="manuell")
        self.assertEqual(gemessene_minuten(self.db, self.habit, self.woche)["minuten"], 30)

    def test_spaeter_angelegte_quelle_repariert_die_vergangenheit(self):
        """Die Zuordnung wird beim Abfragen aufgeloest, nicht beim Schreiben."""
        self._messung(minuten=40)
        self.assertEqual(gemessene_minuten(self.db, self.habit, self.woche)["minuten"], 0)
        self._quelle()
        self.assertEqual(gemessene_minuten(self.db, self.habit, self.woche)["minuten"], 40)

    def test_fremde_kennung_derselben_quelle_zaehlt_nicht(self):
        """`dienst IN (...) AND kennung IN (...)` waere hier falsch.

        Mit den Quellen `lernen/mathe` und `schach/` zoege eine Filterung ueber zwei
        getrennte Listen auch `lernen/` und `schach/mathe` herein, also Messungen,
        die zu keinem Ziel gehoeren. Gefiltert wird deshalb auf Paare.
        """
        self._quelle(dienst="lernen", kennung="mathe")
        self._messung(minuten=40, quelle="lernen", taetigkeit="franzoesisch")
        self.assertEqual(gemessene_minuten(self.db, self.habit, self.woche)["minuten"], 0)
        self._messung(minuten=40, quelle="lernen", taetigkeit="mathe", stunde=20)
        self.assertEqual(gemessene_minuten(self.db, self.habit, self.woche)["minuten"], 40)

    def test_andere_woche_zaehlt_nicht_mit(self):
        self._quelle()
        self._messung(minuten=40, tag=self.woche - timedelta(days=3))
        self.assertEqual(gemessene_minuten(self.db, self.habit, self.woche)["minuten"], 0)

    def test_mehrere_quellen_je_ziel_summieren(self):
        """Der Grund fuer die eigene Tabelle: 'Lesen' hat Leser UND Timer."""
        self._quelle(dienst="bibliothek")
        self._quelle(dienst="timer")
        self._messung(minuten=25, quelle="bibliothek", stunde=10)
        self._messung(minuten=35, quelle="timer", stunde=20)
        self.assertEqual(gemessene_minuten(self.db, self.habit, self.woche)["minuten"], 60)


class QuellenApiTest(unittest.TestCase):
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

    def _habit(self, name: str, kategorie: str = "hobby") -> str:
        antwort = self.client.post(
            "/api/habits",
            json={"name": name, "category": kategorie, "target_hours_per_week": 0.5},
            headers=self.auth,
        )
        self.assertEqual(antwort.status_code, 201, antwort.text)
        return antwort.json()["id"]

    def test_hobby_ist_ueber_die_api_anlegbar(self):
        """Bis zum 2026-10-25 lehnte das Schema `hobby` ab, waehrend das uebrige
        Haus die Kategorie kannte. Der Habit 'Schach' stand in der Datenbank und
        war ueber die API weder anlegbar noch aenderbar."""
        habit_id = self._habit("Schach-Pruefung", "hobby")
        antwort = self.client.put(
            f"/api/habits/{habit_id}", json={"category": "sport"}, headers=self.auth
        )
        self.assertEqual(antwort.status_code, 200, antwort.text)
        self.assertEqual(antwort.json()["category"], "sport")

    def test_quelle_anlegen_lesen_entfernen(self):
        habit_id = self._habit("Quellen-Pruefung")
        angelegt = self.client.post(
            f"/api/habits/{habit_id}/quellen",
            json={"dienst": "lernen", "kennung": "it-zertifikate"},
            headers=self.auth,
        )
        self.assertEqual(angelegt.status_code, 201, angelegt.text)
        gelesen = self.client.get(f"/api/habits/{habit_id}/quellen", headers=self.auth)
        self.assertEqual([q["kennung"] for q in gelesen.json()], ["it-zertifikate"])
        weg = self.client.delete(
            f"/api/habits/{habit_id}/quellen/{angelegt.json()['id']}", headers=self.auth
        )
        self.assertEqual(weg.status_code, 204)
        self.assertEqual(self.client.get(
            f"/api/habits/{habit_id}/quellen", headers=self.auth).json(), [])

    def test_quelle_ohne_kennung_ist_der_leerstring(self):
        """Nicht None: die Spalte ist NOT NULL, weil SQLite zwei NULL nicht fuer
        gleich haelt und die Eindeutigkeitsbedingung damit wirkungslos waere.

        Eigener Dienstname je Test. Eine Quelle gehoert genau einem Ziel, also
        teilen sich zwei Tests keinen: sonst haengt das Ergebnis an der
        Ausfuehrungsreihenfolge, und die ist alphabetisch, nicht beabsichtigt.
        """
        habit_id = self._habit("Leerkennung")
        antwort = self.client.post(
            f"/api/habits/{habit_id}/quellen", json={"dienst": "leerkennung"},
            headers=self.auth,
        )
        self.assertEqual(antwort.status_code, 201, antwort.text)
        self.assertEqual(antwort.json()["kennung"], "")

    def test_eine_quelle_gehoert_genau_einem_ziel(self):
        erstes = self._habit("Erstes Ziel")
        zweites = self._habit("Zweites Ziel")
        self.client.post(
            f"/api/habits/{erstes}/quellen", json={"dienst": "einziges"}, headers=self.auth
        )
        doppelt = self.client.post(
            f"/api/habits/{zweites}/quellen", json={"dienst": "einziges"}, headers=self.auth
        )
        self.assertEqual(doppelt.status_code, 409)
        self.assertIn("Erstes Ziel", doppelt.json()["detail"])

    def test_dieselbe_quelle_am_selben_ziel_ist_kein_fehler(self):
        """Idempotent, damit ein wiederholter Einrichtungslauf nichts bricht.

        Geprueft wird die Kennung, nicht der Status: dass der zweite Aufruf
        dieselbe Zeile liefert statt einer zweiten, ist die Zusicherung.
        """
        habit_id = self._habit("Wiederholung")
        erst = self.client.post(
            f"/api/habits/{habit_id}/quellen", json={"dienst": "timer"}, headers=self.auth
        )
        nochmal = self.client.post(
            f"/api/habits/{habit_id}/quellen", json={"dienst": "timer"}, headers=self.auth
        )
        self.assertIn(nochmal.status_code, (200, 201), nochmal.text)
        self.assertEqual(nochmal.json()["id"], erst.json()["id"])
        alle = self.client.get(f"/api/habits/{habit_id}/quellen", headers=self.auth)
        self.assertEqual(len(alle.json()), 1)

    def test_quellen_brauchen_anmeldung(self):
        antwort = self.client.get("/api/habits/egal/quellen")
        self.assertIn(antwort.status_code, (401, 403))


class MelderTokenTest(unittest.TestCase):
    """Der Token gilt fuer SEINE Quelle, und das wird im Stapel geprueft."""

    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def setUp(self):
        self._einzel = settings.ZEIT_INGEST_TOKEN
        self._mehrere = settings.ZEIT_INGEST_TOKENS
        settings.ZEIT_INGEST_TOKEN = ""
        settings.ZEIT_INGEST_TOKENS = "schach:geheim-schach,laptop:geheim-laptop"

    def tearDown(self):
        settings.ZEIT_INGEST_TOKEN = self._einzel
        settings.ZEIT_INGEST_TOKENS = self._mehrere

    def _senden(self, token, quellen, bestaetigt="automatisch"):
        eintraege = []
        for index, quelle in enumerate(quellen):
            start = datetime(2026, 10, 19, 18 + index)
            eintraege.append({
                "quelle": quelle,
                "start": start.isoformat(),
                "ende": (start + timedelta(minutes=40)).isoformat(),
                "art": "spiel",
                "taetigkeit": "Minecraft",
                "bestaetigt": bestaetigt,
                "genau": False,
                "extern_id": f"{quelle}-{index}",
            })
        kopf = {"X-Zeit-Token": token} if token else {}
        return self.client.post("/api/zeit/ingest", json={"eintraege": eintraege}, headers=kopf)

    def test_eigene_quelle_wird_angenommen(self):
        antwort = self._senden("geheim-schach", ["schach"])
        self.assertEqual(antwort.status_code, 200, antwort.text)
        self.assertEqual(antwort.json()["aufgenommen"], 1)

    def test_fremde_quelle_wird_abgelehnt(self):
        antwort = self._senden("geheim-schach", ["laptop"])
        self.assertEqual(antwort.status_code, 401)
        self.assertIn("laptop", antwort.json()["detail"])

    def test_fremde_quelle_im_stapel_lehnt_den_ganzen_stapel_ab(self):
        """Der eigentliche Grund fuer die Pruefung je Eintrag.

        Wer nur den ersten Eintrag prueft, laesst den Rest fremde Quellen
        schreiben, und zwar mit HTTP 200. Geprueft wird auch, dass **nichts**
        geschrieben wurde: eine Teilannahme saehe wie ein Datenproblem aus und
        nicht wie eines mit den Rechten.
        """
        antwort = self._senden("geheim-schach", ["schach", "laptop"])
        self.assertEqual(antwort.status_code, 401)
        db = SessionLocal()
        try:
            self.assertEqual(db.query(ZeitIst).filter(ZeitIst.quelle == "laptop").count(), 0)
        finally:
            db.close()

    def test_unbekannte_quelle_hat_keinen_rueckfall_auf_den_sammeltoken(self):
        """Ein gesetzter Alt-Token darf die Bindung nicht stillschweigend aufheben."""
        settings.ZEIT_INGEST_TOKEN = "alter-sammeltoken"
        antwort = self._senden("alter-sammeltoken", ["gamehost"])
        self.assertEqual(antwort.status_code, 401)

    def test_alter_sammeltoken_gilt_solange_keine_melder_eingerichtet_sind(self):
        """Uebergang: das Einrichten der Melder-Token ist ein eigener Schritt."""
        settings.ZEIT_INGEST_TOKENS = ""
        settings.ZEIT_INGEST_TOKEN = "alter-sammeltoken"
        antwort = self._senden("alter-sammeltoken", ["irgendwas"])
        self.assertEqual(antwort.status_code, 200, antwort.text)

    def test_ohne_jeden_token_ist_der_eingang_aus(self):
        settings.ZEIT_INGEST_TOKENS = ""
        settings.ZEIT_INGEST_TOKEN = ""
        self.assertEqual(self._senden("beliebig", ["schach"]).status_code, 403)

    def test_unbekannter_stand_wird_benannt_statt_verschluckt(self):
        """Ein unbrauchbarer Eintrag wird abgelehnt UND benannt, der Stapel laeuft.

        Das ist die bestehende Bauart des Eingangs und bewusst anders als beim
        falschen Token: ein Geraet liefert einen Tag am Stueck, und ein Abbruch am
        zwanzigsten Eintrag verloere die neunzehn guten. Stilles Ueberspringen waere
        dabei das Schlimmste, denn dann haelt man die Bilanz fuer vollstaendig -
        also beides, schreiben und melden.
        """
        antwort = self._senden("geheim-schach", ["schach"], bestaetigt="vielleicht")
        self.assertEqual(antwort.status_code, 200, antwort.text)
        daten = antwort.json()
        self.assertEqual(daten["aufgenommen"], 0)
        self.assertEqual(daten["abgelehnt"], 1)
        self.assertIn("vielleicht", daten["fehler"][0]["meldung"])

    def test_leerer_stapel_mit_falschem_token_wird_abgelehnt(self):
        """Der Fall, der die Pruefung je Eintrag allein entwertet haette.

        Ohne Nutzlast laeuft keine Quellen-Pruefung, der Aufruf kam deshalb mit 200
        zurueck - und wer die Gueltigkeit seines Tokens so probiert, bekommt eine
        falsche Zusage. Die Authentisierung (`token_bekannt`) laeuft seither vor der
        Berechtigung und unabhaengig von der Nutzlast.
        """
        kopf = {"X-Zeit-Token": "falsch"}
        antwort = self.client.post("/api/zeit/ingest", json={"eintraege": []}, headers=kopf)
        self.assertEqual(antwort.status_code, 401)

    def test_leerer_stapel_mit_gueltigem_token_ist_in_ordnung(self):
        """Gegenprobe, damit die Haerte von oben nicht einen gueltigen Aufruf trifft."""
        kopf = {"X-Zeit-Token": "geheim-schach"}
        antwort = self.client.post("/api/zeit/ingest", json={"eintraege": []}, headers=kopf)
        self.assertEqual(antwort.status_code, 200, antwort.text)

    def test_taetigkeit_und_stand_landen_in_der_zeile(self):
        self._senden("geheim-laptop", ["laptop"])
        db = SessionLocal()
        try:
            zeile = db.query(ZeitIst).filter(ZeitIst.quelle == "laptop").first()
            self.assertEqual(zeile.taetigkeit, "Minecraft")
            self.assertEqual(zeile.bestaetigt, "automatisch")
            self.assertFalse(zeile.genau)
        finally:
            db.close()


class MetrikenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def test_anker_existiert_auch_ohne_eine_einzige_messung(self):
        """Auf eine fehlende Zeitreihe kann keine Alarmregel feuern.

        Ohne diese Zeile waere ein Dienst, der nie eine Messung sah, von einem
        abgeschalteten nicht zu unterscheiden.
        """
        db = SessionLocal()
        try:
            db.query(ZeitIst).delete()
            db.commit()
        finally:
            db.close()
        antwort = self.client.get("/metrics")
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("kalender_zeit_zeilen_gesamt 0", antwort.text)

    def test_keine_taetigkeit_und_kein_titel_in_den_metriken(self):
        """⚠️ Der Endpunkt ist ohne Anmeldung erreichbar, und was hier steht, liegt
        danach in Prometheus, in Grafana und in jedem Backup davon. Der
        Laptop-Melder beruehrt spaeter Fenstertitel; Filmtitel aus dem Medien-Vault
        haben dort nichts zu suchen (Regel vom 2026-10-10)."""
        db = SessionLocal()
        try:
            start = datetime(2026, 10, 19, 18)
            db.add(ZeitIst(
                date=date(2026, 10, 19), start=start, end=start + timedelta(minutes=40),
                art="spiel", quelle="laptop", taetigkeit="Geheimprojekt-X",
                titel="Ein sehr privater Fenstertitel", extern_id="metrik-probe",
            ))
            db.commit()
        finally:
            db.close()
        text = self.client.get("/metrics").text
        self.assertIn('kalender_zeit_zeilen{quelle="laptop"}', text)
        self.assertNotIn("Geheimprojekt-X", text)
        self.assertNotIn("privater Fenstertitel", text)
        self.assertNotIn("owner_sub", text)


if __name__ == "__main__":
    unittest.main()
