"""질문하기 as a conversation: a follow-up is rewritten into a standalone question through the paid gateway, the
answer streams as unvalidated text until validation replaces it, and every turn keeps idempotency, ownership,
cancellation and per-request billing. Real PostgreSQL; only the provider (FakeTransport) is controlled."""

import json
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from rfp_assistant import api, budget, generation, service, store
from rfp_assistant.contracts import AnswerRequest, EvidenceUnit, Principal, RetrievalResult
from rfp_assistant.generation import FakeTransport, ProviderError, ProviderResponse
from tests import fixtures

FIRST = "통합 정보시스템 구축 사업은 어떤 사업인가요?"
FOLLOW = "그 사업의 하자보수 기간은?"
STANDALONE = "통합 정보시스템 구축 사업의 하자보수 기간은?"


def rewritten(query=STANDALONE):
    return lambda messages: ProviderResponse(json.dumps({"query": query}, ensure_ascii=False), None, "stop",
                                             {"prompt_tokens": 200, "completion_tokens": 30}, str(uuid.uuid4()))


class HeldTransport(FakeTransport):
    """Streams the whole answer, then holds the call open until the test releases it."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.streamed = threading.Event()
        self.release = threading.Event()

    def chat(self, **kw):
        response = super().chat(**kw)
        if kw.get("on_delta"):
            self.streamed.set()
            if not self.release.wait(15):
                raise AssertionError("never released")
        return response


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))
        self.transport = HeldTransport(rewriter=rewritten())
        self.transport.release.set()
        self.res = service.Resources(self.env.settings, transport=self.transport)
        self.a, self.d = self.env.refs["기관A"], self.env.refs["기관D"]

    def tearDown(self):
        self.transport.release.set()
        self.res.close()
        self.tmp.cleanup()

    def turn(self, question, previous="", scope=None, mode="single", key=None, principal=None):
        return service.answer(self.res, principal or self.env.consultant, AnswerRequest(
            idempotency_key=key or str(uuid.uuid4()), generation_id="g", question=question,
            scope=[self.a] if scope is None else scope, mode=mode, as_of="2026-10-06", previous_request_id=previous))

    def attempts(self, request_id):
        with store.open_db(self.env.settings.db_path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT stage, state, settled_micro_usd FROM attempts WHERE request_id = ? ORDER BY created_at",
                (request_id,))]

    def trace(self, request_id):
        with store.open_db(self.env.settings.db_path) as conn:
            return json.loads(conn.execute("SELECT trace_json FROM requests WHERE request_id = ?",
                                           (request_id,)).fetchone()[0])

    def asked(self, call):
        return json.loads(call["messages"][-1]["content"])


class FollowUpTest(Base):
    def test_a_follow_up_is_rewritten_billed_traced_and_answered_from_the_standalone_question(self):
        first = self.turn(FIRST)
        self.assertEqual(first.status, "answered", first.error)
        self.assertEqual([c["kind"] for c in self.transport.calls], ["answer"])  # a first turn is never rewritten
        self.assertEqual(first.standalone_question, "")
        second = self.turn(FOLLOW, previous=first.request_id)
        self.assertEqual(second.status, "answered", second.error)
        rewrite, answer = self.transport.calls[1:]
        sent = self.asked(rewrite)
        self.assertEqual((sent["follow_up"], sent["conversation"][0]["question"]), (FOLLOW, FIRST))
        self.assertIn(first.summary, sent["conversation"][0]["answer"])
        self.assertEqual(self.asked(answer)["question"], STANDALONE)  # retrieval and the answer use the rewrite
        self.assertEqual(second.standalone_question, STANDALONE)
        self.assertEqual([(a["stage"], a["state"]) for a in self.attempts(second.request_id)],
                         [("query_rewrite", "settled"), ("generation", "settled")])
        trace = self.trace(second.request_id)
        self.assertEqual((trace["question"], trace["rewrite"]["query"]), (FOLLOW, STANDALONE))
        self.assertEqual(trace["rewrite"]["settlement"]["settled_micro_usd"],
                         self.attempts(second.request_id)[0]["settled_micro_usd"])
        third = self.turn("요약해 주세요", previous=second.request_id)  # history carries the resolved question
        self.assertEqual([t["question"] for t in self.asked(self.transport.calls[3])["conversation"]],
                         [FIRST, STANDALONE])
        self.assertEqual(third.status, "answered", third.error)

    def test_the_answer_sees_the_conversation_and_the_evidence_the_previous_answer_cited(self):
        first = self.turn(FIRST)
        cited = {first.evidence[i]["quote"] for i in service._cited_ids(asdict(first))}
        self.assertTrue(cited)
        self.transport.rewriter = rewritten("기관A 사업을 쉽게 설명해 주세요")
        second = self.turn("쉽게 다시 말해 주세요", previous=first.request_id)
        sent = self.asked(self.transport.calls[-1])
        self.assertEqual([t["question"] for t in sent["conversation"]], [FIRST])
        self.assertIn(first.summary, sent["conversation"][0]["answer"])
        self.assertLessEqual(cited, {e["text"] for e in sent["evidence"]})  # every cited fact can be cited again
        self.assertEqual(second.status, "answered", second.error)
        self.assertEqual(self.trace(second.request_id)["carried_evidence_ids"],
                         [e["evidence_id"] for e in sent["evidence"] if e["text"] in cited])
        self.assertNotIn("conversation", self.asked(self.transport.calls[0]))  # a first turn has none

    def test_carried_evidence_follows_the_fresh_evidence_once_and_only_while_it_is_indexed(self):
        chunks = self.res.index().chunks
        unit = lambda i, chunk: EvidenceUnit(f"E{i}", "doc", "h", chunk["extraction_id"], chunk["chunk_id"],
                                             [], chunk["body"], {}, 5)
        fresh = unit(1, chunks[0])
        retrieval = RetrievalResult("keyword", [], [], [], [fresh], [], [], 5, {}, "t", None)
        gone = replace(unit(1, chunks[2]), chunk_id="no-longer-indexed")
        merged = service._with_carried(self.res, retrieval, [unit(4, chunks[0]), unit(7, chunks[1]), gone])
        self.assertEqual([(e.evidence_id, e.chunk_id) for e in merged.evidence],
                         [("E1", chunks[0]["chunk_id"]), ("E2", chunks[1]["chunk_id"])])
        self.assertEqual(merged.evidence_tokens, 10)
        self.assertIs(service._with_carried(self.res, retrieval, []), retrieval)
        self.assertEqual(service._with_carried(self.res, retrieval, [unit(7, chunks[1])], {"other"}).evidence, [fresh])

    def test_a_turn_is_idempotent_and_its_target_names_its_place_in_the_conversation(self):
        first = self.turn(FIRST)
        one = self.turn(FOLLOW, previous=first.request_id, key="k-follow")
        again = self.turn(FOLLOW, previous=first.request_id, key="k-follow")
        self.assertEqual(one.request_id, again.request_id)
        self.assertEqual(len(self.attempts(one.request_id)), 2)
        with self.assertRaises(service.ServiceError):  # the same key may not continue another turn
            self.turn(FOLLOW, previous=one.request_id, key="k-follow")
        scope = [(self.a.doc_id, self.a.source_hash)]
        self.assertNotEqual(service.target_key(scope, FOLLOW, "single", "d"),
                            service.target_key(scope, FOLLOW, "single", "d", first.request_id))

    def test_only_a_finished_turn_of_the_same_member_and_scope_can_be_continued(self):
        first = self.turn(FIRST)
        for kw, message in (({"previous": "missing"}, "찾을 수 없습니다"),
                            ({"previous": first.request_id, "principal": Principal("c2", frozenset({"consultant"}))},
                             "찾을 수 없습니다"),
                            ({"previous": first.request_id, "scope": [self.d]}, "범위를 바꿀 수 없습니다"),
                            ({"previous": first.request_id, "scope": [], "mode": "corpus"}, "범위를 바꿀 수 없습니다")):
            with self.subTest(kw=kw), self.assertRaisesRegex(service.ServiceError, message):
                self.turn(FOLLOW, **kw)
        with store.open_db(self.env.settings.db_path) as conn, store.tx(conn, immediate=True):
            conn.execute("UPDATE requests SET status = 'running' WHERE request_id = ?", (first.request_id,))
        with self.assertRaisesRegex(service.ServiceError, "끝난 뒤에"):
            self.turn(FOLLOW, previous=first.request_id)
        self.assertEqual(len(self.transport.calls), 1)  # nothing was queued or paid for a refused turn

    def test_a_refused_or_failed_rewrite_ends_the_turn_without_an_answer_call(self):
        first = self.turn(FIRST)
        cases = (
            (ProviderError("RateLimitError", pre_execution=True), ("technical_error", "released")),
            (ProviderResponse('{"answer": "x"}', None, "stop", {"prompt_tokens": 9, "completion_tokens": 3}, "bad"),
             ("technical_error", "settled")),
            (ProviderError("APITimeoutError", pre_execution=False), ("technical_error", "unknown")),
        )
        for outcome, expected in cases:
            with self.subTest(expected=expected):
                self.transport.rewriter = lambda m, outcome=outcome: outcome
                before = len(self.transport.calls)
                r = self.turn(FOLLOW, previous=first.request_id)
                self.assertEqual((r.status, r.billing_state), expected)
                self.assertEqual([c["kind"] for c in self.transport.calls[before:]], ["rewrite"])
        before = len(self.transport.calls)  # the unknown rewrite holds every dispatch until it is reconciled
        r = self.turn(FOLLOW, previous=first.request_id)
        self.assertEqual((r.status, r.billing_state), ("budget_blocked", "released"))
        self.assertIn("unknown PostgreSQL billing", r.error)
        self.assertEqual(len(self.transport.calls), before)
        budget.set_paid_enabled(self.env.settings.db_path, "owner", False, "test")
        r = self.turn(FOLLOW, previous=first.request_id)
        self.assertEqual((r.status, r.error), ("budget_blocked", "paid_disabled"))

    def test_cancelling_during_the_rewrite_stops_before_the_answer_and_keeps_the_rewrite_billed(self):
        first = self.turn(FIRST)

        def cancel_then_rewrite(messages):  # the member presses 요청 취소 while the rewrite call runs
            with store.open_db(self.env.settings.db_path) as conn:
                rid = conn.execute("SELECT request_id FROM requests WHERE idempotency_key = 'k-cancel'").fetchone()[0]
            service.cancel_request(self.res, self.env.consultant, rid)
            return rewritten()(messages)

        self.transport.rewriter = cancel_then_rewrite
        rid = service.submit_answer(self.res, self.env.consultant, AnswerRequest(
            idempotency_key="k-cancel", generation_id="g", question=FOLLOW, scope=[self.a], as_of="2026-10-06",
            previous_request_id=first.request_id))
        end = time.monotonic() + 10
        while service.request_status(self.res, self.env.consultant, rid).status in ("queued", "running"):
            self.assertLess(time.monotonic(), end)
            time.sleep(0.02)
        view = service.request_status(self.res, self.env.consultant, rid)
        self.assertEqual(view.status, "cancelled")
        self.assertEqual([(a["stage"], a["state"]) for a in self.attempts(rid)], [("query_rewrite", "settled")])

    def test_corpus_and_comparison_conversations_keep_their_scope(self):
        first = self.turn(FIRST, scope=[], mode="corpus")
        second = self.turn(FOLLOW, previous=first.request_id, scope=[], mode="corpus")
        self.assertEqual(second.status, "answered", second.error)
        self.assertEqual(self.asked(self.transport.calls[1])["documents"], "all documents")
        self.assertEqual(self.asked(self.transport.calls[2])["mode"], "corpus")
        pair = self.turn("통합 정보시스템과 도서관 좌석 예약 사업을 비교해 주세요", scope=[self.a, self.d], mode="compare")
        self.assertEqual(pair.status, "answered", pair.error)
        self.transport.rewriter = rewritten("통합 정보시스템과 도서관 좌석 예약의 하자보수 조건 비교")
        follow = self.turn("하자보수 조건은?", previous=pair.request_id, scope=[self.a, self.d], mode="compare")
        self.assertEqual(follow.status, "answered", follow.error)
        self.assertEqual(self.asked(self.transport.calls[4])["documents"],
                         ["통합 정보시스템 구축", "도서관 좌석 예약"])
        self.assertEqual({c["doc_id"] for c in follow.claims}, {self.a.doc_id, self.d.doc_id})
        info = self.turn("", previous=follow.request_id, scope=[self.a, self.d], mode="metadata")  # free turn
        self.assertTrue(info.facts)
        self.assertEqual(len(self.transport.calls), 6)


class StreamingTest(Base):
    def submit(self, question=FIRST, generation_id="g"):
        return service.submit_answer(self.res, self.env.consultant, AnswerRequest(
            idempotency_key=str(uuid.uuid4()), generation_id=generation_id, question=question, scope=[self.a],
            as_of="2026-10-06"))

    def wait(self, rid):
        end = time.monotonic() + 10
        while service.request_status(self.res, self.env.consultant, rid).status in ("queued", "running"):
            self.assertLess(time.monotonic(), end)
            time.sleep(0.02)
        return service.request_status(self.res, self.env.consultant, rid)

    def test_the_answer_streams_unvalidated_then_the_validated_outcome_replaces_it(self):
        self.transport.release.clear()
        rid = self.submit()
        self.assertTrue(self.transport.streamed.wait(10))
        progress = service.answer_progress(self.res, self.env.consultant, rid, "g")
        self.assertFalse(progress["finished"])
        self.assertEqual(progress["partial"]["summary"], "가짜 제공자 응답입니다.")
        self.assertEqual(progress["partial"]["claims"][0]["evidence_ids"], ["E1"])
        self.assertIsNone(service.answer_progress(self.res, self.env.consultant, rid, "another")["partial"])
        self.transport.release.set()
        view = self.wait(rid)
        self.assertEqual(view.result.status, "answered")
        self.assertEqual(view.result.summary_evidence_ids, ["E1"])
        self.assertEqual(service.answer_progress(self.res, self.env.consultant, rid, "g"),
                         {"finished": True, "partial": None})

    def test_a_rejected_stream_is_replaced_by_its_failure_state(self):
        def fabricated(messages):
            doc_id = self.asked({"messages": messages})["selected_documents"][0]["doc_id"]
            return ProviderResponse(json.dumps({
                "status": "answered", "summary": "지어낸 답", "summary_evidence_ids": ["E99"], "missing_fields": [],
                "conflicts": [], "next_action": None,
                "claims": [{"text": "지어낸 사실", "kind": "source_fact", "doc_id": doc_id, "evidence_ids": ["E99"]}]},
                ensure_ascii=False), None, "stop", {"prompt_tokens": 100, "completion_tokens": 10}, "fab")

        self.transport.responder = fabricated
        self.transport.release.clear()
        rid = self.submit()
        self.assertTrue(self.transport.streamed.wait(10))
        self.assertEqual(service.answer_progress(self.res, self.env.consultant, rid, "g")["partial"]["summary"],
                         "지어낸 답")
        self.transport.release.set()
        view = self.wait(rid)
        self.assertEqual(view.result.status, "technical_error")
        self.assertIn("unknown_evidence_id", view.result.error)
        self.assertIsNone(service.answer_progress(self.res, self.env.consultant, rid, "g")["partial"])

    def test_an_answered_summary_must_cite_and_partial_output_never_shows_a_half_written_id(self):
        ev = EvidenceUnit("E1", "A", "h", "x", "c", ["e"], "하자보수 12개월", {}, 5)
        body = {"status": "answered", "summary": "s", "summary_evidence_ids": [], "missing_fields": [],
                "conflicts": [], "next_action": None,
                "claims": [{"text": "t", "kind": "source_fact", "doc_id": "A", "evidence_ids": ["E1"]}]}
        response = ProviderResponse(json.dumps(body), None, "stop", None, None)
        with self.assertRaisesRegex(generation.TechnicalError, "summary_without_evidence"):
            generation.validate_answer(response, [ev], {"A"}, {"E1": ev.quote})
        self.assertIn("summary_evidence_ids", generation.answer_json_schema()["json_schema"]["schema"]["required"])
        partial = generation.partial_answer(
            '{"status":"answered","summary":"하자보수는","summary_evidence_ids":["E1","E1')
        self.assertEqual((partial["summary"], partial["summary_evidence_ids"]), ("하자보수는", ["E1"]))
        partial = generation.partial_answer(
            '{"summary":"s","summary_evidence_ids":[],"claims":[{"text":"빠진 항목","kind":"absence","doc_id":"A",'
            '"evidence_ids":[]},{"text":"12개월로 한')
        self.assertEqual([(c["text"], c["evidence_ids"]) for c in partial["claims"]], [("12개월로 한", [])])
        self.assertIsNone(generation.partial_answer(""))

    def test_evidence_ids_written_into_the_text_are_removed_because_the_markers_show_them(self):
        ev = EvidenceUnit("E1", "A", "h", "x", "c", ["e"], "하자보수 12개월", {}, 5)
        body = {"status": "answered", "summary": "기간은 12개월입니다. [E1][E2]", "summary_evidence_ids": ["E1"],
                "missing_fields": [], "conflicts": [], "next_action": None,
                "claims": [{"text": "하자보수는 12개월입니다 (E1, E3).", "kind": "source_fact", "doc_id": "A",
                            "evidence_ids": ["E1"]}]}
        payload = generation.validate_answer(ProviderResponse(json.dumps(body), None, "stop", None, None),
                                             [ev], {"A"}, {"E1": ev.quote})
        self.assertEqual((payload.summary, payload.claims[0].text), ("기간은 12개월입니다.", "하자보수는 12개월입니다."))
        self.assertEqual(generation.partial_answer('{"summary":"기간은 [E1] 12개월')["summary"], "기간은 12개월")


class StreamRouteTest(Base):
    def test_the_stream_route_sends_the_partial_answer_then_done(self):
        client = TestClient(api.create_app(self.res))
        headers = {"X-Member": quote("c1")}
        with client:
            self.transport.release.clear()
            owned = client.post("/api/ask", headers=headers, json={
                "scope": [{"doc_id": self.a.doc_id, "source_hash": self.a.source_hash}], "question": FIRST,
                "mode": "single"}).json()
            self.assertTrue(self.transport.streamed.wait(10))
            threading.Timer(0.5, self.transport.release.set).start()
            with client.stream("GET", f"/api/requests/{owned['request_id']}/stream", headers=headers,
                               params={"generation_id": owned["generation_id"]}) as stream:
                text = "".join(stream.iter_text())
            self.assertIn('"summary": "가짜 제공자 응답입니다."', text)
            self.assertTrue(text.endswith("event: done\ndata: {}\n\n"))
            follow = client.post("/api/ask", headers=headers, json={
                "scope": [{"doc_id": self.a.doc_id, "source_hash": self.a.source_hash}], "question": FOLLOW,
                "mode": "single", "previous_request_id": owned["request_id"]}).json()
            end = time.monotonic() + 10
            while (view := client.get(f"/api/requests/{follow['request_id']}", headers=headers, params={
                    "generation_id": follow["generation_id"], "target": follow["target"]}).json())["view"]["status"] \
                    in ("queued", "running"):
                self.assertLess(time.monotonic(), end)
                time.sleep(0.05)
            self.assertTrue(view["attachable"])
            self.assertEqual(view["view"]["result"]["standalone_question"], STANDALONE)


if __name__ == "__main__":
    unittest.main()
