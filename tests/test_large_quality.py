import unittest
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from tools.check_large_quality import BoundedReference, ceiling_guard, compare, job_cost, prepare, reference_rows


class QualityAcceptanceTests(unittest.TestCase):
    def test_candidate_dimension_is_fixed_before_dataset_or_dispatch_work(self):
        with patch("tools.check_large_quality.evaluation.frozen_dataset") as frozen:
            for dimensions in (1535, 1537):
                with self.assertRaisesRegex(ValueError, "fixed"):
                    prepare(SimpleNamespace(embedding_model="text-embedding-3-large", embedding_dimensions=dimensions))
            frozen.assert_not_called()

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
