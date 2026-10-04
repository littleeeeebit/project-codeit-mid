import unittest
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from tools.check_large_quality import BoundedReference, candidate_vectors, ceiling_guard, compare, job_cost, prepare, reference_rows


class QualityAcceptanceTests(unittest.TestCase):
    def test_candidate_dimension_is_fixed_before_dataset_or_dispatch_work(self):
        with patch("tools.check_large_quality.evaluation.frozen_dataset") as frozen:
            for dimensions in (1535, 1537):
                with self.assertRaisesRegex(ValueError, "fixed"):
                    prepare(SimpleNamespace(embedding_model="text-embedding-3-large", embedding_dimensions=dimensions))
            frozen.assert_not_called()

    def prepare_with(self, index):
        row = lambda i, x: {"id": i, "doc_id": "d", "source_hash": "h", "extraction_id": x,
                            "answerable": True, "evidence": [{}], "type": "fact"}
        settings = SimpleNamespace(embedding_model="text-embedding-3-large", embedding_dimensions=1536)
        with patch("tools.check_large_quality.evaluation.frozen_dataset", return_value={"current": True, "rows": 2}), \
             patch("tools.check_large_quality.evaluation.validate_gold", return_value={"ok": True}), \
             patch("tools.check_large_quality.evaluation.load_eval_rows",
                   return_value=([row(1, "x1"), row(2, "x2")], [], "sha")), \
             patch("tools.check_large_quality.KeywordIndex.load", return_value=index), \
             patch("tools.check_large_quality.evaluation._ready_dense_for") as dense_lookup:
            with self.assertRaises(ValueError) as caught:
                prepare(settings)
            dense_lookup.assert_not_called()
        return str(caught.exception)

    def test_unindexed_frozen_scope_is_rejected_before_planning(self):
        index = SimpleNamespace(version="kw", rows_by_extraction={"x1": [0]}, has_metadata_snapshot=True,
                                analyzer_fp=None)
        self.assertIn("[2]", self.prepare_with(index))

    def test_analyzer_incompatible_index_is_rejected_before_planning(self):
        index = SimpleNamespace(version="kw", rows_by_extraction={"x1": [0], "x2": [1]},
                                has_metadata_snapshot=True, analyzer_fp="earlier-policy")
        self.assertIn("analyzer/query policy", self.prepare_with(index))

    def test_plan_prices_with_the_ledger_rate_not_the_code_default(self):
        settings = SimpleNamespace(embedding_model="text-embedding-3-large", embedding_dimensions=1536,
                                   channel_top_k=20, fused_top_k=20, rrf_k=60, evidence_target_tokens=1,
                                   evidence_max_tokens=1, evidence_max_units=1,
                                   with_=lambda **k: SimpleNamespace(embedding_model="text-embedding-3-large",
                                                                     embedding_dimensions=3072))
        row = {"id": 1, "doc_id": "d", "source_hash": "h", "extraction_id": "x1", "answerable": True,
               "evidence": [{}], "type": "fact", "question": "q"}
        index = SimpleNamespace(version="kw", manifest_hash="m", rows_by_extraction={"x1": [0]},
                                has_metadata_snapshot=True, analyzer_fp=None)
        payloads = [{"payload_hash": f"p{i}", "extraction_id": "x1", "chunk_id": f"c{i}", "text": f"t{i}"}
                    for i in range(2)]
        ledger = {"rates": {"input": "0.26"}, "rate_version": "ledger-2x",
                  "envelope_remaining_micro_usd": 10**9, "available_micro_usd": 10**9}
        q = "tools.check_large_quality."
        with patch(q + "evaluation.frozen_dataset", return_value={"current": True, "rows": 1}), \
             patch(q + "evaluation.validate_gold", return_value={"ok": True}), \
             patch(q + "evaluation.load_eval_rows", return_value=([row], [], "sha")), \
             patch(q + "evaluation.population_identity", return_value="pop"), \
             patch(q + "KeywordIndex.load", return_value=index), \
             patch(q + "evaluation._ready_dense_for", return_value="dv"), \
             patch(q + "dense.DenseIndex.load", return_value=SimpleNamespace(version="dv")), \
             patch(q + "dense.index_payloads", return_value=(None, payloads)), \
             patch(q + "dense.cache_get", return_value=None), \
             patch(q + "dense.count_embedding_tokens", return_value=100_000), \
             patch(q + "dense.plan_batches", side_effect=lambda s, m: [[p] for p in m]), \
             patch(q + "dense._ledger_view", return_value=ledger):
            plan = prepare(settings)[0]
            self.assertEqual(plan["corpus_max_micro_usd"], 2 * 26000)  # 100k tokens at the ledger's $0.26/M, twice
            self.assertEqual(plan["rate_version"], "ledger-2x")
            with patch(q + "dense._ledger_view", return_value={**ledger, "rates": None}), \
                    self.assertRaisesRegex(ValueError, "ledger rate"):
                prepare(settings)

    def test_paired_probe_reads_the_verified_matrix_not_the_cache(self):
        matrix = np.eye(3, dtype=np.float32)
        index = SimpleNamespace(row_of={"a": 0, "b": 2})
        with patch("tools.check_large_quality.dense.cache_get", side_effect=AssertionError("cache read")):
            vectors = candidate_vectors(SimpleNamespace(database_backend="sqlite"), SimpleNamespace(matrix=matrix),
                                        index, [{"chunk_id": "b", "payload_hash": "pb"}])
        self.assertIs(vectors["pb"].base, matrix)
        np.testing.assert_array_equal(vectors["pb"], matrix[2])

    def aggregate(self, ndcg=.8, packed=.9, critical=()):
        return {"ndcg@5": ndcg, "ndcg_eligible_rows": 50,
                "packed_complete": {"rate": packed, "denominator": 50},
                "critical_failures": list(critical), "wrong_scope_candidates": 0, "fallbacks": []}

    def test_exact_boundary_passes_but_larger_loss_fails(self):
        reference = self.aggregate()
        self.assertTrue(compare(self.aggregate(.78, .88), reference, reference)["passed"])
        self.assertFalse(compare(self.aggregate(.7799, .88), reference, reference)["passed"])
        self.assertFalse(compare(self.aggregate(.78, .8799), reference, reference)["passed"])

    def test_new_critical_failure_blocks_even_when_average_improves(self):
        base = self.aggregate()
        for native in (base, self.aggregate(critical=["numeric"] )):
            self.assertFalse(compare(self.aggregate(.99, 1, ["numeric"]), native, base)["passed"])

    def test_existing_shared_failure_is_not_relabelled_as_a_regression(self):
        same = self.aggregate(critical=["historical-failure"])
        self.assertTrue(compare(same, same, same)["passed"])

    def test_empty_or_changed_denominators_cannot_pass(self):
        reference = self.aggregate()
        for field in ("ndcg_eligible_rows", "packed_complete"):
            candidate = self.aggregate()
            if field == "packed_complete":
                candidate[field]["denominator"] = 0
            else:
                candidate[field] = 49
            self.assertFalse(compare(candidate, reference, reference)["passed"])

    def test_fallback_and_wrong_scope_block_an_equal_average(self):
        reference = self.aggregate()
        candidate = self.aggregate()
        candidate["fallbacks"] = ["missing-query"]
        self.assertFalse(compare(candidate, reference, reference)["passed"])
        candidate = self.aggregate()
        candidate["wrong_scope_candidates"] = 1
        self.assertFalse(compare(candidate, reference, reference)["passed"])

    def test_reference_refuses_unembedded_rows_and_breaks_ties_by_identity(self):
        index = SimpleNamespace(version="base", chunks=[{"chunk_id": "z"}, {"chunk_id": "a"}, {"chunk_id": "b"}])
        vec = np.array([1, 0], dtype=np.float32)
        ref = BoundedReference(index, {0: vec, 1: vec}, "text-embedding-3-large", 2)
        self.assertEqual(ref.search(vec, [0, 1], 2), [(1, 1), (0, 1)])
        with self.assertRaisesRegex(ValueError, "coverage"):
            ref.search(vec, [1, 2], 2)

    def test_shared_payload_retains_both_association_rows(self):
        index = SimpleNamespace(version="base", row_of={"doc1-chunk": 0, "doc2-chunk": 1},
            chunks=[{"chunk_id": "doc1-chunk"}, {"chunk_id": "doc2-chunk"}])
        vec = np.array([1, 0], dtype=np.float32)
        covered = [{"chunk_id": c["chunk_id"], "payload_hash": "same"} for c in index.chunks]
        vectors = reference_rows(index, covered, {"same": vec})
        self.assertIs(vectors[0], vectors[1])
        reference = BoundedReference(index, vectors, "text-embedding-3-large", 2)
        self.assertEqual(reference.search(vec, [0, 1], 20), [(0, 1), (1, 1)])

    def test_dispatch_ceiling_counts_reservations_without_recharging_previous_work(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE attempts(request_id text,state text,settled_micro_usd integer,reserved_micro_usd integer)")
            conn.execute("INSERT INTO attempts VALUES ('job','settled',1000,1200)")
            previous = job_cost(conn, "job")
            conn.execute("INSERT INTO attempts VALUES ('job','reserved',NULL,60)")
            self.assertIsNone(ceiling_guard(conn, "job", previous, 100))
            conn.execute("INSERT INTO attempts VALUES ('job','dispatching',NULL,70)")
            self.assertEqual(ceiling_guard(conn, "job", previous, 100), "comparison_invocation_cost_ceiling")
            conn.execute("UPDATE attempts SET state='released' WHERE reserved_micro_usd=70")
            self.assertIsNone(ceiling_guard(conn, "job", previous, 100))


if __name__ == "__main__":
    unittest.main()
