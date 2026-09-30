import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from rank_bm25 import BM25Okapi

from rfp_assistant import chunking, ingestion, service
from rfp_assistant.auth import AuthError
from rfp_assistant.contracts import DocRef
from rfp_assistant.retrieval import KeywordIndex, retrieve
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


if __name__ == "__main__":
    unittest.main()
