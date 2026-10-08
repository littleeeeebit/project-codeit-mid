"""Offline checks for frozen-package verification; no database or paid provider."""

import unittest
from pathlib import PurePosixPath, PureWindowsPath

from tools.verification.frozen_corpus import audit_quotes, preview, project, relative_name, source_map, validate_records


class FrozenCorpusTest(unittest.TestCase):
    def test_hash_mapping_keeps_duplicate_associations_and_is_portable(self):
        manifest = [{"doc_id": "hash-id", "sha256": "hash", "representative_path": "한.hwp",
                     "source_paths": ["한.hwp", "copy.hwp"]}]
        originals = [{"source_hash": "hash", "doc_id": "uuid-1", "filename": "한.hwp"},
                     {"source_hash": "hash", "doc_id": "uuid-2", "filename": "copy.hwp"}]
        mapped = source_map(manifest, originals)[0]
        self.assertEqual([x["doc_id"] for x in mapped["associations"]], ["uuid-1", "uuid-2"])
        relative = mapped["associations"][0]["original_relative_to_source_dir"]
        self.assertFalse(PurePosixPath(relative).is_absolute())
        self.assertFalse(PureWindowsPath(relative).is_absolute())
        self.assertEqual(str(PurePosixPath("/srv/bidmate/app/원본 데이터") / relative),
                         "/srv/bidmate/app/원본 데이터/files/한.hwp")
        originals[0]["source_hash"] = "wrong"
        with self.assertRaises(ValueError):
            source_map(manifest, originals)
        for name in ("../a.hwp", "C:\\a.hwp", "/a.hwp", "a/b.hwp", "..", "file:stream"):
            with self.assertRaises(ValueError):
                relative_name(name)

    def test_text_views_keep_original_coordinates_out_of_clean_spans_and_preserve_empty_search(self):
        row = {"record_id": "hash:body:1", "doc_id": "hash-id", "sha256": "hash", "raw_text": "원 본",
               "clean_text": "원본", "search_text": "원본 그림 설명", "index_eligible": True,
               "heading_path": [], "pdf_page_number": None, "hwp_element_index": 3,
               "element_type": "body", "raw_start": 100, "raw_end": 103}
        empty = {**row, "record_id": "empty", "search_text": ""}
        validate_records([row, empty], [{"doc_id": "hash-id", "sha256": "hash"}])
        clean_id, clean = project([row, empty], "clean_text", "dataset")
        search_id, search = project([row, empty], "search_text", "dataset")
        self.assertNotEqual(clean_id, search_id)
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["raw_text"], "원본")
        self.assertEqual(search[0]["raw_text"], "원본 그림 설명")
        self.assertEqual(row["raw_text"], "원 본")
        self.assertNotIn("raw_start", clean[0]["location"])
        with self.assertRaises(ValueError):
            validate_records([row, row], [{"doc_id": "hash-id", "sha256": "hash"}])
        with self.assertRaises(ValueError):
            validate_records([{**row, "raw_end": 1}], [{"doc_id": "hash-id", "sha256": "hash"}])
        oversized = {**row, "heading_path": ["사업 요구사항 " * 1000]}
        result = preview([oversized], "clean_text", "dataset")
        self.assertEqual(result["errors"][0]["kind"], "chunker_exception")
        self.assertEqual(result["oversized_headings"][0]["record_id"], row["record_id"])

    def test_quote_diagnostic_requires_whole_quote_in_the_correct_original(self):
        rows = [{"question_id": "q", "evidence_groups": [
            {"group_id": "g1", "alternatives": [{"source_hash": "a", "quote": "기간 1 년"}]},
            {"group_id": "g2", "alternatives": [{"source_hash": "b", "quote": "예외 포함"}]}]}]
        result = audit_quotes(rows, {"a": "기간1년예외포함", "b": "예외"})
        self.assertEqual((result["groups_found"], result["questions_with_all_groups"]), (1, 0))
        self.assertEqual(result["rows"][0]["missing_groups"][0]["group_id"], "g2")


if __name__ == "__main__":
    unittest.main()
