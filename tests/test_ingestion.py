import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from rfp_assistant import chunking, evaluation, ingestion, store
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
            s = Settings(database_backend="sqlite", source_dir=source, data_dir=Path(tmp) / "d", hwp_converter=None, provider="fake")
            with self.assertRaises(ingestion.IngestionError):
                ingestion.read_manifest_csv(s)



class IncrementalIngestTest(unittest.TestCase):
    def test_unchanged_corpus_is_not_reconverted_and_one_changed_source_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp), paid=False, index=False)
            s = env.settings
            with store.open_db(s.db_path) as conn:
                before = {r["source_hash"]: r["active_extraction_id"] for r in conn.execute(
                    "SELECT source_hash, active_extraction_id FROM sources")}
            with mock.patch.object(ingestion, "parse_pdf", side_effect=AssertionError("reconverted")):
                again = ingestion.ingest(s)
            self.assertTrue(all(r.get("reused") for r in again if r["status"] in ("parsed", "quarantined")))
            with store.open_db(s.db_path) as conn:
                after = {r["source_hash"]: r["active_extraction_id"] for r in conn.execute(
                    "SELECT source_hash, active_extraction_id FROM sources")}
            self.assertEqual(before, after)
            # Change one original: only it is parsed again; the others stay reused.
            (s.files_dir / "기관D_도서관 좌석 예약.pdf").write_bytes(
                fixtures.make_pdf([["제안요청서", "Ⅰ. 사업 안내", "열람실 좌석 배정 시스템을 구축한다."]]))
            ingestion.import_manifest(s)
            calls = []
            real = ingestion.parse_pdf
            with mock.patch.object(ingestion, "parse_pdf", side_effect=lambda p: calls.append(p) or real(p)):
                results = ingestion.ingest(s)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sum(not r.get("reused") for r in results if r["status"] == "parsed"), 1)
            # A converter that appears later retries the quarantined HWP instead of reusing its failure.
            conv = Path(tmp) / "hwp5proc.exe"
            conv.write_bytes(b"converter")
            with mock.patch.object(ingestion, "run_hwp_converter", return_value=("hwp_converter_failed", "")) as run:
                results = ingestion.ingest(s.with_(hwp_converter=conv))
            hwp = [r for r in results if r["filename"].endswith(".hwp")][0]
            self.assertEqual((run.call_count, hwp["status"], hwp.get("reused")), (1, "quarantined", None))

    def test_a_parser_crash_is_recorded_per_source_and_the_run_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp), paid=False, index=False)
            with mock.patch.object(ingestion, "parse_pdf", side_effect=MemoryError("boom")):
                results = ingestion.ingest(env.settings, force=True)
            errors = [r for r in results if r["status"] == "error"]
            self.assertEqual(len(errors), 3)  # A and its byte-identical copy share one result, plus D
            self.assertIn("MemoryError", errors[0]["reason"])
            self.assertTrue(any(r["status"] == "quarantined" for r in results))  # the HWP still got its status

    def test_diagnostics_point_at_problems_without_granting_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp), paid=False, index=False)
            with store.open_db(env.settings.db_path) as conn:
                stats = json.loads(conn.execute("SELECT stats_json FROM extraction_inputs WHERE extraction_id != ''"
                                                ).fetchone()[0])
            d = stats["diagnostics"]
            self.assertIn("short_output", d["flags"])
            self.assertEqual(set(d["probes"]), {"start", "middle", "end"})
            self.assertGreater(d["pages"], 0)


class ReviewImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name), paid=False, index=False)
        s = self.env.settings
        ref = self.env.refs["기관D"]
        with store.open_db(s.db_path) as conn:
            self.extraction = conn.execute("SELECT active_extraction_id FROM sources WHERE source_hash = ?",
                                           (ref.source_hash,)).fetchone()[0]
            self.element = conn.execute("SELECT element_id FROM elements WHERE extraction_id = ?",
                                        (self.extraction,)).fetchone()[0]
        self.record = {"doc_id": ref.doc_id, "extraction_id": self.extraction, "reviewer": "reviewer-2",
                       "status": "reviewed", "locations": [{"element_id": self.element}],
                       "checks": {"text": "match"}, "coverage": {"sections": ["Ⅰ. 사업 안내"]}, "limitations": []}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, records) -> Path:
        path = Path(self.tmp.name) / "reviews.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        return path

    def test_valid_review_is_appended_with_its_coverage(self):
        out = ingestion.import_reviews(self.env.settings, self.write([self.record]))
        self.assertEqual(out["imported"], 1)
        cov = {c["source_hash"]: c for c in ingestion.review_coverage(self.env.settings)}
        row = cov[self.env.refs["기관D"].source_hash]
        self.assertEqual((row["review_status"], row["reviewer"], row["review_is_current"]),
                         ("reviewed", "reviewer-2", True))
        self.assertEqual(row["coverage"], {"sections": ["Ⅰ. 사업 안내"]})

    def test_stale_revision_unknown_location_or_missing_reviewer_imports_nothing(self):
        bad = [dict(self.record, extraction_id="old-revision"),
               dict(self.record, locations=[{"element_id": "not-there"}]),
               dict(self.record, reviewer=" "),
               dict(self.record, checks={})]
        for rec in bad:
            with self.assertRaises(ingestion.IngestionError):
                ingestion.import_reviews(self.env.settings, self.write([self.record, rec]))
        with store.open_db(self.env.settings.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews WHERE reviewer = 'reviewer-2'").fetchone()[0], 0)

    def test_converter_success_alone_cannot_become_reviewed(self):
        quarantined = self.env.refs["기관E"]
        rec = {"doc_id": quarantined.doc_id, "extraction_id": None, "reviewer": "r", "status": "reviewed",
               "locations": [], "checks": {"x": 1}, "limitations": []}
        with self.assertRaises(ingestion.IngestionError):
            ingestion.import_reviews(self.env.settings, self.write([rec]))


class RecoveryTest(unittest.TestCase):
    def test_approved_conversion_becomes_a_new_revision_and_keeps_the_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp), paid=False, index=False)
            s = env.settings
            ref = env.refs["기관E"]
            converted = Path(tmp) / "converted.pdf"
            converted.write_bytes(fixtures.make_pdf([["재난 관리 시스템", "Ⅰ. 사업 안내", "상황 전파 기능을 제공한다."]]))
            review = Path(tmp) / "review.json"
            review.write_text(json.dumps({"reviewer": "owner", "method": "hancom_pdf_export",
                                          "compared_locations": [{"page": 1}], "fidelity_passed": False,
                                          "mapping_limitations": "converted PDF pages"}), encoding="utf-8")
            out = ingestion.recover_source(s, ref.doc_id, converted, review)
            self.assertEqual(out["status"], "quarantined")  # a failed comparison keeps the quarantine
            review.write_text(json.dumps({"reviewer": "owner", "method": "hancom_pdf_export",
                                          "compared_locations": [{"page": 1}], "fidelity_passed": True,
                                          "mapping_limitations": "converted PDF pages"}), encoding="utf-8")
            out = ingestion.recover_source(s, ref.doc_id, converted, review)
            self.assertEqual(out["status"], "parsed")
            with store.open_db(s.db_path) as conn:
                src = conn.execute("SELECT * FROM sources WHERE source_hash = ?", (ref.source_hash,)).fetchone()
                rec = json.loads(conn.execute("SELECT recovery_json FROM extractions WHERE extraction_id = ?",
                                              (src["active_extraction_id"],)).fetchone()[0])
            # coverage of the new revision is claimed only by a review against its own elements
            self.assertEqual((src["parse_status"], src["review_status"]), ("parsed", "unreviewed"))
            self.assertEqual(rec["original_hash"], ref.source_hash)
            self.assertEqual(rec["previous_failure"]["reason_code"], "hwp_converter_missing")
            self.assertEqual(ingestion.sha256_file(s.files_dir / "기관E_재난 관리 시스템.hwp"), ref.source_hash)
            # A later ingest keeps the registered recovery instead of failing the conversion again.
            again = [r for r in ingestion.ingest(s) if r["doc_id"] == ref.doc_id][0]
            self.assertEqual(again["status"], "recovered")
            hwpx = Path(tmp) / "c.hwpx"
            hwpx.write_bytes(b"PK")
            with self.assertRaises(ingestion.IngestionError):
                ingestion.recover_source(s, env.refs["기관D"].doc_id, hwpx, review)


class IdentityTest(unittest.TestCase):
    def test_original_cues_are_compared_with_csv_and_resolutions_are_recorded(self):
        els = ingestion.finalize_elements([
            {"path": "p0", "kind": "paragraph", "parent": None, "raw_text": "사업명 : 통합 정보시스템 구축",
             "location": {}},
            {"path": "t0", "kind": "table", "parent": None, "raw_text": "",
             "table": {"rows": 1, "cols": 2, "cells": [{"row": 0, "col": 0, "text": "발주기관"},
                                                       {"row": 0, "col": 1, "text": "기관A"}]}, "location": {}}],
            "x")
        cues = ingestion.original_cues(els)
        self.assertEqual(cues["title"][0]["value"], "통합 정보시스템 구축")
        self.assertEqual(cues["institution"][0]["value"], "기관A")
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp), paid=False, index=False)
            s = env.settings
            report = {r["doc_id"]: r for r in ingestion.identity_report(s)}
            a, c = report[env.refs["기관A"].doc_id], report[env.refs["기관C"].doc_id]
            self.assertEqual(a["original"], c["original"])  # shared bytes share cues
            self.assertIn("institution", {x["field"] for x in c["provenance_conflicts"]})
            with self.assertRaises(ingestion.IngestionError):
                ingestion.resolve_metadata(s, c["doc_id"], "institution", "기관A", "", "notice p1", "owner")
            with self.assertRaises(ingestion.IngestionError):
                ingestion.resolve_metadata(s, c["doc_id"], "amount_krw", "많음", "r", "e", "owner")
            with self.assertRaises(ingestion.IngestionError):  # a date must be ISO text like the CSV values
                ingestion.resolve_metadata(s, c["doc_id"], "bid_close", {"value": "2024/06/11", "precision": "date"},
                                           "r", "e", "owner")
            ingestion.resolve_metadata(s, c["doc_id"], "bid_close", {"value": "2024-06-11", "precision": "date"},
                                       "공고문 마감일", "notice p1", "owner")
            ingestion.resolve_metadata(s, c["doc_id"], "institution", "기관A", "공고문 1쪽 기관명", "notice p1", "owner")
            again = {r["doc_id"]: r for r in ingestion.identity_report(s)}[c["doc_id"]]
            self.assertEqual(again["resolutions"]["institution"]["value"], "기관A")
            self.assertTrue(again["provenance_conflicts"])  # the competing values stay visible


class FamilyTest(unittest.TestCase):
    def test_related_revisions_join_one_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [list(r) for r in fixtures.ROWS]
            rows[2][0] = rows[0][0]  # 기관D shares 기관A's notice number: a related revision
            source = fixtures.write_corpus(Path(tmp), rows)
            s = Settings(database_backend="sqlite", source_dir=source, data_dir=Path(tmp) / "data", hwp_converter=None, provider="fake")
            s.data_dir.mkdir()
            store.init_schema(s.db_path)
            ingestion.import_manifest(s)
            evaluation.assign_families(s)
            fams = json.loads((s.data_dir / "datasets" / "families.json").read_text(encoding="utf-8"))["families"]
            with store.open_db(s.db_path) as conn:
                ids = {r["filename"][:3]: r["doc_id"] for r in conn.execute("SELECT doc_id, filename FROM documents")}
            fam = [f for f in fams.values() if ids["기관A"] in f["doc_ids"]][0]
            self.assertTrue({ids["기관A"], ids["기관C"], ids["기관D"]} <= set(fam["doc_ids"]))
            self.assertEqual(len(fam["related_sources"]), 1)


class RevisionIdentityTest(unittest.TestCase):
    def test_different_output_under_one_parser_fingerprint_is_a_new_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp), paid=False, index=False)
            s = env.settings
            h = env.refs["기관D"].source_hash
            with store.open_db(s.db_path) as conn:
                old = conn.execute("SELECT active_extraction_id, review_status FROM sources WHERE source_hash = ?",
                                   (h,)).fetchone()
                old_artifact = Path(conn.execute("SELECT artifact_path FROM extractions WHERE extraction_id = ?",
                                                 (old[0],)).fetchone()[0])
            self.assertEqual(old[1], "sample_checked")
            ingestion.ingest(s, [env.refs["기관D"].doc_id], force=True)  # identical output: same revision, review kept
            with store.open_db(s.db_path) as conn:
                same = conn.execute("SELECT active_extraction_id, review_status FROM sources WHERE source_hash = ?",
                                    (h,)).fetchone()
            self.assertEqual(tuple(same), tuple(old))
            real = ingestion.parse_pdf

            def changed(path):
                els, warnings, reason = real(path)
                els[-1]["raw_text"] += " (변경된 변환 결과)"
                return els, warnings, reason

            with mock.patch.object(ingestion, "parse_pdf", side_effect=changed):
                ingestion.ingest(s, [env.refs["기관D"].doc_id], force=True)
            with store.open_db(s.db_path) as conn:
                new = conn.execute("SELECT active_extraction_id, review_status FROM sources WHERE source_hash = ?",
                                   (h,)).fetchone()
                old_rows = conn.execute("SELECT COUNT(*) FROM elements WHERE extraction_id = ?", (old[0],)).fetchone()[0]
            self.assertNotEqual(new[0], old[0])
            self.assertEqual(new[1], "unreviewed")  # a reviewed text does not vouch for different text
            self.assertGreater(old_rows, 0)  # the old revision stays for pinned gold rows and citations
            self.assertNotIn("변경된", old_artifact.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
