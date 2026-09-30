import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from rank_bm25 import BM25Okapi

from rfp_assistant import chunking, ingestion, service
from rfp_assistant.auth import AuthError
from rfp_assistant.contracts import DocRef
from rfp_assistant.retrieval import KeywordIndex, retrieve, rrf_fuse
from rfp_assistant.settings import Settings
from tests import fixtures


def _detail(code: str, name: str, text: str) -> str:
    rows = [("요구사항 고유번호", code), ("요구사항 명칭", name), ("세부 내용", text)]
    cells = "".join(
        f'<TableRow><TableCell row="{r}" col="0"><Paragraph><LineSeg><Text>{a}</Text></LineSeg></Paragraph></TableCell>'
        f'<TableCell row="{r}" col="1"><Paragraph><LineSeg><Text>{b}</Text></LineSeg></Paragraph></TableCell></TableRow>'
        for r, (a, b) in enumerate(rows))
    return f'<Paragraph><LineSeg><TableControl><TableBody rows="3" cols="2">{cells}</TableBody></TableControl></LineSeg></Paragraph>'


def _doc(*tables: str) -> list[dict]:
    xml = f"<HwpDoc><BodyText><SectionDef><ColumnSet>{''.join(tables)}</ColumnSet></SectionDef></BodyText></HwpDoc>"
    return ingestion.walk_hwp(ET.fromstring(xml))


class ScopedCodeTest(unittest.TestCase):
    def test_selected_document_exact_detail_wins_and_collisions_are_refused(self):
        summary = ('<Paragraph><LineSeg><TableControl><TableBody rows="2" cols="2">'
                   '<TableRow><TableCell row="0" col="0"><Paragraph><LineSeg><Text>SFR-001</Text></LineSeg></Paragraph>'
                   '</TableCell><TableCell row="0" col="1"><Paragraph><LineSeg><Text>로그인</Text></LineSeg></Paragraph>'
                   '</TableCell></TableRow><TableRow><TableCell row="1" col="0"><Paragraph><LineSeg><Text>SFR-0010</Text>'
                   '</LineSeg></Paragraph></TableCell><TableCell row="1" col="1"><Paragraph><LineSeg><Text>로그 보관'
                   '</Text></LineSeg></Paragraph></TableCell></TableRow></TableBody></TableControl></LineSeg></Paragraph>')
        doc_a = ingestion.finalize_elements(_doc(
            summary, _detail("SFR-001", "로그인", "통합 로그인 기능을 제공하여야 함"),
            _detail("SFR-0010", "로그 보관", "로그인 이력을 1년간 보관하여야 함")), "xa")
        doc_b = ingestion.finalize_elements(_doc(_detail("SFR-001", "결제", "로그인 후 결제 기능을 제공")), "xb")
        chunks, elements = [], {}
        for x, els in (("xa", doc_a), ("xb", doc_b)):
            chunks += chunking.build_chunks(els, x)[0]
            elements.update({(x, e["element_id"]): e for e in els})
        an = fixtures.analyzer()
        rows = {}
        for i, c in enumerate(chunks):
            rows.setdefault(c["extraction_id"], []).append(i)
        # Unrelated padding rows (never in scope) keep IDF positive the way a real corpus does.
        corpus = [an.tokens(c["payload"]) for c in chunks] + [["무관", f"문서{i}"] for i in range(30)]
        index = KeywordIndex("t", "reviewed_only", chunks, BM25Okapi(corpus), rows, elements)
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings(source_dir=Path(tmp), data_dir=Path(tmp), hwp_converter=None, provider="fake")
            r = retrieve(s, index, an, "SFR-001 로그인 요구사항의 세부 내용은?", [(DocRef("A", "ha"), "xa")])
        by_id = {c["chunk_id"]: c for c in chunks}
        self.assertTrue(r.evidence)
        first = by_id[r.evidence[0].chunk_id]
        self.assertEqual((first["requirement_key"], first["chunk_type"]), ("SFR-001", "requirement_detail"))
        self.assertTrue(all(e.doc_id == "A" and e.extraction_id == "xa" for e in r.evidence))
        self.assertNotIn("SFR-0010", {by_id[e.chunk_id]["requirement_key"] for e in r.evidence})
        self.assertTrue(all(by_id[m["chunk_id"]]["extraction_id"] == "xa" for m in r.exact_matches))
        self.assertIn("other_requirement_code", {x["reason"] for x in r.excluded})


class ProjectSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = fixtures.make_env(Path(cls.tmp.name), paid=False)
        cls.res = service.Resources(cls.env.settings)

    @classmethod
    def tearDownClass(cls):
        cls.res.close()
        cls.tmp.cleanup()

    def titles(self, **filters):
        return {i["institution"] for i in service.search_projects(self.res, self.env.consultant, filters, "")}

    def test_unknown_values_do_not_satisfy_typed_filters(self):
        self.assertEqual(self.titles(amount_min=1), {"기관A", "기관C"})
        self.assertEqual(self.titles(amount_min=0), {"기관A", "기관C", "기관E"})  # zero kept, unknown (D) not
        zero = [i for i in service.search_projects(self.res, self.env.consultant, {"amount_max": 0}, "")][0]
        self.assertIn("amount_zero_review", zero["flags"])
        # A and C disagree on closing time for identical bytes: neither silently satisfies a date filter; D is unknown
        self.assertEqual(self.titles(closing_from="2024-01-01"), {"기관E"})

    def test_keyword_search_and_unparsed_projects_remain_discoverable(self):
        hits = service.search_projects(self.res, self.env.consultant, {}, "하자보수")
        self.assertEqual({h["institution"] for h in hits}, {"기관A", "기관C"})
        self.assertTrue(all(h["snippet"] for h in hits))
        (e,) = service.search_projects(self.res, self.env.consultant, {}, "재난")
        self.assertEqual(e["parse_status"], "quarantined")
        self.assertTrue(e["unavailable_reason"])
        self.assertFalse(e["indexed"])

    def test_search_requires_a_principal(self):
        with self.assertRaises(AuthError):
            service.search_projects(self.res, None, {}, "")



def _index(elements_by_extraction: dict[str, list[dict]], profile: str = "structural") -> KeywordIndex:
    chunks, elements = [], {}
    for x, els in elements_by_extraction.items():
        chunks += chunking.build_profile(els, x, profile)[0]
        elements.update({(x, e["element_id"]): e for e in els})
    an = fixtures.analyzer()
    rows: dict[str, list[int]] = {}
    for i, c in enumerate(chunks):
        rows.setdefault(c["extraction_id"], []).append(i)
    corpus = [an.tokens(c["payload"]) or ["∅"] for c in chunks] + [["무관", f"문서{i}"] for i in range(30)]
    return KeywordIndex("t", "reviewed_only", chunks, BM25Okapi(corpus), rows, elements, profile)


def _prose(*paragraphs: str, heading: str = "Ⅲ. 계약 조건") -> list[dict]:
    body = "".join(f"<Paragraph><LineSeg><Text>{t}</Text></LineSeg></Paragraph>" for t in (heading, *paragraphs))
    xml = f"<HwpDoc><BodyText><SectionDef><ColumnSet>{body}</ColumnSet></SectionDef></BodyText></HwpDoc>"
    return ingestion.walk_hwp(ET.fromstring(xml))


class FusionTest(unittest.TestCase):
    def test_rrf_matches_hand_calculated_ranks(self):
        fused = rrf_fuse([["a", "b", "c"], ["c", "d", "a", "a"]], k=60)
        expected = {"a": 1 / 61 + 1 / 63, "b": 1 / 62, "c": 1 / 63 + 1 / 61, "d": 1 / 62}
        self.assertEqual([c for c, _ in fused], ["a", "c", "b", "d"])  # ties broken by chunk ID
        for c, score in fused:
            self.assertAlmostEqual(score, expected[c])
        # an ID absent from one list contributes zero there; repeated inside one list it votes once
        self.assertAlmostEqual(dict(rrf_fuse([["x"], []]))["x"], 1 / 61)


class LexicalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        els = ingestion.finalize_elements(_prose("하자보수 기간은 검수 완료일로부터 12개월로 한다.",
                                                 "지체상금은 계약금액의 1천분의 1로 한다."), "xa")
        cls.index = _index({"xa": els})
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = Settings(source_dir=Path(cls.tmp.name), data_dir=Path(cls.tmp.name), hwp_converter=None,
                                provider="fake")
        cls.scope = [(DocRef("A", "ha"), "xa")]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_mode(self, question: str, mode: str):
        return retrieve(self.settings, self.index, fixtures.analyzer(), question, self.scope, mode=mode)

    def test_kiwi_decomposes_a_compound_query_that_whitespace_misses(self):
        k1 = self.run_mode("하자보수기간", "kiwi_bm25")
        k0 = self.run_mode("하자보수기간", "whitespace_bm25")
        self.assertTrue(k1.evidence and "하자보수" in k1.evidence[0].quote)
        self.assertEqual(k0.evidence, [])  # the documented K0 limitation, not an arbitrary zero-score row
        self.assertIn("idf:global", k1.limitations)

    def test_zero_shared_tokens_return_no_evidence(self):
        r = self.run_mode("블록체인", "kiwi_bm25")
        self.assertEqual((r.evidence, r.ranking), ([], []))

    def test_unavailable_dense_or_reranker_falls_back_and_says_so(self):
        r = self.run_mode("하자보수 기간", "hybrid")
        self.assertEqual(r.mode, "kiwi_bm25")
        self.assertTrue(r.fallback.startswith("hybrid->kiwi_bm25:dense_index_unavailable"))


class SplitConditionTest(unittest.TestCase):
    def test_a_split_row_keeps_its_condition_or_reports_the_gap(self):
        filler = " ".join(f"항목{i} 세부 사양을 충족하여야 한다." for i in range(110))
        long = f"납품 장비 목록 {filler} 단, 부가가치세 별도이며 설치비는 제외한다."
        els = ingestion.finalize_elements(_prose(long), "xa")
        index = _index({"xa": els})
        pieces = [c for c in index.chunks if c["linked"]]
        self.assertGreaterEqual(len(pieces), 2)
        self.assertTrue(all(c["token_count"] <= chunking.HARD_TOKENS for c in index.chunks))
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings(source_dir=Path(tmp), data_dir=Path(tmp), hwp_converter=None, provider="fake")
            r = retrieve(s, index, fixtures.analyzer(), "납품 장비 목록", [(DocRef("A", "ha"), "xa")])
            quotes = "".join(e.quote for e in r.evidence)
            self.assertIn("부가가치세 별도", quotes)  # the condition travels with the fact
            tight = s.with_(evidence_max_tokens=900, evidence_target_tokens=900)
            self.assertLessEqual(len(r.evidence), 1 + 2)  # a long element cannot take every unit
            r = retrieve(tight, index, fixtures.analyzer(), "납품 장비 목록", [(DocRef("A", "ha"), "xa")])
            self.assertTrue(any(x.startswith("linked_evidence_missing:") for x in r.limitations))


class FixedProfileTest(unittest.TestCase):
    def test_windows_obey_ceiling_map_back_to_raw_spans_and_stay_in_one_revision(self):
        text = " ".join(f"제{i}조 수행사는 산출물을 제출하여야 한다." for i in range(120))
        els = {"xa": ingestion.finalize_elements(_prose(text, "둘째 문단"), "xa"),
               "xb": ingestion.finalize_elements(_prose("다른 문서"), "xb")}
        by_id = {(x, e["element_id"]): e for x, es in els.items() for e in es}
        for profile, (size, overlap) in (("fixed-256-32", (256, 32)), ("fixed-512-64", (512, 64))):
            chunks = [c for x, es in els.items() for c in chunking.build_profile(es, x, profile)[0]]
            self.assertTrue(all(c["token_count"] <= size for c in chunks))
            for c in chunks:
                pieces = [by_id[(c["extraction_id"], sp["element_id"])]["raw_text"][sp["start"]:sp["end"]]
                          for sp in c["spans"]]
                self.assertEqual("\n".join(pieces), c["payload"])
                self.assertEqual({(c["extraction_id"], sp["element_id"]) in by_id for sp in c["spans"]}, {True})
            self.assertEqual({c["chunk_type"] for c in chunks}, {"fixed"})
        self.assertNotEqual(chunking.chunker_fingerprint("fixed-256-32"), chunking.chunker_fingerprint("structural"))


class RerankerScopeTest(unittest.TestCase):
    def test_reranker_output_cannot_add_rows_or_break_scope(self):
        a = ingestion.finalize_elements(_prose("하자보수 기간은 12개월이다.", "하자보수 절차는 별첨과 같다."), "xa")
        b = ingestion.finalize_elements(_prose("하자보수 기간은 24개월이다."), "xb")
        index = _index({"xa": a, "xb": b})
        dense = mock.Mock(base_index_version="t", version="d1")
        dense.search = lambda q, allowed, k: [(i, 1.0) for i in allowed][:k]

        class Hostile:
            def rerank(self, question, chunks):
                return [(99, 9.0), (-1, 8.0)] + [(i, 0.0) for i in range(len(chunks))], {}

        with tempfile.TemporaryDirectory() as tmp:
            s = Settings(source_dir=Path(tmp), data_dir=Path(tmp), hwp_converter=None, provider="fake")
            r = retrieve(s, index, fixtures.analyzer(), "하자보수 기간", [(DocRef("A", "ha"), "xa")],
                         mode="hybrid_rerank", dense=dense, query_vector=[1.0], reranker=Hostile())
            self.assertEqual(r.mode, "hybrid_rerank")
            self.assertTrue(r.evidence)
            self.assertTrue(all(e.extraction_id == "xa" for e in r.evidence))
            r = retrieve(s, index, fixtures.analyzer(), "하자보수 기간", [(DocRef("A", "ha"), "xa")],
                         mode="hybrid_rerank", dense=dense, query_vector=[1.0], reranker=None)
            self.assertEqual((r.mode, r.fallback), ("hybrid", "hybrid_rerank->hybrid:reranker_unavailable"))


if __name__ == "__main__":
    unittest.main()
