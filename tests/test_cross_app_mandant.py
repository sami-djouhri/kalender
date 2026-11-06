"""Cross-App-Signale gehoeren dem Mandanten, der fragt (SAGKAL-012, 2026-10-30).

Bis hierher fragte der Kalender Fitness, MealPrep und Lager immer headerlos, und
headerlos heisst bei allen dreien DEFAULT_OWNER_SUB. Saganta hat drei Konten:
jedes sah ueber `/api/assistant/*` Einkaufsliste, Trainingsabstand und
Lagerstand des Owners. Dazu war der Cache nach URL geschluesselt, also haette
selbst eine richtig gestellte Frage innerhalb der Cache-Zeit die Antwort des
vorigen Kontos bekommen.

Geprueft wird an der Leitung (was geht hinaus), nicht nur am Ergebnis.
"""
import hashlib
import hmac
import json
import unittest
from unittest import mock

from backend import cross_app
from backend.config import settings

FREMD = "fremder-mandant-sub"


class _Antwort:
    status = 200

    def __init__(self, daten):
        self._roh = json.dumps(daten).encode("utf-8")

    def read(self):
        return self._roh

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class CrossAppMandantTest(unittest.TestCase):
    def setUp(self):
        cross_app.clear_cache()
        self.gesendet: list = []
        self._vorher = {
            k: getattr(settings, k)
            for k in ("CROSS_APP_ENABLED", "FITNESS_TENANT_SECRET",
                      "MEALPREP_TENANT_SECRET", "LAGER_TENANT_SECRET",
                      "EINKAUFSLISTE_TENANT_SECRET", "DEFAULT_OWNER_SUB")
        }
        settings.CROSS_APP_ENABLED = True
        settings.DEFAULT_OWNER_SUB = settings.DEFAULT_OWNER_SUB or "owner-sub"
        settings.FITNESS_TENANT_SECRET = ""
        settings.MEALPREP_TENANT_SECRET = ""
        settings.LAGER_TENANT_SECRET = ""
        settings.EINKAUFSLISTE_TENANT_SECRET = ""

    def tearDown(self):
        for k, v in self._vorher.items():
            setattr(settings, k, v)
        cross_app.clear_cache()

    def _leitung(self, req, timeout=None):
        self.gesendet.append(req)
        return _Antwort({"offen": 1, "namen": ["Geheime Zutat des Owners"]})

    def _kopf(self, req, name):
        # urllib normalisiert Kopfnamen ("X-saganta-sub").
        return req.get_header(name.capitalize())

    def test_owner_fragt_headerlos(self):
        with mock.patch.object(cross_app.request, "urlopen", self._leitung):
            anzahl, namen = cross_app.shopping_open_count(settings.DEFAULT_OWNER_SUB)
        self.assertEqual(anzahl, 1)
        self.assertEqual(len(self.gesendet), 1)
        self.assertIsNone(self._kopf(self.gesendet[0], "X-Saganta-Sub"))

    def test_system_kontext_fragt_wie_der_owner(self):
        with mock.patch.object(cross_app.request, "urlopen", self._leitung):
            anzahl, _ = cross_app.shopping_open_count(None)
        self.assertEqual(anzahl, 1)
        self.assertIsNone(self._kopf(self.gesendet[0], "X-Saganta-Sub"))

    def test_fremdes_konto_ohne_geheimnis_fragt_gar_nicht(self):
        with mock.patch.object(cross_app.request, "urlopen", self._leitung):
            signale = cross_app.gather_signals(FREMD)
        self.assertEqual(self.gesendet, [], "headerlos waere die Antwort des Owners")
        self.assertEqual(signale["shopping_open_count"], 0)
        self.assertEqual(signale["shopping_names"], [])
        self.assertEqual(signale["lager_critical_count"], 0)
        self.assertIsNone(signale["days_since_workout"])
        self.assertIsNone(signale["muscle_freshness"])

    def test_fremdes_konto_mit_geheimnis_fragt_signiert(self):
        settings.EINKAUFSLISTE_TENANT_SECRET = "test-einkauf-geheimnis"
        with mock.patch.object(cross_app.request, "urlopen", self._leitung):
            cross_app.shopping_open_count(FREMD)
        self.assertEqual(len(self.gesendet), 1)
        req = self.gesendet[0]
        self.assertEqual(self._kopf(req, "X-Saganta-Sub"), FREMD)
        erwartet = hmac.new(b"test-einkauf-geheimnis", FREMD.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(self._kopf(req, "X-Saganta-Sub-Sig"), erwartet)

    def test_geheimnis_gilt_nur_fuer_seinen_dienst(self):
        """Das Einkaufslisten-Geheimnis darf keine Lager-Frage freischalten: jeder
        Dienst prueft sein eigenes, eine fremde Signatur gaebe dort 401."""
        settings.EINKAUFSLISTE_TENANT_SECRET = "test-einkauf-geheimnis"
        with mock.patch.object(cross_app.request, "urlopen", self._leitung):
            self.assertIsNone(cross_app.get_lager_critical(FREMD))
        self.assertEqual(self.gesendet, [])

    def test_cache_ist_je_mandant_getrennt(self):
        with mock.patch.object(cross_app.request, "urlopen", self._leitung):
            owner = cross_app.get_shopping_list(settings.DEFAULT_OWNER_SUB)
            fremd = cross_app.get_shopping_list(FREMD)
        self.assertIsNotNone(owner)
        self.assertIsNone(fremd, "der Cache darf die Owner-Antwort nicht weiterreichen")

    def test_suggestions_fragen_im_namen_der_session(self):
        """Die Kette bis zum Aufrufer: build_suggestions reicht den Mandanten
        der Session durch, statt headerlos zu fragen."""
        from backend import suggestions

        class _Halt(Exception):
            pass

        class _Db:
            info = {"owner_sub": FREMD}

        with mock.patch.object(suggestions, "compute_day_capacity",
                               return_value={"day_type": "frei"}), \
             mock.patch.object(suggestions, "_free_minutes_today", return_value=(0, None)), \
             mock.patch.object(cross_app, "gather_signals", side_effect=_Halt) as gefragt:
            with self.assertRaises(_Halt):
                suggestions.build_suggestions(_Db())
        gefragt.assert_called_once_with(FREMD)

if __name__ == "__main__":
    unittest.main()
