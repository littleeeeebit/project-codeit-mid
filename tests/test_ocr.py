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
        self.assertEqual(ocr.fallback_reasons(text, 0.99, False, 100, truncated=True), ["truncated"])


def image(page, rect, color=(255, 255, 255)):
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 50), False)
    pix.set_rect(pix.irect, color)
    page.insert_image(pymupdf.Rect(rect), pixmap=pix, keep_proportion=False)  # fills the rect exactly


def read(pdf, page, bbox, text="WEB 서버", status="local"):
    """A cache row for the region of `pdf` at (page, bbox), carrying that region's real digest."""
    digest = next(r["digest"] for r in ocr.regions(pdf) if (r["page"], r["bbox"]) == (page, bbox))
    local = {"text": text, "mean_prob": 0.99, "looped": status != "local", "expected": 5, "truncated": False}
    return {"page": page, "bbox": bbox, "digest": digest, "rendering": "original", "engine": ocr.LOCAL_ENGINE,
            "text": text, "local": local, "reasons": [] if status == "local" else ["loop"], "status": status}


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
                image(page, (72, 120, 472, 320))
                page.insert_text((72, 350), "Next paragraph after the figure", fontsize=11)
                image(page, (72, 400, 472, 500), (0, 0, 0))
                doc.save(pdf)
            raw, _, _ = ingestion.parse_pdf(pdf)
            write_jsonl_atomic(ocr.cache_path(settings, "h"), [
                read(pdf, 1, [72.0, 120.0, 472.0, 320.0]),
                read(pdf, 1, [72.0, 400.0, 472.0, 500.0], "미해결", "unresolved")])
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
            write_jsonl_atomic(ocr.cache_path(settings, "h"), [read(pdf, 2, [36.0, 36.0, 559.0, 806.0], "서약서")])
            out, warnings, _ = ocr.merge(settings, "h", raw, pdf)
        self.assertEqual([e["raw_text"].strip()[:8] for e in out], ["Attached", "서약서", "Closing "])
        self.assertEqual(warnings, [])

    def test_the_same_text_placed_elsewhere_is_another_revision(self):
        import pymupdf

        from rfp_assistant import ingestion
        from rfp_assistant.store import write_jsonl_atomic

        suffixes, orders = [], []
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            for y in (40, 300):  # the same picture above, then below, the paragraph
                pdf = Path(tmp) / f"doc{y}.pdf"
                with pymupdf.open() as doc:
                    page = doc.new_page()
                    page.insert_text((72, 220), "Paragraph in the middle of the page", fontsize=11)
                    image(page, (72, y, 472, y + 150))
                    doc.save(pdf)
                raw, _, _ = ingestion.parse_pdf(pdf)
                write_jsonl_atomic(ocr.cache_path(settings, "h"), [read(pdf, 1, [72.0, y, 472.0, y + 150.0])])
                out, _, suffix = ocr.merge(settings, "h", raw, pdf)
                orders.append([e["kind"] for e in out])
                suffixes.append(suffix)
        self.assertEqual(orders, [["image_text", "paragraph"], ["paragraph", "image_text"]])
        self.assertNotEqual(suffixes[0], suffixes[1])

    def test_a_read_of_a_region_the_rendering_no_longer_has_is_stale_not_a_crash(self):
        import pymupdf

        from rfp_assistant import ingestion
        from rfp_assistant.store import write_jsonl_atomic

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            pdf = Path(tmp) / "doc.pdf"
            with pymupdf.open() as doc:
                doc.new_page().insert_text((72, 100), "First page", fontsize=11)
                image(doc.new_page(), (72, 120, 472, 320))
                doc.save(pdf)
            row = read(pdf, 2, [72.0, 120.0, 472.0, 320.0])
            with pymupdf.open() as doc:  # the reprint fits on one page
                doc.new_page().insert_text((72, 100), "First page", fontsize=11)
                doc.save(pdf)
            raw, _, _ = ingestion.parse_pdf(pdf)
            write_jsonl_atomic(ocr.cache_path(settings, "h"), [row])
            out, warnings, suffix = ocr.merge(settings, "h", raw, pdf)
        self.assertEqual((out, suffix), (raw, ""))
        self.assertEqual(warnings, [{"code": "ocr_stale", "count": 1}])

    def test_a_caption_repeated_on_another_page_does_not_take_the_picture(self):
        import pymupdf

        from rfp_assistant import ingestion
        from rfp_assistant.store import write_jsonl_atomic

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            pdf = Path(tmp) / "doc.pdf"
            with pymupdf.open() as doc:
                doc.new_page().insert_text((72, 100), "Opening remarks", fontsize=11)
                page = doc.new_page()
                page.insert_text((72, 100), "Figure 1 network layout", fontsize=11)
                image(page, (72, 120, 472, 320))
                page = doc.new_page()  # dense: its middle is the middle of the element list, like page 2 of 3
                for i in range(30):
                    text = "Figure 1 network layout" if i == 15 else f"Line {i:02d} of the appendix"
                    page.insert_text((72, 60 + 24 * i), text, fontsize=11)
                doc.save(pdf)
            raw, _, _ = ingestion.parse_pdf(pdf)
            write_jsonl_atomic(ocr.cache_path(settings, "h"), [read(pdf, 2, [72.0, 120.0, 472.0, 320.0])])
            out, warnings, _ = ocr.merge(settings, "h", raw, pdf)
        k = next(i for i, e in enumerate(out) if e["kind"] == "image_text")
        self.assertEqual((out[k - 1]["location"]["page"], out[k - 1]["raw_text"].strip()), (2, "Figure 1 network layout"))
        self.assertEqual(warnings, [])

    def test_a_picture_on_a_rotated_page_is_rendered_where_it_is_seen(self):
        import pymupdf

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "doc.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                image(page, (72, 120, 472, 320), (255, 0, 0))
                page.set_rotation(90)
                doc.save(pdf)
            (reg,) = ocr.regions(pdf)
            pix = pymupdf.Pixmap(reg["png"])
        self.assertEqual(reg["bbox"], [72.0, 120.0, 472.0, 320.0])  # unrotated, like the words anchors come from
        self.assertEqual(pix.pixel(pix.width // 2, pix.height // 2), (255, 0, 0))


class RunTest(unittest.TestCase):
    """ocr.run with the GPU reader, Gemini and the sources table replaced by fakes."""

    def setUp(self):
        import io

        from PIL import Image

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = SimpleNamespace(data_dir=root, db_path=root / "x.sqlite3")
        (root / "doc.pdf").write_bytes(b"%PDF")
        buf = io.BytesIO()
        Image.new("RGB", (40, 20), "white").save(buf, "PNG")
        self.png = buf.getvalue()
        self.local_reads = []
        self.gemini_reads = []

    def tearDown(self):
        self.tmp.cleanup()

    def region(self, page, digest):
        return {"page": page, "bbox": [0.0, 0.0, 100.0, 50.0], "digest": digest, "png": self.png}

    def cached(self, page, digest, status, text, source="h"):
        from rfp_assistant.store import write_jsonl_atomic

        local = {"text": text, "mean_prob": 0.99 if status == "local" else 0.5, "looped": False, "expected": 3,
                 "truncated": False}
        path = ocr.cache_path(self.settings, source)
        rows = ocr.load(self.settings, source) + [{
            "page": page, "bbox": [0.0, 0.0, 100.0, 50.0], "digest": digest, "rendering": "hancom_print",
            "engine": ocr.GEMINI_MODEL if status == "gemini" else ocr.LOCAL_ENGINE, "text": text, "local": local,
            "reasons": [] if status == "local" else ["low_confidence"], "status": status}]
        write_jsonl_atomic(path, rows)

    def run_ocr(self, regions, fmt="hwp", gemini=True):
        import sqlite3
        from contextlib import contextmanager

        test = self

        class Local:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def read(self, image):
                test.local_reads.append(image.size)
                return {"text": "새 판독", "mean_prob": 0.99, "looped": False, "truncated": False}

        class Gemini:
            def __init__(self, settings):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def read(self, png, region):
                test.gemini_reads.append(region)
                return "제미니 판독"

        @contextmanager
        def db(_):
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE sources (source_hash, format, original_path, parse_status)")
            conn.execute("INSERT INTO sources VALUES ('h', ?, ?, 'parsed')",
                         (fmt, str(self.settings.data_dir / "doc.pdf")))
            yield conn
            conn.close()

        with mock.patch.object(ocr, "LocalOCR", Local), mock.patch.object(ocr, "GeminiReader", Gemini), \
                mock.patch.object(ocr, "open_db", db), mock.patch.object(ocr, "regions", lambda pdf: regions), \
                mock.patch.object(ocr, "rendering_for", lambda s, src: s.data_dir / "doc.pdf"), \
                mock.patch.object(ocr, "expected_chars", lambda image: 3.0):
            return ocr.run(self.settings, gemini=gemini)

    def test_an_interrupted_rerun_keeps_cached_rows_it_had_not_reached(self):
        self.cached(1, "a", "unresolved", "흐림")
        self.cached(2, "b", "gemini", "이미 결제한 판독")

        def interrupted():
            yield self.region(1, "a")
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_ocr(interrupted())
        rows = {r["digest"]: r for r in ocr.load(self.settings, "h")}
        self.assertEqual(rows["a"]["status"], "gemini")
        self.assertEqual(rows["b"]["text"], "이미 결제한 판독")

    def test_a_different_picture_at_the_same_place_is_read_again(self):
        self.cached(1, "old", "gemini", "옛 그림")
        self.run_ocr([self.region(1, "new")])
        self.assertEqual(len(self.local_reads), 1)
        self.assertEqual(ocr.load(self.settings, "h")[0]["text"], "새 판독")

    def test_a_read_reused_from_another_source_takes_this_sources_rendering(self):
        self.cached(3, "shared", "gemini", "공통 로고", source="other")
        self.run_ocr([self.region(1, "shared")], fmt="pdf")
        row = ocr.load(self.settings, "h")[0]
        self.assertEqual((row["text"], row["rendering"]), ("공통 로고", "original"))
        self.assertEqual(self.local_reads, [])

    def test_a_truncated_local_read_goes_to_gemini(self):
        self.cached(1, "t", "local", "앞부분만")
        rows = ocr.load(self.settings, "h")
        rows[0]["local"]["truncated"] = True
        from rfp_assistant.store import write_jsonl_atomic

        write_jsonl_atomic(ocr.cache_path(self.settings, "h"), rows)
        self.run_ocr([self.region(1, "t")])
        self.assertEqual(self.gemini_reads, ["t"])
        self.assertEqual(ocr.load(self.settings, "h")[0]["status"], "gemini")

    def test_a_local_read_cached_before_truncation_was_recorded_is_read_again(self):
        from rfp_assistant.store import write_jsonl_atomic

        self.cached(1, "old", "local", "옛 판독")
        rows = ocr.load(self.settings, "h")
        del rows[0]["local"]["truncated"]
        write_jsonl_atomic(ocr.cache_path(self.settings, "h"), rows)
        self.assertFalse(ocr._final(rows[0]))
        self.run_ocr([self.region(1, "old")], gemini=False)
        self.assertEqual(len(self.local_reads), 1)
        self.assertEqual(ocr.load(self.settings, "h")[0]["text"], "새 판독")

    def test_a_second_run_is_refused_while_one_writes_the_cache(self):
        from rfp_assistant.store import ProcessLock

        lock = ProcessLock(self.settings.data_dir / "ocr" / "run.lock")
        try:
            with self.assertRaisesRegex(ocr.OcrError, "another OCR run"):
                self.run_ocr([self.region(1, "a")], gemini=False)
        finally:
            lock.release()
        self.assertEqual(self.local_reads, [])

    def test_a_picture_repeated_unresolved_is_sent_to_gemini_once(self):
        self.cached(1, "r", "unresolved", "흐림")
        self.cached(2, "r", "unresolved", "흐림")
        self.run_ocr([self.region(1, "r"), self.region(2, "r")])
        self.assertEqual(self.gemini_reads, ["r"])
        self.assertEqual([r["status"] for r in ocr.load(self.settings, "h")], ["gemini", "gemini"])


class GeminiLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(data_dir=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def reader(self, responses=()):
        with mock.patch.object(ocr, "read_api_key", return_value="test-key"):
            r = ocr.GeminiReader(self.settings)
        r._post = mock.Mock(side_effect=list(responses))
        return r

    def test_settled_call_costs_its_usage_and_an_unknown_outcome_costs_worst_case(self):
        ok = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "제안요청서"}]}}],
              "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100, "thoughtsTokenCount": 20,
                                "totalTokenCount": 1120}}
        with self.reader([{"totalTokens": 1000}, ok, {"totalTokens": 1000}, ocr.OcrError("HTTP 500")]) as r:
            self.assertEqual(r.read(b"png", "a"), "제안요청서")
            settled = ocr._micro(1000, 120)
            self.assertEqual(ocr.spent_micro(r.ledger), settled)
            with self.assertRaises(ocr.OcrError):
                r.read(b"png", "b")
            self.assertEqual(ocr.spent_micro(r.ledger), settled + ocr._micro(1000, ocr.GEMINI_MAX_OUTPUT))

    def test_a_read_without_billing_figures_stays_charged_at_worst_case(self):
        ok = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "제안요청서"}]}}]}
        with self.reader([{"totalTokens": 1000}, ok]) as r:
            self.assertEqual(r.read(b"png", "a"), "제안요청서")
            self.assertEqual(ocr.spent_micro(r.ledger), ocr._micro(1000, ocr.GEMINI_MAX_OUTPUT))

    def test_a_call_that_could_pass_the_cap_is_never_sent(self):
        with self.reader([{"totalTokens": 1000}]) as r:
            with mock.patch.object(ocr, "GEMINI_CAP_MICRO", ocr._micro(1000, ocr.GEMINI_MAX_OUTPUT) - 1):
                with self.assertRaisesRegex(ocr.OcrError, "cap"):
                    r.read(b"png", "a")
            self.assertEqual(r._post.call_count, 1)  # countTokens only
            self.assertEqual(ocr.spent_micro(r.ledger), 0)

    def test_only_one_reader_spends_from_the_ledger_at_a_time(self):
        first, second = self.reader(), self.reader()
        with first:
            with self.assertRaisesRegex(ocr.OcrError, "another process"):
                second.__enter__()
        with self.assertRaisesRegex(ocr.OcrError, "no ledger lock"):
            first.read(b"png", "a")  # outside its with block
        with second:  # released on exit
            pass

    def test_a_reader_that_fails_to_start_releases_the_ledger(self):
        failed = self.reader()  # kept alive, so garbage collection cannot release the lock for it
        with mock.patch("httpx.Client", side_effect=RuntimeError("no client")):
            with self.assertRaises(RuntimeError):
                failed.__enter__()
        with self.reader():
            pass

    def test_a_blocked_or_unfinished_response_is_not_a_read(self):
        usage = {"usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 0}}
        blocked = {"promptFeedback": {"blockReason": "OTHER"}, **usage}
        unfinished = {"candidates": [{"content": {"parts": [{"text": ""}]}}], **usage}
        with self.reader([{"totalTokens": 1000}, blocked, {"totalTokens": 1000}, unfinished]) as r:
            with self.assertRaisesRegex(ocr.OcrError, "blocked OTHER"):
                r.read(b"png", "a")
            with self.assertRaisesRegex(ocr.OcrError, "finish None"):
                r.read(b"png", "b")


if __name__ == "__main__":
    unittest.main()
