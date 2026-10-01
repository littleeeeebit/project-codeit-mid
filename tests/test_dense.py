import json
import shutil
import tempfile
import unittest
import unittest.mock
import uuid
from pathlib import Path

from rfp_assistant import budget, dense, evaluation, generation, ingestion, service, store
from rfp_assistant.contracts import AnswerRequest
from rfp_assistant.retrieval import KeywordIndex, build_keyword_index
from rfp_assistant.store import get_app_setting, read_jsonl
from tests import fixtures


def _index_version(settings) -> str:
    with store.open_db(settings.db_path) as conn:
        return get_app_setting(conn, "active_index")


def _plan_and_build(env, transport):
    version = _index_version(env.settings)
    estimate = dense.plan_embeddings(env.settings, version)
    return estimate, dense.build_dense(env.settings, transport, version, estimate["estimate_id"])


class DenseBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))
        self.transport = generation.FakeTransport()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rerun_embeds_nothing_and_duplicate_bytes_share_vectors(self):
        estimate, out = _plan_and_build(self.env, self.transport)
        self.assertEqual(out["status"], "ready")
        self.assertGreater(estimate["max_cost_micro_usd"], 0)
        self.assertLess(estimate["unique_payloads"], estimate["chunks"] + 1)
        calls = len(self.transport.embed_calls)
        sent = [t for c in self.transport.embed_calls for t in c["inputs"]]
        self.assertEqual(len(sent), len(set(sent)))  # one vector per exact payload
        estimate2, out2 = _plan_and_build(self.env, self.transport)
        self.assertEqual((estimate2["payloads_to_embed"], estimate2["max_cost_micro_usd"]), (0, 0))
        self.assertEqual(len(self.transport.embed_calls), calls)
        self.assertEqual(out2["dense_version"], out["dense_version"])
        self.assertTrue(out2["reused"])
        snap = budget.snapshot(self.env.settings.db_path)
        self.assertEqual(snap.pending_micro_usd, 0)
        self.assertGreater(snap.spent_micro_usd, 0)
        # identical bytes (기관A, 기관C) resolve to the same rows, each under its own association
        s = self.env.settings
        idx = KeywordIndex.load(s)
        d = dense.DenseIndex.load(s, out["dense_version"], base=idx)
        self.assertEqual(d.chunk_ids, [c["chunk_id"] for c in idx.chunks])

    def test_only_changed_source_payloads_are_embedded_and_old_versions_stay_pinned(self):
        _, first = _plan_and_build(self.env, self.transport)
        old_index = _index_version(self.env.settings)
        s = self.env.settings
        (s.files_dir / "기관D_도서관 좌석 예약.pdf").write_bytes(
            fixtures.make_pdf([["제안요청서", "Ⅰ. 사업 안내", "열람실 좌석 배정 시스템을 구축한다."]]))
        ingestion.import_manifest(s)
        ingestion.ingest(s)
        with store.open_db(s.db_path) as conn:
            h = conn.execute("SELECT active_source_hash FROM documents WHERE filename LIKE '기관D%'").fetchone()[0]
        ingestion.record_review(s, h, "fixture-reviewer", "sample_checked", ["p1/i0"], {"fixture": True})
        new_index = build_keyword_index(s, fixtures.analyzer())["index_version"]
        self.assertNotEqual(new_index, old_index)
        estimate = dense.plan_embeddings(s, new_index)
        changed = [c for c in KeywordIndex.load(s, new_index).chunks
                   if dense.cache_get(s, dense.payload_hash(c["payload"], s.embedding_model,
                                                            s.embedding_dimensions)) is None]
        self.assertEqual(estimate["payloads_to_embed"], len({c["payload"] for c in changed}))
        self.assertTrue(changed and all("열람실" in c["payload"] or "좌석" in c["payload"] or "제안요청서" in c["payload"]
                                        for c in changed))
        # the previous index and matrix remain loadable for already-issued citations and rollback
        dense.DenseIndex.load(s, first["dense_version"], base=KeywordIndex.load(s, old_index))

    def test_corrupt_matrix_is_refused(self):
        _, out = _plan_and_build(self.env, self.transport)
        s = self.env.settings
        path = s.data_dir / "indexes" / out["dense_version"] / "embeddings.npy"
        data = bytearray(path.read_bytes())
        data[-1] ^= 0xFF
        path.write_bytes(bytes(data))
        with self.assertRaises(dense.DenseError):
            dense.DenseIndex.load(s, out["dense_version"])

    def test_failed_batch_keeps_partial_cache_and_publishes_nothing(self):
        s = self.env.settings.with_(embedding_batch_inputs=2)
        calls = []

        def flaky(inputs, dims):
            calls.append(inputs)
            if len(calls) == 2:
                return generation.ProviderError("timeout", pre_execution=False)
            return generation.fake_embeddings(inputs, dims)

        transport = generation.FakeTransport(embedder=flaky)
        version = _index_version(s)
        estimate = dense.plan_embeddings(s, version)
        self.assertGreater(estimate["batches"], 2)
        out = dense.build_dense(s, transport, version, estimate["estimate_id"])
        self.assertEqual((out["status"], out["published"]), ("unknown", False))
        self.assertEqual(len(calls), 2)  # no hidden retry of the unknown batch
        with store.open_db(s.db_path) as conn:
            states = [r[0] for r in conn.execute("SELECT state FROM attempts WHERE stage = 'embedding'")]
            dense_rows = [r for r in conn.execute("SELECT config_json FROM indexes")
                          if json.loads(r[0]).get("kind") == "dense"]
        self.assertEqual(sorted(states), ["settled", "unknown"])
        self.assertEqual(dense_rows, [])
        # the first batch stays cached: a fresh estimate needs fewer payloads, never the settled ones again
        again = dense.plan_embeddings(s, version)
        self.assertEqual(again["payloads_to_embed"], estimate["payloads_to_embed"] - 2)

    def test_unknown_batch_blocks_a_rerun_until_reconciled(self):
        s = self.env.settings
        transport = generation.FakeTransport(
            embedder=lambda inputs, dims: generation.ProviderError("timeout", pre_execution=False))
        version = _index_version(s)
        estimate = dense.plan_embeddings(s, version)
        self.assertEqual(dense.build_dense(s, transport, version, estimate["estimate_id"])["status"], "unknown")
        with self.assertRaises(dense.DenseError):
            dense.build_dense(s, self.transport, version, estimate["estimate_id"])
        self.assertEqual(self.transport.embed_calls, [])

    def test_an_estimate_does_not_block_rebuilding_its_index(self):
        s = self.env.settings
        version = _index_version(s)
        dense.plan_embeddings(s, version)
        import shutil
        shutil.rmtree(s.data_dir / "indexes" / version)
        self.assertEqual(build_keyword_index(s, fixtures.analyzer())["index_version"], version)

    def test_missing_usage_stops_further_dispatch(self):
        s = self.env.settings.with_(embedding_batch_inputs=1)
        no_usage = generation.FakeTransport(embedder=lambda inputs, dims: generation.EmbeddingResponse(
            generation.fake_embeddings(inputs, dims).vectors, None))
        version = _index_version(s)
        estimate = dense.plan_embeddings(s, version)
        self.assertGreater(estimate["batches"], 1)
        out = dense.build_dense(s, no_usage, version, estimate["estimate_id"])
        self.assertEqual((out["status"], out["published"]), ("unknown", False))
        self.assertEqual(len(no_usage.embed_calls), 1)
        self.assertEqual(dense.plan_embeddings(s, version)["payloads_to_embed"], estimate["payloads_to_embed"] - 1)
        with self.assertRaises(dense.DenseError):  # reconcile first
            dense.build_dense(s, self.transport, version, estimate["estimate_id"])

    def test_paid_disabled_or_stale_estimate_sends_nothing(self):
        s = self.env.settings
        version = _index_version(s)
        estimate = dense.plan_embeddings(s, version)
        budget.set_paid_enabled(s.db_path, "owner", False, "test")
        out = dense.build_dense(s, self.transport, version, estimate["estimate_id"])
        self.assertEqual((out["status"], out["reason"]), ("blocked", "paid_disabled"))
        self.assertEqual(self.transport.embed_calls, [])
        with self.assertRaises(dense.DenseError):
            dense.build_dense(s.with_(embedding_dimensions=256), self.transport, version, estimate["estimate_id"])


def _dataset(env) -> list[dict]:
    s = env.settings
    rows = []
    with store.open_db(s.db_path) as conn:
        for key, like, quote, qid, question in (
                ("기관A", "%하자보수%", "하자보수 기간은 검수 완료일로부터 12개월로 한다.", "q1", "검수 후 하자 보수 기간은 얼마인가?"),
                ("기관D", "%좌석%", "도서관 좌석 예약 시스템을 구축한다.", "q2", "무엇을 구축하는 사업인가? 좌석 예약")):
            ref = env.refs[key]
            x = conn.execute("SELECT active_extraction_id FROM sources WHERE source_hash = ?",
                             (ref.source_hash,)).fetchone()[0]
            el = conn.execute("SELECT element_id FROM elements WHERE extraction_id = ? AND raw_text LIKE ?",
                              (x, like)).fetchone()[0]
            rows.append({"id": qid, "type": "condition", "question": question, "doc_id": ref.doc_id,
                         "source_hash": ref.source_hash, "extraction_id": x, "split": "dev", "answerable": True,
                         "evidence": [{"element_id": el, "quote": quote}], "drafted_by": "agent",
                         "reviewed_by": "person"})
    rows.append({"id": "m1", "type": "missing_metadata", "question": "마감일은?", "doc_id": env.refs["기관D"].doc_id,
                 "split": "dev", "answerable": False, "metadata_fields": ["bid_close"], "drafted_by": "agent",
                 "reviewed_by": "person"})
    rows.append({"id": "u1", "type": "condition", "question": "검토 전", "doc_id": env.refs["기관D"].doc_id,
                 "split": "dev", "drafted_by": "agent", "reviewed_by": "agent"})
    path = evaluation.dataset_path(s, "dev-pilot")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return rows


class FakeReranker:
    """Promotes chunks mentioning 하자보수, and takes measurable but small time."""

    info = {"model": "fake-reranker", "revision": "0" * 40, "device": "cpu", "max_length": 512, "max_concurrency": 1}

    def rerank(self, question, chunks):
        scores = [float("하자보수" in c["payload"]) for c in chunks]
        order = sorted(range(len(chunks)), key=lambda i: (-scores[i], chunks[i]["chunk_id"]))
        return [(i, scores[i]) for i in order], {"truncated": 0}


class IdentityReranker:
    info = {"model": "identity", "revision": "1" * 40, "device": "cpu", "max_length": 512, "max_concurrency": 1}

    def rerank(self, question, chunks):
        return [(i, float(-i)) for i in range(len(chunks))], {"truncated": 0}


class TimedReranker(IdentityReranker):
    """Reports the queue/inference split and truncation the way LocalReranker does."""

    info = {**IdentityReranker.info, "precision": "fp16"}

    def rerank(self, question, chunks):
        order, _ = super().rerank(question, chunks)
        return order, {"truncated": 1, "queue_ms": 7.5, "infer_ms": 2.5}


class ExplodingReranker(IdentityReranker):
    def rerank(self, question, chunks):
        raise ValueError("expected sequence of length 446 at dim 1 (got 378)")


class DemotingReranker(FakeReranker):
    """Pushes the chunks that mention 하자보수 to the end."""

    def rerank(self, question, chunks):
        order, info = super().rerank(question, chunks)
        return list(reversed(order)), info


class EvaluationRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))
        self.transport = generation.FakeTransport()
        _plan_and_build(self.env, self.transport)
        _dataset(self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def test_span_grades_are_independent_of_chunking(self):
        chunk = {"spans": [{"element_id": "e", "start": 0, "end": 30}]}
        el = {"raw_text": "앞부분.  하자보수   기간은 12개월로 한다. 뒤", "table": None}
        self.assertEqual(evaluation.grade(chunk, {"element_id": "e", "quote": "하자보수 기간은 12개월로 한다."}, el), 2)
        partial = {"spans": [{"element_id": "e", "start": 0, "end": 12}]}
        self.assertEqual(evaluation.grade(partial, {"element_id": "e", "quote": "하자보수 기간은 12개월로 한다."}, el), 1)
        self.assertEqual(evaluation.grade(chunk, {"element_id": "other", "quote": "x"}, el), 0)
        table = {"raw_text": "", "table": {"cells": [{"row": 3, "col": 1, "text": "VAT 포함", "rowspan": 1}]}}
        self.assertEqual(evaluation.grade({"spans": [{"element_id": "t", "rows": [0, 3]}]},
                                          {"element_id": "t", "quote": "VAT 포함"}, table), 2)
        self.assertEqual(evaluation.grade({"spans": [{"element_id": "t", "rows": [0, 4]}]},
                                          {"element_id": "t", "quote": "VAT 포함"}, table), 1)

    def test_ndcg_ideal_and_cross_cell_table_quotes(self):
        els = {("x", "a"): {"raw_text": "가 나 다", "table": None}, ("x", "b"): {"raw_text": "라 마 바", "table": None}}
        row = {"extraction_id": "x", "evidence": [{"element_id": "a", "quote": "가 나 다"},
                                                  {"element_id": "b", "quote": "라 마 바"}]}
        ranking = [{"spans": [{"element_id": "a", "start": 0, "end": 5}]},
                   {"spans": [{"element_id": "b", "start": 0, "end": 5}]}]
        self.assertEqual(evaluation.score_row(row, ranking, ranking, els)["ndcg@5"], 1.0)
        self.assertLess(evaluation.score_row(row, ranking[::-1][:1] + [{"spans": []}] + ranking[:1], [],
                                             els)["ndcg@5"], 1.0)
        table = {"raw_text": "사업기간 | 계약일로부터 6개월", "table": {"cells": [
            {"row": 2, "col": 0, "text": "사업기간"}, {"row": 2, "col": 1, "text": "계약일로부터 6개월"}]}}
        self.assertEqual(evaluation.grade({"spans": [{"element_id": "t", "rows": [0, 2]}]},
                                          {"element_id": "t", "quote": "사업기간 | 계약일로부터 6개월"}, table), 2)

    def test_trial_runs_with_the_frozen_h_settings_not_the_process_settings(self):
        s = self.env.settings
        evaluation.evaluate_retrieval(s, fixtures.analyzer(), self.transport, "dev-pilot", ["H"],
                                      allow_paid_queries=True)
        calls = len(self.transport.embed_calls)
        other = s.with_(evidence_max_units=1, embedding_dimensions=256)
        report = evaluation.trial_reranker(other, fixtures.analyzer(), "dev-pilot", [20], reranker=IdentityReranker(),
                                           load_info=IdentityReranker.info, users=2)
        self.assertEqual(len(self.transport.embed_calls), calls)  # vectors read from H's cache, nothing paid
        hr_config, _ = evaluation.load_run(s, report["depths"][20]["run_id"])
        h_config, _ = evaluation.load_run(s, report["h_run"])
        self.assertEqual((hr_config["limits"], hr_config["embedding"]), (h_config["limits"], h_config["embedding"]))
        traces = lambda run: {t["id"]: t["packed"] for t in read_jsonl(s.data_dir / "runs" / run / "traces.jsonl")
                              if "packed" in t}  # noqa: E731
        self.assertEqual(traces(report["depths"][20]["run_id"]), traces(report["h_run"]))  # same limits applied
        self.assertTrue(any(len(v) > 1 for v in traces(report["h_run"]).values()))

    def test_losing_numeric_evidence_is_a_new_critical_failure(self):
        rows = [json.loads(x) for x in evaluation.dataset_path(self.env.settings, "dev-pilot").read_text(
            encoding="utf-8").splitlines()]
        rows[0]["type"] = "numeric_qualifier"  # q1: 하자보수 기간 12개월
        evaluation.dataset_path(self.env.settings, "dev-pilot").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        s = self.env.settings.with_(evidence_max_units=1)
        evaluation.evaluate_retrieval(s, fixtures.analyzer(), self.transport, "dev-pilot", ["H"],
                                      allow_paid_queries=True)
        bad = evaluation.trial_reranker(s, fixtures.analyzer(), "dev-pilot", [20], reranker=DemotingReranker(),
                                        load_info=DemotingReranker.info, users=2)["depths"][20]
        self.assertIn("q1", bad["new_critical_failures"])
        self.assertFalse(bad["passed"])
        good = evaluation.trial_reranker(s, fixtures.analyzer(), "dev-pilot", [20], reranker=IdentityReranker(),
                                         load_info=IdentityReranker.info, users=2)["depths"][20]
        self.assertEqual(good["new_critical_failures"], [])
        # the rule itself: a better average cannot hide a numeric row whose evidence left the packed context
        lost = {"id": "n", "type": "numeric_qualifier", "metrics": {"packed_complete": 0}}
        kept = {"id": "n", "type": "numeric_qualifier", "metrics": {"packed_complete": 1}}
        flagged = {"id": "c", "type": "condition", "critical": True, "metrics": {"packed_complete": 0}}
        self.assertEqual(evaluation.critical_failures([lost, flagged]), ["c", "n"])
        self.assertEqual(evaluation.critical_failures([kept]), [])

    def _trial(self, concurrency: int) -> str:
        info = {**IdentityReranker.info, "max_concurrency": concurrency}
        report = evaluation.trial_reranker(self.env.settings, fixtures.analyzer(), "dev-pilot", [20],
                                           reranker=IdentityReranker(), load_info=info, users=2)
        return report["depths"][20]["run_id"]

    def _force_gate(self, run_id: str, eval_version: str | None = None) -> None:
        """Stand-in for a passing trial (the tiny fixture cannot improve nDCG@5 by 0.03)."""
        d = self.env.settings.data_dir / "runs" / run_id
        scores = json.loads((d / "scores.json").read_text(encoding="utf-8"))
        scores["gate"]["passed"] = True
        (d / "scores.json").write_text(json.dumps(scores, ensure_ascii=False), encoding="utf-8")
        if eval_version is not None:
            config = json.loads((d / "config.json").read_text(encoding="utf-8"))
            config["eval_version"] = eval_version
            (d / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def test_a_gate_recorded_under_an_older_policy_cannot_be_activated_or_served(self):
        s = self.env.settings
        evaluation.evaluate_retrieval(s, fixtures.analyzer(), self.transport, "dev-pilot", ["H"],
                                      allow_paid_queries=True)
        legacy, current = self._trial(1), None
        self._force_gate(legacy, "retrieval-eval-1")
        decision = Path(self.tmp.name) / "decision.json"
        decision.write_text(json.dumps({"run_id": legacy, "mode": "hybrid_rerank", "decided_by": "owner",
                                        "rationale": "old pass"}), encoding="utf-8")
        with self.assertRaises(evaluation.EvaluationError) as ctx:
            evaluation.activate_run(s, legacy, decision)
        self.assertIn("evaluation policy", str(ctx.exception))
        current = self._trial(2)
        self._force_gate(current)
        decision.write_text(json.dumps({"run_id": current, "mode": "hybrid_rerank", "decided_by": "owner",
                                        "rationale": "current pass"}), encoding="utf-8")
        active = evaluation.activate_run(s, current, decision)
        self.assertEqual((active["mode"], active["reranker"]["max_concurrency"]), ("hybrid_rerank", 2))
        # an HR selection made under a superseded policy keeps hybrid retrieval but stops reranking
        with store.open_db(s.db_path) as conn, store.tx(conn, immediate=True):
            store.set_app_setting(conn, "active_run", json.dumps({**active, "eval_version": "retrieval-eval-1"}))
        res = service.Resources(s, transport=self.transport)
        try:
            served = res.serving()
        finally:
            res.close()
        self.assertEqual((served["mode"], served["reranker"], served["stale_policy"]),
                         ("hybrid", None, "retrieval-eval-1"))

    def test_the_measured_reranker_concurrency_is_frozen_and_served(self):
        s = self.env.settings
        evaluation.evaluate_retrieval(s, fixtures.analyzer(), self.transport, "dev-pilot", ["H"],
                                      allow_paid_queries=True)
        one, two = self._trial(1), self._trial(2)
        self.assertNotEqual(one, two)  # the bound is part of the run identity
        self.assertEqual(evaluation.load_run(s, one)[0]["reranker"]["max_concurrency"], 1)
        self._force_gate(one)
        decision = Path(self.tmp.name) / "decision.json"
        decision.write_text(json.dumps({"run_id": one, "mode": "hybrid_rerank", "decided_by": "owner",
                                        "rationale": "test"}), encoding="utf-8")
        evaluation.activate_run(s, one, decision)
        seen = []
        res = service.Resources(s.with_(reranker_max_concurrency=6), transport=self.transport)
        try:
            with unittest.mock.patch.object(dense, "load_reranker",
                                            side_effect=lambda st: seen.append(st) or (IdentityReranker(), {})):
                self.assertIsNotNone(res.reranker())
            self.assertEqual(res.run_settings().reranker_max_concurrency, 1)
        finally:
            res.close()
        self.assertEqual([(x.reranker_max_concurrency, x.reranker_max_length) for x in seen], [(1, 512)])

    def test_comparison_recommends_k1_without_measured_benefit_and_drafts_an_inert_decision(self):
        s = self.env.settings
        runs = {o["label"]: o["run_id"] for o in evaluation.evaluate_retrieval(
            s, fixtures.analyzer(), self.transport, "dev-pilot", ["K0", "K1", "D", "H"], allow_paid_queries=True)}
        out = evaluation.compare_runs(s, list(runs.values()))
        self.assertEqual(out["recommendation"]["selected"], runs["K1"])  # D/H gain < 0.03 on this fixture
        self.assertIn(out["recommendation"]["finalist"], (runs["D"], runs["H"]))
        table = Path(out["markdown"]).read_text(encoding="utf-8")
        self.assertIn("Wilson", table)
        self.assertIn(runs["H"], table)
        draft_path = Path(self.tmp.name) / "decision-draft.json"
        draft = evaluation.draft_activation(s, list(runs.values()), draft_path)
        self.assertEqual((draft["run_id"], draft["mode"], draft["decided_by"], draft["blocking"]),
                         (runs["K1"], "kiwi_bm25", "", []))
        self.assertIn("nDCG@5", draft["rationale_draft"])
        with self.assertRaises(evaluation.EvaluationError):  # the draft alone activates nothing
            evaluation.activate_run(s, runs["K1"], draft_path)
        decision = json.loads(draft_path.read_text(encoding="utf-8"))
        decision.update(decided_by="owner", rationale=decision["rationale_draft"])
        draft_path.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
        report = evaluation.write_phase2_report(s).read_text(encoding="utf-8")
        self.assertIn("| selection activated under the current policy | **no** |", report)
        active = evaluation.activate_run(s, runs["K1"], draft_path)
        self.assertEqual(active["finalist_run_id"], decision["finalist_run_id"])
        report = evaluation.write_phase2_report(s).read_text(encoding="utf-8")
        self.assertIn("| selection activated under the current policy | yes |", report)
        self.assertIn("but the dataset does not validate", report)  # the 4-row fixture is not a valid pilot
        self.assertIn("## Inputs for phase 3", report)
        source_map = json.loads((s.data_dir / "releases" / "phase-2" / "source-map.json").read_text(encoding="utf-8"))
        self.assertEqual(len(source_map), 4)
        # a run scored under another policy is shown as blocked and never recommended
        legacy_dir = s.data_dir / "runs" / runs["H"]
        config = json.loads((legacy_dir / "config.json").read_text(encoding="utf-8"))
        config["eval_version"] = "retrieval-eval-1"
        (legacy_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
        summary = {r["run_id"]: r for r in evaluation.compare_runs(s, list(runs.values()))["runs"]}
        self.assertTrue(any("evaluation policy" in e for e in summary[runs["H"]]["blocking"]))
        with self.assertRaises(evaluation.EvaluationError):  # an owner override cannot pick a blocked run either
            draft = evaluation.draft_activation(s, list(runs.values()), Path(self.tmp.name) / "d2.json",
                                                select=runs["H"])
            decision = {**draft, "decided_by": "owner", "rationale": "x"}
            (Path(self.tmp.name) / "d2.json").write_text(json.dumps(decision), encoding="utf-8")
            evaluation.activate_run(s, runs["H"], Path(self.tmp.name) / "d2.json")

    def test_trial_reports_queue_inference_truncation_and_precision(self):
        s = self.env.settings
        evaluation.evaluate_retrieval(s, fixtures.analyzer(), self.transport, "dev-pilot", ["H"],
                                      allow_paid_queries=True)
        report = evaluation.trial_reranker(s, fixtures.analyzer(), "dev-pilot", [20], reranker=TimedReranker(),
                                           load_info=TimedReranker.info, users=2)
        run = report["depths"][20]["run_id"]
        config, scores = evaluation.load_run(s, run)
        latency = scores["latency_ms"]
        self.assertEqual((latency["queue_p95_under_load"], latency["infer_p95_under_load"]), (7.5, 2.5))
        self.assertGreater(latency["truncated_pairs"], 0)
        self.assertEqual(config["reranker"]["precision"], "fp16")
        traces = read_jsonl(s.data_dir / "runs" / run / "traces.jsonl")
        self.assertTrue(any((t.get("timings_ms") or {}).get("rerank_queue") == 7.5 for t in traces))
        with self.assertRaises(Exception):
            from rfp_assistant.settings import validate
            validate(s.with_(reranker_precision="int4"))

    def test_an_inference_error_becomes_a_bypass_artifact_not_a_crash_or_a_score(self):
        s = self.env.settings
        evaluation.evaluate_retrieval(s, fixtures.analyzer(), self.transport, "dev-pilot", ["H"],
                                      allow_paid_queries=True)
        report = evaluation.trial_reranker(s, fixtures.analyzer(), "dev-pilot", [10, 20], reranker=ExplodingReranker(),
                                           load_info=ExplodingReranker.info, users=2)
        self.assertEqual((report["gate"]["passed"], report["gate"]["decision"]), (False, "bypass"))
        self.assertIn("reranker inference failed", report["gate"]["reason"])
        _, scores = evaluation.load_run(s, report["run_id"])
        self.assertEqual(scores["status"], "blocked")

    def test_shared_tokenizer_is_never_used_by_two_workers_at_once(self):
        import threading
        import time as _time

        class Tokenizer:
            def __init__(self):
                self.busy = threading.Lock()
                self.overlaps = 0

            def use(self):
                if not self.busy.acquire(blocking=False):
                    self.overlaps += 1
                    return
                _time.sleep(0.002)
                self.busy.release()

            def __call__(self, q, p):
                self.use()
                return {"input_ids": list(range(len(p)))}

        class Model:
            def __init__(self):
                self.tokenizer = Tokenizer()

            def predict(self, pairs, show_progress_bar=False):
                self.tokenizer.use()  # prediction preprocesses with the same tokenizer
                return [float(len(p)) for _, p in pairs]

        r = dense.LocalReranker.__new__(dense.LocalReranker)
        r.model, r.max_length, r._sem = Model(), 512, threading.BoundedSemaphore(1)
        chunks = [{"chunk_id": f"c{i}", "payload": "x" * (i + 1)} for i in range(5)]
        threads = [threading.Thread(target=lambda: [r.rerank("q", chunks) for _ in range(5)]) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(r.model.tokenizer.overlaps, 0)
        from rfp_assistant.settings import SettingsError, validate
        with self.assertRaises(SettingsError):
            validate(self.env.settings.with_(reranker_max_concurrency=2))
        with self.assertRaises(dense.DenseError):
            dense.LocalReranker(self.env.settings.with_(reranker_revision="abc", reranker_max_concurrency=2))

    def test_missing_query_usage_stops_paid_evaluation_queries(self):
        no_usage = generation.FakeTransport(embedder=lambda inputs, dims: generation.EmbeddingResponse(
            generation.fake_embeddings(inputs, dims).vectors, None))
        out = evaluation.evaluate_retrieval(self.env.settings, fixtures.analyzer(), no_usage, "dev-pilot", ["D"],
                                            allow_paid_queries=True)
        self.assertEqual(out[0]["status"], "blocked")
        self.assertEqual(len(no_usage.embed_calls), 1)  # the second question is not paid for

    def test_runs_are_retrieval_only_frozen_and_reused(self):
        s = self.env.settings
        blocked = evaluation.evaluate_retrieval(s, fixtures.analyzer(), self.transport, "dev-pilot", ["D"])
        self.assertEqual(blocked[0]["status"], "blocked")  # uncached queries are not paid without the flag
        self.assertEqual(sum(len(c["inputs"]) == 1 for c in self.transport.embed_calls), 0)
        out = evaluation.evaluate_retrieval(s, fixtures.analyzer(), self.transport, "dev-pilot",
                                            ["K0", "K1", "D", "H"], allow_paid_queries=True)
        self.assertEqual([o["status"] for o in out], ["complete"] * 4)
        self.assertEqual(self.transport.calls, [])  # no answer generation
        query_calls = [c for c in self.transport.embed_calls if len(c["inputs"]) == 1]
        self.assertEqual(len(query_calls), 2)  # each question embedded once, shared by D and H
        config, scores = evaluation.load_run(s, out[3]["run_id"])
        agg = scores["aggregate"]
        self.assertEqual(agg["passage_rows"], 2)
        self.assertEqual(agg["not_scored"]["non_passage_rows"], 1)
        self.assertEqual([x["reason"] for x in agg["not_scored"]["skipped"]], ["not_independently_reviewed"])
        self.assertEqual(agg["wrong_scope_candidates"], 0)
        self.assertEqual(config["dataset_sha256"], evaluation.load_eval_rows(s, "dev-pilot")[2])
        with store.open_db(s.db_path) as conn:
            purposes = {r[0] for r in conn.execute("SELECT purpose FROM attempts WHERE stage = 'embedding' "
                                                   "AND estimated_input_tokens < 100")}
        self.assertEqual(purposes, {"gold_eval"})
        again = evaluation.evaluate_retrieval(s, fixtures.analyzer(), self.transport, "dev-pilot", ["K1", "H"])
        self.assertTrue(all(o["reused"] for o in again))
        self.assertEqual(len([c for c in self.transport.embed_calls if len(c["inputs"]) == 1]), 2)
        self.assertTrue((s.data_dir / "runs" / out[1]["run_id"] / "report.md").exists())

    def test_reranker_trial_gate_and_activation(self):
        s = self.env.settings
        runs = {o["label"]: o["run_id"] for o in evaluation.evaluate_retrieval(
            s, fixtures.analyzer(), self.transport, "dev-pilot", ["K1", "H"], allow_paid_queries=True)}
        report = evaluation.trial_reranker(s, fixtures.analyzer(), "dev-pilot", [10, 20], reranker=FakeReranker(),
                                           load_info=FakeReranker.info, users=2)
        self.assertEqual(set(report["depths"]), {10, 20})
        hr = report["depths"][20]
        self.assertIn("added_p95_ms_under_load", hr)
        self.assertEqual(hr["passed"], hr["ndcg@5_gain"] >= 0.03 and not hr["new_critical_failures"]
                         and hr["added_p95_ms_under_load"] <= 1000)
        bypass = evaluation.trial_reranker(s, fixtures.analyzer(), "dev-pilot", [10], reranker=None,
                                           load_info=None)
        self.assertEqual(bypass["gate"]["decision"], "bypass")  # unpinned revision: the bypass stays

        decision = Path(self.tmp.name) / "decision.json"
        if not hr["passed"]:
            decision.write_text(json.dumps({"run_id": hr["run_id"], "mode": "hybrid_rerank", "decided_by": "owner",
                                            "rationale": "try"}), encoding="utf-8")
            with self.assertRaises(evaluation.EvaluationError):
                evaluation.activate_run(s, hr["run_id"], decision)
        decision.write_text(json.dumps({"run_id": runs["H"], "mode": "hybrid", "decided_by": "owner",
                                        "rationale": "dev hit@20 and nDCG@5 improved over K1",
                                        "finalist_run_id": runs["K1"]}), encoding="utf-8")
        active = evaluation.activate_run(s, runs["H"], decision)
        self.assertEqual((active["mode"], active["finalist_run_id"]), ("hybrid", runs["K1"]))
        with store.open_db(s.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM activations").fetchone()[0], 1)
        report_path = evaluation.write_phase2_report(s)
        text = report_path.read_text(encoding="utf-8")
        self.assertIn(runs["H"], text)
        self.assertIn("## Selection", text)



class PopulationAndGateTest(unittest.TestCase):
    """Runs bind the population they scored; report gates are recomputed from today's dataset and sources."""

    QUOTE = "하자보수 기간은 검수 완료일로부터 12개월로 한다."

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = fixtures.make_env(self.root)
        self.transport = generation.FakeTransport()
        _plan_and_build(self.env, self.transport)
        s = self.env.settings
        evaluation.assign_families(s)
        path = s.data_dir / "datasets" / "families.json"
        fams = json.loads(path.read_text(encoding="utf-8"))
        for f in fams["families"].values():
            f["split"] = "dev"
        path.write_text(json.dumps(fams, ensure_ascii=False), encoding="utf-8")
        self.fam_of = {d: k for k, f in fams["families"].items() for d in f["doc_ids"]}

    def tearDown(self):
        self.tmp.cleanup()

    def element(self, key: str, like: str) -> tuple[str, str]:
        ref = self.env.refs[key]
        with store.open_db(self.env.settings.db_path) as conn:
            x = conn.execute("SELECT active_extraction_id FROM sources WHERE source_hash = ?",
                             (ref.source_hash,)).fetchone()[0]
            return x, conn.execute("SELECT element_id FROM elements WHERE extraction_id = ? AND raw_text LIKE ?",
                                   (x, like)).fetchone()[0]

    def row(self, rid: str, key: str, like: str, quote: str, question: str, kind: str = "condition") -> dict:
        ref = self.env.refs[key]
        x, el = self.element(key, like)
        return {"id": rid, "type": kind, "question": question, "doc_id": ref.doc_id, "source_hash": ref.source_hash,
                "extraction_id": x, "family": self.fam_of[ref.doc_id], "split": "dev", "answerable": True,
                "evidence": [{"element_id": el, "quote": quote}], "drafted_by": "agent", "reviewed_by": "person"}

    def write(self, rows: list[dict], name: str = "dev-pilot") -> None:
        store.write_jsonl_atomic(evaluation.dataset_path(self.env.settings, name), rows)

    def evaluate(self, labels, name="dev-pilot", force=False):
        return {o["label"]: o for o in evaluation.evaluate_retrieval(
            self.env.settings, fixtures.analyzer(), self.transport, name, labels, allow_paid_queries=True,
            force=force)}

    def revise(self, filename: str, lines: list[str]) -> None:
        s = self.env.settings
        (s.files_dir / filename).write_bytes(fixtures.make_pdf([lines]))
        ingestion.import_manifest(s)
        ingestion.ingest(s)

    def test_dropping_a_hard_question_is_not_an_improvement(self):
        s = self.env.settings
        q1 = self.row("q1", "기관D", "%좌석%", "도서관 좌석 예약 시스템을 구축한다.", "열람 공간 사용 신청 방식")  # lexical miss
        q2 = self.row("q2", "기관A", "%하자보수%", self.QUOTE, "검수 후 하자 보수 기간은 얼마인가?")
        self.write([q1, q2])
        k1 = self.evaluate(["K1"])["K1"]
        self.revise("기관D_도서관 좌석 예약.pdf", ["제안요청서", "Ⅰ. 사업 안내", "열람실 좌석 배정 시스템을 구축한다."])
        d = self.evaluate(["D"])["D"]
        k1_config, _ = evaluation.load_run(s, k1["run_id"])
        d_config, _ = evaluation.load_run(s, d["run_id"])
        self.assertEqual(k1_config["dataset_sha256"], d_config["dataset_sha256"])  # same bytes...
        self.assertNotEqual(k1_config["population_sha256"], d_config["population_sha256"])  # ...other questions
        self.assertEqual((k1_config["population_size"], d_config["population_size"]), (2, 1))
        out = evaluation.compare_runs(s, [k1["run_id"], d["run_id"]])
        self.assertNotEqual(out["recommendation"]["selected"], d["run_id"])
        k1_summary = {r["run_id"]: r for r in out["runs"]}[k1["run_id"]]
        self.assertTrue(any("evaluated population changed" in e for e in k1_summary["blocking"]))
        self.assertIn("Not directly comparable", Path(out["markdown"]).read_text(encoding="utf-8"))
        # rerunning K1 after the change scores the new population under a new identity, not the old frozen run
        again = self.evaluate(["K1"])["K1"]
        self.assertNotEqual(again["run_id"], k1["run_id"])
        self.assertFalse(again["reused"])
        rec = evaluation.compare_runs(s, [again["run_id"], d["run_id"]])["recommendation"]
        self.assertEqual(rec["selected"], again["run_id"])  # matching population: compared normally
        self.assertEqual(rec["finalist"], d["run_id"])

    def test_the_trial_uses_the_h_run_matching_todays_population(self):
        s = self.env.settings
        q1 = self.row("q1", "기관D", "%좌석%", "도서관 좌석 예약 시스템을 구축한다.", "도서관 좌석 예약은 무엇인가?")
        q2 = self.row("q2", "기관A", "%하자보수%", self.QUOTE, "검수 후 하자 보수 기간은 얼마인가?")
        self.write([q1, q2])
        h_a = self.evaluate(["H"])["H"]["run_id"]
        original = (s.files_dir / "기관D_도서관 좌석 예약.pdf").read_bytes()
        self.revise("기관D_도서관 좌석 예약.pdf", ["제안요청서", "Ⅰ. 사업 안내", "열람실 좌석 배정 시스템을 구축한다."])
        h_b = self.evaluate(["H"])["H"]["run_id"]
        self.assertNotEqual(h_a, h_b)
        (s.files_dir / "기관D_도서관 좌석 예약.pdf").write_bytes(original)  # back to population A
        ingestion.import_manifest(s)
        ingestion.ingest(s)
        self.assertEqual(self.evaluate(["H"])["H"]["run_id"], h_a)
        calls = len(self.transport.embed_calls)
        report = evaluation.trial_reranker(s, fixtures.analyzer(), "dev-pilot", [20], reranker=IdentityReranker(),
                                           load_info=IdentityReranker.info, users=2)
        self.assertEqual(report["h_run"], h_a)
        self.assertEqual(len(self.transport.embed_calls), calls)
        shutil.rmtree(s.data_dir / "runs" / h_a)  # no H over today's population: the rerun instruction stays
        with self.assertRaises(evaluation.EvaluationError) as ctx:
            evaluation.trial_reranker(s, fixtures.analyzer(), "dev-pilot", [20], reranker=IdentityReranker(),
                                      load_info=IdentityReranker.info, users=2)
        self.assertIn("run evaluate-retrieval with H", str(ctx.exception))

    def test_equal_sized_but_different_question_sets_differ(self):
        q2 = self.row("q2", "기관A", "%하자보수%", self.QUOTE, "검수 후 하자 보수 기간은 얼마인가?")
        rows, skipped = [q2], []
        other = dict(q2, question="하자보수는 언제부터 계산하나?")
        self.assertNotEqual(evaluation.population_identity(rows, skipped),
                            evaluation.population_identity([other], skipped))
        self.write([q2])
        first = self.evaluate(["K1"])["K1"]
        self.write([other])
        second = self.evaluate(["K1"])["K1"]
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertFalse(second["reused"])

    def _valid_pilot(self) -> list[dict]:
        converted = self.root / "converted.pdf"
        converted.write_bytes(fixtures.make_pdf([["재난 관리 시스템", "Ⅰ. 사업 안내", "상황 전파 기능을 제공한다."]]))
        review = self.root / "recovery.json"
        review.write_text(json.dumps({"reviewer": "owner", "method": "hancom_pdf_export",
                                      "compared_locations": [{"page": 1}], "fidelity_passed": True,
                                      "mapping_limitations": "converted PDF pages"}), encoding="utf-8")
        ingestion.recover_source(self.env.settings, self.env.refs["기관E"].doc_id, converted, review)
        kinds = ("late_content", "table_fact", "repeated_code", "condition", "numeric_qualifier")
        rows = [self.row(f"p{i}", "기관A", "%하자보수%", self.QUOTE, f"하자보수 조건 질문 {i}", kinds[i % 5])
                for i in range(20)]
        c = self.env.refs["기관C"]
        rows += [{"id": f"m{i}", "type": ("missing_metadata", "provenance_conflict")[i % 2], "question": f"메타 {i}",
                  "doc_id": c.doc_id, "source_hash": c.source_hash, "family": self.fam_of[c.doc_id], "split": "dev",
                  "answerable": True, "metadata_fields": ["institution"], "drafted_by": "agent",
                  "reviewed_by": "person"} for i in range(4)]
        return rows

    def gates(self) -> dict:
        evaluation.write_phase2_report(self.env.settings)
        manifest = json.loads((self.env.settings.data_dir / "releases" / "phase-2" / "manifest.json").read_text(
            encoding="utf-8"))
        return {g["gate"]: g["met"] for g in manifest["gates"]}

    def test_report_gates_follow_the_current_dataset_and_a_compatible_pair(self):
        rows = self._valid_pilot()
        self.write(rows)
        self.assertTrue(evaluation.validate_gold(self.env.settings, "dev-pilot")["ok"])
        runs = self.evaluate(["K0", "K1"])
        g = self.gates()
        self.assertTrue(g["independently reviewed dev dataset validates"])
        self.assertTrue(g["frozen K0/K1 comparison under the current policy"])
        # a passing validation file does not survive a changed dataset
        self.write([])
        g = self.gates()
        self.assertFalse(g["independently reviewed dev dataset validates"])
        self.assertFalse(g["frozen K0/K1 comparison under the current policy"])
        # K0 on another dataset and K1 on this one are not a pair
        self.write(rows)
        self.write(rows[:23] + [dict(rows[23], question="다른 질문")], name="other-set")
        shutil.rmtree(self.env.settings.data_dir / "runs" / runs["K0"]["run_id"])
        self.evaluate(["K0"], name="other-set")
        self.assertFalse(self.gates()["frozen K0/K1 comparison under the current policy"])
        self.evaluate(["K0"])
        self.assertTrue(self.gates()["frozen K0/K1 comparison under the current policy"])
        # same dataset bytes, but a source revision makes the pinned evidence stale
        self.revise("기관A_통합 정보시스템.pdf", ["제안요청서", "Ⅲ. 계약 조건", "하자보수 기간은 24개월로 한다."])
        g = self.gates()
        self.assertFalse(g["independently reviewed dev dataset validates"])
        self.assertFalse(g["frozen K0/K1 comparison under the current policy"])



class IndexUpgradeTest(unittest.TestCase):
    """An index and runs recorded under an earlier query policy must be rebuilt, not silently reused."""

    def test_runs_on_an_index_from_an_earlier_query_policy_require_a_rebuild(self):
        from rfp_assistant import retrieval

        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(retrieval, "QUERY_POLICY", "scope-redundant-1"):
                env = fixtures.make_env(Path(tmp))  # keyword index built under the earlier policy
                transport = generation.FakeTransport()
                _plan_and_build(env, transport)
                _dataset(env)
                s = env.settings
                old = {o["label"]: o for o in evaluation.evaluate_retrieval(
                    s, fixtures.analyzer(), transport, "dev-pilot", ["K1"])}["K1"]
            old_index = _index_version(s)
            with self.assertRaises(evaluation.EvaluationError) as ctx:  # no silent reuse of the old frozen run
                evaluation.evaluate_retrieval(s, fixtures.analyzer(), transport, "dev-pilot", ["K1"],
                                              index_version=old_index)
            self.assertIn("rebuild it with build-keyword", str(ctx.exception))
            self.assertTrue(any("rebuild" in e for e in evaluation.run_errors(s, old["run_id"])))
            decision = Path(tmp) / "decision.json"
            decision.write_text(json.dumps({"run_id": old["run_id"], "mode": "kiwi_bm25", "decided_by": "owner",
                                            "rationale": "old"}), encoding="utf-8")
            with self.assertRaises(evaluation.EvaluationError):
                evaluation.activate_run(s, old["run_id"], decision)
            res = service.Resources(s, transport=transport)
            try:
                r = service.retrieve(res, env.consultant, "하자보수 기간", [env.refs["기관A"]])
            finally:
                res.close()
            self.assertTrue(any(x.startswith("index_outdated:") for x in r.limitations))  # served, but flagged
            rebuilt = build_keyword_index(s, fixtures.analyzer(), activate=False)["index_version"]
            self.assertNotEqual(rebuilt, old_index)
            self.assertTrue((s.data_dir / "indexes" / old_index / "manifest.json").exists())  # kept for citations
            self.assertEqual(dense.plan_embeddings(s, rebuilt)["payloads_to_embed"], 0)  # vectors are reused
            new = {o["label"]: o for o in evaluation.evaluate_retrieval(
                s, fixtures.analyzer(), transport, "dev-pilot", ["K1"], index_version=rebuilt)}["K1"]
            self.assertEqual(evaluation.run_errors(s, new["run_id"]), [])

    def test_an_index_without_the_metadata_snapshot_is_incompatible(self):
        from rfp_assistant import retrieval

        index = KeywordIndex("legacy", "reviewed_only", [], None, {}, {}, has_metadata_snapshot=False)
        self.assertIn("predates the frozen title/institution snapshot", retrieval.index_compatibility(index))


class ServingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))
        self.transport = generation.FakeTransport()
        _, self.built = _plan_and_build(self.env, self.transport)
        _dataset(self.env)
        s = self.env.settings
        runs = {o["label"]: o["run_id"] for o in evaluation.evaluate_retrieval(
            s, fixtures.analyzer(), self.transport, "dev-pilot", ["H"], allow_paid_queries=True)}
        decision = Path(self.tmp.name) / "decision.json"
        decision.write_text(json.dumps({"run_id": runs["H"], "mode": "hybrid", "decided_by": "owner",
                                        "rationale": "test"}), encoding="utf-8")
        evaluation.activate_run(s, runs["H"], decision)
        self.res = service.Resources(s, transport=self.transport)

    def tearDown(self):
        self.res.close()
        self.tmp.cleanup()

    def test_serving_uses_the_activated_runs_embedding_and_limits(self):
        s = self.env.settings.with_(embedding_dimensions=256, channel_top_k=3, evidence_max_units=1)
        res = service.Resources(s, transport=self.transport)
        before = len(self.transport.embed_calls)
        try:
            run = res.serving()
            self.assertEqual((run["embedding"]["dims"], run["limits"]["evidence_max_units"]), (1536, 6))
            result = service.answer(res, self.env.consultant, AnswerRequest(
                idempotency_key=str(uuid.uuid4()), generation_id="g", question="새로운 질문: 하자보수 기간 조건",
                scope=[self.env.refs["기관A"]]))
        finally:
            res.close()
        self.assertNotEqual(result.status, "technical_error", result.error)
        self.assertEqual([c["dimensions"] for c in self.transport.embed_calls[before:]], [1536])
        with store.open_db(s.db_path) as conn:
            status = conn.execute("SELECT status FROM requests WHERE request_id = ?",
                                  (result.request_id,)).fetchone()[0]
        self.assertEqual(status, "completed")

    def test_a_query_vector_of_another_size_falls_back_without_scoring(self):
        from rfp_assistant.retrieval import retrieve

        idx = self.res.index()
        r = retrieve(self.res.run_settings(), idx, fixtures.analyzer(), "하자보수 기간",
                     [(self.env.refs["기관A"], self.res.index().chunks[0]["extraction_id"])], mode="hybrid",
                     dense=self.res.dense(), query_vector=[0.0] * 256)
        self.assertEqual(r.mode, "kiwi_bm25")
        self.assertIn("query_vector_dimension_mismatch", r.fallback)

    def test_resolved_metadata_is_what_search_shows_and_finds(self):
        c = self.env.refs["기관C"]
        ingestion.resolve_metadata(self.env.settings, c.doc_id, "institution", "Canonical Agency",
                                   "공고문 1쪽 기관명", "notice p1", "owner")
        (hit,) = service.search_projects(self.res, self.env.consultant, {}, "Canonical Agency")
        self.assertEqual((hit["doc_id"], hit["institution"]), (c.doc_id, "Canonical Agency"))
        self.assertEqual(hit["csv_metadata"], {"institution": "기관C"})
        (hit,) = service.search_projects(self.res, self.env.consultant, {"institution": "Canonical"}, "")
        self.assertEqual(hit["institution"], "Canonical Agency")

    def test_reranker_reloads_when_its_input_length_changes(self):
        loads = []

        class Loaded:
            def __init__(self, s):
                self.max_length = s.reranker_max_length

        def fake_load(s):
            loads.append(s.reranker_max_length)
            return Loaded(s), {}

        def select(length):
            with store.open_db(self.env.settings.db_path) as conn, store.tx(conn, immediate=True):
                run = json.loads(get_app_setting(conn, "active_run"))
                run["reranker"] = {"model": "m", "revision": "r", "max_length": length, "depth": 20}
                store.set_app_setting(conn, "active_run", json.dumps(run))

        with unittest.mock.patch.object(dense, "load_reranker", side_effect=fake_load):
            select(512)
            self.assertEqual(self.res.reranker().max_length, 512)
            select(1024)
            self.assertEqual(self.res.reranker().max_length, 1024)
            first = self.res.reranker()
            self.assertIs(self.res.reranker(), first)
            with store.open_db(self.env.settings.db_path) as conn, store.tx(conn, immediate=True):
                run = json.loads(get_app_setting(conn, "active_run"))
                run["reranker"]["precision"] = "fp16"
                store.set_app_setting(conn, "active_run", json.dumps(run))
            self.assertIsNot(self.res.reranker(), first)  # a precision change reloads the model
            self.assertEqual(self.res.run_settings().reranker_precision, "fp16")
        self.assertEqual(loads, [512, 1024, 1024])

    def test_a_legacy_hr_without_recorded_precision_serves_fp32(self):
        with store.open_db(self.env.settings.db_path) as conn, store.tx(conn, immediate=True):
            run = json.loads(get_app_setting(conn, "active_run"))
            run["reranker"] = {"model": "m", "revision": "r", "max_length": 512, "max_concurrency": 1, "depth": 20}
            store.set_app_setting(conn, "active_run", json.dumps(run))
        res = service.Resources(self.env.settings.with_(reranker_precision="fp16"), transport=self.transport)
        try:
            self.assertEqual(res.run_settings().reranker_precision, "fp32")
        finally:
            res.close()

    def test_answer_billing_reports_every_paid_stage(self):
        no_usage = generation.FakeTransport(embedder=lambda inputs, dims: generation.EmbeddingResponse(
            generation.fake_embeddings(inputs, dims).vectors, None))
        res = service.Resources(self.env.settings, transport=no_usage)
        try:
            r = service.answer(res, self.env.consultant, AnswerRequest(
                idempotency_key=str(uuid.uuid4()), generation_id="g", question="처음 묻는 하자보수 질문",
                scope=[self.env.refs["기관A"]]))
        finally:
            res.close()
        with store.open_db(self.env.settings.db_path) as conn:
            stages = {a["stage"]: a["state"] for a in conn.execute(
                "SELECT stage, state FROM attempts WHERE request_id = ?", (r.request_id,))}
        self.assertEqual(stages, {"embedding": "unknown", "generation": "settled"})
        self.assertEqual((r.status, r.billing_state, len(r.attempt_ids)), ("answered", "unknown", 2))
        real = budget.reserve

        def no_generation(*args, **kw):
            if kw.get("stage") == "generation":
                return {"admitted": False, "reason": "cap_exhausted", "attempt_id": None}
            return real(*args, **kw)

        with unittest.mock.patch.object(budget, "reserve", side_effect=no_generation):
            r = service.answer(self.res, self.env.consultant, AnswerRequest(
                idempotency_key=str(uuid.uuid4()), generation_id="g", question="또 다른 하자보수 질문",
                scope=[self.env.refs["기관A"]]))
        self.assertEqual((r.status, r.billing_state, len(r.attempt_ids)), ("budget_blocked", "settled", 1))

    def test_free_retrieval_never_pays_for_a_query_vector(self):
        ref = self.env.refs["기관A"]
        before = len(self.transport.embed_calls)
        r = service.retrieve(self.res, self.env.consultant, "완전히 새로운 질문 하자보수", [ref])
        self.assertEqual(len(self.transport.embed_calls), before)
        self.assertEqual(r.mode, "kiwi_bm25")
        self.assertIn("query_vector_unavailable", r.fallback)
        cached = service.retrieve(self.res, self.env.consultant, "검수 후 하자 보수 기간은 얼마인가?", [ref])
        self.assertEqual((cached.mode, cached.query_embedding["cache"]), ("hybrid", "hit"))
        self.assertTrue({c["channel"] for c in cached.candidates} >= {"bm25", "dense", "rrf"})

    def test_empty_scope_incurs_no_embedding_and_paid_answer_meters_its_query(self):
        quarantined = self.env.refs["기관E"]
        before = len(self.transport.embed_calls)
        result = service.answer(self.res, self.env.consultant, AnswerRequest(
            idempotency_key=str(uuid.uuid4()), generation_id="g", question="새 질문 재난", scope=[quarantined]))
        self.assertEqual(result.status, "ingestion_unavailable")
        self.assertEqual(len(self.transport.embed_calls), before)
        result = service.answer(self.res, self.env.consultant, AnswerRequest(
            idempotency_key=str(uuid.uuid4()), generation_id="g", question="하자보수 기간 조건을 알려줘",
            scope=[self.env.refs["기관A"]]))
        self.assertEqual(len(self.transport.embed_calls), before + 1)
        self.assertEqual(len(result.attempt_ids), 2)  # query embedding + generation, both in the ledger
        with store.open_db(self.env.settings.db_path) as conn:
            stages = {r["stage"]: r["state"] for r in conn.execute(
                "SELECT stage, state FROM attempts WHERE attempt_id IN (?, ?)", tuple(result.attempt_ids))}
        self.assertEqual(stages, {"embedding": "settled", "generation": "settled"})

    def test_corrupt_matrix_falls_back_to_keyword_with_a_reason(self):
        s = self.env.settings
        path = s.data_dir / "indexes" / self.built["dense_version"] / "rows.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace('"row": 0', '"row": 9'), encoding="utf-8")
        res = service.Resources(s, transport=self.transport)
        try:
            r = service.retrieve(res, self.env.consultant, "검수 후 하자 보수 기간은 얼마인가?", [self.env.refs["기관A"]])
        finally:
            res.close()
        self.assertEqual(r.mode, "kiwi_bm25")
        self.assertIn("dense_index_unavailable", r.fallback)
        self.assertIn("dense_unavailable", r.limitations)
        self.assertTrue(r.evidence)


if __name__ == "__main__":
    unittest.main()
