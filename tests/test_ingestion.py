import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from rfp_assistant import chunking, ingestion, store
from rfp_assistant.settings import Settings
from tests import fixtures

NESTED_HWP = """<HwpDoc><BodyText><SectionDef><ColumnSet>
<Paragraph><LineSeg><Text>Ⅲ. 제안 요청 내용</Text></LineSeg></Paragraph>
<Paragraph><LineSeg><TableControl><TableBody rows="3" cols="2">
 <TableRow><TableCell row="0" col="0" colspan="2" rowspan="1"><Paragraph><LineSeg><Text>사업 예산</Text></LineSeg></Paragraph></TableCell></TableRow>
 <TableRow><TableCell row="1" col="0"><Paragraph><LineSeg><Text>금액</Text></LineSeg></Paragraph></TableCell>
  <TableCell row="1" col="1"><Paragraph><LineSeg><Text>130,000,000원 (VAT 포함)</Text></LineSeg></Paragraph></TableCell></TableRow>
 <TableRow><TableCell row="2" col="0"><Paragraph><LineSeg><Text>비고</Text></LineSeg></Paragraph></TableCell>
  <TableCell row="2" col="1"><ColumnSet><Paragraph><LineSeg><Text>반복</Text><TableControl><TableBody rows="1" cols="1">
   <TableRow><TableCell row="0" col="0"><Paragraph><LineSeg><Text>반복</Text></LineSeg></Paragraph></TableCell></TableRow>
  </TableBody></TableControl></LineSeg></Paragraph></ColumnSet></TableCell></TableRow>
</TableBody></TableControl></LineSeg></Paragraph>
<Paragraph><LineSeg><Header><HeaderParagraphList><Paragraph><LineSeg><Text>머리말</Text></LineSeg></Paragraph></HeaderParagraphList></Header><Text>본문 끝</Text></LineSeg></Paragraph>
</ColumnSet></SectionDef></BodyText></HwpDoc>"""


class HwpWalkerTest(unittest.TestCase):
    def test_nested_table_text_is_owned_once_and_structure_survives(self):
        raw = ingestion.walk_hwp(ET.fromstring(NESTED_HWP))
        els = ingestion.finalize_elements(raw, "x1")
        tables = [e for e in els if e["kind"] == "table"]
        self.assertEqual(len(tables), 2)
        outer, nested = tables
        owned = [c["text"] for t in tables for c in t["table"]["cells"]]
        owned += [e["raw_text"] for e in els if e["kind"] != "table"]
        self.assertEqual(sum(t.count("반복") for t in owned), 2)  # outer cell once, nested cell once
        cell = [c for c in outer["table"]["cells"] if (c["row"], c["col"]) == (2, 1)][0]
        self.assertEqual(cell["text"], "반복")
        self.assertEqual(nested["parent_id"], outer["element_id"])
        self.assertEqual(nested["location"]["parent_cell"], [2, 1])
        header = [c for c in outer["table"]["cells"] if c["row"] == 0][0]
        self.assertEqual((header["colspan"], header["text"]), (2, "사업 예산"))
        self.assertIn("130,000,000원 (VAT 포함)", outer["raw_text"])
        self.assertEqual(outer["location"]["section_path"], ["Ⅲ. 제안 요청 내용"])
        self.assertNotIn("머리말", json.dumps(els, ensure_ascii=False))  # page chrome excluded
        self.assertEqual(len({e["element_id"] for e in els}), len(els))

    def test_table_chunk_keeps_header_with_amount(self):
        els = ingestion.finalize_elements(ingestion.walk_hwp(ET.fromstring(NESTED_HWP)), "x1")
        chunks, _ = chunking.build_chunks(els, "x1")
        table_chunks = [c for c in chunks if "130,000,000원" in c["payload"]]
        self.assertEqual(len(table_chunks), 1)
        self.assertIn("사업 예산", table_chunks[0]["payload"])
        self.assertIn("VAT 포함", table_chunks[0]["payload"])
        self.assertLessEqual(max(c["token_count"] for c in chunks), chunking.HARD_TOKENS)

    def test_equation_script_reads_as_the_printed_formula(self):
        script = "평점=입찰가격평가`배점한도 TIMES  LEFT ( {최저입찰가격} over {당해입찰가격} RIGHT )"
        text = ingestion.equation_text(script)
        self.assertEqual(text, "평점=입찰가격평가 배점한도 × ( 최저입찰가격 / 당해입찰가격 )")
        root = ET.fromstring("<HwpDoc><BodyText><SectionDef><Paragraph><LineSeg><Text>산식:</Text>"
                             "<Control chid='eqed'><EqEdit/></Control></LineSeg></Paragraph></SectionDef></BodyText>"
                             "</HwpDoc>")
        next(root.iter("EqEdit")).text = text
        self.assertIn("최저입찰가격 / 당해입찰가격", ingestion.walk_hwp(root)[0]["raw_text"])

    def test_title_line_travels_with_its_table_and_toc_lines_are_not_sections(self):
        root = ET.fromstring(
            "<HwpDoc><BodyText><SectionDef>"
            "<Paragraph><LineSeg><Text>Ⅰ. 사업 개요\t 01</Text></LineSeg></Paragraph>"
            "<Paragraph><LineSeg><Text>Ⅰ. 사업 개요</Text></LineSeg></Paragraph>"
            "<Paragraph><LineSeg><Text>1. 명  칭 : 통합시스템</Text></LineSeg></Paragraph>"
            "<Paragraph><LineSeg><Text>□ 조직별 역할</Text></LineSeg></Paragraph>"
            "<Paragraph><LineSeg><TableControl><TableBody rows='2' cols='2'>"
            "<TableRow><TableCell row='0' col='0'><Paragraph><LineSeg><Text>구분</Text></LineSeg></Paragraph></TableCell>"
            "<TableCell row='0' col='1'><Paragraph><LineSeg><Text>역할</Text></LineSeg></Paragraph></TableCell></TableRow>"
            "<TableRow><TableCell row='1' col='0'><Paragraph><LineSeg><Text>현업부서</Text></LineSeg></Paragraph></TableCell>"
            "<TableCell row='1' col='1'><Paragraph><LineSeg><Text>요청 자료 제공</Text></LineSeg></Paragraph></TableCell></TableRow>"
            "</TableBody></TableControl></LineSeg></Paragraph>"
            "</SectionDef></BodyText></HwpDoc>")
        els = ingestion.finalize_elements(ingestion.walk_hwp(root), "x1")
        self.assertEqual([e["kind"] for e in els[:3]], ["toc", "heading", "paragraph"])
        chunks, _ = chunking.build_chunks(els, "x1")
        (table,) = [c for c in chunks if c["chunk_type"] == "table"]
        self.assertIn("□ 조직별 역할\n구분 | 역할\n현업부서 | 요청 자료 제공", table["payload"])
        self.assertEqual(table["section_path"], ["Ⅰ. 사업 개요"])
        self.assertFalse([c for c in chunks if c["body"] == "□ 조직별 역할"])

    def test_picture_caption_is_extracted(self):
        root = ET.fromstring("<HwpDoc><BodyText><SectionDef><Paragraph><LineSeg><GShapeObjectControl>"
                             "<GShapeObjectCaption><Paragraph><LineSeg><Text>그림. 추진 로드맵</Text></LineSeg>"
                             "</Paragraph></GShapeObjectCaption></GShapeObjectControl></LineSeg></Paragraph>"
                             "</SectionDef></BodyText></HwpDoc>")
        self.assertEqual([e["raw_text"] for e in ingestion.walk_hwp(root)], ["그림. 추진 로드맵"])

    def test_malformed_converter_output_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp), paid=False, index=False)
            with store.open_db(env.settings.db_path) as conn:
                row = conn.execute("SELECT parse_status, review_status, reason_code FROM sources WHERE format = 'hwp'"
                                   ).fetchone()
            self.assertEqual(tuple(row), ("quarantined", "needs_recovery", "hwp_converter_missing"))
            self.assertIn("hwp_converter_missing", ingestion.QUARANTINE_TEXT)


class PdfTest(unittest.TestCase):
    def test_physical_page_is_distinct_from_printed_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.pdf"
            path.write_bytes(fixtures.PDF_A)
            els, warnings, reason = ingestion.parse_pdf(path)
            self.assertIsNone(reason)
            hit = [e for e in els if "하자보수 기간" in e["raw_text"]][0]
            self.assertEqual(hit["location"]["page"], 3)
            self.assertEqual(hit["location"]["page_label"], "1")
            self.assertFalse(any(e["raw_text"].strip() == "-1-" for e in els))


class ManifestTest(unittest.TestCase):
    def test_bom_csv_multiline_utf8_artifacts_duplicates_and_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp), paid=False, index=False)
            s = env.settings
            with store.open_db(s.db_path) as conn:
                docs = {r["filename"][:3]: dict(r) for r in conn.execute("SELECT * FROM documents")}
                extractions = conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0]
                artifacts = [r[0] for r in conn.execute("SELECT artifact_path FROM extractions")]
            self.assertEqual(len(docs), 4)
            self.assertEqual(json.loads(docs["기관A"]["raw_metadata_json"])["summary"], "- 첫 줄\n- 둘째 줄")
            # identical bytes: two associations, one shared extraction, visible institution conflict
            self.assertEqual(docs["기관A"]["active_source_hash"], docs["기관C"]["active_source_hash"])
            self.assertEqual(extractions, 2)
            quality = json.loads(docs["기관A"]["quality_json"])
            self.assertEqual(quality["shared_source_with"], [docs["기관C"]["doc_id"]])
            self.assertIn("institution", {c["field"] for c in quality["provenance_conflicts"]})
            for a in artifacts:
                data = Path(a).read_bytes()
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
                data.decode("utf-8", errors="strict")

    def test_missing_and_zero_values_stay_distinct(self):
        base = {k: "" for k in ingestion.CSV_COLUMNS.values()}
        missing, flags_m = ingestion.normalize_row({**base, "title": "t", "format": "pdf"})
        zero, flags_z = ingestion.normalize_row({**base, "title": "t", "format": "pdf", "amount": "0.0",
                                                 "revision": "0.0", "bid_close": "2024-06-11"})
        self.assertIsNone(missing["amount_krw"])
        self.assertIsNone(missing["revision"])
        self.assertIn("amount_missing", flags_m)
        self.assertEqual((zero["amount_krw"], zero["revision"]), (0, 0))
        self.assertIn("amount_zero_review", flags_z)
        self.assertEqual(zero["bid_close"], {"value": "2024-06-11", "precision": "date"})
        frac, flags_f = ingestion.normalize_row({**base, "title": "t", "format": "pdf", "amount": "100.5"})
        self.assertIsNone(frac["amount_krw"])
        self.assertIn("amount_fractional", flags_f)

    def test_unsafe_filename_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [list(fixtures.ROWS[0])]
            rows[0][10] = "../escape.pdf"
            source = fixtures.write_corpus(Path(tmp), rows)
            s = Settings(source_dir=source, data_dir=Path(tmp) / "d", hwp_converter=None, provider="fake")
            with self.assertRaises(ingestion.IngestionError):
                ingestion.read_manifest_csv(s)


if __name__ == "__main__":
    unittest.main()
