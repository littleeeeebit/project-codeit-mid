"""Langfuse tracing of /api/ask and drafting: trace shape, deterministic scores, one masking exit, and no effect on
answers or the ledger when Langfuse is absent, unreachable or failing. No test needs a Langfuse server."""

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rfp_assistant import api, budget, drafting, generation, service, settings as settings_mod, store, tracing
from tests import fixtures
from tests import phase4_fixtures as fx

KEY = "sk-proj-" + "Zq8" * 16  # key-shaped, never a real credential
NOWHERE = "http://127.0.0.1:9"  # discard port: connections are refused at once


def planted_answer(messages):
    """The fake provider's grounded answer with a key-shaped string in its summary."""
    response = generation._echo_first_evidence(messages)
    payload = json.loads(response.content)
    payload["summary"] = f"요약 {KEY} 끝"
    response.content = json.dumps(payload, ensure_ascii=False)
    return response


def exported_text(exporter: InMemorySpanExporter) -> str:
    """Every exported attribute value as readable text (Langfuse serialises structured values ASCII-escaped)."""
    def readable(value):
        if isinstance(value, str):
            try:
                return json.dumps(json.loads(value), ensure_ascii=False)
            except ValueError:
                return value
        return json.dumps(value, ensure_ascii=False)

    return "\n".join(readable(v) for s in exporter.get_finished_spans() for v in s.attributes.values())


class AskTracingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, tracer, responder=None, question=None) -> tuple[dict, list[dict], dict]:
        """One /api/ask through the HTTP app; returns the finished view, the ledger's attempts and the budget."""
        res = service.Resources(self.env.settings, generation.FakeTransport(responder), tracer=tracer)
        client = TestClient(api.create_app(res))
        client.__enter__()
        headers = {"X-Member": quote("김검토")}
        try:
            ref = self.env.refs["기관A"]
            body = {"question": question or "하자보수 기간은 얼마인가요?", "mode": "single", "as_of": "2024-06-01",
                    "scope": [{"doc_id": ref.doc_id, "source_hash": ref.source_hash}]}
            owned = client.post("/api/ask", json=body, headers=headers).json()
            end = time.monotonic() + 10
            while True:
                view = client.get(f"/api/requests/{owned['request_id']}", headers=headers).json()["view"]
                if view["status"] not in ("queued", "running") or time.monotonic() > end:
                    break
                time.sleep(0.05)
        finally:
            client.__exit__(None, None, None)
            res.close()
        with store.open_db(self.env.settings.db_path) as conn:
            attempts = [{k: r[k] for k in r.keys() if not k.endswith(("_id", "_at"))}
                        for r in conn.execute("SELECT * FROM attempts ORDER BY created_at")]
            adjustments = [tuple(r) for r in conn.execute("SELECT amount_micro_usd, scope FROM adjustments")]
        return view, attempts, (asdict(budget.snapshot(self.env.settings.db_path)), adjustments)

    def test_one_ask_is_one_trace_with_nested_steps_scores_and_masked_secrets(self):
        exporter = InMemorySpanExporter()
        tracer = tracing.Tracing(NOWHERE, "pk-lf-test-ask", "sk-lf-test-ask", span_exporter=exporter)
        scores = []
        with mock.patch.object(tracer.client, "create_score", side_effect=lambda **kw: scores.append(kw)):
            view, _, _ = self.ask(tracer, planted_answer, question=f"하자보수 기간은 얼마인가요? 키 {KEY}")
        self.assertEqual(view["status"], "completed")
        spans = exporter.get_finished_spans()  # closing Resources flushed them
        names = sorted(s.name for s in spans)
        self.assertEqual(names, ["answer-question", "assemble-evidence", "generate-answer", "retrieve-evidence",
                                 "validate-answer"])
        root = next(s for s in spans if s.name == "answer-question")
        self.assertEqual({s.context.trace_id for s in spans}, {root.context.trace_id})
        self.assertEqual(format(root.context.trace_id, "032x"),
                         tracer.client.create_trace_id(seed=view["request_id"]))
        self.assertTrue(all(s.parent.span_id == root.context.span_id for s in spans if s is not root))
        attrs = {s.name: dict(s.attributes) for s in spans}
        self.assertEqual(attrs["generate-answer"]["langfuse.observation.type"], "generation")
        self.assertEqual(attrs["generate-answer"]["langfuse.observation.model.name"], "gpt-6-luna")
        self.assertIn("cost_details", " ".join(attrs["generate-answer"]))
        self.assertEqual(attrs["retrieve-evidence"]["langfuse.observation.type"], "retriever")
        self.assertEqual(attrs["answer-question"]["user.id"], "김검토")
        self.assertIn(view["request_id"], json.dumps(attrs["answer-question"], ensure_ascii=False))
        evidence = json.loads(attrs["assemble-evidence"]["langfuse.observation.output"])
        self.assertTrue(evidence["evidence"] and evidence["evidence_tokens"] > 0)
        # the planted key leaves in neither the question nor the answer; the readable text around it survives
        text = exported_text(exporter)
        self.assertNotIn(KEY, text)
        self.assertIn(tracing.REDACTED, text)
        self.assertIn("하자보수 기간은 얼마인가요?", text)
        self.assertIn("하자보수 기간은 검수 완료일로부터 12개월로 한다.", evidence["evidence"][0]["text"])
        # retrieved chunk, packed unit, prompt, generation output and trace output all carry the source sentence
        self.assertEqual(text.count("하자보수 기간은 검수 완료일로부터 12개월로 한다."), 5)
        self.assertEqual({s["name"]: (s["value"], s["data_type"]) for s in scores},
                         {"citation_valid": (1.0, "BOOLEAN"), "insufficient_evidence": (0.0, "BOOLEAN"),
                          "evidence_tokens": (float(evidence["evidence_tokens"]), "NUMERIC")})
        from langfuse._client.resource_manager import LangfuseResourceManager

        self.assertNotIn("pk-lf-test-ask", LangfuseResourceManager._instances)  # the owner's close freed the key

    def test_an_invalid_citation_is_scored_false(self):
        def wrong_citation(messages):
            response = generation._echo_first_evidence(messages)
            payload = json.loads(response.content)
            payload["claims"][0]["evidence_ids"] = ["E99"]
            response.content = json.dumps(payload, ensure_ascii=False)
            return response

        tracer = tracing.Tracing(NOWHERE, "pk-lf-test-cite", "sk-lf-test-cite", span_exporter=InMemorySpanExporter())
        scores = []
        with mock.patch.object(tracer.client, "create_score", side_effect=lambda **kw: scores.append(kw)):
            view, _, _ = self.ask(tracer, wrong_citation)
        self.assertEqual(view["result"]["status"], "technical_error")
        self.assertIn(("citation_valid", 0.0), [(s["name"], s["value"]) for s in scores])

    def test_unreachable_or_failing_langfuse_changes_neither_answer_nor_ledger(self):
        baseline = self.ask(None)

        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("langfuse is broken")

        raising = tracing.Tracing(NOWHERE, "pk-lf-test-raise", "sk-lf-test-raise")
        real, raising.client = raising.client, Exploding()
        unreachable = tracing.Tracing(NOWHERE, "pk-lf-test-down", "sk-lf-test-down")
        try:
            for tracer in (unreachable, raising):
                with self.subTest(tracer=tracer.client.__class__.__name__):
                    self.tearDown()
                    self.setUp()
                    view, attempts, snapshot = self.ask(tracer)
                    self.assertEqual(view["status"], "completed")
                    self.assertEqual(_without_volatile((view["result"], attempts, snapshot)),
                                     _without_volatile((baseline[0]["result"], baseline[1], baseline[2])))
        finally:
            real.shutdown()  # Resources closed the tracer while its client was the exploding stand-in


VOLATILE = ("request_id", "attempt_ids", "generation_id", "updated_at")


def _without_volatile(value):
    """Drops the identifiers and clocks that differ between any two runs."""
    if isinstance(value, dict):
        return {k: _without_volatile(v) for k, v in value.items() if k not in VOLATILE and not k.endswith("_at")}
    if isinstance(value, (list, tuple)):
        return [_without_volatile(v) for v in value]
    return value


class TracingLifecycleTest(unittest.TestCase):
    def test_tracing_needs_all_three_settings_and_a_real_provider(self):
        values = dict(zip(settings_mod.TRACING_ENV, (NOWHERE, "pk-lf-test-env", "sk-lf-test-env")))
        with tempfile.TemporaryDirectory() as folder:
            env = fixtures.make_env(Path(folder), index=False)
            with mock.patch("langfuse.Langfuse", side_effect=AssertionError("no client may be built")):
                for missing in settings_mod.TRACING_ENV:
                    partial = {k: ("" if k == missing else v) for k, v in values.items()}
                    with mock.patch.object(settings_mod, "read_api_key", side_effect=partial.get):
                        self.assertIsNone(tracing.Tracing.from_settings(env.settings.with_(provider="openai")))
                with mock.patch.object(settings_mod, "read_api_key", side_effect=values.get):
                    self.assertIsNone(tracing.Tracing.from_settings(env.settings))  # provider "fake"
            with mock.patch.object(settings_mod, "read_api_key", side_effect=values.get):
                built = tracing.Tracing.from_settings(env.settings.with_(provider="openai"))
            self.assertIsNotNone(built)
            built.close()

    def test_one_owner_per_key_and_close_frees_it(self):
        first = tracing.Tracing(NOWHERE, "pk-lf-test-own", "sk-lf-test-own")
        with self.assertRaises(tracing.TracingError):
            tracing.Tracing(NOWHERE, "pk-lf-test-own", "sk-lf-test-own")
        first.close()
        first.close()  # idempotent
        tracing.Tracing(NOWHERE, "pk-lf-test-own", "sk-lf-test-own").close()

    def test_mask_redacts_secret_shapes_and_keeps_rfp_text(self):
        for secret in (KEY, "sk-lf-0123456789abcdef0123", "pk-lf-0123456789abcdef0123", "Bearer abcdefghijklmnop1234",
                       "AKIAABCDEFGHIJKLMNOP", "ghp_" + "a" * 36, '"api_key": "s3cr3tv4lue"', "password=hunter2hunter2",
                       '{\\"api_key\\": \\"s3cr3tv4lue\\"}',
                       "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4fw"):
            self.assertNotIn(secret.split()[-1].split(":")[-1].strip('" '), tracing.mask(f"앞 {secret} 뒤"))
        rfp = '하자보수 기간은 검수 완료일로부터 12개월이다. "input_tokens": 1234, "evidence_tokens": 900, risk-assessment'
        self.assertEqual(tracing.mask(rfp), rfp)


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is required")
class LangfuseLauncherTest(unittest.TestCase):
    def run_script(self, root: Path, volume_exists: bool = False) -> subprocess.CompletedProcess:
        volume = "'bidmate-langfuse_postgres_data'" if volume_exists else "$null"
        command = (f"function docker {{ $global:LASTEXITCODE = 0; if ($args[0] -eq 'volume') {{ {volume} }} "
                   f"else {{ Add-Content -LiteralPath '{(root / 'calls.txt').as_posix()}' -Value ($args -join ' ') }} }}; "
                   f". '{(root / 'tools/start-langfuse.ps1').as_posix()}'")
        return subprocess.run(["pwsh", "-NoProfile", "-Command", command], capture_output=True, encoding="utf-8",
                              errors="replace")

    def test_secrets_are_generated_once_and_dotenv_keeps_its_other_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            shutil.copyfile(Path(__file__).resolve().parents[1] / "tools/start-langfuse.ps1",
                            root / "tools/start-langfuse.ps1")
            (root / ".env").write_text("OPENAI_API_KEY=keep-me\nLANGFUSE_HOST=http://stale\n"
                                       'LANGFUSE_BASE_URL="http://elsewhere"\n', encoding="utf-8")
            for _ in range(2):
                result = self.run_script(root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if _ == 0:
                    first = (root / ".runtime/langfuse.env").read_text(encoding="utf-8")
            self.assertEqual((root / ".runtime/langfuse.env").read_text(encoding="utf-8"), first)  # reused
            secrets = dict(line.split("=", 1) for line in first.splitlines())
            self.assertTrue(secrets["LANGFUSE_PUBLIC_KEY"].startswith("pk-lf-"))
            self.assertEqual(len(secrets["LANGFUSE_ENCRYPTION_KEY"]), 64)
            dotenv = (root / ".env").read_text(encoding="utf-8").splitlines()
            self.assertEqual(dotenv, ["OPENAI_API_KEY=keep-me", "LANGFUSE_HOST=http://127.0.0.2:3100",
                                      f"LANGFUSE_PUBLIC_KEY={secrets['LANGFUSE_PUBLIC_KEY']}",
                                      f"LANGFUSE_SECRET_KEY={secrets['LANGFUSE_SECRET_KEY']}"])
            calls = (root / "calls.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(c.endswith("compose.langfuse.yaml up -d --wait") for c in calls), calls)
            (root / ".runtime/langfuse.env").unlink()
            refused = self.run_script(root, volume_exists=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("volumes exist", refused.stdout + refused.stderr)
            self.assertFalse((root / ".runtime/langfuse.env").exists())


class DraftingTracingTest(unittest.TestCase):
    def test_a_drafting_run_is_one_trace_with_its_generations_nested(self):
        with tempfile.TemporaryDirectory() as folder:
            env = fx.make_env(Path(folder))
            ref = fx.scope(env, '기관A')
            slot = {'question_id': 'trace-warranty', 'revision': 1, 'question_type': 'direct_fact',
                    'intent': 'Warranty period', 'scope': [ref], 'as_of_date': '2024-06-01',
                    'sources': [{'doc_id': ref['doc_id'], 'element_id': fx.element(env, '기관A', '%하자보수%')}]}
            draft = {'question_id': slot['question_id'], 'question': 'How long after acceptance?',
                     'answer': '12 months', 'difficulty': 'Reference event',
                     'pins': [{'source': 0, 'quote': fx.WARRANTY}], 'claims': [{'kind': 'number', 'value': 12,
                         'unit': '개월', 'support': [0], 'qualifiers': [['검수 완료일로부터']], 'critical_kind': None}],
                     'skip_reason': None}
            transport = generation.FakeTransport(lambda _: generation.ProviderResponse(
                json.dumps({'drafts': [draft]}), None, 'stop', {'prompt_tokens': 10, 'completion_tokens': 10},
                'traced-draft'))
            exporter = InMemorySpanExporter()
            tracer = tracing.Tracing(NOWHERE, "pk-lf-test-draft", "sk-lf-test-draft", span_exporter=exporter)
            try:
                receipt = drafting._generate(env.settings, {'slots': [slot]}, Path(folder) / 'out', 100000,
                                             transport, 'dev', tracer=tracer)
            finally:
                tracer.close()
            self.assertEqual(receipt['calls'], 1)
            spans = {s.name: s for s in exporter.get_finished_spans()}
            self.assertEqual(sorted(spans), ['draft-gold-questions', 'draft-question-batch'])
            root, call = spans['draft-gold-questions'], spans['draft-question-batch']
            self.assertEqual(call.parent.span_id, root.context.span_id)
            self.assertEqual(call.attributes["langfuse.observation.type"], "generation")
            self.assertIn("12 months", call.attributes["langfuse.observation.output"])


if __name__ == "__main__":
    unittest.main()
