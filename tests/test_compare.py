"""Comparison runner pieces that need no GPU: matrix expansion, cache identity, the Gemini cap and score cache."""

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from rfp_assistant import compare, dense, models, store
from rfp_assistant.store import dumps
from tests import fixtures

BASE = {"profile": "structural", "analyzer": "kiwi", "retrieval": "hybrid", "embedding": "text-embedding-3-large",
        "fusion": "keyword_first:60:1.0:6", "reranker": None, "rerank_mode": None, "units": 10, "depth": 50}


class MatrixTest(unittest.TestCase):
    def test_built_in_matrices_vary_only_their_axes(self):
        emb = compare.matrix_rows(compare.MATRICES["embedding"], BASE)
        self.assertEqual(len(emb), 13 * 2)
        self.assertEqual({r["fusion"] for r in emb} | {r["depth"] for r in emb} | {r["units"] for r in emb},
                         {"keyword_first:60:1.0:6", 50, 10})
        self.assertEqual({compare.row_label(r) for r in emb}, {"D", "H"})
        rr = compare.matrix_rows(compare.MATRICES["reranker"], BASE)
        self.assertEqual(len(rr), 8 * 2)
        self.assertEqual({r["embedding"] for r in rr}, {"text-embedding-3-large"})  # the serving hybrid's pool
        self.assertEqual({r["rerank_mode"] for r in rr}, {"whole", "below_head"})
        chunks = compare.matrix_rows(compare.MATRICES["chunking"], BASE)
        self.assertEqual([(r["profile"], compare.row_label(r), r["embedding"]) for r in chunks],
                         [(p, "K1", None) for p in ("structural", "fixed-256-32", "fixed-512-64", "fixed-800-96")])
        self.assertEqual([compare.row_label(r) for r in compare.matrix_rows(compare.MATRICES["lexical"], BASE)],
                         ["K0", "K1"])

    def test_a_declared_matrix_file_names_known_axes_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.json"
            path.write_text('{"axes": {"units": [6, 10]}}', encoding="utf-8")
            self.assertEqual(compare.load_matrix(str(path))[1]["axes"], {"units": [6, 10]})
            path.write_text('{"axes": {"temperature": [0]}}', encoding="utf-8")
            with self.assertRaisesRegex(compare.CompareError, "unknown"):
                compare.load_matrix(str(path))


class IdentityTest(unittest.TestCase):
    def test_openai_vectors_keep_their_cache_identity(self):
        legacy = hashlib.sha256(dumps({"text": "가", "model": "text-embedding-3-large", "dims": 1536,
                                       "policy": dense.EMBED_POLICY}).encode()).hexdigest()
        self.assertEqual(dense.payload_hash("가", "text-embedding-3-large", 1536, "query"), legacy)
        self.assertEqual(dense.embed_policy("text-embedding-3-large"), dense.EMBED_POLICY)

    def test_a_prefixed_model_keys_queries_apart_and_a_new_revision_changes_every_vector(self):
        qwen = "Qwen/Qwen3-Embedding-0.6B"
        self.assertNotEqual(dense.payload_hash("가", qwen, 1024, "query"), dense.payload_hash("가", qwen, 1024))
        bge = "BAAI/bge-m3"  # no prefix: one vector serves both kinds
        self.assertEqual(dense.payload_hash("가", bge, 1024, "query"), dense.payload_hash("가", bge, 1024))
        spec = models.EMBEDDINGS[qwen]
        self.assertNotEqual(spec.policy, replace(spec, revision="0" * 40).policy)

    def test_every_compared_model_is_pinned(self):
        for spec in list(models.EMBEDDINGS.values()) + list(models.RERANKERS.values()):
            if getattr(spec, "backend", "") in ("openai", "gemini"):
                continue
            self.assertRegex(spec.revision or "", r"^[0-9a-f]{40}$", spec.key)


class LedgerAndCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))
        self.db = self.env.settings.db_path

    def tearDown(self):
        self.tmp.cleanup()

    def test_row_settings_carry_the_model_and_the_fixed_limits(self):
        out = compare.row_settings(self.env.settings, {**BASE, "embedding": "nlpai-lab/KURE-v1"})
        self.assertEqual((out.embedding_model, out.embedding_dimensions, out.fusion, out.keyword_head,
                          out.fused_top_k, out.evidence_max_units, out.evidence_max_tokens),
                         ("nlpai-lab/KURE-v1", 1024, "keyword_first", 6, 50, 10, 4800))

    def test_gemini_spends_only_under_the_persons_cap_and_an_unknown_outcome_blocks(self):
        self.assertFalse(models.external_reserve(self.db, 1000, "embedding")["admitted"])  # no cap set
        with self.assertRaisesRegex(models.ModelError, "name and a reason"):
            models.set_external_cap(self.db, 1000, "", "x")
        models.set_external_cap(self.db, models.gemini_cost_micro(1_000_000), "owner", "comparison")
        first = models.external_reserve(self.db, 600_000, "embedding")
        self.assertTrue(first["admitted"])
        self.assertEqual(models.external_reserve(self.db, 600_000, "embedding")["reason"], "gemini_cap_exhausted")
        models.external_finish(self.db, first["attempt_id"], "unknown", None, "timeout")
        blocked = models.external_reserve(self.db, 1, "embedding")
        self.assertIn("unknown billing", blocked["reason"])
        with self.assertRaisesRegex(models.ModelError, "below"):
            models.set_external_cap(self.db, 1, "owner", "lower")
        self.assertEqual(models.external_status(self.db)["unknown"], 1)
        with self.assertRaisesRegex(models.ModelError, "name and a reason"):
            models.external_resolve(self.db, first["attempt_id"], True, "owner", " ")
        resolved = models.external_resolve(self.db, first["attempt_id"], True, "owner", "timeout counted as charged")
        self.assertEqual(resolved["settled_micro_usd"], first["reserved_micro_usd"])
        self.assertTrue(models.external_reserve(self.db, 1, "embedding")["admitted"])  # unblocked, still under the cap
        with self.assertRaisesRegex(models.ModelError, "not an attempt with unknown billing"):
            models.external_resolve(self.db, first["attempt_id"], False, "owner", "again")
        question = models.external_reserve(self.db, 40, "gold_eval")
        models.external_finish(self.db, question["attempt_id"], "settled", 1)
        gemini = replace(self.env.settings, embedding_model="gemini-embedding-001", embedding_dimensions=3072)
        latency = compare.ledger_query_ms(gemini)  # question calls only, never the corpus batches
        self.assertEqual(len(latency), 1)
        self.assertGreaterEqual(latency[0], 0)

    def test_reranker_scores_are_computed_once_per_pair(self):
        calls = []

        class Model:
            spec = replace(models.RERANKERS["BAAI/bge-reranker-v2-m3"])
            info = {"model": spec.key}

            def score(self, pairs):
                calls.append(len(pairs))
                return [float(len(p)) for _, p in pairs], [len(p) > 3 for _, p in pairs]

        cached = compare.CachedReranker(self.env.settings, Model())
        chunks = [{"chunk_id": "a", "payload": "가나"}, {"chunk_id": "b", "payload": "가나다라마"}]
        order, info = cached.rerank("질문", chunks)
        self.assertEqual(([i for i, _ in order], info["truncated"]), ([1, 0], 1))
        again, _ = cached.rerank("질문", chunks + [{"chunk_id": "c", "payload": "가"}])
        self.assertEqual(calls, [2, 1])  # only the new pair is scored
        self.assertEqual([i for i, _ in again], [1, 0, 2])

    def test_bulk_cache_insert_refuses_a_different_vector_for_the_same_payload(self):
        s = self.env.settings
        v = np.zeros(s.embedding_dimensions, dtype=np.float32)
        v[0] = 1
        meta = {"kind": "chunk", "normalized_payload_sha256": "0" * 64}
        dense.cache_put_many(s, [("h1", v, meta)])
        dense.cache_put_many(s, [("h1", v, meta)])  # the same vector again is a no-op
        w = np.zeros_like(v)
        w[1] = 1
        with self.assertRaises(dense.DenseError):
            dense.cache_put_many(s, [("h1", w, meta)])
        from rfp_assistant.vector_store import cached_hashes

        self.assertEqual(cached_hashes(s, {"h1", "h2"}), {"h1"})
        with store.open_db(s.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM embedding_payloads WHERE payload_hash='h1'"
                                          ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
