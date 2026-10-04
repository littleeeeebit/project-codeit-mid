import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from rfp_assistant import auth, budget, generation, service, store
from rfp_assistant.contracts import AnswerRequest, EvidenceUnit
from rfp_assistant.generation import FakeTransport, ProviderError, ProviderResponse
from tests import fixtures


def _usage():
    return {"prompt_tokens": 900, "completion_tokens": 50, "cached_tokens": 0}


class AnswerFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))
        self.transport = FakeTransport()
        self.res = service.Resources(self.env.settings, transport=self.transport)
        self.ref = self.env.refs["기관A"]

    def tearDown(self):
        self.res.close()
        self.tmp.cleanup()

    def ask(self, question="하자보수 기간은 얼마인가요?", key=None, ref=None, principal=None):
        return service.answer(self.res, principal or self.env.consultant, AnswerRequest(
            idempotency_key=key or str(uuid.uuid4()), generation_id="g1", question=question,
            scope=[ref or self.ref], as_of="2026-09-30"))

    def attempts(self):
        with store.open_db(self.env.settings.db_path) as conn:
            return [dict(r) for r in conn.execute("SELECT state, reserved_micro_usd, settled_micro_usd FROM attempts")]

    def test_grounded_answer_opens_late_pdf_evidence(self):
        r = self.ask()
        self.assertEqual(r.status, "answered", r.error)
        self.assertEqual(r.billing_state, "settled")
        eid = r.claims[0]["evidence_ids"][0]
        view = service.open_evidence(self.res, self.env.consultant, r.request_id, eid)
        self.assertIn("하자보수 기간은 검수 완료일로부터 12개월", view.quote)
        self.assertEqual((view.location["page"], view.location["page_label"]), (3, "1"))
        dl = service.original_download(self.res, self.env.consultant, self.ref.doc_id, self.ref.source_hash)
        self.assertEqual(dl.data, fixtures.PDF_A)
        [a] = self.attempts()
        self.assertEqual(a["state"], "settled")
        # untrusted source text reaches the model only as JSON data inside the user message
        sent = self.transport.calls[0]["messages"]
        self.assertNotIn("<script>", sent[0]["content"])
        self.assertEqual(json.loads(sent[1]["content"])["allowed_evidence_ids"][0], "E1")

    def test_same_key_dispatches_once_and_changed_input_conflicts(self):
        first = self.ask(key="k1")
        second = self.ask(key="k1")
        self.assertEqual(first.request_id, second.request_id)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(len(self.attempts()), 1)
        with self.assertRaises(service.ServiceError):
            self.ask(question="다른 질문입니다", key="k1")

    def test_timeout_keeps_unknown_cost_without_retry(self):
        self.transport.responder = lambda m: ProviderError("APITimeoutError", pre_execution=False)
        r = self.ask()
        self.assertEqual((r.status, r.billing_state), ("technical_error", "unknown"))
        self.assertEqual(len(self.transport.calls), 1)
        [a] = self.attempts()
        self.assertEqual(a["state"], "unknown")
        self.assertEqual(budget.snapshot(self.env.settings.db_path).pending_micro_usd, a["reserved_micro_usd"])

    def test_pre_execution_rejection_releases(self):
        self.transport.responder = lambda m: ProviderError("RateLimitError", pre_execution=True)
        r = self.ask()
        self.assertEqual(r.billing_state, "released")
        self.assertEqual(budget.snapshot(self.env.settings.db_path).pending_micro_usd, 0)

    def test_malformed_json_is_charged_and_not_retried(self):
        self.transport.responder = lambda m: ProviderResponse('{"status": "answered"', None, "stop", _usage(), "r1")
        r = self.ask()
        self.assertEqual((r.status, r.billing_state), ("technical_error", "settled"))
        self.assertEqual(len(self.transport.calls), 1)
        [a] = self.attempts()
        self.assertEqual((a["state"], a["settled_micro_usd"] > 0), ("settled", True))

    def test_fabricated_evidence_and_scope_are_rejected(self):
        def fabricated(messages):
            doc_id = json.loads(messages[1]["content"])["selected_documents"][0]["doc_id"]
            body = {"status": "answered", "summary": "s", "missing_fields": [], "conflicts": [], "next_action": None,
                    "claims": [{"text": "t", "kind": "source_fact", "doc_id": doc_id, "evidence_ids": ["E99"]}]}
            return ProviderResponse(json.dumps(body), None, "stop", _usage(), "r2")

        self.transport.responder = fabricated
        r = self.ask()
        self.assertEqual(r.status, "technical_error")
        self.assertIn("unknown_evidence_id", r.error)
        self.transport.responder = lambda m: ProviderResponse(
            None, "I can't help with that", "stop", _usage(), "r3")
        self.assertIn("model_refusal", self.ask().error)
        self.transport.responder = lambda m: ProviderResponse("{}", None, "length", _usage(), "r4")
        self.assertIn("output_truncated", self.ask().error)

    def test_quarantined_source_and_disabled_budget_do_not_dispatch(self):
        r = self.ask(ref=self.env.refs["기관E"])
        self.assertEqual(r.status, "ingestion_unavailable")
        budget.set_paid_enabled(self.env.settings.db_path, "owner", False, "test")
        r = self.ask()
        self.assertEqual((r.status, r.error), ("budget_blocked", "paid_disabled"))
        self.assertEqual(self.transport.calls, [])
        self.assertTrue(service.search_projects(self.res, self.env.consultant, {}, "하자보수"))  # free path survives

    def test_verifier_actions_require_the_role(self):
        with self.assertRaises(auth.AuthError):
            service.verifier_trace(self.res, self.env.consultant, "하자보수", [self.ref], "2026-09-30")
        trace = service.verifier_trace(self.res, self.env.verifier, "하자보수", [self.ref], "2026-09-30")
        self.assertGreater(trace["estimate_micro_usd"], 0)
        self.assertEqual(self.transport.calls, [])  # retrieval-only by default
        r = self.ask()
        with self.assertRaises(auth.AuthError):  # another consultant cannot read this request's evidence
            service.open_evidence(self.res, auth.Principal("c2", frozenset({"consultant"})), r.request_id, "E1")


class SafetyTest(unittest.TestCase):
    def test_fake_provider_never_builds_a_real_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp), paid=False, index=False)
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-not-used"}):
                res = service.Resources(env.settings)
            try:
                self.assertIsInstance(res.transport, FakeTransport)
            finally:
                res.close()

    def test_source_markup_is_rendered_as_text(self):
        """Source and model text reach the screens only as React text nodes, which escape markup; nothing in web/
        injects raw HTML or renders markdown."""
        web = Path(__file__).resolve().parents[1] / "web" / "src"
        for path in web.rglob("*.tsx"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("dangerouslySetInnerHTML", source, path)
            self.assertNotIn("react-markdown", source, path)

    def test_strict_schema_rejects_unknown_fields(self):
        schema = generation.answer_json_schema()["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        bad = ProviderResponse(json.dumps({"status": "answered", "summary": "", "claims": [], "missing_fields": [],
                                           "conflicts": [], "next_action": None, "url": "x"}), None, "stop", None, None)
        with self.assertRaises(generation.TechnicalError):
            generation.validate_answer(bad, [], set(), {})

    def test_only_a_declared_absence_of_a_listed_missing_field_is_dropped(self):
        # The owner's comparison: each side's absence was stated twice, as an uncited claim and a missing field. A
        # restatement is declared kind "absence"; text never decides it, so every uncited inference or fact fails.
        ev = EvidenceUnit("E1", "A", "h", "x", "c", ["e"], "제안서 10부", {}, 5)

        def respond(claims, missing=()):
            return ProviderResponse(json.dumps({
                "status": "answered", "summary": "s", "conflicts": [], "next_action": None,
                "claims": [{"text": t, "kind": k, "doc_id": d, "evidence_ids": ids} for t, k, d, ids in claims],
                "missing_fields": [{"doc_id": d, "field": f, "reason": "not_found_in_context"} for d, f in missing]},
                ensure_ascii=False), None, "stop", None, None)

        def validate(response):
            return generation.validate_answer(response, [ev], {"A", "B"}, {"E1": ev.quote}, {"A", "B"})

        fact = ("A는 제안서 10부를 요구한다", "source_fact", "A", ["E1"])
        absence = ("제안서  제출 부수", "absence", "B", [])  # exactly the listed field (whitespace aside)
        payload = validate(respond([fact, absence], missing=[("B", "제안서 제출 부수")]))
        self.assertEqual([c.text for c in payload.claims], [fact[0]])
        for text, missing in (("B 문서의 예산은 충분하다", [("B", "일정")]),  # an assertion labelled absence
                              ("B 문서의 예산은 충분하다", [("B", "예산")]),  # names the field, asserts more
                              ("B 문서에서는 제안서 제출 부수를 확인할 수 없다", [("B", "제안서 제출 부수")])):
            with self.subTest(text=text, missing=missing), \
                    self.assertRaisesRegex(generation.TechnicalError, "absence_not_listed"):
                validate(respond([fact, (text, "absence", "B", [])], missing))
        for claims, missing in (
                ([fact, absence[:1] + ("inference",) + absence[2:]], [("B", "제안서 제출 부수")]),  # undeclared
                ([fact, ("B의 일정은 위험해 보인다", "inference", "B", [])], [("B", "예산")]),  # unrelated
                ([fact, ("B 문서의 예산은 충분하다", "inference", "B", [])], [("B", "예산")]),  # names the field
                ([fact, ("B는 5부", "source_fact", "B", [])], [("B", "부수")])):  # an uncited fact
            with self.subTest(claims=claims[1][0], missing=missing), \
                    self.assertRaisesRegex(generation.TechnicalError, "claim_without_evidence"):
                validate(respond(claims, missing))
        for missing in ([], [("A", "제안서 제출 부수")]):  # nothing listed for B
            with self.subTest(missing=missing), \
                    self.assertRaisesRegex(generation.TechnicalError, "absence_not_listed"):
                validate(respond([fact, absence], missing))


if __name__ == "__main__":
    unittest.main()
