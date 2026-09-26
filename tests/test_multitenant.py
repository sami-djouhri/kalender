"""Multi-Tenant-Isolation (fail-closed ORM-Scoping via backend/tenant.py).

Tenant kommt als X-Saganta-Sub-Header (gestempelt vom kalender-bff/app-proxy).
Ohne Header -> DEFAULT_OWNER_SUB (Owner, rueckwaertskompatibel).
"""
import os
import unittest
from datetime import date

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-multitenant.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.config import settings
from backend.database import Base, engine
from backend.main import app, create_jwt_token
from backend.system_calendars import DAYTYPE_CALENDARS, FEIERTAG_CALENDAR_ID

SUB_A = "tenant-a-sub"
SUB_B = "tenant-b-sub"


class MultiTenantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        auth = {"Authorization": f"Bearer {create_jwt_token()}"}
        cls.ha = {**auth, "X-Saganta-Sub": SUB_A}
        cls.hb = {**auth, "X-Saganta-Sub": SUB_B}
        cls.howner = auth  # kein Sub-Header -> DEFAULT_OWNER_SUB

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    # --- Todos (strict) ---

    def test_todos_isolated(self):
        r = self.client.post("/api/todos", json={"title": "A-geheim"}, headers=self.ha)
        self.assertIn(r.status_code, (200, 201), r.text)
        todo_id = r.json()["id"]

        titles_b = [t["title"] for t in self.client.get("/api/todos", headers=self.hb).json()]
        self.assertNotIn("A-geheim", titles_b)
        titles_a = [t["title"] for t in self.client.get("/api/todos", headers=self.ha).json()]
        self.assertIn("A-geheim", titles_a)

        # B darf As Todo weder lesen noch loeschen (scoped -> 404)
        self.assertEqual(self.client.delete(f"/api/todos/{todo_id}", headers=self.hb).status_code, 404)
        # Owner (headerlos) sieht As Todo NICHT (fail-closed pro sub)
        self.assertNotIn("A-geheim", [t["title"] for t in self.client.get("/api/todos", headers=self.howner).json()])

    # --- Events (shared: eigene ODER globale NULL-Zeilen) ---

    def test_events_isolated_but_feiertage_shared(self):
        r = self.client.post("/api/events", json={
            "calendar_id": "system-termine-0000-0000-000000000000",
            "title": "A-Termin", "start": "2026-08-01T10:00:00+00:00",
            "end": "2026-08-01T11:00:00+00:00",
        }, headers=self.ha)
        self.assertIn(r.status_code, (200, 201), r.text)

        ev_b = self.client.get("/api/events", params={"start": "2026-08-01", "end": "2026-08-02"}, headers=self.hb).json()
        self.assertNotIn("A-Termin", [e["title"] for e in ev_b])
        ev_a = self.client.get("/api/events", params={"start": "2026-08-01", "end": "2026-08-02"}, headers=self.ha).json()
        self.assertIn("A-Termin", [e["title"] for e in ev_a])

        # Feiertage (owner_sub NULL, Startup-Seed) sieht auch Tenant B
        year = date.today().year
        ev = self.client.get("/api/events", params={"start": f"{year}-12-25", "end": f"{year}-12-26"}, headers=self.hb).json()
        self.assertTrue(any("Weihnacht" in e["title"] for e in ev), ev)

    def test_fremder_mandant_sieht_feiertag_aber_keinen_owner_termin(self):
        """Das Leck, das den (verworfenen) Kalender-Fork begruendet hat.

        Vorher galt fuer Event die SHARED-Regel „eigener sub ODER owner_sub IS NULL".
        Ein Termin des Owners im Kalender daytype-arbeit trug zwar dessen sub, aber
        jede owner-lose Zeile war global sichtbar. Jetzt ist Event STRICT, global ist
        nur noch ein echtes Feiertags-Event.
        """
        # Ueber den vorgesehenen Weg: Tagestyp-Kalender sind Steuerdaten, direktes
        # Anlegen per /api/events ist seit 2026-08-24 mit 403 gesperrt.
        r = self.client.post("/api/day-type/set",
                             params={"date": "2026-09-07", "type": "arbeit"},
                             headers=self.howner)
        self.assertEqual(r.status_code, 200, r.text)

        params = {"start": "2026-09-07", "end": "2026-09-08"}
        titel_b = [e["title"] for e in self.client.get("/api/events", params=params, headers=self.hb).json()]
        self.assertNotIn("Arbeit", titel_b,
                         "fremder Mandant sieht die Arbeitszeiten des Owners")

        # Feiertage bleiben fuer alle sichtbar, sonst kippt die Tagestyp-Kette.
        year = date.today().year
        ev = self.client.get("/api/events",
                             params={"start": f"{year}-12-25", "end": f"{year}-12-26"},
                             headers=self.hb).json()
        self.assertTrue(any("Weihnacht" in e["title"] for e in ev), ev)

    def test_ownerloses_event_ausserhalb_der_feiertage_ist_nicht_global(self):
        """Das eigentliche Leck der alten SHARED-Regel.

        Alt galt fuer Event „eigener sub ODER owner_sub IS NULL". Termine, die ueber
        die API entstehen, tragen immer einen sub, deshalb war nichts davon live
        sichtbar. Owner-lose Zeilen entstehen aber im System-Kontext (Startup-Seeds,
        Migrationen, Reparaturskripte): jede solche Zeile in einem Tagestyp-Kalender
        waere fuer JEDEN Mandanten sichtbar gewesen, samt Titel und Uhrzeiten.
        Genau das schliesst die STRICT-Regel mit der Feiertags-Ausnahme.
        """
        from datetime import datetime, timezone

        from backend.database import SessionLocal
        from backend.models import Event

        db = SessionLocal()
        db.info["owner_sub"] = None  # System-Kontext: kein Stempeln -> owner_sub NULL
        try:
            db.add(Event(
                calendar_id=DAYTYPE_CALENDARS["arbeit"],
                title="Systemzeile ohne Besitzer",
                start=datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc),
                end=datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc),
                all_day=False,
            ))
            db.commit()
        finally:
            db.close()

        params = {"start": "2026-10-05", "end": "2026-10-06"}
        for name, headers in (("A", self.ha), ("B", self.hb), ("Owner", self.howner)):
            titel = [e["title"] for e in
                     self.client.get("/api/events", params=params, headers=headers).json()]
            self.assertNotIn(
                "Systemzeile ohne Besitzer", titel,
                f"Mandant {name} sieht eine owner-lose Zeile aus einem Tagestyp-Kalender",
            )

    def test_fremder_mandant_sieht_owner_tagestyp_nicht(self):
        """Tagestyp ist pro Mandant. B darf den Arbeitstag des Owners nicht erben."""
        r = self.client.post("/api/day-type/set",
                             params={"date": "2026-09-21", "type": "urlaub"},
                             headers=self.howner)
        self.assertEqual(r.status_code, 200, r.text)
        # ⚠️ Der Parameter heisst `week_start`. Bis 2026-08-24 stand hier `start`:
        # der ist unbekannt, `week_start` aber Pflicht → die Antwort war 422, und
        # der komplette Vergleich lag in einem `if status == 200`-Zweig. Der Test
        # lief jahrelang gruen, ohne je etwas zu pruefen. Aufgefallen ist es erst
        # durch backend/parameter_wache.py. Deshalb hier keine Zweige mehr.
        r_b = self.client.get("/api/day-type/week",
                              params={"week_start": "2026-09-21"}, headers=self.hb)
        self.assertEqual(r_b.status_code, 200, r_b.text)
        tage = {d["date"]: d["type"] for d in r_b.json()}
        self.assertIn("2026-09-21", tage)
        self.assertNotEqual(tage["2026-09-21"], "urlaub",
                            "fremder Mandant erbt den Tagestyp des Owners")

    def test_termin_im_feiertagskalender_bleibt_privat(self):
        """Die Ausnahme ist ein UND: Feiertagskalender allein macht nichts global.

        Der Schreibpfad ist inzwischen ohnehin gesperrt (403), der Test haelt beides
        fest: die Sperre und, falls sie je faellt, die Sichtbarkeitsregel dahinter.
        """
        r = self.client.post("/api/events", json={
            "calendar_id": FEIERTAG_CALENDAR_ID,
            "title": "B-Schmuggel", "start": "2026-09-14T09:00:00+00:00",
            "end": "2026-09-14T10:00:00+00:00",
        }, headers=self.hb)
        self.assertEqual(r.status_code, 403, r.text)

        titel_a = [e["title"] for e in self.client.get(
            "/api/events", params={"start": "2026-09-14", "end": "2026-09-15"},
            headers=self.ha).json()]
        self.assertNotIn("B-Schmuggel", titel_a)

    def test_eigene_kalender_anlegen_ist_gesperrt(self):
        r = self.client.post("/api/calendars",
                             json={"name": "Mein Kalender", "color": "#123456"},
                             headers=self.ha)
        self.assertEqual(r.status_code, 403, r.text)

    def test_scoping_ueberlebt_wechselnde_mandanten(self):
        """Schutz gegen die Cache-Poisoning-Klasse von 2026-07-06.

        Die Kriterien in tenant.py sind als Lambda formuliert, damit SQLAlchemys
        warmer Statement-Cache nicht den sub des ersten Aufrufers einfriert. Hier
        laufen mehrere Mandanten nacheinander im selben Prozess, und einer davon
        ein zweites Mal, nach den anderen.
        """
        self.client.post("/api/todos", json={"title": "Cache-A"}, headers=self.ha)
        self.client.post("/api/todos", json={"title": "Cache-B"}, headers=self.hb)

        def titel(headers):
            return [t["title"] for t in self.client.get("/api/todos", headers=headers).json()]

        for headers, eigen, fremd in (
            (self.ha, "Cache-A", "Cache-B"),
            (self.hb, "Cache-B", "Cache-A"),
            (self.ha, "Cache-A", "Cache-B"),
            (self.hb, "Cache-B", "Cache-A"),
        ):
            gesehen = titel(headers)
            self.assertIn(eigen, gesehen)
            self.assertNotIn(fremd, gesehen, f"Cross-Tenant-Leck: {fremd} sichtbar")

    def test_system_calendars_visible_for_all(self):
        cals_b = {c["id"] for c in self.client.get("/api/calendars", headers=self.hb).json()}
        self.assertIn("system-termine-0000-0000-000000000000", cals_b)
        self.assertIn("daytype-feiertag-0000-0000-000000000000", cals_b)

    # --- Contacts + Geburtstags-Regeneration (bulk delete/insert) ---

    def test_contacts_and_birthdays_isolated(self):
        r = self.client.post("/api/contacts", json={"name": "Oma A", "birthday": "1950-03-15"}, headers=self.ha)
        self.assertIn(r.status_code, (200, 201), r.text)
        names_b = [c["name"] for c in self.client.get("/api/contacts", headers=self.hb).json()]
        self.assertNotIn("Oma A", names_b)

        # Geburtstags-Events: nur fuer A sichtbar
        year = date.today().year
        ev_a = self.client.get("/api/events", params={"start": f"{year+1}-03-15", "end": f"{year+1}-03-16"}, headers=self.ha).json()
        self.assertTrue(any(e["title"].startswith("Oma A") for e in ev_a), ev_a)
        ev_b = self.client.get("/api/events", params={"start": f"{year+1}-03-15", "end": f"{year+1}-03-16"}, headers=self.hb).json()
        self.assertFalse(any(e["title"].startswith("Oma A") for e in ev_b), ev_b)

        # Bs Kontakt-Anlage (loest bulk-Regeneration aus) darf As Events nicht killen
        self.client.post("/api/contacts", json={"name": "Opa B", "birthday": "1948-03-15"}, headers=self.hb)
        ev_a2 = self.client.get("/api/events", params={"start": f"{year+1}-03-15", "end": f"{year+1}-03-16"}, headers=self.ha).json()
        self.assertTrue(any(e["title"].startswith("Oma A") for e in ev_a2), ev_a2)
        self.assertFalse(any(e["title"].startswith("Opa B") for e in ev_a2), ev_a2)

    # --- Reviews (Composite-PK owner_sub+date) ---

    def test_reviews_same_date_per_tenant(self):
        d = "2026-08-10"
        ra = self.client.put(f"/api/reviews/{d}", json={"what_went_well": "A-Notiz"}, headers=self.ha)
        self.assertIn(ra.status_code, (200, 201), ra.text)
        rb = self.client.put(f"/api/reviews/{d}", json={"what_went_well": "B-Notiz"}, headers=self.hb)
        self.assertIn(rb.status_code, (200, 201), rb.text)
        self.assertEqual(self.client.get(f"/api/reviews/{d}", headers=self.ha).json()["what_went_well"], "A-Notiz")
        self.assertEqual(self.client.get(f"/api/reviews/{d}", headers=self.hb).json()["what_went_well"], "B-Notiz")

    # --- Habits/Projects (strict) ---

    def test_habits_projects_isolated(self):
        r = self.client.post("/api/projects", json={"name": "Projekt A"}, headers=self.ha)
        self.assertIn(r.status_code, (200, 201), r.text)
        r = self.client.post("/api/habits", json={"name": "Habit A"}, headers=self.ha)
        self.assertIn(r.status_code, (200, 201), r.text)
        self.assertNotIn("Projekt A", [p["name"] for p in self.client.get("/api/projects", headers=self.hb).json()])
        self.assertNotIn("Habit A", [h["name"] for h in self.client.get("/api/habits", headers=self.hb).json()])

    # --- Tagesziele (strict) ---

    def test_daily_goals_isolated(self):
        r = self.client.post("/api/goals", json={"title": "Ziel A", "date": "2026-08-11"}, headers=self.ha)
        self.assertIn(r.status_code, (200, 201), r.text)
        goals_b = self.client.get("/api/goals", params={"date": "2026-08-11"}, headers=self.hb).json()
        titles_b = [g["title"] for g in (goals_b if isinstance(goals_b, list) else goals_b.get("goals", []))]
        self.assertNotIn("Ziel A", titles_b)


if __name__ == "__main__":
    unittest.main()
