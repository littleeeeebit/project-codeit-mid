import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rfp_assistant import evaluation, gold, ingestion, store
from tests.fixtures import make_env, make_pdf

QUOTE = "하자보수 기간은 검수 완료일로부터 12개월로 한다."


class GoldReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = make_env(self.root, paid=False, index=False)
        s = self.env.settings
        evaluation.assign_families(s)
        fam_path = s.data_dir / "datasets" / "families.json"
        fams = json.loads(fam_path.read_text(encoding="utf-8"))
        for f in fams["families"].values():
            f["split"] = "dev"
        fam_path.write_text(json.dumps(fams, ensure_ascii=False), encoding="utf-8")
        self.fam_of = {d: k for k, f in fams["families"].items() for d in f["doc_ids"]}
        ref = self.env.refs["기관A"]
        with store.open_db(s.db_path) as conn:
            self.extraction = conn.execute("SELECT active_extraction_id FROM sources WHERE source_hash = ?",
                                           (ref.source_hash,)).fetchone()[0]
            self.element = conn.execute("SELECT element_id FROM elements WHERE extraction_id = ? AND raw_text LIKE ?",
                                        (self.extraction, "%하자보수%")).fetchone()[0]

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, cid: str, question: str) -> dict:
        ref = self.env.refs["기관A"]
        return {"id": cid, "type": "condition", "question": question, "doc_id": ref.doc_id,
                "source_hash": ref.source_hash, "extraction_id": self.extraction, "family": self.fam_of[ref.doc_id],
                "split": "dev", "answerable": True, "operational_case": False,
                "evidence": [{"element_id": self.element, "quote": QUOTE}], "expected_answer": "검수 완료일부터 12개월",
                "drafted_by": "agent-a", "reviewed_by": None, "reviewed_at": None}

    def submit(self, batch: str, rows: list[dict]) -> dict:
        path = self.root / f"{batch}.jsonl"
        store.write_jsonl_atomic(path, rows)
        return gold.submit(self.env.settings, path, batch, "dev-pilot", "agent-a")

    def sha(self, cid: str) -> str:
        return gold.candidate(self.env.settings, cid)["row_sha256"]

    def test_approval_goes_to_dataset_and_rejection_to_a_consistent_wiki_page(self):
        s = self.env.settings
        self.submit("b1", [self.row("r1", "하자보수 기간은?"), self.row("r2", "하자보수는 몇 년인가?")])
        gold.decide(s, "r1", "approve", "v1", self.sha("r1"))
        (approved,) = store.read_jsonl(evaluation.dataset_path(s, "dev-pilot"))
        self.assertEqual((approved["id"], approved["reviewed_by"]), ("r1", "v1"))
        gold.decide(s, "r2", "reject", "v1", self.sha("r2"), ["wrong_answer"], "12개월이지 1년 단위가 아님")
        page = (gold.rejections_dir(s) / "r2.md").read_text(encoding="utf-8")
        self.assertIn(f'row_sha256: "{self.sha("r2")}"', page)
        self.assertIn('"question": "하자보수는 몇 년인가?"', page)  # verbatim row
        self.assertIn(QUOTE, page)  # source context at rejection
        self.assertIn('inference_status: "pending"', page)
        self.assertIn("[r2](r2.md)", (gold.rejections_dir(s) / "index.md").read_text(encoding="utf-8"))
        self.assertEqual(gold.check(s), [])
        with self.assertRaises(gold.GoldError):  # first decision wins
            gold.decide(s, "r1", "reject", "v2", self.sha("r1"), ["too_easy"])

    def test_decisions_need_an_independent_reviewer_the_seen_version_and_a_reason(self):
        s = self.env.settings
        self.submit("b1", [self.row("r1", "하자보수 기간은?")])
        for args in (("approve", "agent-a", self.sha("r1")),  # the drafter
                     ("approve", "v1", "stale"),  # another version than the one shown
                     ("reject", "v1", self.sha("r1"))):  # no category
            with self.assertRaises(gold.GoldError):
                gold.decide(s, "r1", *args)
        with self.assertRaises(gold.GoldError):
            gold.decide(s, "r1", "reject", "v1", self.sha("r1"), ["other"], "")

    def test_drafting_waits_for_inferred_reasons_and_never_repeats_a_question(self):
        s = self.env.settings
        self.submit("b1", [self.row("r1", "하자보수 기간은?")])
        gold.decide(s, "r1", "reject", "v1", self.sha("r1"), ["leading_question"], "")
        with self.assertRaisesRegex(gold.GoldError, "r1"):
            self.submit("b2", [self.row("r2", "무상 유지보수 기간은?")])
        gold.infer(s, "r1", "agent-a", {"cause": "c", "lesson": "l", "drafting_rule": "기간을 질문에 쓰지 않는다"})
        self.assertIn("기간을 질문에 쓰지 않는다", (gold.rejections_dir(s) / "index.md").read_text(encoding="utf-8"))
        self.assertIn('inference_status: "inferred"', (gold.rejections_dir(s) / "r1.md").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(gold.GoldError, "already drafted as r1"):
            self.submit("b2", [self.row("r2", "하자보수  기간은?")])
        self.assertEqual(self.submit("b2", [self.row("r2", "무상 유지보수 기간은?")])["rows"], 1)
        self.assertEqual(gold.check(s), [])

    def test_parser_revision_repins_pending_rows_by_element_path(self):
        s = self.env.settings
        self.submit("b1", [self.row("r1", "하자보수 기간은?")])
        ref = self.env.refs["기관A"]
        with mock.patch.object(ingestion, "PDF_WALKER_VERSION", "pdf-test-revision"), \
                mock.patch.object(ingestion, "HWP_WALKER_VERSION", "hwp-test-revision"):
            new = ingestion.ingest_source(s, ref.source_hash)["extraction_id"]
        self.assertNotEqual(new, self.extraction)
        self.assertEqual(gold.repin(s, "b1-repin")["rows"], 1)
        row = gold.candidate(s, "r1")["row"]
        self.assertEqual((row["extraction_id"], row["repinned_from"]["batch_id"]), (new, "b1"))
        with store.open_db(s.db_path) as conn:
            self.assertEqual(evaluation.RowChecker(s, conn).check(row, "r1"), [])
        self.assertEqual(gold.check(s), [])
        self.assertEqual(gold.repin(s, "b1-again")["rows"], 0)  # nothing stale left

    def test_check_detects_hand_edits_and_sync_restores_the_projection(self):
        s = self.env.settings
        self.submit("b1", [self.row("r1", "하자보수 기간은?")])
        gold.decide(s, "r1", "reject", "v1", self.sha("r1"), ["too_easy"], "")
        page = gold.rejections_dir(s) / "r1.md"
        page.write_text(page.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
        (gold.rejections_dir(s) / "stray.md").write_text("x", encoding="utf-8")
        errors = gold.check(s)
        self.assertEqual(len(errors), 2)
        gold.sync(s)
        self.assertEqual(gold.check(s), [])



class ValidationApplicabilityTest(unittest.TestCase):
    """The converter-failure type is mandatory only while a source is actually quarantined."""

    PASSAGE = ("late_content", "table_fact", "repeated_code", "condition", "numeric_qualifier")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = make_env(self.root, paid=False, index=False)
        s = self.env.settings
        evaluation.assign_families(s)
        path = s.data_dir / "datasets" / "families.json"
        fams = json.loads(path.read_text(encoding="utf-8"))
        for f in fams["families"].values():
            f["split"] = "dev"
        path.write_text(json.dumps(fams, ensure_ascii=False), encoding="utf-8")
        self.fam_of = {d: k for k, f in fams["families"].items() for d in f["doc_ids"]}
        ref = self.env.refs["기관A"]
        with store.open_db(s.db_path) as conn:
            self.extraction = conn.execute("SELECT active_extraction_id FROM sources WHERE source_hash = ?",
                                           (ref.source_hash,)).fetchone()[0]
            self.element = conn.execute("SELECT element_id FROM elements WHERE extraction_id = ? AND raw_text LIKE ?",
                                        (self.extraction, "%하자보수%")).fetchone()[0]

    def tearDown(self):
        self.tmp.cleanup()

    def passage(self, i: int, kind: str) -> dict:
        ref = self.env.refs["기관A"]
        return {"id": f"p{i}", "type": kind, "question": f"질문 {i}", "doc_id": ref.doc_id,
                "source_hash": ref.source_hash, "extraction_id": self.extraction, "family": self.fam_of[ref.doc_id],
                "split": "dev", "answerable": True, "evidence": [{"element_id": self.element, "quote": QUOTE}],
                "drafted_by": "agent-a", "reviewed_by": "person-b"}

    def metadata(self, i: int, kind: str) -> dict:
        ref = self.env.refs["기관C"]
        return {"id": f"m{i}", "type": kind, "question": f"메타 {i}", "doc_id": ref.doc_id,
                "source_hash": ref.source_hash, "family": self.fam_of[ref.doc_id], "split": "dev",
                "answerable": True, "metadata_fields": ["institution"], "drafted_by": "agent-a",
                "reviewed_by": "person-b"}

    def converter(self, key: str) -> dict:
        ref = self.env.refs[key]
        return {"id": f"c-{key}", "type": "converter_unavailable", "question": "원문 변환 실패 문서 질문",
                "doc_id": ref.doc_id, "source_hash": ref.source_hash, "family": self.fam_of[ref.doc_id],
                "split": "dev", "answerable": False, "operational_case": True, "drafted_by": "agent-a",
                "reviewed_by": "person-b"}

    def dataset(self) -> list[dict]:
        rows = [self.passage(i, self.PASSAGE[i % len(self.PASSAGE)]) for i in range(20)]
        rows += [self.metadata(i, ("missing_metadata", "provenance_conflict")[i % 2]) for i in range(4)]
        return rows

    def validate(self, rows: list[dict]) -> dict:
        store.write_jsonl_atomic(evaluation.dataset_path(self.env.settings, "dev-pilot"), rows)
        return evaluation.validate_gold(self.env.settings, "dev-pilot")

    def recover_everything(self) -> None:
        converted = self.root / "converted.pdf"
        converted.write_bytes(make_pdf([["재난 관리 시스템", "Ⅰ. 사업 안내", "상황 전파 기능을 제공한다."]]))
        review = self.root / "recovery.json"
        review.write_text(json.dumps({"reviewer": "owner", "method": "hancom_pdf_export",
                                      "compared_locations": [{"page": 1}], "fidelity_passed": True,
                                      "mapping_limitations": "converted PDF pages"}), encoding="utf-8")
        self.assertEqual(ingestion.recover_source(self.env.settings, self.env.refs["기관E"].doc_id, converted,
                                                  review)["status"], "parsed")

    def test_a_fully_recovered_corpus_validates_without_a_converter_row(self):
        self.recover_everything()
        report = self.validate(self.dataset())
        self.assertTrue(report["ok"], report["errors"])
        self.assertNotIn("converter_unavailable", report["required_types"])
        self.assertFalse(report["operational_cases"]["applicable"])
        # a converter row pointing at the recovered (now parsed) document is still rejected
        report = self.validate(self.dataset() + [self.converter("기관E")])
        self.assertFalse(report["ok"])
        self.assertTrue(any("not quarantined" in e for e in report["errors"]))

    def test_a_quarantined_source_keeps_the_operational_case_mandatory(self):
        report = self.validate(self.dataset())
        self.assertFalse(report["ok"])
        self.assertIn("converter_unavailable", report["required_types"])
        self.assertEqual(report["operational_cases"]["quarantined_documents"], 1)
        self.assertTrue(any("converter_unavailable" in e for e in report["errors"]))
        report = self.validate(self.dataset() + [self.converter("기관E")])
        self.assertTrue(report["ok"], report["errors"])
        report = self.validate(self.dataset() + [self.converter("기관A")])  # a parsed document
        self.assertTrue(any("not quarantined" in e for e in report["errors"]))

    def test_a_converter_candidate_goes_stale_when_its_document_is_recovered(self):
        s = self.env.settings
        row = {**self.converter("기관E"), "id": "dp-023", "reviewed_by": None}
        path = self.root / "batch.jsonl"
        store.write_jsonl_atomic(path, [row])
        gold.submit(s, path, "b-ops", "dev-pilot", "agent-a")
        self.assertEqual(gold.status(s)["pending_invalid"], [])
        self.recover_everything()
        (stale,) = gold.status(s)["pending_invalid"]
        self.assertEqual(stale["candidate_id"], "dp-023")
        self.assertTrue(any("not quarantined" in e for e in stale["errors"]))
        self.assertTrue(gold.candidate(s, "dp-023")["current_errors"])

    def test_existing_row_checks_still_apply(self):
        self.recover_everything()
        bad = self.dataset()
        bad[0]["extraction_id"] = "stale-revision"
        bad[1]["evidence"] = [{"element_id": self.element, "quote": "원문에 없는 인용"}]
        bad[2]["family"] = "src-wrongfamily"
        bad[3]["reviewed_by"] = bad[3]["drafted_by"]
        errors = self.validate(bad)["errors"]
        for needle in ("extraction revision is not the active one", "quote not found", "family missing",
                       "independent reviewer"):
            self.assertTrue(any(needle in e for e in errors), needle)



class ExcerptPackTest(unittest.TestCase):
    def test_pack_has_scoped_candidates_and_the_queue_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(Path(tmp), paid=False, index=False)
            s = env.settings
            evaluation.assign_families(s)
            path = s.data_dir / "datasets" / "families.json"
            fams = json.loads(path.read_text(encoding="utf-8"))
            for f in fams["families"].values():
                f["split"] = "dev"
            path.write_text(json.dumps(fams, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(gold.GoldError):
                gold.write_excerpts(s, Path("relative"))
            out = Path(tmp) / "pack"
            summary = gold.write_excerpts(s, out)
            rows = store.read_jsonl(out / "excerpts.jsonl")
            self.assertEqual(summary["excerpts"], len(rows))
            hit = [r for r in rows if "12개월" in r["text"]]
            self.assertTrue(hit and hit[0]["category"] == "numeric_qualifier")
            with store.open_db(s.db_path) as conn:
                for r in rows:  # every excerpt points at a real element of the active revision
                    self.assertIsNotNone(conn.execute(
                        "SELECT 1 FROM elements WHERE extraction_id = ? AND element_id = ?",
                        (r["extraction_id"], r["element_id"])).fetchone())
            sources = {r["source_hash"] for r in rows}
            self.assertEqual(len(sources), len({(r["source_hash"], r["doc_id"]) for r in rows}))  # one per copy
            context = json.loads((out / "drafting-context.json").read_text(encoding="utf-8"))
            self.assertIn("rejections", context)
            with self.assertRaises(gold.GoldError):  # earlier packs survive
                gold.write_excerpts(s, out)
            for r in rows:  # every excerpt carries its trigger and maps back to raw spans
                self.assertIn(r["trigger"], r["text"])

    def test_a_late_trigger_is_inside_its_bounded_excerpt(self):
        prefix = " ".join(f"일반 사항 {i}번을 설명한다." for i in range(80))  # well over 800 characters
        raw = f"{prefix} 제안서는 2024. 6. 11. 17시까지 제출하여야 하며, 지연 제출은 접수하지 않는다. 기타 사항."
        self.assertGreater(raw.index("2024"), 800)
        start, end = gold._trigger("deadline", raw, {})
        window = gold._window(raw, "paragraph", (start, end), 300)
        self.assertIn("2024. 6. 11", window["text"])
        self.assertIn("지연 제출은 접수하지 않는다", window["text"])  # the condition in the same sentence
        self.assertLessEqual(len(window["text"]), 300)
        (a, b), = window["offsets"]
        self.assertEqual(raw[a:b].strip(), window["text"])
        self.assertFalse(window["context_clipped"])
        table = "구분 | 내용\n" + "\n".join(f"항목{i} | 일반 설명" for i in range(120)) + \
                "\n사업 금액 | 130,000,000원 (부가가치세 포함)\n비고 | 분할 지급 불가"
        start, end = gold._trigger("numeric_qualifier", table, {})
        window = gold._window(table, "table", (start, end), 200)
        self.assertTrue(window["text"].startswith("구분 | 내용"))  # header row kept with the fact
        self.assertIn("130,000,000원 (부가가치세 포함)", window["text"])
        self.assertIn("분할 지급 불가", window["text"])  # the following condition row fits the bound
        self.assertEqual(["\n".join(table[a:b] for a, b in window["offsets"])], [window["text"]])
        self.assertIsNone(gold._window("x" * 50, "paragraph", (0, 50), 20))  # cannot fit: no fake candidate


if __name__ == "__main__":
    unittest.main()
