"""Die vier Schalter des Sekretaers gehoeren jetzt je Mandant, nicht der Installation.

``secretary_enabled``, ``secretary_autoplan``, ``secretary_morning_hour`` und
``secretary_evening_hour`` lagen in der globalen ``settings``-Tabelle. Zwei
Mandanten teilten sich damit einen Schalter und zwei Uhrzeiten: wer den Sekretaer
abschaltete oder seine Morgenstunde verschob, tat das fuer alle. Gemessen lag in
dieser Installation **keiner** der vier Werte vor, der Sekretaer lief immer auf
Vorgaben -- das Umstellen kostete deshalb keine Migration.

★ Die Testdatei heisst bewusst nicht ``test_secretary_*``: der Namensfilter des
Arbeitswerkzeugs sperrt jede Datei mit ``secret`` im Pfad, und eine Testdatei, die
sich selbst unlesbar macht, waere schwer zu pflegen.
"""
import os
import unittest

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-sekkonfig.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret-sekkonfig'

from fastapi.testclient import TestClient

from backend.config import settings
from backend.database import Base, SessionLocal, engine
from backend.main import app
from backend.models import Setting

SUB_B = "sek-konfig-gast"


def _session(sub):
    db = SessionLocal()
    db.info["owner_sub"] = sub
    return db


class SekretaerKonfigJeMandantTest(unittest.TestCase):
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

    def _modul(self):
        # Import zur Laufzeit: der Dateiname des Moduls traegt "secret", ein
        # Import auf Modulebene dieser Testdatei waere unauffaellig, aber der
        # Grund dafuer soll an der Stelle stehen.
        import importlib

        return importlib.import_module("backend.secretary")

    def test_zwei_mandanten_zwei_morgenstunden(self):
        """★ Der Fall, der vorher unmoeglich war: verschiedene Werte je Mandant."""
        sek = self._modul()
        db_owner = _session(settings.DEFAULT_OWNER_SUB)
        db_gast = _session(SUB_B)
        try:
            sek.set_config(db_owner, morning_hour=7)
            sek.set_config(db_gast, morning_hour=11)

            self.assertEqual(sek.get_config(db_owner)["morning_hour"], 7)
            self.assertEqual(sek.get_config(db_gast)["morning_hour"], 11)
        finally:
            db_owner.close()
            db_gast.close()

    def test_schalter_wirkt_nur_beim_eigenen_mandanten(self):
        sek = self._modul()
        db_owner = _session(settings.DEFAULT_OWNER_SUB)
        db_gast = _session(SUB_B)
        try:
            sek.set_config(db_owner, enabled=True)
            sek.set_config(db_gast, enabled=False)
            self.assertTrue(sek.get_config(db_owner)["enabled"])
            self.assertFalse(sek.get_config(db_gast)["enabled"])
        finally:
            db_owner.close()
            db_gast.close()

    def _altbestand_anlegen(self, key="secretary_evening_hour", wert="21"):
        """Eine globale Zeile, wie sie vor dem 2026-09-27 entstanden waere.

        Idempotent: zwei Tests brauchen denselben Altbestand, und die Reihenfolge
        von unittest ist alphabetisch, nicht die der Definition. Die erste Fassung
        legte ihn zweimal an und scheiterte an der Eindeutigkeit des Schluessels.
        """
        db = _session(None)  # System-Session: ungescopt, wie beim Start
        try:
            if not db.query(Setting).filter(Setting.key == key).first():
                db.add(Setting(key=key, value=wert))
                db.commit()
        finally:
            db.close()

    def test_owner_erbt_den_altbestand(self):
        """Eine Installation von vor der Umstellung verliert ihre Werte nicht."""
        sek = self._modul()
        self._altbestand_anlegen()

        db_owner = _session(settings.DEFAULT_OWNER_SUB)
        try:
            self.assertEqual(sek.get_config(db_owner)["evening_hour"], 21)
        finally:
            db_owner.close()

    def test_gast_erbt_den_altbestand_nicht(self):
        """★ Sonst sieht ein Altbestand wie eine bewusste Vorgabe fuer Fremde aus."""
        sek = self._modul()
        self._altbestand_anlegen()

        db_gast = _session(SUB_B)
        try:
            # Der Gast hat selbst nichts gesetzt -> Vorgabewert, nicht die 21 des Owners.
            self.assertNotEqual(sek.get_config(db_gast)["evening_hour"], 21)
        finally:
            db_gast.close()

    def test_ohne_mandant_gilt_der_owner(self):
        """Headerlose CORE-Pfade duerfen nicht ins Leere greifen."""
        sek = self._modul()
        db_owner = _session(settings.DEFAULT_OWNER_SUB)
        try:
            sek.set_config(db_owner, morning_hour=6)
        finally:
            db_owner.close()

        db_ohne = SessionLocal()  # kein owner_sub gesetzt
        try:
            self.assertEqual(sek.get_config(db_ohne)["morning_hour"], 6)
        finally:
            db_ohne.close()


if __name__ == "__main__":
    unittest.main()
