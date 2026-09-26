"""Tests für die opt-in LLM-Auswertung von Aktivitäts-Randnotizen (feedback_llm).

Gateway wird gemockt (kein echter LLM-Call). Deckt: Flag-off = No-op, Feld-Übernahme,
Sentiment-Validierung, Idempotenz (llm_processed_at), fail-soft bei Netzfehler und
optionale Follow-up-Todos.
"""

import json
import os
import unittest
from datetime import date

os.environ['DATABASE_URL'] = 'sqlite:////tmp/kalender-unittest-feedbackllm.db'
os.environ['KALENDER_PASSWORD'] = 'test-password'
os.environ['SECRET_KEY'] = 'test-secret'

from backend.database import Base, engine, get_db
import backend.tenant  # noqa: F401  (Tenant-Scoping)
from backend.config import settings
from backend.models import ActivityFeedback, Todo
import backend.feedback_llm as fl


class FeedbackLLMBaseTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self._prev = (
            settings.FEEDBACK_LLM_ENABLED,
            settings.LLM_GATEWAY_URL,
            settings.FEEDBACK_LLM_FOLLOWUP_TODOS,
        )
        self._prev_call = fl._call_llm
        self.db = next(get_db())
        self.db.info["owner_sub"] = settings.DEFAULT_OWNER_SUB

    def tearDown(self):
        (settings.FEEDBACK_LLM_ENABLED, settings.LLM_GATEWAY_URL,
         settings.FEEDBACK_LLM_FOLLOWUP_TODOS) = self._prev
        fl._call_llm = self._prev_call
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _fb(self, note, **kw):
        kw.setdefault("owner_sub", settings.DEFAULT_OWNER_SUB)
        kw.setdefault("event_id", "ev-1")
        kw.setdefault("occurrence_date", date(2026, 7, 20))
        fb = ActivityFeedback(note=note, **kw)
        self.db.add(fb)
        self.db.commit()
        self.db.refresh(fb)
        return fb

    def _enable(self):
        settings.FEEDBACK_LLM_ENABLED = True
        settings.LLM_GATEWAY_URL = "http://mock-gateway"

    def _reget(self, fb_id):
        self.db.expire_all()
        return self.db.get(ActivityFeedback, fb_id)


class ApplyTest(FeedbackLLMBaseTest):
    def test_apply_sets_fields(self):
        fb = self._fb("war top, viel gelernt", activity_type="lernen", title="CompTIA")
        followups = fl.apply_llm_result(
            self.db, fb,
            {"summary": "Produktive Lerneinheit.", "sentiment": "positiv",
             "followups": ["Kapitel 4 wiederholen"]},
        )
        self.db.commit()
        self.assertEqual(fb.llm_summary, "Produktive Lerneinheit.")
        self.assertEqual(fb.llm_sentiment, "positiv")
        self.assertEqual(json.loads(fb.llm_followups), ["Kapitel 4 wiederholen"])
        self.assertIsNotNone(fb.llm_processed_at)
        self.assertEqual(followups, ["Kapitel 4 wiederholen"])

    def test_empty_result_still_marks_processed(self):
        fb = self._fb("...")
        fl.apply_llm_result(self.db, fb, {})
        self.db.commit()
        self.assertIsNone(fb.llm_summary)
        self.assertIsNone(fb.llm_sentiment)
        self.assertIsNone(fb.llm_followups)
        self.assertIsNotNone(fb.llm_processed_at)  # verhindert Endlos-Retry

    def test_invalid_sentiment_dropped(self):
        fb = self._fb("gemischt")
        fl.apply_llm_result(self.db, fb, {"sentiment": "super-toll", "summary": "x"})
        self.assertIsNone(fb.llm_sentiment)
        self.assertEqual(fb.llm_summary, "x")

    def test_followups_capped_and_cleaned(self):
        fb = self._fb("viel zu tun")
        fl.apply_llm_result(self.db, fb, {"followups": ["a", "  ", "b", "c", "d"]})
        self.assertEqual(json.loads(fb.llm_followups), ["a", "b", "c"])  # leer raus, auf 3 gedeckelt


class BatchTest(FeedbackLLMBaseTest):
    def test_disabled_is_noop(self):
        self._fb("eine notiz")
        self.assertEqual(fl.process_pending_feedback_notes(), 0)  # Flags default aus

    def test_batch_processes_and_is_idempotent(self):
        fb = self._fb("lief gut", activity_type="sport")
        self._enable()
        fl._call_llm = lambda note, at, title: {"summary": "Gutes Training.", "sentiment": "positiv", "followups": []}
        n1 = fl.process_pending_feedback_notes()
        self.assertEqual(n1, 1)
        row = self._reget(fb.id)
        self.assertEqual(row.llm_summary, "Gutes Training.")
        self.assertIsNotNone(row.llm_processed_at)
        # Zweiter Lauf: nichts mehr offen (idempotent über llm_processed_at).
        self.assertEqual(fl.process_pending_feedback_notes(), 0)

    def test_network_error_leaves_note_for_retry(self):
        fb = self._fb("noch offen")
        self._enable()
        fl._call_llm = lambda note, at, title: None  # simulierter Netz-/Gatewayfehler
        n = fl.process_pending_feedback_notes()
        self.assertEqual(n, 0)
        row = self._reget(fb.id)
        self.assertIsNone(row.llm_processed_at)  # bleibt offen → späterer Retry

    def test_followup_todos_created_with_flag(self):
        self._fb("dranbleiben", activity_type="lernen")
        self._enable()
        settings.FEEDBACK_LLM_FOLLOWUP_TODOS = True
        fl._call_llm = lambda note, at, title: {"summary": "s", "sentiment": "neutral", "followups": ["Vokabeln üben"]}
        fl.process_pending_feedback_notes()
        todos = self.db.query(Todo).filter(Todo.title == "Vokabeln üben").all()
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0].scheduling_mode, "pool")


if __name__ == "__main__":
    unittest.main()
