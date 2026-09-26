"""Der feed_token darf nicht im Zugriffsprotokoll landen.

Befund vom 2026-08-25: 509 von 644 Protokollzeilen trugen den Token im Klartext,
und ueber promtail/Loki auch die Off-Site-Sicherungen. Hintergrund und Begruendung
der Loesung in `backend/zugriffsprotokoll.py`.

★ Jeder Test hier prueft **beide Richtungen**: dass das Geheimnis weg ist UND dass
weiterhin etwas Brauchbares protokolliert wird. Nur auf Abwesenheit zu pruefen
waere wertlos, ein kaputtes Zugriffsprotokoll bestuende denselben Test.
"""
import logging
import os
import unittest

os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/kalender-unittest-zugriff.db')
os.environ.setdefault('KALENDER_PASSWORD', 'test-password')
os.environ.setdefault('SECRET_KEY', 'test-secret')

from backend.zugriffsprotokoll import (
    ERSATZ,
    GEHEIME_PARAMETER,
    GeheimnisFilter,
    filter_installieren,
    geheimnisse_maskieren,
)

# Erfundener Wert in der FORM des echten feed_token (43 Zeichen, URL-sicheres
# Base64 mit '-' und '_'). ★ Bewusst frei erfunden: einen echten Token als
# Testwert einzusetzen wuerde genau das Geheimnis, das dieser Filter aus dem
# Protokoll haelt, dauerhaft ins Repository schreiben, und von dort in Gitea
# und den GitHub-Spiegel.
GEHEIM = "TESTWERT-nicht-echt_0000000000000000000000"


class MaskierungTest(unittest.TestCase):
    def test_token_wird_ersetzt_name_bleibt(self):
        aus = geheimnisse_maskieren(f"/api/day-type/today?token={GEHEIM}")
        self.assertEqual(aus, f"/api/day-type/today?token={ERSATZ}")

    def test_harmlose_parameter_bleiben_lesbar(self):
        aus = geheimnisse_maskieren(f"/api/day-type?date=2026-08-31&token={GEHEIM}")
        self.assertIn("date=2026-08-31", aus)
        self.assertIn(f"token={ERSATZ}", aus)

    def test_kein_praefix_des_geheimnisses_ueberlebt(self):
        """Struktur zeigen, nie Werte, auch nicht gekuerzt."""
        aus = geheimnisse_maskieren(f"/api/ical?token={GEHEIM}&download=1")
        for laenge in range(4, len(GEHEIM) + 1):
            self.assertNotIn(GEHEIM[:laenge], aus,
                             f"{laenge} Zeichen des Geheimnisses stehen noch da: {aus}")
        self.assertIn("download=1", aus)

    def test_alle_bekannten_geheimnis_namen(self):
        for name in GEHEIME_PARAMETER:
            aus = geheimnisse_maskieren(f"/pfad?{name}={GEHEIM}")
            self.assertEqual(aus, f"/pfad?{name}={ERSATZ}", f"{name} nicht maskiert")

    def test_grossschreibung_wird_erkannt(self):
        self.assertEqual(geheimnisse_maskieren(f"/p?TOKEN={GEHEIM}"), f"/p?TOKEN={ERSATZ}")
        self.assertEqual(geheimnisse_maskieren(f"/p?Api_Key={GEHEIM}"), f"/p?Api_Key={ERSATZ}")

    def test_mehrere_geheimnisse_in_einer_adresse(self):
        aus = geheimnisse_maskieren(f"/p?token={GEHEIM}&x=1&secret={GEHEIM}")
        self.assertEqual(aus, f"/p?token={ERSATZ}&x=1&secret={ERSATZ}")

    def test_adresse_ohne_query_bleibt_unveraendert(self):
        for adresse in ("/api/health", "/", "/api/events"):
            self.assertEqual(geheimnisse_maskieren(adresse), adresse)

    def test_aehnlicher_parametername_wird_nicht_getroffen(self):
        """`tokenizer` ist kein `token`, sonst maskiert man Nutzdaten weg."""
        self.assertEqual(geheimnisse_maskieren("/p?tokenizer=bpe"), "/p?tokenizer=bpe")

    def test_idempotent(self):
        einmal = geheimnisse_maskieren(f"/p?token={GEHEIM}")
        self.assertEqual(geheimnisse_maskieren(einmal), einmal)


class FilterTest(unittest.TestCase):
    """Der Filter am echten uvicorn-Datensatz."""

    def setUp(self):
        self.logger = logging.getLogger("uvicorn.access")
        filter_installieren()

    def _datensatz(self, adresse: str) -> logging.LogRecord:
        # Exakt die Form aus uvicorn/protocols/http/h11_impl.py
        return self.logger.makeRecord(
            "uvicorn.access", logging.INFO, __file__, 0,
            '%s - "%s %s HTTP/%s" %d',
            ("10.210.3.6", "GET", adresse, "1.1", 200),
            None,
        )

    def test_filter_maskiert_die_adresse(self):
        satz = self._datensatz(f"/api/day-type/today?token={GEHEIM}")
        GeheimnisFilter().filter(satz)
        self.assertNotIn(GEHEIM, satz.getMessage())
        self.assertIn(f"token={ERSATZ}", satz.getMessage())

    def test_stelligkeit_bleibt_fuenf(self):
        """uvicorns AccessFormatter entpackt genau fuenf Namen, sonst bricht es."""
        satz = self._datensatz(f"/api/ical?token={GEHEIM}")
        GeheimnisFilter().filter(satz)
        self.assertIsInstance(satz.args, tuple)
        self.assertEqual(len(satz.args), 5)
        # Und die Meldung laesst sich weiterhin formatieren
        self.assertIn("10.210.3.6", satz.getMessage())
        self.assertIn("200", satz.getMessage())

    def test_filter_laesst_den_datensatz_durch(self):
        """Maskieren heisst nicht unterdruecken, es muss weiter protokolliert werden."""
        satz = self._datensatz(f"/p?token={GEHEIM}")
        self.assertTrue(GeheimnisFilter().filter(satz))

    def test_fremde_datensaetze_bleiben_unangetastet(self):
        satz = self.logger.makeRecord(
            "uvicorn.access", logging.INFO, __file__, 0, "irgendetwas %s", ("x",), None,
        )
        self.assertTrue(GeheimnisFilter().filter(satz))
        self.assertEqual(satz.getMessage(), "irgendetwas x")

    def test_installation_ist_mehrfach_aufrufbar(self):
        vorher = len([f for f in self.logger.filters if isinstance(f, GeheimnisFilter)])
        filter_installieren()
        filter_installieren()
        nachher = len([f for f in self.logger.filters if isinstance(f, GeheimnisFilter)])
        self.assertEqual(vorher, nachher, "Filter wurde mehrfach angehaengt")

    def test_am_logger_haengend_nicht_am_handler(self):
        """Damit er ein erneutes dictConfig von uvicorn ueberlebt."""
        self.assertTrue(
            any(isinstance(f, GeheimnisFilter) for f in self.logger.filters),
            "Filter haengt nicht am Logger 'uvicorn.access'",
        )

    def test_ganzer_weg_ueber_den_logger(self):
        """Vom Logger bis zur ausgegebenen Zeile, mit Gegenprobe."""
        with self.assertLogs("uvicorn.access", level="INFO") as protokoll:
            self.logger.info(
                '%s - "%s %s HTTP/%s" %d',
                "10.210.3.6", "GET", f"/api/day-type?date=2026-08-31&token={GEHEIM}",
                "1.1", 200,
            )
        zeile = protokoll.output[0]
        self.assertNotIn(GEHEIM, zeile)                 # Geheimnis weg …
        self.assertIn(f"token={ERSATZ}", zeile)         # … Struktur da …
        self.assertIn("date=2026-08-31", zeile)         # … Nutzdaten lesbar …
        self.assertIn("200", zeile)                     # … Zeile vollstaendig


if __name__ == "__main__":
    unittest.main()
