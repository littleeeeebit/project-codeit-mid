import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from rank_bm25 import BM25Okapi

from rfp_assistant import chunking, ingestion, retrieval, service
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
        dense = mock.Mock(base_index_version="t", version="d1", dims=1)
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


class SplitRowGradingTest(unittest.TestCase):
    def test_each_piece_of_an_oversized_row_is_graded_on_the_text_it_carries(self):
        from rfp_assistant import evaluation

        long = " ".join(f"항목{i} 세부 사양을 충족하여야 한다." for i in range(150)) + " 단, 부가가치세 별도이며 설치비는 제외한다."
        cells = (f'<TableRow><TableCell row="0" col="0"><Paragraph><LineSeg><Text>구분</Text></LineSeg></Paragraph></TableCell>'
                 f'<TableCell row="0" col="1"><Paragraph><LineSeg><Text>내용</Text></LineSeg></Paragraph></TableCell></TableRow>'
                 f'<TableRow><TableCell row="1" col="0"><Paragraph><LineSeg><Text>납품 조건</Text></LineSeg></Paragraph></TableCell>'
                 f'<TableCell row="1" col="1"><Paragraph><LineSeg><Text>{long}</Text></LineSeg></Paragraph></TableCell></TableRow>')
        table = (f'<Paragraph><LineSeg><TableControl><TableBody rows="2" cols="2">{cells}</TableBody></TableControl>'
                 f'</LineSeg></Paragraph>')
        els = ingestion.finalize_elements(_doc(table), "xa")
        el = [e for e in els if e["kind"] == "table"][0]
        pieces = [c for c in chunking.build_chunks(els, "xa")[0] if any("fragment" in s for s in c["spans"])]
        self.assertGreaterEqual(len(pieces), 2)
        unit = {"element_id": el["element_id"], "quote": "단, 부가가치세 별도이며 설치비는 제외한다."}
        grades = [evaluation.grade(c, unit, el) for c in pieces]
        self.assertEqual(grades[0], 0 if "부가가치세" not in pieces[0]["body"] else 2)
        self.assertEqual(grades[-1], 2)
        self.assertTrue(all(("부가가치세 별도" in c["body"]) == (g == 2) for c, g in zip(pieces, grades)))
        header = {"element_id": el["element_id"], "quote": "내용"}
        self.assertEqual({evaluation.grade(c, header, el) for c in pieces}, {2})



def _sections(sections: list[tuple[str, list[str]]]) -> list[dict]:
    body = "".join(f"<Paragraph><LineSeg><Text>{t}</Text></LineSeg></Paragraph>"
                   for heading, paras in sections for t in (heading, *paras))
    xml = f"<HwpDoc><BodyText><SectionDef><ColumnSet>{body}</ColumnSet></SectionDef></BodyText></HwpDoc>"
    return ingestion.walk_hwp(ET.fromstring(xml))


class ObservedNumericMissTest(unittest.TestCase):
    """Synthetic shapes of the live misses dp-005, dp-020 (spacing) and dp-014 (restated project name)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(source_dir=Path(self.tmp.name), data_dir=Path(self.tmp.name), hwp_converter=None,
                                 provider="fake")

    def tearDown(self):
        self.tmp.cleanup()

    def first(self, index, question, x="xa"):
        r = retrieve(self.settings, index, fixtures.analyzer(), question, [(DocRef("A", "ha"), x)])
        return r, (r.evidence[0].quote if r.evidence else "")

    def test_spaced_forms_match_unspaced_questions_without_touching_evidence(self):
        late = "지체 상금율은 계약금액의 1천분의 1.5로 한다."
        cost = "사 업 비 : 130,000,000원 (부가가치세 포함)"
        els = ingestion.finalize_elements(_sections([
            ("1. 일반 사항", ["포상금 지급 기준은 별도로 정한다.", "지체 없이 감독관에게 통보하여야 한다."]),
            ("2. 사업 개요", ["사업 범위와 비용 산정 방식은 별첨을 따른다."]),
            ("3. 계약 조건", [late]),
            ("4. 예산", [cost])]), "xa")
        index = _index({"xa": els})
        r, quote = self.first(index, "지체상금율은 얼마인가?")
        self.assertIn(late, quote)  # raw evidence keeps the source spelling
        r, quote = self.first(index, "사업비는 얼마인가?")
        self.assertIn(cost, quote)
        self.assertTrue(any(late in e["raw_text"] for e in els))

    def test_a_restated_project_name_does_not_drown_the_asked_fact(self):
        title = "한빛대학교 차세대 포털 학사 정보시스템 구축사업"
        budget = "사업 예산은 금 1,200,000,000원이며 대금은 기성 검사 후 30일 이내 지급한다."
        filler = " ".join(f"세부 지급 절차 {i}단계는 계약 일반조건에 따른다." for i in range(15))
        sections = [(f"{i + 1}. {title} 과업{i}", [f"{title} 과업 {i}의 예산 범위와 지급 대상 내용을 기술한다."])
                    for i in range(12)]
        sections.insert(9, ("10. 대금", [f"{budget} {filler}"]))
        els = ingestion.finalize_elements(_sections(sections), "xa")
        index = _index({"xa": els})
        long_q = f"{title}의 사업 예산과 대금 지급 조건은?"
        r, quote = self.first(index, long_q)
        self.assertIn(budget, quote)
        self.assertTrue(any(x.startswith("scope_redundant_terms:") for x in r.limitations))
        r_short, quote_short = self.first(index, "사업 예산과 대금 지급 조건은?")
        self.assertIn(budget, quote_short)
        # without the rule the restated name outranks the fact (the observed failure)
        an = fixtures.analyzer()
        plain = retrieval.rank_lexical(index, an, long_q, index.rows_by_extraction["xa"], 20)
        rank = next(n for n, (i, _) in enumerate(plain, 1) if budget in index.chunks[i]["payload"])
        self.assertGreater(rank, 5)

    def test_codes_digits_and_negations_are_never_dropped(self):
        index = _index({"xa": ingestion.finalize_elements(_sections(
            [(f"{i + 1}. 항목{i}", [f"공통 문구 SFR-001 1천 불가 내용 {i}"]) for i in range(10)]), "xa")})
        drop = retrieval.scope_redundant_terms(index, fixtures.analyzer().tokens("공통 문구 SFR-001 1천 불가"),
                                               index.rows_by_extraction["xa"], set())
        self.assertNotIn("code:SFR-001", drop)
        self.assertFalse({"1", "불가"} & drop)
        self.assertIn("공통", drop)
        # nothing is dropped when no other term would remain
        self.assertEqual(retrieval.scope_redundant_terms(index, ["공통", "문구"], index.rows_by_extraction["xa"],
                                                         set()), set())

    def test_metadata_terms_are_frozen_into_the_index(self):
        from rfp_assistant import service

        env = fixtures.make_env(Path(self.tmp.name) / "env", paid=False)
        s = env.settings
        a, c = env.refs["기관A"], env.refs["기관C"]
        index = KeywordIndex.load(s)
        self.assertTrue({"통합", "구축", "기관"} <= set(index.scope_terms[a.doc_id]))  # CSV title and institution
        before = dict(index.scope_terms)
        ingestion.resolve_metadata(s, c.doc_id, "institution", "새한국정보원", "공고문 1쪽", "notice p1", "owner")
        res = service.Resources(s)
        try:
            self.assertEqual(res.index().scope_terms, before)  # serving reads the snapshot, not live metadata
        finally:
            res.close()
        rebuilt = retrieval.build_keyword_index(s, fixtures.analyzer(), activate=False)
        self.assertNotEqual(rebuilt["index_version"], index.version)  # a correction takes effect via a new index
        self.assertIn("새한국정보원", KeywordIndex.load(s, rebuilt["index_version"]).scope_terms[c.doc_id])

    def test_a_common_fact_term_is_not_dropped_into_false_absence(self):
        els = ingestion.finalize_elements(_sections(
            [(f"{i + 1}. 하자보수 {i}", [f"하자보수 기간은 품목 {i}에 대하여 {i + 1}년으로 한다."]) for i in range(10)]),
            "xa")
        index = _index({"xa": els})
        r, quote = self.first(index, "하자보수 기간은 얼마인가?")
        self.assertTrue(r.evidence)
        self.assertIn("하자보수 기간", quote)
        self.assertFalse(any(x.startswith("scope_redundant_terms:") for x in r.limitations))

if __name__ == "__main__":
    unittest.main()
