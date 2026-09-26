import os
import unittest

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-recurrence.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from fastapi.testclient import TestClient

from backend.database import Base, engine
from backend.main import TERMINE_CAL_ID, app, create_jwt_token
from backend.recurrence import add_exdate, expand_event, parse_exdates, set_until
from backend.models import Event
from datetime import datetime, timezone


class RecurrenceUnitTest(unittest.TestCase):
    def _evt(self, rule, exdates=None):
        return Event(
            id="base-1",
            calendar_id=TERMINE_CAL_ID,
            title="Standup",
            start=datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc),  # Mo
            end=datetime(2026, 7, 6, 9, 30, tzinfo=timezone.utc),
            all_day=False,
            recurrence_rule=rule,
            recurrence_exdates=exdates,
            created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )

    def test_weekly_expansion_in_window(self):
        evt = self._evt("FREQ=WEEKLY;BYDAY=MO")
        ws = datetime(2026, 7, 1, tzinfo=timezone.utc)
        we = datetime(2026, 7, 31, tzinfo=timezone.utc)
        inst = expand_event(evt, ws, we)
        # Mondays in July 2026: 6, 13, 20, 27
        self.assertEqual(len(inst), 4)
        self.assertTrue(all(i["series_id"] == "base-1" for i in inst))
        self.assertEqual(inst[0]["id"], "base-1::2026-07-06")
        # Dauer bleibt erhalten
        self.assertEqual((inst[0]["end"] - inst[0]["start"]).total_seconds(), 1800)

    def test_count_limits_occurrences(self):
        evt = self._evt("FREQ=DAILY;COUNT=3")
        inst = expand_event(evt, datetime(2026, 7, 1, tzinfo=timezone.utc),
                            datetime(2026, 12, 31, tzinfo=timezone.utc))
        self.assertEqual(len(inst), 3)

    def test_until_limits_occurrences(self):
        evt = self._evt("FREQ=WEEKLY;BYDAY=MO;UNTIL=20260714T000000Z")
        inst = expand_event(evt, datetime(2026, 7, 1, tzinfo=timezone.utc),
                            datetime(2026, 8, 31, tzinfo=timezone.utc))
        self.assertEqual(len(inst), 2)  # 6. + 13.

    def test_exdate_skips_instance(self):
        evt = self._evt("FREQ=WEEKLY;BYDAY=MO", exdates="2026-07-13")
        inst = expand_event(evt, datetime(2026, 7, 1, tzinfo=timezone.utc),
                            datetime(2026, 7, 31, tzinfo=timezone.utc))
        days = [i["start"].date().isoformat() for i in inst]
        self.assertNotIn("2026-07-13", days)
        self.assertEqual(len(inst), 3)

    def test_non_recurring_returns_self(self):
        evt = self._evt(None)
        inst = expand_event(evt, datetime(2026, 7, 1, tzinfo=timezone.utc),
                            datetime(2026, 7, 31, tzinfo=timezone.utc))
        self.assertEqual(len(inst), 1)
        self.assertFalse(inst[0]["is_recurring_instance"])

    def test_broken_rule_does_not_crash(self):
        evt = self._evt("TOTAL=GARBAGE")
        inst = expand_event(evt, datetime(2026, 7, 1, tzinfo=timezone.utc),
                            datetime(2026, 7, 31, tzinfo=timezone.utc))
        self.assertEqual(len(inst), 1)

    def test_exdate_helpers(self):
        self.assertEqual(parse_exdates("2026-07-06, 2026-07-13"), {"2026-07-06", "2026-07-13"})
        self.assertEqual(add_exdate(None, "2026-07-06"), "2026-07-06")
        self.assertEqual(add_exdate("2026-07-13", "2026-07-06"), "2026-07-06,2026-07-13")
        self.assertEqual(add_exdate("2026-07-06", "2026-07-06"), "2026-07-06")  # dedup

    def test_set_until_replaces_existing(self):
        """Wanduhrzeit herein, Wanduhrzeit heraus (nur formal mit Z etikettiert)."""
        out = set_until("FREQ=WEEKLY;BYDAY=MO;UNTIL=20270101T000000Z",
                        datetime(2026, 7, 14, 0, 0))
        self.assertEqual(out.count("UNTIL="), 1)
        self.assertIn("UNTIL=20260714T000000Z", out)

    def test_set_until_rechnet_zonenbehaftete_eingabe_um(self):
        """Eine zonenbehaftete Zeit ist ein echter Zeitpunkt und wird umgerechnet.

        Das Z im Ergebnis ist ein Formatzwang von RFC 5545, kein Zonenwechsel:
        die Serienstarts liegen als Berliner Wanduhr vor (backend/wanduhr.py), und
        UNTIL muss in derselben Rechnung liegen, sonst schneidet es um den
        UTC-Versatz daneben. Mitternacht UTC am 14.07. ist 02:00 Berliner Zeit.
        """
        out = set_until("FREQ=DAILY", datetime(2026, 7, 14, tzinfo=timezone.utc))
        self.assertIn("UNTIL=20260714T020000Z", out)


class RecurrenceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.h = {"Authorization": f"Bearer {create_jwt_token()}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        Base.metadata.drop_all(bind=engine)

    def _create(self, **kw):
        body = {
            "calendar_id": TERMINE_CAL_ID,
            "title": "Sport",
            "start": "2026-09-07T17:00:00+00:00",  # Mo
            "end": "2026-09-07T18:00:00+00:00",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
        }
        body.update(kw)
        r = self.client.post("/api/events", json=body, headers=self.h)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_list_expands_within_window(self):
        ev = self._create()
        r = self.client.get(
            "/api/events",
            params={"start": "2026-09-01T00:00:00+00:00", "end": "2026-09-30T23:59:59+00:00"},
            headers=self.h,
        )
        self.assertEqual(r.status_code, 200)
        mine = [e for e in r.json() if e.get("series_id") == ev["id"]]
        self.assertEqual(len(mine), 4)  # Mondays 7,14,21,28

    def test_delete_instance_adds_exdate(self):
        ev = self._create(title="EXDATE-Test")
        r = self.client.delete(f"/api/events/{ev['id']}/instances/2026-09-14", headers=self.h)
        self.assertEqual(r.status_code, 204)
        r2 = self.client.get(
            "/api/events",
            params={"start": "2026-09-01T00:00:00+00:00", "end": "2026-09-30T23:59:59+00:00"},
            headers=self.h,
        )
        days = [e["start"][:10] for e in r2.json() if e.get("series_id") == ev["id"]]
        self.assertNotIn("2026-09-14", days)
        # Basis-Event existiert noch
        self.assertEqual(self.client.get(f"/api/events/{ev['id']}", headers=self.h).status_code, 200)

    def test_truncate_series_sets_until(self):
        ev = self._create(title="Truncate-Test")
        r = self.client.post(
            f"/api/events/{ev['id']}/truncate",
            params={"occ_date": "2026-09-21"},
            headers=self.h,
        )
        self.assertEqual(r.status_code, 204)
        r2 = self.client.get(
            "/api/events",
            params={"start": "2026-09-01T00:00:00+00:00", "end": "2026-09-30T23:59:59+00:00"},
            headers=self.h,
        )
        days = [e["start"][:10] for e in r2.json() if e.get("series_id") == ev["id"]]
        self.assertEqual(sorted(days), ["2026-09-07", "2026-09-14"])

    def test_instance_id_resolves_to_base(self):
        ev = self._create(title="Resolve-Test")
        r = self.client.get(f"/api/events/{ev['id']}::2026-09-14", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], ev["id"])

    def test_ical_contains_rrule_and_exdate(self):
        ev = self._create(title="ICS-Test")
        self.client.delete(f"/api/events/{ev['id']}/instances/2026-09-14", headers=self.h)
        token = self.client.get("/api/feed-token", headers=self.h).json()["feed_token"]
        r = self.client.get("/api/ical", params={"token": token})
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("RRULE:FREQ=WEEKLY", body)
        self.assertIn("EXDATE", body)


if __name__ == "__main__":
    unittest.main()
