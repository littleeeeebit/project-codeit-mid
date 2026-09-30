import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rfp_assistant import ocr


class OcrRulesTest(unittest.TestCase):
    def test_loop_rule_stops_cycles_but_not_a_table_repeating_one_cell(self):
        self.assertTrue(ocr.looping(["요구사항 명칭", "요구사항 분류", "기타사항"] * 20))
        self.assertTrue(ocr.looping(["채제비"] * 12))
        self.assertFalse(ocr.looping(["EA 현행화 자료"] * 9 + [f"행 {i}" for i in range(60)]))

    def test_fallback_flags_loops_low_confidence_and_runaway_length_but_not_a_shortfall(self):
        text = "가" * 100
        self.assertEqual(ocr.fallback_reasons(text, 0.99, False, 100), [])
        self.assertEqual(ocr.fallback_reasons(text, 0.99, True, 100), ["loop"])
        self.assertEqual(ocr.fallback_reasons(text, 0.89, False, 100), ["low_confidence"])
        self.assertEqual(ocr.fallback_reasons(text, 0.99, False, 400), [])  # contents leaders inflate the ink
        self.assertEqual(ocr.fallback_reasons(text, 0.99, False, 4), ["text_invented"])  # over 20x
        self.assertEqual(ocr.fallback_reasons(text, 0.99, False, 15), [])  # 6.7x but short
        self.assertEqual(ocr.fallback_reasons("가" * 600, 0.99, False, 100), ["text_invented"])  # 6x and long


class MergeTest(unittest.TestCase):
    def test_image_text_lands_between_the_text_around_the_picture_and_chunks_on_its_own(self):
        import pymupdf

        from rfp_assistant import ingestion
        from rfp_assistant.chunking import build_chunks
        from rfp_assistant.store import write_jsonl_atomic

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            pdf = Path(tmp) / "doc.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                page.insert_text((72, 100), "System overview figure follows", fontsize=11)
                pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100), False)
                pix.set_rect(pix.irect, (255, 255, 255))
                page.insert_image(pymupdf.Rect(72, 120, 472, 320), pixmap=pix)
                page.insert_text((72, 350), "Next paragraph after the figure", fontsize=11)
                doc.save(pdf)
            raw, _, _ = ingestion.parse_pdf(pdf)
            local = {"text": "WEB 서버", "mean_prob": 0.99, "looped": False, "expected": 5}
            write_jsonl_atomic(ocr.cache_path(settings, "h"), [
                {"page": 1, "bbox": [72, 120, 472, 320], "digest": "d", "rendering": "original",
                 "engine": ocr.LOCAL_ENGINE, "text": "WEB 서버", "local": local, "reasons": [], "status": "local"},
                {"page": 1, "bbox": [72, 400, 472, 500], "digest": "e", "rendering": "original",
                 "engine": ocr.LOCAL_ENGINE, "text": "미해결", "local": {**local, "looped": True},
                 "reasons": ["loop"], "status": "unresolved"}])
            out, warnings, suffix = ocr.merge(settings, "h", raw, pdf)
        self.assertEqual([e["kind"] for e in out], ["paragraph", "image_text", "paragraph"])
        self.assertEqual(out[1]["location"]["format"], "image_ocr")
        self.assertEqual(warnings, [{"code": "ocr_unresolved", "count": 1}])
        self.assertTrue(suffix.startswith("ocr-"))
        elements = ingestion.finalize_elements(out, "x")
        chunks, _ = build_chunks(elements, "x")
        self.assertEqual([c["chunk_type"] for c in chunks if "WEB" in c["body"]], ["image_text"])

    def test_a_page_that_is_only_a_picture_follows_the_previous_page(self):
        import pymupdf

        from rfp_assistant import ingestion
        from rfp_assistant.store import write_jsonl_atomic

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            pdf = Path(tmp) / "doc.pdf"
            with pymupdf.open() as doc:
                doc.new_page().insert_text((72, 100), "Attached form on the next page", fontsize=11)
                pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
                pix.set_rect(pix.irect, (255, 255, 255))
                doc.new_page().insert_image(pymupdf.Rect(36, 36, 559, 806), pixmap=pix)
                doc.new_page().insert_text((72, 100), "Closing section after the form", fontsize=11)
                doc.save(pdf)
            raw, _, _ = ingestion.parse_pdf(pdf)
            local = {"text": "서약서", "mean_prob": 0.99, "looped": False, "expected": 3}
            write_jsonl_atomic(ocr.cache_path(settings, "h"), [
                {"page": 2, "bbox": [36, 36, 559, 806], "digest": "d", "rendering": "original",
                 "engine": ocr.LOCAL_ENGINE, "text": "서약서", "local": local, "reasons": [], "status": "local"}])
            out, warnings, _ = ocr.merge(settings, "h", raw, pdf)
        self.assertEqual([e["raw_text"].strip()[:8] for e in out], ["Attached", "서약서", "Closing "])
        self.assertEqual(warnings, [])


class GeminiLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(data_dir=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def reader(self, responses):
        with mock.patch.object(ocr, "read_api_key", return_value="test-key"):
            r = ocr.GeminiReader(self.settings)
        r._post = mock.Mock(side_effect=responses)
        return r

    def test_settled_call_costs_its_usage_and_an_unknown_outcome_costs_worst_case(self):
        ok = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "제안요청서"}]}}],
              "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100, "thoughtsTokenCount": 20}}
        r = self.reader([{"totalTokens": 1000}, ok, {"totalTokens": 1000}, ocr.OcrError("HTTP 500")])
        self.assertEqual(r.read(b"png", "a"), "제안요청서")
        settled = ocr._micro(1000, 120)
        self.assertEqual(ocr.spent_micro(r.ledger), settled)
        with self.assertRaises(ocr.OcrError):
            r.read(b"png", "b")
        self.assertEqual(ocr.spent_micro(r.ledger), settled + ocr._micro(1000, ocr.GEMINI_MAX_OUTPUT))

    def test_a_call_that_could_pass_the_cap_is_never_sent(self):
        r = self.reader([{"totalTokens": 1000}])
        with mock.patch.object(ocr, "GEMINI_CAP_MICRO", ocr._micro(1000, ocr.GEMINI_MAX_OUTPUT) - 1):
            with self.assertRaisesRegex(ocr.OcrError, "cap"):
                r.read(b"png", "a")
        self.assertEqual(r._post.call_count, 1)  # countTokens only
        self.assertEqual(ocr.spent_micro(r.ledger), 0)


if __name__ == "__main__":
    unittest.main()
