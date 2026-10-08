"""Recorded retrieval geometry in answer-comparison artifacts; no provider calls or database writes."""

import tempfile
import unittest
from pathlib import Path

from rfp_assistant.service import answers
from rfp_assistant.settings import Settings


class RecordedRetrievalTest(unittest.TestCase):
    def test_varying_fusion_limits_and_rerankers_are_reported_per_row(self):
        limits = {"fusion": "keyword_first", "rrf_k": 60, "dense_weight": 1.0, "keyword_head": 6,
                  "channel_top_k": 50, "fused_top_k": 50, "evidence_max_units": 10, "evidence_max_tokens": 8000}
        finalists = [
            {"run_id": "K1", "mode": "kiwi_bm25", "limits": limits, "embedding": None, "reranker": None},
            {"run_id": "H1", "mode": "hybrid", "limits": limits, "embedding": {"model": "local"}, "reranker": None},
            {"run_id": "H2", "mode": "hybrid_rerank", "embedding": {"model": "local"},
             "limits": {**limits, "fusion": "rrf", "rrf_k": 30, "dense_weight": 0.5, "keyword_head": 0,
                        "channel_top_k": 10, "fused_top_k": 8, "evidence_max_units": 3},
             "reranker": {"model": "fixture-reranker", "depth": 8, "protect": 0}},
        ]
        config = {"run_id": "E-fixture", "model": "gpt-5-mini", "reasoning_effort": "minimal",
                  "max_output_tokens": 2000, "finalists": finalists}
        runs = [f["run_id"] for f in finalists]
        scores = {"finalists": {r: {**answers.aggregate_answers([]), "completed": 0, "of": 1} for r in runs},
                  "comparison": answers.paired_against_baseline("K1", [], runs)}
        with tempfile.TemporaryDirectory() as tmp:
            table = answers.write_comparison_table(Settings(source_dir=Path(tmp), data_dir=Path(tmp), hwp_converter=None),
                                                   config, scores)
            self.assertEqual(table["fixed"]["evidence_max_tokens"], 8000)
            self.assertFalse(set(table["fixed"]) & {"retrieval", "fusion", "depth", "fused_depth", "units", "reranker"})
            keyword, hybrid, reranked = [r["retrieval"] for r in table["rows"]]
            self.assertIsNone(keyword["fusion"])
            self.assertEqual(hybrid["fusion"], "keyword_first:60:1.0:6")
            self.assertEqual((reranked["mode"], reranked["fusion"], reranked["depth"], reranked["fused_depth"],
                              reranked["units"], reranked["reranker"]),
                             ("hybrid_rerank", "rrf:30:0.5:0", 10, 8, 3, "fixture-reranker"))
            self.assertEqual(reranked["reranker_config"], finalists[2]["reranker"])
            self.assertIn("rrf:30:0.5:0", answers.comparison_md(table))
