"""All-documents Ask, the fusion gate (including the two pilot questions weighted RRF lost) and HNSW against exact
pgvector search, on real PostgreSQL with the fake provider."""

import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from rfp_assistant.contracts import AnswerRequest
from rfp_assistant.gateway.generation import FakeTransport
from rfp_assistant.retrieval import dense, vector_store
from rfp_assistant.retrieval.retrieval import KeywordIndex, corpus_scope, fuse, retrieve, route_corpus
from rfp_assistant.service import service
from rfp_assistant.storage import store
from tests import fixtures
from tools.retrieval.fusion_gate import gate, needle_hits, parse_variant, variant_name
from tools.retrieval.hnsw_recall import recall


class CorpusAskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))
        self.transport = FakeTransport()
        self.res = service.Resources(self.env.settings, transport=self.transport)

    def tearDown(self):
        self.res.close()
        self.tmp.cleanup()

    def ask(self, question, mode="corpus", scope=()):
        return service.answer(self.res, self.env.consultant, AnswerRequest(
            idempotency_key=str(uuid.uuid4()), generation_id="g1", question=question, scope=list(scope),
            mode=mode, as_of="2026-09-30"))

    def test_all_documents_finds_the_passage_without_a_selection(self):
        r = self.ask("하자보수 기간은 얼마인가요?")
        self.assertEqual(r.status, "answered", r.error)
        view = service.open_evidence(self.res, self.env.consultant, r.request_id, r.claims[0]["evidence_ids"][0])
        self.assertIn("하자보수 기간은 검수 완료일로부터 12개월", view.quote)
        self.assertEqual(view.doc_id, self.env.refs["기관A"].doc_id)  # a shared original cites its first document

    def test_one_or_two_selected_documents_still_work_and_corpus_takes_no_selection(self):
        a, d = self.env.refs["기관A"], self.env.refs["기관D"]
        self.assertEqual(self.ask("하자보수 기간은 얼마인가요?", "single", [a]).status, "answered")
        self.assertNotIn(self.ask("시스템 구축", "compare", [a, d]).status, ("technical_error", "budget_blocked"))
        with self.assertRaises(service.ServiceError):
            self.ask("하자보수 기간은 얼마인가요?", "corpus", [a])

    def test_a_question_with_no_lexical_hit_returns_no_evidence_and_pays_nothing(self):
        index = KeywordIndex.load(self.env.settings)
        scope = corpus_scope(self.env.settings, index)

        def never(*_):
            raise AssertionError("nearest neighbours were searched without a lexical match")
        stub = SimpleNamespace(base_index_version=index.version, version="d-stub", dims=4, search=never)
        got = retrieve(self.env.settings, index, fixtures.analyzer(), "쿼크 글루온 플라스마", scope, mode="hybrid",
                       dense=stub, query_vector=np.ones(4, dtype=np.float32) / 2)
        self.assertEqual(got.evidence, [])
        self.assertIn("no_lexical_match:dense_not_used", got.limitations)
        r = self.ask("쿼크 글루온 플라스마")
        self.assertEqual(r.status, "insufficient_evidence")
        self.assertEqual(self.transport.calls, [])

    def test_a_question_naming_a_project_routes_to_its_documents(self):
        index = KeywordIndex.load(self.env.settings)
        scope = corpus_scope(self.env.settings, index)
        self.assertEqual({ref.doc_id for ref, _ in scope}, {self.env.refs["기관A"].doc_id, self.env.refs["기관D"].doc_id})
        routed = route_corpus(index, fixtures.analyzer(), "도서관 좌석 예약 사업은 무엇을 구축하나요?", scope)
        self.assertEqual([ref.doc_id for ref, _ in routed], [self.env.refs["기관D"].doc_id])
        self.assertEqual(route_corpus(index, fixtures.analyzer(), "하자보수 기간은?", scope), [])

    def test_a_question_that_only_names_a_project_ranks_its_documents_by_meaning(self):
        index = KeywordIndex.load(self.env.settings)
        scope = corpus_scope(self.env.settings, index)
        searched = []

        def search(_vector, allowed, k, _settings=None):
            searched.append(set(allowed))
            return [(i, 1.0 - n / 100) for n, i in enumerate(sorted(allowed)[:k])]
        stub = SimpleNamespace(base_index_version=index.version, version="d-stub", dims=4, search=search)
        got = retrieve(self.env.settings, index, fixtures.analyzer(), "도서관 좌석 예약에 대해 알려줘", scope,
                       mode="hybrid", dense=stub, query_vector=np.ones(4, dtype=np.float32) / 2)
        self.assertIn("name_only_question:dense_within_named_documents", got.limitations)
        self.assertEqual({e.doc_id for e in got.evidence}, {self.env.refs["기관D"].doc_id})
        self.assertFalse([c for c in got.candidates if c["channel"] == "bm25"])  # no match on "대하" or "알리"
        self.assertTrue(searched)
        # A fact term keeps the ordinary keyword path.
        got = retrieve(self.env.settings, index, fixtures.analyzer(), "도서관 좌석 예약 사업의 하자보수 기간은?", scope,
                       mode="hybrid", dense=stub, query_vector=np.ones(4, dtype=np.float32) / 2)
        self.assertNotIn("name_only_question:dense_within_named_documents", got.limitations)


class RoutingTest(unittest.TestCase):
    """route_corpus on title/institution terms alone, with the live corpus's frequencies: 100 projects, "대학교" in
    four titles and "사업" in 29. The former rule routed 서영대학교 too (4.457 against half of 7.824)."""

    def route(self, question_terms, extra=None):
        terms = {"a": ["한영", "대학교", "트랙", "학사"], "b": ["서영", "대학교", "사업", "교육"],
                 "c": ["조선", "대학교"], "d": ["남서울", "대학교"],
                 **{f"p{n}": (["사업"] if n < 28 else []) + ["구축", f"기관{n}"] for n in range(96)}}
        terms.update(extra or {})
        index = SimpleNamespace(scope_terms=terms)
        analyzer = SimpleNamespace(tokens=lambda _q: list(question_terms))
        scope = [(SimpleNamespace(doc_id=d), f"x-{d}") for d in terms]
        return sorted(ref.doc_id for ref, _ in route_corpus(index, analyzer, "q", scope))

    def test_a_document_sharing_only_generic_words_is_not_routed(self):
        # 서영대학교 shares "대학교" and "사업" with the question; only 한영대학교 is named.
        self.assertEqual(self.route(["한영", "대학교", "사업", "알리"]), ["a"])

    def test_generic_title_words_alone_route_nothing(self):
        # Only 서영대학교 has both "대학교" (four titles) and "사업" (29), but neither names a project.
        self.assertEqual(self.route(["대학교", "사업"]), [])
        generic = {"c": ["조선", "대학교", "학사"], "d": ["남서울", "대학교", "학사"], "e": ["학사", "관리"]}
        self.assertEqual(self.route(["대학교", "학사", "사업"], generic), [])  # "학사" in four titles, as live
        self.assertEqual(self.route(["청주", "구축"], {"q": ["청주", "구축"]}), ["q"])  # a name in one title routes

    def test_two_named_projects_are_both_routed(self):
        self.assertEqual(self.route(["한영", "대학교", "트랙", "서영", "교육"]), ["a", "b"])

    def test_another_edition_with_the_same_name_is_kept(self):
        self.assertEqual(self.route(["한영", "대학교", "트랙"], {"a2": ["한영", "대학교", "트랙", "고도"]}), ["a", "a2"])


# BM25 and dense top 20 recorded on the frozen pilot (chunk ID prefixes), and the BM25 rows K1 packed that weighted
# RRF pushed out of the top five.
TRAINING_HANDOVER = {  # refresh50-b-training-handover
    "bm25": "22bc38 ea59d9 dfcd81 4ad5c6 81b5c6 21ccb3 805f50 cb51b5 35e1b5 bddce5 31808f d0c563 68397e 938d99 "
            "5cbd78 45e012 64c9bc 30fc8d 14279e 369f68",
    "dense": "22bc38 fc4b26 e24e0c 6aad20 21ccb3 4c412c 81b5c6 d0c563 17cd05 3af69b 02e2fe a40f9b dfcd81 3cbe30 "
             "bddce5 1c153d 88d684 d71a21 35e1b5 938d99",
    "k1_packed": ["ea59d9", "4ad5c6"]}
INPUT_ERROR = {  # refresh50-eg-input-error, first document of the pair
    "bm25": "99ce7d 6fbea0 66f595 034693 0fb5d4 8b3a7c c606ff 35f921 ab46e8 2ce640 deee9c 6a33bb f28ca5 426443 "
            "2e67f6 d9406b c59125 7808c7 f6b244 b08308",
    "dense": "72f7e7 d4d290 c59125 ff074e deee9c f70e86 e14778 2e67f6 e4a4ee efd8db f28ca5 e64927 6a33bb c9109c "
             "7b89a0 2ec60c 2ce640 e00db3 6cae18 1d476e",
    "k1_packed": ["99ce7d"]}


def fusion_settings(variant):
    return SimpleNamespace(fused_top_k=20, **parse_variant(variant))


class FusionGateTest(unittest.TestCase):
    def test_weighted_rrf_lost_both_known_questions_and_keyword_first_keeps_them(self):
        for case in (TRAINING_HANDOVER, INPUT_ERROR):
            lexical, dense_ids = case["bm25"].split(), case["dense"].split()
            for variant in ("rrf:60:1.0", "rrf:60:0.25"):
                top5 = [c for c, _ in fuse(fusion_settings(variant), lexical, dense_ids)][:5]
                self.assertFalse(set(case["k1_packed"]) <= set(top5), (variant, top5))
            served = [c for c, _ in fuse(fusion_settings("keyword_first:60:1.0:6"), lexical, dense_ids)]
            self.assertEqual(served[:6], lexical[:6])  # the BM25 head K1 answers from, in BM25 order
            self.assertEqual(len(served), 20)
            self.assertTrue(set(served[6:]) & set(dense_ids) - set(lexical[:6]))  # dense still fills the tail

    def test_variant_names_round_trip(self):
        for text in ("rrf:60:1.0", "keyword_first:60:1.0:6"):
            self.assertEqual(variant_name(parse_variant(text)), text)
        self.assertEqual(parse_variant("keyword_first:60:0.5")["keyword_head"], 20)

    def aggregate(self, ndcg=0.9405, complete=53, critical=(), denominator=55, eligible=50):
        return {"ndcg@5": ndcg, "ndcg_eligible_rows": eligible, "critical_failures": list(critical),
                "packed_complete": {"numerator": complete, "denominator": denominator},
                "wrong_scope_candidates": 0, "fallbacks": []}

    def test_gate_requires_no_new_critical_failure_and_no_loss_against_k1(self):
        k1 = self.aggregate(critical=["shared-miss"])
        self.assertTrue(gate(self.aggregate(critical=["shared-miss"]), k1)["passed"])  # equal to K1 passes
        lost = gate(self.aggregate(0.99, 55, ["shared-miss", "refresh50-eg-input-error"]), k1)
        self.assertFalse(lost["passed"])  # a better average never buys a new critical failure
        self.assertEqual(lost["new_critical_failures"], ["refresh50-eg-input-error"])
        self.assertFalse(gate(self.aggregate(0.9404, critical=["shared-miss"]), k1)["passed"])
        self.assertFalse(gate(self.aggregate(complete=52, critical=["shared-miss"]), k1)["passed"])
        for changed in (self.aggregate(denominator=54), self.aggregate(eligible=49)):
            changed["critical_failures"] = ["shared-miss"]
            self.assertFalse(gate(changed, k1)["passed"])
        candidate = self.aggregate(critical=["shared-miss"])
        candidate["fallbacks"] = ["hybrid->kiwi_bm25:query_vector_unavailable"]
        self.assertFalse(gate(candidate, k1)["passed"])

    def test_needle_hits_report_numerators_denominators_and_wilson_intervals(self):
        traces = [{"id": f"n{i}", "metrics": {"hit@5": int(i < 20), "hit@10": int(i < 28)}} for i in range(33)]
        traces.append({"id": "no-metrics", "metrics": None})
        hits = needle_hits(traces)
        self.assertEqual((hits["denominator"], hits["top5"]["numerator"], hits["top10"]["numerator"]), (33, 20, 28))
        low, high = hits["top5"]["wilson95"]
        self.assertTrue(low < 20 / 33 < high)
        self.assertEqual(hits["missed_top10"], [f"n{i}" for i in range(28, 33)])


class HnswRecallTest(unittest.TestCase):
    """The HNSW path really runs on the index and is measured against exact search before it may serve."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = fixtures.make_env(Path(self.tmp.name), paid=False)
        self.settings = env.settings.with_(embedding_dimensions=64)
        with store.open_db(self.settings.db_path) as conn:
            version = store.get_app_setting(conn, "active_index")
        row, payloads = dense.index_payloads(self.settings, version)
        rng = np.random.default_rng(20261004)
        for item in payloads:
            if dense.cache_get(self.settings, item["payload_hash"]) is None:
                dense.cache_put(self.settings, item["payload_hash"], dense.unit_vector(rng.normal(size=64), 64),
                                {"construction": "synthetic-hnsw-recall", "normalized_payload_sha256":
                                 hashlib.sha256(item["text"].encode()).hexdigest()})
        self.index = dense.DenseIndex.load(self.settings, dense.publish_dense(self.settings, row, payloads)["dense_version"])
        self.built = vector_store.build_hnsw(self.settings)
        self.rows = list(range(len(payloads)))
        self.queries = [dense.unit_vector(rng.normal(size=64), 64) for _ in range(5)]

    def tearDown(self):
        self.tmp.cleanup()

    def test_hnsw_recall_against_exact_search_and_its_plan(self):
        self.assertEqual((self.built["index"], self.built["dimensions"]), ("embedding_payloads_hnsw_64", 64))
        qs = [(group, f"q{i}", q, rows) for i, q in enumerate(self.queries)
              for group, rows in (("fixture/unscoped", self.rows), ("fixture/scoped", self.rows[::2]))]
        measured = recall(self.settings, self.index, qs)
        for ef, result in measured.items():
            self.assertTrue(result["passes"], (ef, result))
            self.assertEqual({g["mean_recall@20"] for k, g in result.items() if k != "passes"}, {1.0})
        hnsw = self.settings.with_(dense_search="hnsw", hnsw_ef_search=40)
        for q in self.queries:
            self.assertEqual([r for r, _ in self.index.search(q, self.rows[::2], 20, hnsw)],
                             [r for r, _ in self.index.search(q, self.rows[::2], 20)])
        vector = vector_store.verified(self.queries[0], 64)
        with store.open_db(self.settings.db_path) as conn, store.tx(conn):
            conn.execute(vector_store.HNSW_SESSION, ("40",))
            plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + vector_store.HNSW_SQL.format(d=64),
                                                        (vector, self.index.version, self.rows, vector, 20)))
        self.assertIn("embedding_payloads_hnsw_64", plan)


if __name__ == "__main__":
    unittest.main()
