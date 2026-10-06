"""The service entry points behind the web screens: document search order, a candidate's evidence spans, and
dataset generation from development sources through the budget gateway to a decision by someone else."""

import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from fastapi.testclient import TestClient

from rfp_assistant import api
from rfp_assistant.service import auth, service
from rfp_assistant.gateway import budget, generation
from rfp_assistant.evaluation import gold
from rfp_assistant.storage import postgres
from rfp_assistant.storage.store import open_db
from tests import fake_hub
from tests import release_fixtures as fx


class CandidateSpansTest(unittest.TestCase):
    def candidate(self, text: str, quote: str, offsets=None) -> dict:
        return {"row": {"evidence_groups": [{"group_id": "g1", "alternatives": [
                    {"element_id": "e1", "offsets": offsets}]}]},
                "context": {"evidence": [{"group_id": "g1", "element_id": "e1", "doc_id": "d", "quote": quote,
                                          "text": text, "location": None}]}}

    def test_the_recorded_offsets_pick_which_repeat_is_cited(self):
        (span,) = service.candidate_spans(self.candidate("가 12개월 나 12개월 다", "12개월", [9, 13]))
        self.assertTrue(span["cited_found"])
        self.assertEqual(span["segments"], [{"text": "가 12개월 나 ", "cited": False}, {"text": "12개월", "cited": True},
                                            {"text": " 다", "cited": False}])

    def test_a_quote_not_in_the_element_marks_nothing(self):
        (span,) = service.candidate_spans(self.candidate("하자보수 기간", "12개월"))
        self.assertFalse(span["cited_found"])
        self.assertEqual(span["segments"], [{"text": "하자보수 기간", "cited": False}])
        (gone,) = service.candidate_spans(self.candidate("", "12개월"))
        self.assertTrue(gone["missing"])


class DraftSlotTest(unittest.TestCase):
    def test_a_slot_needs_a_type_an_intent_and_sources_in_every_document(self):
        src = [{"doc_id": "a", "element_id": "e1", "text": "ignored"}]
        for args in (("unknown", "의도", ["a"], src), ("direct_fact", " ", ["a"], src),
                     ("direct_fact", "의도", ["a", "b", "c"], src), ("direct_fact", "의도", ["a", "a"], src),
                     ("cross_document", "의도", ["a", "b"], src), ("direct_fact", "의도", ["a"], [])):
            with self.assertRaises(service.ServiceError, msg=args[:3]):
                service.draft_slot(*args)
        slot = service.draft_slot("direct_fact", " 하자보수 기간 ", ["a"], src)
        self.assertRegex(slot["question_id"], r"^ui-[0-9a-f]{10}$")
        self.assertEqual((slot["intent"], slot["sources"]), ("하자보수 기간", [{"doc_id": "a", "element_id": "e1"}]))


class ShellServiceTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = fx.make_env(Path(self.folder.name))
        self.s = self.env.settings
        self.transport = generation.FakeTransport(self.reply)
        self.res = service.Resources(self.s, transport=self.transport)
        self.a, self.b = auth.visitor("person-a"), auth.visitor("person-b")
        self.drafts = {}

    def tearDown(self):
        for t in list(service._DRAFT_JOBS.values()):
            t.join(timeout=30)
        self.res.close()
        self.folder.cleanup()

    def reply(self, messages):
        packed = json.loads(messages[-1]["content"])
        drafts = [{**self.drafts[s["question_id"]], "question_id": s["question_id"]} for s in packed["slots"]]
        return generation.ProviderResponse(json.dumps({"drafts": drafts}), None, "stop",
                                           {"prompt_tokens": 100, "completion_tokens": 100, "cached_tokens": 0},
                                           f"shell-draft-{len(self.transport.calls)}")

    def test_documents_that_can_be_asked_come_first(self):
        items = service.find_documents(self.res, self.env.consultant)
        flags = [i["indexed"] for i in items]
        self.assertIn(False, flags)  # the unparsed HWP stays listed
        self.assertEqual(flags, sorted(flags, reverse=True))

    def test_drafting_sources_are_development_documents_only(self):
        ids = {d["doc_id"] for d in service.draft_documents(self.res, self.a)}
        self.assertIn(self.env.refs["기관F"].doc_id, ids)
        self.assertNotIn(self.env.refs["기관D"].doc_id, ids)  # sealed family
        sealed = self.env.refs["기관D"].doc_id
        with self.assertRaises(service.ServiceError):
            service.draft_elements(self.res, self.a, sealed)
        slot = service.draft_slot("direct_fact", "좌석 예약", [sealed],
                                  [{"doc_id": sealed, "element_id": fx.element(self.env, "기관D", "%좌석%")}])
        with self.assertRaises(service.ServiceError):
            service.plan_drafting(self.res, self.a, [slot])
        found = service.draft_elements(self.res, self.a, self.env.refs["기관F"].doc_id, "부가 가치세")
        self.assertTrue(found and all("부가가치세" in e["text"] for e in found))  # spacing does not matter

    def numeric_slot(self):
        row = fx.amount_row(self.env, reviewed=False)
        sources = [{"doc_id": g["doc_id"], "element_id": g["alternatives"][0]["element_id"]}
                   for g in row["evidence_groups"]]
        slot = service.draft_slot("table_numeric", "예산과 세금 조건", [row["scope"][0]["doc_id"]], sources)
        self.drafts[slot["question_id"]] = {
            "question": "사업 예산은 얼마이고 세금은 어떻게 되나요?", "answer": "130,000,000원, 부가가치세 포함",
            "difficulty": "세금 조건이 다른 문단에 있음",
            "pins": [{"source": 0, "quote": fx.AMOUNT}, {"source": 1, "quote": fx.VAT}],
            "claims": [{"kind": "number", "value": 130000000, "unit": "KRW", "support": [0, 1],
                        "qualifiers": [["부가가치세를 포함"]], "critical_kind": "amount"}], "skip_reason": None}
        return slot

    def shutdown_after_response(self, slot_count):
        entered, finish = threading.Event(), threading.Event()

        def blocked(messages):
            if len(self.transport.calls) == 1:
                entered.set()
                if not finish.wait(15):
                    raise AssertionError("first drafting batch was not released")
            return self.reply(messages)

        self.transport.responder = blocked
        self.res.settings = self.s.with_(shutdown_wait_seconds=0.01)  # self.res owns the gateway (it has a transport)
        slots = [self.numeric_slot() for _ in range(slot_count)]
        maximum = service.plan_drafting(self.res, self.a, slots)["max_micro_usd"]
        run_id = service.start_drafting(self.res, self.a, slots, maximum)
        thread = service._DRAFT_JOBS[run_id]
        try:
            self.assertTrue(entered.wait(15))
            self.res.close()
            replacement = postgres.GatewayOwner(self.s.db_path)
            try:
                finish.set()
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(self.transport.calls), 1)
            finally:
                replacement.release()
            self.assertTrue(self.transport.closed)
            self.assertEqual(service.drafting_runs(self.res, self.a)[0]["status"], "interrupted")
            self.assertEqual(budget.snapshot(self.s.db_path).pending_micro_usd, 0)
            with open_db(self.s.db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts WHERE stage = 'gold_drafting'").fetchone()[0], 1)
        finally:
            finish.set()
            thread.join(timeout=15)

    def test_shutdown_stops_drafting_before_the_next_batch_under_a_replacement_owner(self):
        self.shutdown_after_response(6)

    def test_shutdown_during_the_last_batch_does_not_report_completion(self):
        self.shutdown_after_response(1)

    def test_shutdown_between_reservation_and_dispatch_releases_without_a_provider_call(self):
        ready = threading.Event()
        dispatch = budget.mark_dispatching

        def stopped_before_marker(db, attempt_id, guard=None):
            ready.set()
            deadline = time.monotonic() + 10
            while not self.res._closed and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertTrue(self.res._closed)
            return dispatch(db, attempt_id, guard)

        self.res.settings = self.s.with_(shutdown_wait_seconds=1)
        slot = self.numeric_slot()
        maximum = service.plan_drafting(self.res, self.a, [slot])["max_micro_usd"]
        with mock.patch.object(budget, "mark_dispatching", side_effect=stopped_before_marker):
            run_id = service.start_drafting(self.res, self.a, [slot], maximum)
            self.assertTrue(ready.wait(15))
            self.res.close()
            service._DRAFT_JOBS[run_id].join(timeout=15)
        self.assertEqual(len(self.transport.calls), 0)
        self.assertEqual(service.drafting_runs(self.res, self.a)[0]["status"], "interrupted")
        self.assertEqual(budget.snapshot(self.s.db_path).pending_micro_usd, 0)

    def test_a_drafting_run_is_billed_then_decided_by_someone_other_than_its_requester(self):
        slot = self.numeric_slot()
        est = service.plan_drafting(self.res, self.a, [slot])
        self.assertTrue(est["fits"])
        self.assertGreater(est["max_micro_usd"], 0)
        self.assertEqual(self.transport.calls, [])  # estimating is free
        with self.assertRaises(service.ServiceError):  # above what the person agreed to
            service.start_drafting(self.res, self.a, [slot], est["max_micro_usd"] - 1)

        run_id = service.start_drafting(self.res, self.a, [slot], est["max_micro_usd"])
        service._DRAFT_JOBS[run_id].join(timeout=60)
        (run,) = service.drafting_runs(self.res, self.a)
        self.assertEqual((run["run_id"], run["status"], run["requested_by"], len(run["rows"]), run["submitted"]),
                         (run_id, "completed", "person-a", 1, False), run["error"])
        snap = budget.snapshot(self.s.db_path)
        self.assertEqual(snap.pending_micro_usd, 0)
        self.assertEqual(run["receipt"]["settled_micro_usd"], snap.spent_micro_usd)
        self.assertLessEqual(snap.spent_micro_usd, est["max_micro_usd"])
        self.assertEqual(len(self.transport.calls), 1)

        service.submit_drafts(self.res, self.a, run_id)
        self.assertTrue(service.drafting_runs(self.res, self.a)[0]["submitted"])
        with self.assertRaises(service.ServiceError):
            service.submit_drafts(self.res, self.a, run_id)
        app = api.create_app(self.res, fake_hub.login("person-a", "person-b"))
        with TestClient(app) as client, TestClient(app) as drafter:  # the review screen's calls, over HTTP
            members = {"person-b": client, "person-a": drafter}
            for name, browser in members.items():
                fake_hub.sign_in(browser, name)
            (pending,) = client.get("/api/gold/pending").json()
            c = client.get(f"/api/gold/{pending['candidate_id']}").json()
            self.assertEqual((c["requested_by"], c["documents"][0]["found"]), ("person-a", True))
            self.assertTrue(c["spans"] and all(s["cited_found"] for s in c["spans"]))
            self.assertIn({"text": fx.AMOUNT, "cited": True}, c["spans"][0]["segments"])

            def decide(who: str, note: str):
                return members[who].post(f"/api/gold/{c['candidate_id']}/decide", json={
                    "decision": "approve", "expected_sha": c["row_sha256"], "note": note, "original_inspected": True})

            self.assertIn("메모", decide("person-b", "  ").json()["detail"])
            self.assertIn("요청한 사람", decide("person-a", "원문 확인").json()["detail"])
            self.assertEqual(decide("person-b", "원문 1쪽 예산과 2쪽 부가세 문구 확인").json()["status"], "approved")
        self.assertEqual(gold.candidate(self.s, c["candidate_id"])["status"], "approved")


if __name__ == "__main__":
    unittest.main()
