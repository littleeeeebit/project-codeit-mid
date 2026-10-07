"""Characterization of the settings, CLI, scoring, comparison and service helpers as they behave today, so a
refactor of these modules can show it changed nothing observable. Values are pinned from the current code."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rfp_assistant import cli
from rfp_assistant import settings as settings_mod
from rfp_assistant.contracts import AnswerRequest
from rfp_assistant.corpus import ingestion
from rfp_assistant.evaluation import compare, evaluation, judges, release
from rfp_assistant.gateway import generation
from rfp_assistant.service import answers, ops, service
from rfp_assistant.settings import Settings, SettingsError
from rfp_assistant.storage import store
from tests import fixtures
from tests import test_dense, test_release, test_service  # their scenarios; module attributes, so their tests are not collected twice

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = Path(__file__).parent / "snapshots"
CLI_SNAPSHOT = SNAPSHOTS / "cli_parser.json"
HELP_SHA = "7c15c41ca120a1fc"  # sha256 of every help text at COLUMNS=100, pinned from the original build_parser


def rate(k, n, lo_hi):
    return {"numerator": k, "denominator": n, "rate": None if not n else round(k / n, 4), "wilson95": lo_hi}


def parser_shape() -> dict:
    """Every subcommand's arguments: flag or positional name, destination, default, required, choices, type, nargs,
    action. Help text is left out."""
    sub = cli.build_parser()._subparsers._group_actions[0]
    return {name: [{"name": a.option_strings or a.dest, "dest": a.dest, "default": a.default, "required": a.required,
                    "choices": list(a.choices) if a.choices else None,
                    "type": getattr(a.type, "__name__", None), "nargs": a.nargs, "action": type(a).__name__}
                   for a in sp._actions if not isinstance(a, argparse._HelpAction)]
            for name, sp in sorted(sub.choices.items())}


class RateIntervalTest(unittest.TestCase):
    def test_wilson_interval(self):
        self.assertEqual([evaluation.wilson(k, n) for k, n in [(0, 0), (0, 1), (1, 1), (3, 10), (45, 50), (7, 7)]],
                         [None, [0.0, 0.7935], [0.2065, 1.0], [0.1078, 0.6032], [0.7864, 0.9565], [0.6457, 1.0]])

    def test_rate_dicts_agree_across_modules(self):
        for k, n in [(0, 0), (3, 10), (1, 1)]:
            self.assertEqual(judges.wilson(k, n), answers._wilson_rate(k, n))
        self.assertEqual(judges.wilson(3, 10), rate(3, 10, [0.1078, 0.6032]))
        self.assertEqual(judges.wilson(0, 0), rate(0, 0, None))

    def test_rate_formatting(self):
        r = judges.wilson(3, 10)
        self.assertEqual(evaluation._fmt_rate(r), "0.3 (3/10, [0.1078, 0.6032])")
        self.assertEqual([evaluation._fmt_rate(x) for x in ({}, None, judges.wilson(0, 0))], ["—"] * 3)
        self.assertEqual(answers._fmt(r), "0.3 (3/10, Wilson [0.1078, 0.6032])")
        self.assertEqual([answers._fmt(x) for x in ({}, None, judges.wilson(0, 0))], ["n/a (0 eligible)"] * 3)


class SettingsTest(unittest.TestCase):
    def settings(self, **kw):
        return Settings(source_dir=Path("/src"), data_dir=Path("/data"), hwp_converter=None, **kw)

    def test_defaults_and_constants(self):
        s = self.settings()
        self.assertEqual((s.provider, s.generation_model, s.generation_reasoning_effort, s.embedding_model,
                          s.retrieval_mode, s.fusion, s.dense_search, s.request_workers, s.request_admission,
                          s.jev_model, s.database_dsn_env),
                         ("openai", "gpt-6-luna", "low", "text-embedding-3-large", "kiwi_bm25", "rrf", "exact", 6, 12,
                          "jev-1.13.0", "RFP_DATABASE_DSN"))
        self.assertEqual((s.csv_path, s.files_dir), (Path("/src/data_list.csv"), Path("/src/files")))
        self.assertEqual(settings_mod.ALLOWED_GENERATION_MODELS, ("gpt-6-luna", "gpt-5-mini", "gpt-5-nano"))
        self.assertEqual(settings_mod.REASONING_EFFORTS, ("none", "low", "medium", "high", "xhigh", "max"))
        self.assertEqual(settings_mod.TRACING_ENV, ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"))

    def test_fingerprint(self):
        s = self.settings()
        data = {k: str(v) if isinstance(v, Path) else v for k, v in asdict(s).items()}
        expected = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        self.assertEqual(s.fingerprint(), expected)
        self.assertNotEqual(s.with_(generation_model="gpt-5-mini").fingerprint(), s.fingerprint())
        self.assertEqual(s.with_().fingerprint(), s.fingerprint())

    def test_rate_card(self):
        self.assertEqual(settings_mod.rate_card("gpt-5-mini"),
                         {"input": Decimal("0.25"), "cached_input": Decimal("0.025"), "output": Decimal("2.00")})
        with self.assertRaisesRegex(SettingsError, "no verified rate for model 'nope'"):
            settings_mod.rate_card("nope")

    def test_read_api_key_env_then_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text("﻿X_KEY = 'from-file'\nEMPTY=\nY_KEY=\"q\"\n", encoding="utf-8")
            with mock.patch.object(settings_mod, "REPO_ROOT", Path(tmp)), \
                    mock.patch.dict(os.environ, {"X_KEY": "from-env"}):
                self.assertEqual(settings_mod.read_api_key("X_KEY"), "from-env")
            with mock.patch.object(settings_mod, "REPO_ROOT", Path(tmp)), mock.patch.dict(os.environ, clear=True):
                self.assertEqual(settings_mod.read_api_key("X_KEY"), "from-file")
                self.assertEqual(settings_mod.read_api_key("Y_KEY"), "q")
                self.assertIsNone(settings_mod.read_api_key("EMPTY"))
                self.assertIsNone(settings_mod.tracing_credentials())

    def test_load_settings_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {"RFP_DATA_DIR": tmp, "RFP_SOURCE_DIR": tmp, "RFP_HWP_CONVERTER": str(Path(tmp, "hwp5proc"))}
            with mock.patch.dict(os.environ, {**base, "RFP_DATA_DIR": "relative"}, clear=True):
                with self.assertRaisesRegex(SettingsError, "RFP_DATA_DIR must be an absolute path"):
                    settings_mod.load_settings()
            cfg = Path(tmp, "cfg.json")
            cfg.write_text(json.dumps({"bogus": 1, "provider": "fake"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {**base, "RFP_CONFIG_FILE": str(cfg)}, clear=True):
                with self.assertRaisesRegex(SettingsError, r"unknown configuration keys: \['bogus'\]"):
                    settings_mod.load_settings()
            with mock.patch.dict(os.environ, base, clear=True):
                with self.assertRaisesRegex(SettingsError, "RFP_DATABASE_DSN is required"):
                    settings_mod.load_settings()
            with mock.patch.dict(os.environ, {**base, "RFP_DATABASE_DSN": "x"}, clear=True):
                s = settings_mod.load_settings(provider="fake")
                self.assertEqual((s.data_dir, s.source_dir, s.hwp_converter, s.provider),
                                 (Path(tmp), Path(tmp), Path(tmp, "hwp5proc"), "fake"))

    def test_validate_messages(self):
        cases = [
            ({"database_pool_max": 0}, "database pool maximum must be 1..16"),
            ({"provider": "x"}, "provider must be 'openai' or 'fake'"),
            ({"generation_model": "gpt-4"}, "generation model 'gpt-4' is not allowlisted"),
            ({"embedding_model": "x"}, "embedding model 'x' is not a compared model"),
            ({"reranker_model": "x"}, "reranker 'x' is not a compared model"),
            ({"evidence_target_tokens": 6000}, "evidence target must be positive"),
            ({"generation_reasoning_effort": "x"}, "generation_reasoning_effort must be one of"),
            ({"generation_max_output_tokens": 8001}, "generation_max_output_tokens must be within 1..8000"),
            ({"reranker_enabled": True}, "the reranker is enabled only through `activate-run`"),
            ({"retrieval_mode": "hybrid"}, "retrieval_mode is the keyword default"),
            ({"embedding_dimensions": 4000}, "embedding_dimensions exceeds the model's native size"),
            ({"embedding_batch_inputs": 2049}, "embedding_batch_inputs must be within 1..2048"),
            ({"embedding_batch_tokens": 300_001}, "embedding_batch_tokens must be within 1..300000"),
            ({"reranker_max_concurrency": 2}, "reranker_max_concurrency must be 1"),
            ({"reranker_precision": "int8"}, "reranker_precision must be"),
            ({"fusion": "x"}, "fusion must be 'rrf' or 'keyword_first'"),
            ({"dense_search": "x"}, "dense_search must be 'exact' or 'hnsw'"),
            ({"rrf_k": 0}, "rrf_k, top-k depths and reranker concurrency must be positive"),
            ({"request_workers": 7}, "request_workers must be 1..6"),
            ({"fake_delay_seconds": 1.0}, "fake_delay_seconds applies only to provider 'fake'"),
            ({"provider": "fake", "fake_delay_seconds": 121.0}, "fake_delay_seconds must be within 0..120"),
        ]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"RFP_DATABASE_DSN": "x"}):
            ok = Settings(source_dir=Path(tmp), data_dir=Path(tmp, "d"), hwp_converter=None)
            settings_mod.validate(ok)
            self.assertTrue(Path(tmp, "d").is_dir())
            for changes, message in cases:
                with self.subTest(changes=changes), self.assertRaisesRegex(SettingsError, message):
                    settings_mod.validate(ok.with_(**changes))


class CliTest(unittest.TestCase):
    def test_parser_matches_snapshot(self):
        self.assertEqual(json.loads(json.dumps(parser_shape(), ensure_ascii=False, default=str)),
                         json.loads(CLI_SNAPSHOT.read_text(encoding="utf-8")))

    def test_help_text_is_unchanged(self):
        """Every help string, root and per subcommand, at a fixed width; the parser snapshot localizes other changes."""
        with mock.patch.dict(os.environ, {"COLUMNS": "100"}):
            parser = cli.build_parser()
            sub = parser._subparsers._group_actions[0]
            text = "\n\0".join([parser.format_help(), *(sp.format_help() for _, sp in sorted(sub.choices.items()))])
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest()[:16], HELP_SHA)

    def test_every_subcommand_dispatches(self):
        sub = cli.build_parser()._subparsers._group_actions[0]
        self.assertEqual(set(cli.COMMANDS), set(sub.choices))
        self.assertTrue(all(f.__name__ == "cmd_" + name.replace("-", "_") for name, f in cli.COMMANDS.items()))

    def test_ids(self):
        self.assertEqual(cli._ids(" a, ,b,"), ["a", "b"])
        self.assertIsNone(cli._ids(""))
        self.assertIsNone(cli._ids(None))


class CompareFormattingTest(unittest.TestCase):
    def test_fmt_and_value(self):
        self.assertEqual([compare._fmt(v) for v in (None, 0.12345, 12345.67, -3.0, 7, "x", True)],
                         ["—", "0.1235", "12,345.7", "-3.0000", "7", "x", "True"])
        self.assertIsNone(compare.value({"a": {"b": None}}, "a.b.c"))
        self.assertIsNone(compare.value({"a": 5}, "a.b"))
        self.assertEqual(compare.value({"a": {"b": 2}}, "a.b"), 2)
        self.assertEqual([compare.value({"gate_passed": g}, "gate") for g in (True, False, None)],
                         ["pass", "fail", None])

    def test_failure_reason(self):
        self.assertEqual(compare.failure_reason(ValueError("boom\nsecond")), "ValueError: boom")
        self.assertEqual(compare.failure_reason(ValueError()), "ValueError: ")
        self.assertEqual(compare.failure_reason(type("OutOfMemoryError", (Exception,), {})()), "out of GPU memory")
        self.assertTrue(compare.failure_reason(type("GatedRepoError", (Exception,), {})()).startswith("gated model:"))
        self.assertEqual(len(compare.failure_reason(ValueError("x" * 400))), 300)

    def test_table_md(self):
        table = {"title": "T", "matrix": "embedding", "created_at": "2026-10-07T00:00:00Z",
                 "fixed": {"fusion": "keyword_first:60:1.0:6"},
                 "columns": [{"key": "dev.ndcg@5", "label": "nDCG@5"}, {"key": "gate", "label": "gate"},
                             {"key": "ms", "label": "ms"}],
                 "rows": [{"name": "a · b", "status": "complete", "dev": {"ndcg@5": 0.51234}, "gate_passed": True,
                           "ms": 12345.6, "run_id": "R-1"},
                          {"name": "c", "status": "failed", "reason": "x" * 200, "gate_passed": None}],
                 "populations": {"dev": "d", "whole": "w"}, "needs_evidence_review": ["q1", "q2"]}
        self.assertEqual(compare.table_md(table), "\n".join([
            "# T (embedding)", "",
            'Generated 2026-10-07T00:00:00Z by `compare --matrix embedding`. Fixed: '
            '`{"fusion": "keyword_first:60:1.0:6"}`.', "",
            "| Row | status | nDCG@5 | gate | ms | run |",
            "| --- | --- | --- | --- | --- | --- |",
            "| a · b | complete | 0.5123 | pass | 12,345.6 | `R-1` |",
            "| c | failed: " + "x" * 112 + " | — | — | — | `—` |", "",
            "Populations: dev: d; whole: w.", "",
            "Not verified: 2 question(s) the served rows are graded on are left out of the rebuilt rows, because a "
            "re-parse replaced the extraction their evidence is pinned to. Review their evidence again before the "
            "rebuilt rows count: `q1`, `q2`.", "",
            "Fusion `keyword_first` keeps the leading BM25 results (its last field, 6) in place, so nDCG@5 equals "
            "K1's on every hybrid and below-the-head row; the varied axis moves the evidence after them, which "
            "complete support and the critical counts show.", ""]))
        plain = {**table, "fixed": {}, "needs_evidence_review": [], "rows": []}
        self.assertTrue(compare.table_md(plain).endswith("| --- | --- | --- | --- | --- | --- |\n\n"
                                                         "Populations: dev: d; whole: w.\n"))


def scored_row(qid, typ, ans, outcome, claims=(), links=(), ac=(), passed=True, status_ok=True, lat=100, cost=10,
               attempt=1, groups=None, leaks=0):
    return {"question_id": qid, "type": typ, "answerability": ans, "outcome": outcome, "claims": list(claims),
            "links": list(links), "answer_claims": list(ac), "passed": passed, "status_ok": status_ok,
            "latency_ms": lat, "settled_micro_usd": cost, "attempt_no": attempt, "groups": groups,
            "scope_leaks": leaks}


def claim(i, verdict, kind=None):
    return {"claim_id": i, "verdict": verdict, "critical_kind": kind}


class AnswerScoringTest(unittest.TestCase):
    SCORED = [
        scored_row("q1", "fact", "answerable", "answered", [claim("c1", "correct"), claim("c2", "wrong_value", "amount")],
                   [{"support": "supporting", "valid": True}, {"support": "unsupported", "valid": False}],
                   [{"supported": True}, {"supported": False}], passed=False, groups={"gold": 2, "retrieved": 1}),
        scored_row("q2", "fact", "answerable", "insufficient_evidence", [claim("c3", "needs_review", "date")],
                   [{"support": "unjudged", "valid": True}], [{"supported": None}], lat=300, cost=30, attempt=2, leaks=1),
        scored_row("q3", "compare", "answerable", "answered", [claim("c4", "correct")], lat=None),
        scored_row("q4", "negative", "unanswerable", "answered", status_ok=False, cost=5),
        scored_row("q5", "negative", "unanswerable", "technical_error", lat=50),
        {"question_id": "m1", "type": "metadata", "answerability": "answerable", "outcome": "answered",
         "metadata_correct": True, "latency_ms": 10, "settled_micro_usd": 0, "attempt_no": 1, "scope_leaks": 0,
         "claims": [], "links": [], "answer_claims": []},
    ]

    def test_aggregate_answers(self):
        third = rate(1, 3, [0.0615, 0.7923])
        self.assertEqual(answers.aggregate_answers(self.SCORED), {
            "rows": 6, "answerable_rows": 3, "rows_passed": rate(4, 5, [0.3755, 0.9638]), "rows_failed": ["q1"],
            "gold_groups_not_retrieved": {"q1": 1}, "required_claim_correctness": rate(2, 4, [0.15, 0.85]),
            "question_completeness": third, "claims_needing_review": 1,
            "claim_verdicts": {"correct": 2, "wrong_value": 1, "contested": 0, "incomplete_qualifier": 0, "missing": 0,
                               "needs_review": 1},
            "critical_wrong": [{"question_id": "q1", "claim_id": "c2", "kind": "amount"}],
            "critical_unresolved": [{"question_id": "q2", "claim_id": "c3", "kind": "date", "verdict": "needs_review"}],
            "citation_precision_judged": rate(1, 2, [0.0945, 0.9055]), "citation_precision_lower_bound": third,
            "links_unjudged": 1, "citation_coverage": third, "unsupported_claim_rate": third,
            "answer_claims_unjudged": 1, "link_validity": rate(2, 3, [0.2077, 0.9385]),
            "negative_handling": rate(1, 2, [0.0945, 0.9055]), "false_answers": ["q4"],
            "unnecessary_refusals": third, "metadata_correct": rate(1, 1, [0.2065, 1.0]),
            "technical_outcomes": {"technical_error": 1}, "scope_leaks": 1,
            "by_type": {"compare": rate(1, 1, [0.2065, 1.0]), "fact": third},
            "cost": {"settled_micro_usd": 65, "per_question_micro_usd": 10.8, "retried_rows": 1},
            "latency_ms": {"p50": 100.0, "p95": 260.0, "n": 5, "condition": "sequential, single process, one call at a time"},
        })

    def test_aggregate_answers_empty(self):
        agg = answers.aggregate_answers([])
        empty = rate(0, 0, None)
        self.assertEqual({k: v for k, v in agg.items() if v == empty},
                         {k: empty for k in ("rows_passed", "required_claim_correctness", "question_completeness",
                                             "citation_precision_judged", "citation_precision_lower_bound",
                                             "citation_coverage", "unsupported_claim_rate", "link_validity",
                                             "negative_handling", "unnecessary_refusals", "metadata_correct")})
        self.assertEqual((agg["cost"], agg["latency_ms"]["p50"], agg["latency_ms"]["n"], agg["by_type"]),
                         ({"settled_micro_usd": 0, "per_question_micro_usd": None, "retried_rows": 0}, None, 0, {}))

    def test_compare_finalists(self):
        agg = answers.aggregate_answers(self.SCORED)
        extra = {"question_id": "q9", "claim_id": "c9", "kind": "date"}
        per = {"A": agg, "B": {**agg, "required_claim_correctness": {"rate": 0.75}, "negative_handling": {"rate": None},
                               "critical_wrong": agg["critical_wrong"] + [extra]}}
        self.assertEqual(answers.compare_finalists(per), {
            "baseline": "A", "candidate": "B", "claim_correctness_gain": 0.25, "negative_handling_gain": None,
            "new_critical_wrong": [extra], "p95_ms": [260.0, 260.0], "settled_micro_usd": [65, 65],
            "note": "development evidence for the owner's selection; small denominators: read the intervals"})

    def test_doc_text_and_verbatim_support(self):
        ans = {"summary": "요약 3개월", "claims": [{"text": "A는 12개월", "doc_id": "A"}, {"text": "B는 6개월", "doc_id": "B"}],
               "conflicts": [{"alternatives": [{"value": "A 9개월", "doc_id": "A"}, {"value": "B 1년", "doc_id": "B"}]}]}
        self.assertEqual(answers.doc_text(ans, "A", True), "요약 3개월 A는 12개월 A 9개월")
        self.assertEqual(answers.doc_text(ans, "A", False), "A는 12개월 A 9개월")
        self.assertEqual(answers.doc_text({}, None, True), "")
        self.assertEqual([answers.verbatim_support(a, b) for a, b in [("하자 보수 12개월", "계약 후 하자보수12개월 이내"),
                                                                     ("abc", "xabcx"), ("하자보수 6개월", "하자보수 12개월")]],
                         [True, False, False])


class RunIdentityTest(unittest.TestCase):
    def test_run_dirs(self):
        s = Settings(source_dir=Path("/src"), data_dir=Path("/data"), hwp_converter=None)
        self.assertEqual(answers.run_dir(s, "A-1f"), Path("/data/runs/A-1f"))
        self.assertEqual(answers.run_dir(s, "S-1f"), Path("/data/sealed/runs/S-1f"))
        for bad in ("", None, "A/1", "A_1", "../x"):
            with self.subTest(bad=bad), self.assertRaisesRegex(answers.AnswerEvalError, "invalid answer run id"):
                answers.run_dir(s, bad)
        self.assertEqual(judges.run_dir(s, "J-calibration-0123456789ab"),
                         Path("/data/judges/runs/J-calibration-0123456789ab"))
        for bad in ("", None, "J-Calibration-0123456789ab", "J-calibration-0123456789", "J-x-0123456789AB"):
            with self.subTest(bad=bad), self.assertRaisesRegex(judges.JudgeError, "invalid judge run id"):
                judges.run_dir(s, bad)
        self.assertEqual(judges.run_id_for({"part": "p"}),
                         "J-p-" + hashlib.sha256(b'{"part": "p"}').hexdigest()[:12])


class EstimateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.s = Settings(source_dir=Path(tmp.name), data_dir=Path(tmp.name), hwp_converter=None,
                          database_dsn_env=fixtures.database())

    def put(self, estimate_id, action, hours):
        now = datetime.now(timezone.utc)
        est = {"estimate_id": estimate_id, "action": action, "fingerprint": "f", "created_at": now.isoformat(),
               "expires_at": (now + timedelta(hours=hours)).isoformat()}
        answers._store_estimate(self.s, est)
        return est

    def test_load_estimate_in_both_modules(self):
        judge = self.put("e-judge", judges.ACTION, 1)
        run = self.put("e-run", "answer-finalists", 1)
        self.put("e-old", judges.ACTION, -1)
        self.assertEqual(judges.load_estimate(self.s, "e-judge"), judge)
        self.assertEqual(answers.load_estimate(self.s, "e-run"), run)
        self.assertEqual(answers.load_estimate(self.s, "e-judge"), judge)  # answers does not filter by action
        with self.assertRaisesRegex(judges.JudgeError, "unknown judge estimate e-run; plan again"):
            judges.load_estimate(self.s, "e-run")
        with self.assertRaisesRegex(answers.AnswerEvalError, "unknown estimate nope; run plan-run first"):
            answers.load_estimate(self.s, "nope")
        with self.assertRaisesRegex(judges.JudgeError, "the estimate expired; plan again"):
            judges.load_estimate(self.s, "e-old")
        with self.assertRaisesRegex(answers.AnswerEvalError, "the estimate expired; run plan-run again"):
            answers.load_estimate(self.s, "e-old")


class ServiceHelpersTest(unittest.TestCase):
    def test_describe_serving(self):
        self.assertEqual(service.describe_serving({"run_id": None}), "keyword default (kiwi_bm25), no activated run")
        self.assertEqual(service.describe_serving({"run_id": "R-1", "mode": "hybrid"}), "run `R-1` (hybrid)")
        self.assertEqual(service.describe_serving({"run_id": "R-1", "fallback_reason": "x", "stale_run_id": "R-0"}),
                         "keyword default (kiwi_bm25); activated run `R-0` not served: x")
        self.assertEqual(service.describe_serving({"run_id": "R-1", "fallback_reason": "x"}),
                         "keyword default (kiwi_bm25); activated run `R-1` not served: x")

    def test_metadata_facts(self):
        d = {"doc_id": "D1", "meta": {"title": "T", "institution": None, "amount_krw": 0, "notice": "N"},
             "resolutions": {"notice": {"value": "N2", "evidence": "e"}},
             "quality": {"provenance_conflicts": [{"field": "title", "values": ["T", "T2"]}]}}
        unknown = lambda f: {"doc_id": "D1", "field": f, "value": None, "state": "unknown", "provenance": "csv"}  # noqa: E731
        self.assertEqual(service.metadata_facts(d), [
            {"doc_id": "D1", "field": "title", "value": "T", "state": "conflict", "provenance": "csv",
             "alternatives": ["T", "T2"]},
            unknown("institution"),
            {"doc_id": "D1", "field": "notice", "value": "N2", "state": "resolved", "provenance": "resolution",
             "evidence": "e", "csv_value": "N"},
            unknown("revision"),
            {"doc_id": "D1", "field": "amount_krw", "value": 0, "state": "zero_review", "provenance": "csv"},
            unknown("published_at"), unknown("bid_start"), unknown("bid_close")])

    def test_inventory_order_and_cited_ids(self):
        items = [{"id": 1, "source_form": "SFR-001"}, {"id": 2, "source_form": "PER_01"}, {"id": 3, "source_form": None},
                 {"id": 4, "source_form": "SFR-002"}, {"id": 5, "source_form": ""}, {"id": 6, "source_form": "PER-2"}]
        self.assertEqual([i["id"] for i in service.inventory_display_order(items)], [1, 4, 2, 6, 3, 5])
        self.assertEqual(service._cited_ids({"summary_evidence_ids": ["e2", "e1"], "claims": [{"evidence_ids": ["e1", "e3"]}],
                                             "conflicts": [{"alternatives": [{"evidence_ids": ["e4", "e2"]}]}]}),
                         ["e2", "e1", "e3", "e4"])


class IngestionHelpersTest(unittest.TestCase):
    def test_review_record_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl, single, many, bom = (Path(tmp, n) for n in ("r.jsonl", "one.json", "many.json", "bom.json"))
            jsonl.write_text('{"a": 1}\n\n{"a": 2}\n', encoding="utf-8")
            single.write_text('{"a": 1}', encoding="utf-8")
            many.write_text('[{"a": 1}, {"a": 2}]', encoding="utf-8")
            bom.write_bytes(b"\xef\xbb\xbf{}")
            self.assertEqual(ingestion._load_review_records(jsonl), [{"a": 1}, {"a": 2}])
            self.assertEqual(ingestion._load_review_records(single), [{"a": 1}])
            self.assertEqual(ingestion._load_review_records(many), [{"a": 1}, {"a": 2}])
            with self.assertRaisesRegex(ingestion.IngestionError, "UTF-8 without BOM"):
                ingestion._load_review_records(bom)
        with self.assertRaisesRegex(ingestion.IngestionError, "absolute path"):
            ingestion._load_review_records(Path("r.jsonl"))

    def test_identity_and_text(self):
        self.assertEqual(ingestion.doc_id_for("기관A_통합 정보시스템.pdf"), "2325c786-bc0e-5646-b409-7f9717850c4d")
        self.assertEqual(ingestion.search_text(" 가  나\n다 "), "가 나 다")


class CompareCapCommandTest(unittest.TestCase):
    def test_stores_the_cap_and_audits_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp))
            args = argparse.Namespace(usd="1.25", actor="owner", reason="comparison")
            with mock.patch("builtins.print"):
                self.assertEqual(cli.cmd_compare_cap(args, env.settings), 0)
            with store.open_db(env.settings.db_path) as conn:
                cap = conn.execute("SELECT cap_micro_usd, updated_by, reason FROM external_ledger").fetchall()
                audit = conn.execute("SELECT actor, action, target, reason, details_json FROM audit_events").fetchall()
        self.assertEqual([tuple(r) for r in cap], [(1_250_000, "owner", "comparison")])
        self.assertEqual([tuple(r) for r in audit],
                         [("owner", "set-external-cap", "gemini", "comparison", '{"cap_micro_usd": 1250000}')])


STAMP = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?(\+00:00|Z)?")


def pinned_provenance(test: unittest.TestCase) -> None:
    """Code, metric and hardware fingerprints fixed for the test: they hash the package source, which a refactor
    changes, and they flow into run identities and reports."""
    test.enterContext(mock.patch.object(evaluation, "code_fingerprint", return_value={
        "source_sha256": "c" * 64, "git_revision": "r" * 40, "git_dirty": False}))
    test.enterContext(mock.patch.object(evaluation, "metric_code_sha256", return_value="m" * 64))
    test.enterContext(mock.patch.object(evaluation, "hardware", return_value={"machine": "fixture"}))


def snapshot(test: unittest.TestCase, name: str, text: str) -> None:
    path = SNAPSHOTS / f"{name}.txt"
    if os.environ.get("RFP_WRITE_SNAPSHOTS"):  # only from code whose behaviour is the reference
        path.write_text(text, encoding="utf-8", newline="\n")
    with test.subTest(snapshot=name):
        test.assertEqual(text, path.read_text(encoding="utf-8"))


UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
DIGEST = re.compile(r"\b(?=[0-9a-z]*\d)[0-9a-z]{10,64}\b")  # index versions, run IDs, fingerprints, hashes
# Timings, and spend: the fake provider prices prompts that carry random request IDs, so cents move between runs.
MEASURED = re.compile(r'("(?:p50|p95|p95_ms|mean|max|min|cold_wave_ms|wall_ms|total|exact|lexical|dense|fuse|rerank|'
                      r'pack|ms|infer_ms|queue_ms|elapsed_ms|total_ms|retrieval_ms|generation_ms|[a-z_]*micro_usd|'
                      r'settled|reserved|used_incl_open|[a-z_]*percent|prompt|evaluation-job[\w-]*)": )-?[0-9.]+'
                      r'|\b\d+(\.\d+)?(?=(/\d+(\.\d+)?)? ms\b)|(?<=\$)[0-9,]+\.\d+|(?<=\()\d+\.\d+(?=%\))')


CONCURRENT = re.compile(r'"question_id": "[^"]*"(?=,\n\s*"ms": )')  # a latency wave lists requests as they finish


def mask_p95_columns(text: str) -> str:
    """The `p95 ms` column of a Markdown table, located from its header."""
    out, column = [], None
    for line in text.split("\n"):
        cells = line.split(" | ")
        if line.startswith("|") and any(c.strip(" |") == "p95 ms" for c in cells):
            column = next(i for i, c in enumerate(cells) if c.strip(" |") == "p95 ms")
        elif not line.startswith("|"):
            column = None
        elif column is not None and not line.startswith("| ---") and len(cells) > column:
            cells[column] = "<ms>" + (" |" if cells[column].endswith(" |") else "")
            line = " | ".join(cells)
        out.append(line)
    return "\n".join(out)


def read_outputs(folder: Path, names: tuple[str, ...], root: Path) -> str:
    """The files joined, with what differs between identical runs made stable: timestamps, the temporary root, and
    measured timings masked; identifiers derived from the temporary paths renamed in order of first appearance, so
    the snapshot still shows which ones are the same."""
    text = "\n\0".join((folder / n).read_text(encoding="utf-8") for n in names)
    for form in (str(root), str(root).replace("\\", "\\\\"), root.as_posix()):
        text = text.replace(form, "<tmp>")
    text = CONCURRENT.sub('"question_id": "<concurrent>"', mask_p95_columns(STAMP.sub("<time>", UUID.sub("<uuid>", text))))
    text = MEASURED.sub(lambda m: (m.group(1) or "") + "<n>", text)
    text = re.sub(r'"evaluation-job-\d+": <n>', '"evaluation-job-<k>": <n>', text)  # members are ordered by spend
    names_seen: dict[str, str] = {}
    return DIGEST.sub(lambda m: names_seen.setdefault(m.group(), f"<id{len(names_seen) + 1}>"), text)


class Phase2ReportTest(unittest.TestCase):
    """report.md, manifest.json and source-map.json before and after an activation, byte for byte apart from
    timestamps and the temporary directory's name; snapshots taken from the original write_phase2_report."""

    def render(self, s) -> str:
        evaluation.write_phase2_report(s)
        return read_outputs(s.data_dir / "releases" / "phase-2", ("report.md", "manifest.json", "source-map.json"),
                            s.data_dir.parent)

    def test_outputs_are_unchanged(self):
        pinned_provenance(self)
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp))
            self.enterContext(fixtures.paid_gateway(env.settings))
            transport = generation.FakeTransport()
            test_dense._plan_and_build(env, transport)
            test_dense._dataset(env)
            s = env.settings
            runs = {o["label"]: o["run_id"] for o in evaluation.evaluate_retrieval(
                s, fixtures.analyzer(), transport, "dev-pilot", ["K0", "K1", "H"], allow_paid_queries=True)}
            before = self.render(s)
            decision = Path(tmp) / "decision.json"
            decision.write_text(json.dumps({"run_id": runs["K1"], "mode": "kiwi_bm25", "decided_by": "owner",
                                            "finalist_run_id": runs["H"], "rationale": "fixture"}), encoding="utf-8")
            evaluation.activate_run(s, runs["K1"], decision)
            after = self.render(s)
        snapshot(self, "phase2_before", before)
        snapshot(self, "phase2_after", after)


class ReleaseReportTest(unittest.TestCase):
    """The five release outputs for a draft and for a sealed release, timestamps and measured latencies masked;
    snapshots taken from the original write_release_report."""

    NAMES = ("report.md", "manifest.json", "coverage.json", "evaluation.json", "budget.json")

    def render(self, s, path: Path) -> str:
        return read_outputs(path.parent, self.NAMES, s.data_dir.parent)

    def test_a_draft_release(self):
        pinned_provenance(self)
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp))
            text = self.render(env.settings, release.write_release_report(env.settings, "latest"))
        snapshot(self, "release_draft", re.sub(r"draft-\d{4}-\d\d-\d\d", "draft-<date>", text))

    def test_a_sealed_release(self):
        """Every report test_release's sealed scenario writes (sealed, stale check, current check, changed prompt),
        captured as that scenario writes it, so the scenario lives in one place."""
        pinned_provenance(self)
        rendered = []
        original = release.write_release_report

        def capture(s, release_id=None):
            path = original(s, release_id)
            rendered.append(self.render(s, path))
            return path

        scenario = test_release.ReleaseReportTest("test_the_report_reads_a_sealed_release_without_calling_the_provider")
        with mock.patch.object(release, "write_release_report", capture):
            outcome = unittest.TestResult()
            scenario.run(outcome)
        self.assertEqual((outcome.errors, outcome.failures), ([], []))
        self.assertEqual(len(rendered), 4)
        snapshot(self, "release_sealed", "\n\0\0".join(rendered))


class LoadCheckTest(unittest.TestCase):
    """ops.load_check against the fake provider. How many of six users' requests the cap blocks depends on thread
    timing, so that run pins the invariants; the three-user run with injected timeouts is pinned whole."""

    KEYS = ["kind", "synthetic", "users", "requests_per_user", "fake_delay_seconds", "fail_every", "cap_micro_usd",
            "max_reservation_micro_usd", "affordable_at_once", "outcomes", "attempt_states", "provider_calls",
            "spent_micro_usd", "pending_unknown_micro_usd", "submit_ms", "poll_ms", "wall_seconds", "violations",
            "checks", "passed"]
    CHECKS = {"cap_never_exceeded": True, "one_generation_attempt_per_request": True,
              "no_open_reservation_left": True, "all_requests_finished": True, "duplicate_submit_reused_request": True}

    def test_six_users_under_the_cap(self):
        r = ops.load_check(users=6, requests_per_user=2, delay_seconds=0.05)
        self.assertEqual(list(r), self.KEYS)
        self.assertEqual((r["cap_micro_usd"], r["max_reservation_micro_usd"], r["checks"], r["passed"],
                          r["violations"], r["pending_unknown_micro_usd"]), (10224, 2272, self.CHECKS, True, [], 0))
        answered = r["outcomes"].get("completed/answered", 0)
        self.assertLessEqual(set(r["outcomes"]), {"completed/answered", "completed/budget_blocked"})
        self.assertEqual(sum(r["outcomes"].values()), 12)
        self.assertEqual((r["provider_calls"], r["attempt_states"], r["spent_micro_usd"]),
                         (answered, {"settled": answered}, 155 * answered))
        self.assertEqual((list(r["submit_ms"]), list(r["poll_ms"])), (["p50", "p95"], ["samples", "p50", "p95", "max"]))
        self.assertGreater(r["poll_ms"]["samples"], 0)

    def test_injected_timeouts(self):
        r = ops.load_check(users=3, requests_per_user=2, delay_seconds=0.05, fail_every=2)
        for k in ("submit_ms", "poll_ms", "wall_seconds"):
            r.pop(k)
        self.assertEqual(r, {
            "kind": "fake_provider_load_check", "synthetic": True, "users": 3, "requests_per_user": 2,
            "fake_delay_seconds": 0.05, "fail_every": 2, "cap_micro_usd": 10224, "max_reservation_micro_usd": 2272,
            "affordable_at_once": 4, "outcomes": {"completed/answered": 2, "completed/budget_blocked": 2,
                                                  "failed/technical_error": 2},
            "attempt_states": {"settled": 2, "unknown": 2}, "provider_calls": 4, "spent_micro_usd": 310,
            "pending_unknown_micro_usd": 4544, "violations": [], "checks": self.CHECKS, "passed": True})


class Phase3ReportTest(unittest.TestCase):
    """ops.write_phase3_report with nothing recorded, then with every optional record present."""

    RECORDS = {
        "load-check.json": {"users": 6, "requests_per_user": 2, "fake_delay_seconds": 0.5, "fail_every": 0,
                            "passed": True, "checks": {"cap_never_exceeded": True}, "outcomes": {"completed/answered": 12},
                            "provider_calls": 12, "poll_ms": {"p95": 4.2, "samples": 80}, "submit_ms": {"p95": 9.5}},
        "browser-results.json": {"summary": "six browsers", "screenshots": ["a.png", "b.png"],
                                 "steps": [{"step": "ask", "role": "consultant", "viewport": "1280", "outcome": "ok"}]},
        "paid-smoke.json": {"request_id": "smoke-1", "settled_micro_usd": 1200},
        "team-host.json": {"host": "codeit", "recorded_at": "2026-10-01", "reach": "tunnel", "tunnel": "ssh -L",
                           "members": ["a", "b"]},
    }

    def test_reports(self):
        rendered = []
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp))
            path = ops.write_phase3_report(env.settings)
            rendered.append(read_outputs(path.parent, ("report.md",), env.settings.data_dir.parent))
            for name, data in self.RECORDS.items():
                ops.save_json(ops.phase3_dir(env.settings) / name, data)
            path = ops.write_phase3_report(env.settings)
            rendered.append(read_outputs(path.parent, ("report.md",), env.settings.data_dir.parent))
        snapshot(self, "phase3_report", "\n\0\0".join(rendered))


class JudgeRunTest(unittest.TestCase):
    """judges.run and finalize end to end on a 400-item fake reference (link and claim items): the bridge, the Luna
    judge through the shared ledger, both Jev arms, the calibration fit, then a scored held-out run. Luna and the
    bridge answer through a fake transport; Jev is mocked. Every label follows the digit in the item's text."""

    @staticmethod
    def digit(text: str) -> int:
        return int(re.search(r"\d", text).group())

    def reference(self, s) -> None:
        items = []
        for i in range(400):
            d, item = i % 5, {"blind_id": f"B{i:04d}", "question": "기간은?", "passages": [f"기간은 {i % 5}개월"]}
            if i % 2:
                item.update(kind="claim", required=f"계약기간 {d}개월", conditions=[], answer_summary="요약",
                            answer_claims=[f"기간 {d}개월"], allowed=["correct", "incomplete_qualifier", "missing"],
                            reference="correct" if d in (1, 3) else "missing")
            else:
                item.update(kind="link", claim=f"계약기간 {d}개월", allowed=["supporting", "unsupported"],
                            reference="supporting" if d in (1, 3) else "unsupported")
            items.append(item)
        body = "".join(json.dumps(i, ensure_ascii=False, sort_keys=True) + "\n" for i in items)
        out = judges.root(s) / "reference"
        out.mkdir(parents=True)
        (out / "reference.jsonl").write_text(body, encoding="utf-8", newline="")
        (out / "manifest.json").write_text(json.dumps({"run_id": "A-test", "items": 400, "labels": {},
                                                       "reference_sha256": judges._sha(body)}), encoding="utf-8")

    def responder(self, messages):
        data, n = json.loads(messages[-1]["content"]), next(self.responses)
        if messages[0]["content"] == judges.JUDGE_INSTRUCTIONS:
            d = self.digit(data.get("claim") or data["required"])
            yes = data["allowed"][0]
            body = {"verdict": yes if d in (1, 3, 4) else data["allowed"][-1], "reason": f"digit {d}"}
        else:
            body = {"segments": [{"id": x["id"], "text": re.sub(r"[가-힣]+", "word", x["text"])}
                                 for x in data["segments"]]}
        return generation.ProviderResponse(json.dumps(body, ensure_ascii=False), None, "stop",
                                           {"prompt_tokens": 50, "completion_tokens": 10, "cached_tokens": 0},
                                           f"fake-{n}")

    def jev(self, key, model, state, questions, timeout=None):
        d = self.digit(state.get("claim") or state["required_fact"])
        answers = {"support": 0.9 if d in (1, 3) else 0.1}
        if "label" in questions:
            answers["label"] = {"probabilities": {"incomplete_qualifier": 0.3, "missing": 0.7}}
        return {"answers": answers, "usage": {"tokens": 1}, "model": model, "cost_usd": 0.0001}

    def test_calibration_then_held_out(self):
        import itertools

        from rfp_assistant.storage.postgres import gateway_lock

        self.responses = itertools.count(1)
        rendered = []
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp))
            s = env.settings
            with store.open_db(s.db_path) as conn:
                envelopes = json.loads(conn.execute("SELECT envelopes_json FROM budget_settings").fetchone()[0])
                conn.execute("UPDATE budget_settings SET envelopes_json = ?",
                             (json.dumps({**envelopes, "judge_eval": 3_000_000}),))
            self.reference(s)
            judges.make_split(s)
            owner = gateway_lock(s.db_path)
            self.addCleanup(owner.release)
            patches = [mock.patch.object(judges, "load_estimate", lambda st, e: {"part": e, "max_micro_usd": 5_000_000}),
                       mock.patch.object(judges, "recheck", lambda st, e: None),
                       mock.patch.object(judges, "organisations", lambda st: []),
                       mock.patch.object(judges, "read_api_key", lambda name: "k"),
                       mock.patch.object(judges, "jev_call", self.jev)]
            for p in patches:
                self.enterContext(p)
            for part in ("calibration", "held_out"):
                result = judges.run(s, generation.FakeTransport(self.responder), part, "owner")
                self.assertEqual((result["status"], result["stop_reason"]), ("complete", None))
                text = re.sub(r'"latency_ms": [0-9.]+', '"latency_ms": <n>', read_outputs(
                    judges.run_dir(s, result["run_id"]), ("results.json", "judgements.jsonl"), s.data_dir.parent))
                results, log = text.split("\n\0")
                firsts = {}  # the judgement log whole by digest, and each arm's first record to read
                for line in log.splitlines():
                    firsts.setdefault(re.match(r'\{"arm": "(\w+)"', line).group(1), line)
                rendered.append("\n".join([results, *firsts.values(),
                                           f"judgements.jsonl sha256 {hashlib.sha256(log.encode()).hexdigest()}"]))
        snapshot(self, "judge_runs", "\n\0\0".join(rendered))


class ImportReferenceTest(unittest.TestCase):
    """judges.import_reference on a synthetic 750-item archive in the pilot runtime's layout: the installed
    reference and manifest, the idempotent second import, and the two refusals (missing files, a receipt that does
    not match)."""

    def archive(self, root: Path) -> Path:
        run = root / "runtime" / "runs" / judges.REFERENCE_RUN
        run.mkdir(parents=True)
        sheet, key, verdicts, rows = [], {}, [], []
        for i in range(judges.REFERENCE_ITEMS):
            b, qid, kind = f"X{i:04d}", f"q-{i // 3}", ("link", "answer_claim", "claim")[i % 3]
            row = {"blind_id": b, "question": f"질문 {i}", "allowed": ["supporting", "unsupported"]}
            if kind == "link":
                row.update(claim=f"주장 {i}", cited_quote=f"인용 {i}" if i % 2 else None)
                key[b] = f"F1|{qid}|link"
            elif kind == "answer_claim":
                row.update(claim=f"답변 주장 {i}")
                key[b] = f"F1|{qid}|answer_claim|0"
                rows.append({"finalist": "F1", "question_id": qid, "answer": {"claims": [{"evidence_ids": ["E1", "E9"]}]},
                             "evidence": {"E1": {"quote": f"근거 {i}"}}})
            else:
                row.update(expected=[{"type": "number", "value": i, "unit": "개월"}, {"type": "text", "patterns": ["가", "나"]},
                                     {"type": "date", "value": "2026-01-02", "time": "10:00"}, {"type": "other"}][i % 4],
                           qualifiers=[["조건", "단서"]] if i % 2 else None, gold_quotes=[f"정답 {i}"],
                           answer_summary="요약", answer_claims=["주장"], deterministic="wrong_value" if i % 5 == 0 else None)
                key[b] = f"F1|{qid}|claim"
            sheet.append(row)
            verdicts.append({"blind_id": b, "verdict": row["allowed"][i % 2]})
        write = lambda path, lines: path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),  # noqa: E731
                                                    encoding="utf-8", newline="")
        write(run / "review-sheet.jsonl", reversed(sheet))
        write(run / "review.jsonl", [{"item": key[v["blind_id"]], "verdict": v["verdict"]} for v in verdicts])
        write(run / "rows.jsonl", rows)
        (run / "review-key.json").write_text(json.dumps(key), encoding="utf-8")
        write(root / judges.VERDICTS, verdicts)
        (root / judges.PACKET).write_text("packet", encoding="utf-8")
        (root / judges.RECEIPT).write_text(json.dumps({
            "run_id": judges.REFERENCE_RUN, "items": judges.REFERENCE_ITEMS, "reviewer": "owner",
            "all_verdicts_imported_literally_once": True, "packet_sha256": judges._sha(b"packet"),
            "verdict_sha256": judges._sha((root / judges.VERDICTS).read_bytes())}), encoding="utf-8")
        return root

    def test_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = self.archive(Path(tmp) / "archive")
            s = SimpleNamespace(data_dir=Path(tmp) / "data")
            first = judges.import_reference(s, archive)
            self.assertEqual(judges.import_reference(s, archive), {k: v for k, v in first.items() if k != "imported_at"})
            reference = (judges.root(s) / "reference" / "reference.jsonl").read_text(encoding="utf-8")
            firsts = {}
            for line in reference.splitlines():
                firsts.setdefault(json.loads(line)["kind"], line)
            text = "\n".join([json.dumps(first, ensure_ascii=False, indent=1, sort_keys=True), *firsts.values(),
                              f"reference.jsonl sha256 {hashlib.sha256(reference.encode()).hexdigest()}"])
            snapshot(self, "judge_reference", STAMP.sub("<time>", text.replace(json.dumps(str(archive))[1:-1], "<archive>")))

            (archive / judges.PACKET).write_text("changed", encoding="utf-8")
            with self.assertRaises(judges.JudgeError) as refused:
                judges.import_reference(SimpleNamespace(data_dir=Path(tmp) / "other"), archive)
            self.assertEqual(str(refused.exception), "the reference does not match its receipt; ask the owner before "
                                                     "using another reference: the blind packet hash differs from the "
                                                     "receipt")
            (archive / judges.PACKET).unlink()
            with self.assertRaisesRegex(judges.JudgeError, "^reference files are missing; ask the owner before using "
                                                           "another reference: .*blind-groups.jsonl$"):
                judges.import_reference(s, archive)


class PaidAnswerOutcomeTest(test_service.Base):
    """The exits of a paid answer the service tests do not reach, each pinned whole: the request's outcome,
    the request row's status and the attempts it left (amounts vary with the prompt, so only stage and state)."""

    def setUp(self):
        super().setUp()
        self.transport.gate.set()

    def outcome(self, request):
        r = service.answer(self.res, self.env.consultant, request)
        return {"status": r.status, "summary": r.summary, "error": r.error, "billing_state": r.billing_state,
                "missing_fields": r.missing_fields, "request": self.request_row(r.request_id)["status"],
                "attempts": [(a["stage"], a["state"]) for a in self.attempts() if a["request_id"] == r.request_id]}

    def test_every_document_unavailable(self):
        self.assertEqual(self.outcome(test_service.req(self.e)), {
            "status": "ingestion_unavailable", "summary": "원문 전체를 확인할 수 없어 답변하지 않습니다. HWP 변환기가 설정되지 "
            "않았습니다.", "error": None, "billing_state": "none", "missing_fields": [
                {"doc_id": self.e.doc_id, "field": "document", "reason": "ingestion_unavailable"}],
            "request": "completed", "attempts": []})

    def test_retrieval_failure(self):
        with mock.patch.object(service, "prepare_answer", side_effect=RuntimeError("index gone")):
            self.assertEqual(self.outcome(test_service.req(self.a)), {
                "status": "technical_error", "summary": "검색 또는 비용 추정에 실패했습니다.",
                "error": "RuntimeError: index gone", "billing_state": "none", "missing_fields": [],
                "request": "failed", "attempts": []})

    def test_a_frozen_run_whose_prompt_grew(self):
        run = service.verifier_trace(self.res, self.env.verifier, test_service.Q, [self.a], "2026-09-30")
        real = service._frozen_prep

        def grown(*a, **kw):
            prep = real(*a, **kw)
            return {**prep, "estimate_micro_usd": prep["estimate_micro_usd"] + 1}

        with mock.patch.object(service, "_frozen_prep", side_effect=grown):
            r = service.answer(self.res, self.env.verifier, AnswerRequest(
                "k", "g", test_service.Q, [self.a], as_of="2026-09-30", config_id=run["config"]["config_id"],
                verifier_run_id=run["run_id"]))
        self.assertEqual((r.status, r.summary, r.error, self.request_row(r.request_id)["status"], self.attempts(),
                          self.transport.calls), (
            "clarification_required", "검증 실행 이후 프롬프트가 바뀌어 동의한 최대 비용을 넘을 수 있습니다. 새 검증 실행을 "
            "만든 뒤 다시 생성하세요. (유료 호출 없음)", None, "failed", [], []))

    def test_no_paid_connection(self):
        with mock.patch.object(self.res, "paid_refusal", return_value="no key for this session"):
            self.assertEqual(self.outcome(test_service.req(self.a)), {
                "status": "technical_error", "summary": "유료 모델 연결이 설정되지 않았습니다.",
                "error": "no key for this session", "billing_state": "released", "missing_fields": [],
                "request": "failed", "attempts": [("generation", "released")]})

    def test_stopped_inside_the_dispatch_transaction(self):
        with mock.patch.object(service, "_dispatch_guard", return_value=lambda conn: "cancelled"):
            self.assertEqual(self.outcome(test_service.req(self.a)), {
                "status": "cancelled", "summary": "요청이 취소되어 다음 유료 단계를 시작하지 않았습니다.", "error": None,
                "billing_state": "released", "missing_fields": [], "request": "cancelled",
                "attempts": [("generation", "released")]})

    def test_connection_lost_during_the_call(self):
        self.transport.responder = lambda m: ConnectionResetError("transport closed")
        self.assertEqual(self.outcome(test_service.req(self.a)), {
            "status": "technical_error", "summary": "모델 호출 중 연결이 끊겼습니다. 비용은 확인 전까지 보류로 남습니다.",
            "error": "ConnectionResetError: transport closed", "billing_state": "unknown", "missing_fields": [],
            "request": "failed", "attempts": [("generation", "unknown")]})

    def test_provider_returned_no_usage(self):
        echo = generation.FakeTransport().responder
        self.transport.responder = lambda m: generation.ProviderResponse(echo(m).content, None, "stop", None, "r-1")
        self.assertEqual(self.outcome(test_service.req(self.a)), {
            "status": "answered", "summary": "가짜 제공자 응답입니다.", "error": None, "billing_state": "unknown",
            "missing_fields": [], "request": "completed", "attempts": [("generation", "unknown")]})

    def test_settlement_failure(self):
        with mock.patch.object(service.budget, "settle", side_effect=RuntimeError("ledger locked")):
            self.assertEqual(self.outcome(test_service.req(self.a)), {
                "status": "technical_error", "summary": "사용량을 기록하지 못했습니다. 비용은 확인 전까지 보류로 남습니다.",
                "error": "settlement_failed: RuntimeError", "billing_state": "unknown", "missing_fields": [],
                "request": "failed", "attempts": [("generation", "unknown")]})


class VerifyToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("verify_tool", ROOT / "tools" / "verify.py")
        cls.verify = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.verify)

    def test_unittest_summary(self):
        v = self.verify
        self.assertEqual(v.unittest_summary("...\nRan 12 tests in 3.4s\n\nOK\n", 0), "12 tests in 3.4 s: OK (exit 0)")
        self.assertEqual(v.unittest_summary("Ran 1 test in 0.1s\nFAILED (failures=1)", 1),
                         "1 tests in 0.1 s: FAILED (failures=1) (exit 1)")
        self.assertEqual(v.unittest_summary("Ran 2 tests in 1s", 0), "2 tests in 1 s: no result line (exit 0)")
        self.assertEqual(v.unittest_summary("nothing", 2), "no unittest summary (exit 2)")

    def test_load_check_summary(self):
        v = self.verify
        self.assertEqual(v.load_check_summary(0, 'log {"passed": true, "n": 6, "x": [1]} tail'),
                         (True, 'passed=True exit 0; {"n": 6}'))
        self.assertEqual(v.load_check_summary(1, '{"passed": true}'), (False, "passed=True exit 1; {}"))
        self.assertEqual(v.load_check_summary(0, "no json"), (False, "no JSON result (exit 0)"))
        self.assertEqual((v.TERMINAL, v.FAKE_QUESTION), (("completed", "failed", "cancelled", "interrupted"),
                                                         "하자보수 기간은 얼마인가요?"))


if __name__ == "__main__":
    unittest.main()
