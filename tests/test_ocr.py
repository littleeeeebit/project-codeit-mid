import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rfp_assistant.corpus import ocr
from rfp_assistant.storage import postgres, store
from tests import fixtures


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

        from rfp_assistant.corpus import ingestion
        from rfp_assistant.retrieval.chunking import build_chunks
        from rfp_assistant.storage.store import write_jsonl_atomic

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

        from rfp_assistant.corpus import ingestion
        from rfp_assistant.storage.store import write_jsonl_atomic

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

        from rfp_assistant.corpus import ingestion
        from rfp_assistant.storage.store import write_jsonl_atomic

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

        from rfp_assistant.corpus import ingestion
        from rfp_assistant.storage.store import write_jsonl_atomic

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

        from rfp_assistant.corpus import ingestion
        from rfp_assistant.storage.store import write_jsonl_atomic

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

    def test_a_caption_repeated_on_the_same_page_leaves_the_lower_anchor_to_place_the_picture(self):
        import pymupdf

        from rfp_assistant.corpus import ingestion
        from rfp_assistant.storage.store import write_jsonl_atomic

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            pdf = Path(tmp) / "doc.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for y, text in ((80, "Figure network layout"), (230, "Paragraph after the first figure"),
                                (300, "Figure network layout"), (400, "Paragraph in the middle"),
                                (600, "Figure network layout"), (700, "Paragraph at the end")):
                    page.insert_text((72, y), text, fontsize=11)
                image(page, (72, 100, 472, 200))
                doc.save(pdf)
            raw, _, _ = ingestion.parse_pdf(pdf)
            write_jsonl_atomic(ocr.cache_path(settings, "h"), [read(pdf, 1, [72.0, 100.0, 472.0, 200.0])])
            out, warnings, _ = ocr.merge(settings, "h", raw, pdf)
        k = next(i for i, e in enumerate(out) if e["kind"] == "image_text")
        self.assertEqual(out[k + 1]["raw_text"].strip(), "Paragraph after the first figure")
        self.assertEqual(warnings, [])

    def test_a_picture_before_the_first_heading_belongs_to_no_section(self):
        import pymupdf

        from rfp_assistant.corpus import ingestion
        from rfp_assistant.storage.store import write_jsonl_atomic

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            pdf = Path(tmp) / "doc.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                image(page, (72, 40, 472, 190))
                page.insert_text((72, 220), "1. Requirements", fontsize=11)
                page.insert_text((72, 250), "The system shall keep an audit log", fontsize=11)
                doc.save(pdf)
            raw, _, _ = ingestion.parse_pdf(pdf)
            write_jsonl_atomic(ocr.cache_path(settings, "h"), [read(pdf, 1, [72.0, 40.0, 472.0, 190.0])])
            out, _, _ = ocr.merge(settings, "h", raw, pdf)
        self.assertEqual(out[0]["kind"], "image_text")
        self.assertTrue(raw[0]["location"].get("section_path"))  # the heading opens a section after the picture
        self.assertEqual(out[0]["location"]["section_path"], [])

    def test_an_unplaced_picture_belongs_to_no_section(self):
        import pymupdf

        from rfp_assistant.corpus import ingestion
        from rfp_assistant.storage.store import write_jsonl_atomic

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            pdf = Path(tmp) / "doc.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for y, text in ((60, "1. Overview"), (90, "Figure network layout"), (230, "See the figure above"),
                                (300, "Figure network layout"), (330, "See the figure above"),
                                (500, "2. Appendix"), (530, "Closing notes of the appendix")):
                    page.insert_text((72, y), text, fontsize=11)
                image(page, (72, 110, 472, 210))
                doc.save(pdf)
            raw, _, _ = ingestion.parse_pdf(pdf)
            write_jsonl_atomic(ocr.cache_path(settings, "h"), [read(pdf, 1, [72.0, 110.0, 472.0, 210.0])])
            out, warnings, _ = ocr.merge(settings, "h", raw, pdf)
        self.assertEqual(warnings, [{"code": "ocr_unplaced", "count": 1}])
        self.assertTrue(raw[-1]["location"].get("section_path"))  # the last element sits in 2. Appendix
        self.assertEqual((out[-1]["kind"], out[-1]["location"]["section_path"]), ("image_text", []))

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
    """ocr.run with the GPU reader, the gpt-5-mini reader and the sources table replaced by fakes."""

    def setUp(self):
        import io

        from PIL import Image

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = SimpleNamespace(data_dir=root, db_path=postgres.Target(fixtures.database(ready=False)))
        (root / "doc.pdf").write_bytes(b"%PDF")
        buf = io.BytesIO()
        Image.new("RGB", (40, 20), "white").save(buf, "PNG")
        self.png = buf.getvalue()
        self.local_reads = []
        self.remote_reads = []
        self.remote_error = None

    def tearDown(self):
        self.tmp.cleanup()

    def region(self, page, digest):
        return {"page": page, "bbox": [0.0, 0.0, 100.0, 50.0], "digest": digest, "png": self.png}

    def cached(self, page, digest, status, text, source="h"):
        from rfp_assistant.storage.store import write_jsonl_atomic

        local = {"text": text, "mean_prob": 0.99 if status == "local" else 0.5, "looped": False, "expected": 3,
                 "truncated": False}
        path = ocr.cache_path(self.settings, source)
        rows = ocr.load(self.settings, source) + [{
            "page": page, "bbox": [0.0, 0.0, 100.0, 50.0], "digest": digest, "rendering": "hancom_print",
            "engine": ocr.REMOTE_MODEL if status == "remote" else ocr.LOCAL_ENGINE, "text": text, "local": local,
            "reasons": [] if status == "local" else ["low_confidence"], "status": status}]
        write_jsonl_atomic(path, rows)

    def run_ocr(self, regions, fmt="hwp", remote=True, unavailable=()):
        test = self
        with store.open_db(self.settings.db_path) as conn:  # only the columns ocr.run reads
            conn.execute("CREATE TABLE IF NOT EXISTS sources (source_hash text, format text, original_path text, "
                         "parse_status text)")
            conn.execute("DELETE FROM sources")
            conn.execute("INSERT INTO sources VALUES ('h', ?, ?, 'parsed')",
                         (fmt, str(self.settings.data_dir / "doc.pdf")))

        class Local:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def read(self, image):
                test.local_reads.append(image.size)
                return {"text": "새 판독", "mean_prob": 0.99, "looped": False, "truncated": False}

        class Remote:
            def __init__(self, settings):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def read(self, png, region):
                test.remote_reads.append(region)
                if test.remote_error:
                    raise test.remote_error
                return "원격 판독", 1200

        rendering = "hwp_bindata" if fmt == "hwp" else "original"
        with mock.patch.object(ocr, "LocalOCR", Local), mock.patch.object(ocr, "RemoteReader", Remote), \
                mock.patch.object(ocr, "_source_regions", lambda s, src: (regions, unavailable, rendering)), \
                mock.patch.object(ocr, "expected_chars", lambda image: 3.0):
            return ocr.run(self.settings, remote=remote)

    def test_an_interrupted_rerun_keeps_cached_rows_it_had_not_reached(self):
        self.cached(1, "a", "unresolved", "흐림")
        self.cached(2, "b", "remote", "이미 결제한 판독")

        def interrupted():
            yield self.region(1, "a")
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_ocr(interrupted())
        rows = {r["digest"]: r for r in ocr.load(self.settings, "h")}
        self.assertEqual(rows["a"]["status"], "remote")
        self.assertEqual(rows["b"]["text"], "이미 결제한 판독")

    def test_a_different_picture_at_the_same_place_is_read_again(self):
        self.cached(1, "old", "remote", "옛 그림")
        self.run_ocr([self.region(1, "new")])
        self.assertEqual(len(self.local_reads), 1)
        self.assertEqual(ocr.load(self.settings, "h")[0]["text"], "새 판독")

    def test_a_read_reused_from_another_source_takes_this_sources_rendering(self):
        self.cached(3, "shared", "remote", "공통 로고", source="other")
        self.run_ocr([self.region(1, "shared")], fmt="pdf")
        row = ocr.load(self.settings, "h")[0]
        self.assertEqual((row["text"], row["rendering"]), ("공통 로고", "original"))
        self.assertEqual(self.local_reads, [])

    def test_a_truncated_local_read_goes_to_the_remote_reader(self):
        self.cached(1, "t", "local", "앞부분만")
        rows = ocr.load(self.settings, "h")
        rows[0]["local"]["truncated"] = True
        from rfp_assistant.storage.store import write_jsonl_atomic

        write_jsonl_atomic(ocr.cache_path(self.settings, "h"), rows)
        self.run_ocr([self.region(1, "t")])
        self.assertEqual(self.remote_reads, ["t"])
        self.assertEqual(ocr.load(self.settings, "h")[0]["status"], "remote")

    def test_a_local_read_cached_before_truncation_was_recorded_is_read_again(self):
        from rfp_assistant.storage.store import write_jsonl_atomic

        self.cached(1, "old", "local", "옛 판독")
        rows = ocr.load(self.settings, "h")
        del rows[0]["local"]["truncated"]
        write_jsonl_atomic(ocr.cache_path(self.settings, "h"), rows)
        self.assertFalse(ocr._final(rows[0]))
        self.run_ocr([self.region(1, "old")], remote=False)
        self.assertEqual(len(self.local_reads), 1)
        self.assertEqual(ocr.load(self.settings, "h")[0]["text"], "새 판독")

    def test_a_second_run_is_refused_while_one_writes_the_cache(self):
        from rfp_assistant.storage.store import ProcessLock

        lock = ProcessLock(self.settings.data_dir / "ocr" / "run.lock")
        try:
            with self.assertRaisesRegex(ocr.OcrError, "another OCR run"):
                self.run_ocr([self.region(1, "a")], remote=False)
        finally:
            lock.release()
        self.assertEqual(self.local_reads, [])

    def test_a_picture_repeated_unresolved_is_sent_to_the_remote_reader_once(self):
        self.cached(1, "r", "unresolved", "흐림")
        self.cached(2, "r", "unresolved", "흐림")
        self.run_ocr([self.region(1, "r"), self.region(2, "r")])
        self.assertEqual(self.remote_reads, ["r"])
        self.assertEqual([r["status"] for r in ocr.load(self.settings, "h")], ["remote", "remote"])

    def test_a_stopped_paid_read_ends_the_run_after_keeping_what_was_read(self):
        self.cached(1, "a", "unresolved", "흐림")
        self.cached(2, "b", "remote", "이미 결제한 판독")
        self.remote_error = ocr.OcrStop("unknown billing")
        with self.assertRaisesRegex(ocr.OcrStop, "unknown billing"):
            self.run_ocr([self.region(1, "a"), self.region(2, "c")])
        rows = {r["digest"]: r for r in ocr.load(self.settings, "h")}
        self.assertEqual((rows["a"]["status"], rows["a"]["error"]), ("unresolved", "unknown billing"))
        self.assertEqual(rows["b"]["text"], "이미 결제한 판독")  # not reached, kept
        self.assertNotIn("c", rows)
        self.assertEqual(self.remote_reads, ["a"])

    def test_embedded_pictures_are_keyed_by_their_stream_and_unreadable_ones_are_kept_as_unavailable(self):
        bindata = {"bindata": "BIN0001.png", "digest": "p", "png": self.png}
        totals = self.run_ocr([bindata], unavailable=[{"bindata": "BIN0002.bmp", "reason": "format"}], remote=False)
        rows = ocr.load(self.settings, "h")
        self.assertEqual([(r["bindata"], r["status"], r["rendering"]) for r in rows],
                         [("BIN0002.bmp", "unavailable", "hwp_bindata"), ("BIN0001.png", "local", "hwp_bindata")])
        self.assertEqual((totals["unavailable"], totals["local"]), (1, 1))
        self.run_ocr([bindata], unavailable=[{"bindata": "BIN0002.bmp", "reason": "format"}], remote=False)
        self.assertEqual(len(self.local_reads), 1)  # read once, reused by its stream


def picture(bindata_id, width=30000, height=20000):
    return (f'<GShapeObjectControl chid="gso " width="{width}" height="{height}"><ShapeComponent chid="$pic" '
            f'width="{width}" height="{height}"><ShapePicture><PictureInfo bindata-id="{bindata_id}"/></ShapePicture>'
            '</ShapeComponent></GShapeObjectControl>')


def shape(chid, inner=""):
    return (f'<GShapeObjectControl width="30000" height="20000"><ShapeComponent chid="{chid}" width="30000" '
            f'height="20000">{inner}</ShapeComponent></GShapeObjectControl>')


# BIN0001 in a paragraph, BIN0002 in a table cell, an OLE object, a drawing, a text box, then BIN0001 too small to
# hold text and a picture linked to an outside file (bindata 3).
HWP_XML = f"""<HwpDoc><DocInfo><IdMappings>
<BinData storage="embedding"><BinDataEmbedding storage-id="BIN0001" ext="png"/></BinData>
<BinData storage="embedding"><BinDataEmbedding storage-id="BIN0002" ext="bmp"/></BinData>
<BinData storage="link"/>
</IdMappings></DocInfo><BodyText><SectionDef><PageDef width="59528" height="84188"/>
<Paragraph><Text>1. 시스템 구성</Text></Paragraph>
<Paragraph><Text>구성도는 다음과 같다</Text>{picture(1)}</Paragraph>
<Paragraph><Text>구성도 다음 문단</Text></Paragraph>
<Paragraph><TableControl><TableBody rows="1" cols="2"><TableRow>
<TableCell row="0" col="0"><Paragraph><Text>구분</Text></Paragraph></TableCell>
<TableCell row="0" col="1"><Paragraph>{picture(2)}</Paragraph></TableCell>
</TableRow></TableBody></TableControl></Paragraph>
<Paragraph>{shape("$ole")}</Paragraph>
<Paragraph>{shape("$rec")}</Paragraph>
<Paragraph>{shape("$rec", "<TextboxParagraphList><Paragraph><Text>글상자</Text></Paragraph></TextboxParagraphList>")}</Paragraph>
<Paragraph><Text>끝 문단</Text>{picture(1, 3000, 2000)}{picture(3)}</Paragraph>
</SectionDef></BodyText></HwpDoc>"""


def bindata_row(bindata, text, status="local", reason=None):
    if status == "unavailable":
        return {"bindata": bindata, "rendering": "hwp_bindata", "status": status, "reason": reason}
    local = {"text": text, "mean_prob": 0.99 if status == "local" else 0.5, "looped": False, "expected": 5,
             "truncated": False}
    return {"bindata": bindata, "digest": f"d-{bindata}", "rendering": "hwp_bindata",
            "engine": ocr.REMOTE_MODEL if status == "remote" else ocr.LOCAL_ENGINE, "text": text, "local": local,
            "reasons": [] if status == "local" else ["low_confidence"], "status": status}


class HwpMarkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(data_dir=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def walk(self):
        import xml.etree.ElementTree as ET

        from rfp_assistant.corpus import ingestion

        return ingestion.walk_hwp(ET.fromstring(HWP_XML), markers=True)

    def merge(self, raw, rows):
        from rfp_assistant.storage.store import write_jsonl_atomic

        write_jsonl_atomic(ocr.cache_path(self.settings, "h"), rows)
        return ocr.merge_hwp(self.settings, "h", raw)

    def test_the_walk_marks_each_picture_where_its_control_sits(self):
        import xml.etree.ElementTree as ET

        from rfp_assistant.corpus import ingestion

        raw = self.walk()
        markers = [(e["path"], e["location"]["object"], e["location"].get("bindata")) for e in raw
                   if e["kind"] == "picture"]
        self.assertEqual(markers, [("s0/p1/g0/i0", "picture", "BIN0001.png"),
                                   ("s0/p3/t0/r0c1/p0/g0/i0", "picture", "BIN0002.bmp"),
                                   ("s0/p4/g0/i0", "ole", None), ("s0/p5/g0/i0", "drawing", None),
                                   ("s0/p7/g0/i0", "picture", "BIN0001.png"), ("s0/p7/g1/i0", "picture", None)])
        self.assertEqual(raw[[e["path"] for e in raw].index("s0/p1/g0/i0") - 1]["raw_text"], "구성도는 다음과 같다")
        self.assertEqual(raw[[e["path"] for e in raw].index("s0/p3/t0/r0c1/p0/g0/i0") - 1]["path"], "s0/p3/t0")
        self.assertNotIn("picture", [e["kind"] for e in ingestion.walk_hwp(ET.fromstring(HWP_XML))])

    def test_image_text_takes_the_markers_place_and_unreadable_objects_are_counted(self):
        out, warnings, suffix = self.merge(self.walk(), [
            bindata_row("BIN0001.png", "WEB 서버"), bindata_row("BIN0002.bmp", "표 안 그림", "remote"),
            bindata_row(None, "", "unavailable", "missing")])
        self.assertEqual([(e["kind"], e["raw_text"]) for e in out], [
            ("heading", "1. 시스템 구성"), ("paragraph", "구성도는 다음과 같다"), ("image_text", "WEB 서버"),
            ("paragraph", "구성도 다음 문단"), ("table", "구분"), ("image_text", "표 안 그림"),
            ("paragraph", "글상자"), ("paragraph", "끝 문단")])  # the small picture is not read
        loc = out[2]["location"]
        self.assertEqual((loc["format"], loc["rendering"], loc["bindata"], loc["path"], loc["section_path"]),
                         ("image_ocr", "hwp_bindata", "BIN0001.png", "s0/p1/g0/i0", ["1. 시스템 구성"]))
        self.assertNotIn("page", loc)  # placed by structure: no print page anchors
        self.assertEqual(warnings, [{"code": "ocr_unavailable", "count": 3,
                                     "by": {"drawing": 1, "missing": 1, "ole": 1}}])
        self.assertTrue(suffix.startswith("ocr-"))

    def test_unresolved_and_undecodable_pictures_leave_no_text_and_are_reported(self):
        out, warnings, suffix = self.merge(self.walk(), [
            bindata_row("BIN0001.png", "흐림", "unresolved"), bindata_row("BIN0002.bmp", "", "unavailable", "format")])
        self.assertEqual([e["kind"] for e in out if e["kind"] in ("image_text", "picture")], [])
        self.assertEqual(warnings, [{"code": "ocr_unavailable", "count": 3, "by": {"drawing": 1, "format": 1, "ole": 1}},
                                    {"code": "ocr_unresolved", "count": 1}])
        self.assertEqual(suffix, "")

    def test_the_same_text_at_another_marker_is_another_revision(self):
        rows = [bindata_row("BIN0001.png", "WEB 서버")]
        raw = self.walk()
        _, _, here = self.merge(raw, rows)
        k = [e["path"] for e in raw].index("s0/p1/g0/i0")
        moved = raw[:k] + raw[k + 1:k + 2] + [raw[k]] + raw[k + 2:]  # after the next paragraph instead
        _, _, there = self.merge(moved, rows)
        self.assertNotEqual(here, there)


def png_bytes(mode="RGB", color="white", size=(40, 20)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


class HwpRegionsTest(unittest.TestCase):
    def test_embedded_images_are_read_once_and_the_rest_reported_unavailable(self):
        import xml.etree.ElementTree as ET

        from PIL import Image

        from rfp_assistant.corpus import ingestion

        raw = ingestion.walk_hwp(ET.fromstring(HWP_XML), markers=True)
        streams = {"BIN0001.PNG": png_bytes("RGBA", (0, 0, 0, 0)), "BIN0002.bmp": b""}  # names differ in case

        def hwp5file(path):
            return {"BinData": {n: SimpleNamespace(open=lambda d=d: io.BytesIO(d)) for n, d in streams.items()}}

        with mock.patch.object(ocr, "parse_hwp", return_value=(raw, [], None)), \
                mock.patch("hwp5.xmlmodel.Hwp5File", hwp5file):
            found, unavailable = ocr.hwp_regions(SimpleNamespace(), Path("doc.hwp"))
        self.assertEqual([r["bindata"] for r in found], ["BIN0001.png"])  # once, though placed twice
        self.assertEqual(unavailable, [{"bindata": "BIN0002.bmp", "reason": "format"},
                                       {"bindata": None, "reason": "missing"}])
        image = Image.open(io.BytesIO(found[0]["png"]))
        self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))  # transparency on white
        with mock.patch.object(ocr, "parse_hwp", return_value=([], [], "hwp_xml_malformed")):
            self.assertIsNone(ocr.hwp_regions(SimpleNamespace(), Path("doc.hwp")))  # loader-parsed: no positions

    def test_a_large_picture_is_scaled_to_the_reading_size(self):
        from PIL import Image

        png = ocr.to_png(png_bytes(size=(4000, 1000)))
        self.assertEqual(Image.open(io.BytesIO(png)).size, (ocr.MAX_SIDE, ocr.MAX_SIDE // 4))


@unittest.skipUnless(sys.platform == "win32", "Pillow renders metafiles only on Windows")
class MetafileTest(unittest.TestCase):
    def test_a_wmf_without_the_placeable_header_becomes_a_bitmap(self):
        from PIL import Image

        body = b"".join([struct.pack("<IHhh", 5, 0x020B, 0, 0),  # SETWINDOWORG y, x
                         struct.pack("<IHhh", 5, 0x020C, 100, 200),  # SETWINDOWEXT height, width
                         struct.pack("<IHhhhh", 7, 0x041B, 80, 180, 20, 20),  # RECTANGLE
                         struct.pack("<IH", 3, 0)])  # EOF
        wmf = struct.pack("<HHHIHIH", 1, 9, 0x0300, (18 + len(body)) // 2, 0, 7, 0) + body
        image = Image.open(io.BytesIO(ocr.to_png(wmf)))
        self.assertEqual((image.format, image.size), ("PNG", (ocr.MAX_SIDE, ocr.MAX_SIDE // 2)))

    def test_an_emf_becomes_a_bitmap(self):
        from PIL import Image

        header = struct.pack("<II4i4iIIIIHHIIIiiii", 1, 88, 0, 0, 199, 99, 0, 0, 5291, 2645, 0x464D4520, 0x10000,
                             108, 2, 1, 0, 0, 0, 0, 1920, 1080, 508, 286)
        emf = header + struct.pack("<IIIII", 14, 20, 0, 16, 20)  # EMR_EOF
        image = Image.open(io.BytesIO(ocr.to_png(emf)))
        self.assertEqual(image.format, "PNG")
        self.assertLessEqual(abs(max(image.size) - ocr.MAX_SIDE), 1)


class Replies:
    """A transport answering each chat with the next reply (or raising it)."""

    def __init__(self, *replies):
        self.replies, self.calls, self.closed = list(replies), [], False

    def chat(self, **kw):
        self.calls.append(kw)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        self.closed = True


class RemoteReaderLedgerTest(unittest.TestCase):
    """gpt-5-mini reads go through the gateway: reserved at worst case, settled on usage, unknown kept charged."""

    USAGE = {"prompt_tokens": 900, "completion_tokens": 120, "cached_tokens": 0, "cache_write_tokens": 0}

    def setUp(self):
        from datetime import date

        from rfp_assistant.gateway import budget
        from rfp_assistant.settings import DEFAULT_RATES, Settings

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(source_dir=root, data_dir=root, database_dsn_env=fixtures.database(),
                                 hwp_converter=None, provider="fake")
        db = self.settings.db_path
        budget.ensure_budget_row(db)
        budget.configure(db, "owner", project_start=date(2026, 9, 30), project_end=date(2026, 10, 28),
                         prior_use_micro=0, prior_use_evidence="fixture", rates=DEFAULT_RATES,
                         rate_version="fixture", enable_paid=True)
        budget.set_paid_enabled(db, "owner", True, "fixture")
        with store.open_db(db) as conn:
            self.envelopes = json.loads(conn.execute("SELECT envelopes_json FROM budget_settings").fetchone()[0])
        self.fund(1_000_000)
        self.rates = DEFAULT_RATES[ocr.REMOTE_MODEL]

    def tearDown(self):
        self.tmp.cleanup()

    def fund(self, micro):
        from rfp_assistant.gateway import budget

        envelopes = {**self.envelopes, "ocr": micro,
                     "interactive": self.envelopes["interactive"] + self.envelopes["ocr"] - micro}
        budget.set_envelopes(self.settings.db_path, "owner", envelopes, "fixture")

    def attempts(self):
        with store.open_db(self.settings.db_path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT purpose, model, stage, state, reserved_micro_usd, settled_micro_usd FROM attempts")]

    def reply(self, content="제안요청서", finish="stop", usage=USAGE, refusal=None):
        from rfp_assistant.gateway import generation

        return generation.ProviderResponse(content, refusal, finish, usage, "resp-1")

    def test_a_read_is_reserved_at_worst_case_and_settled_on_its_usage(self):
        from rfp_assistant.gateway import budget

        transport = Replies(self.reply())
        with ocr.RemoteReader(self.settings, transport) as reader:
            text, micro = reader.read(b"png", "d1")
        self.assertEqual(text, "제안요청서")
        worst = budget.max_cost(self.rates, ocr.remote_input_tokens(self.settings), ocr.REMOTE_MAX_OUTPUT)
        settled = budget.micro_cost(self.rates, 900, 0, 120)
        self.assertEqual(self.attempts(), [{"purpose": "ocr", "model": "gpt-5-mini", "stage": "ocr:d1",
                                            "state": "settled", "reserved_micro_usd": worst,
                                            "settled_micro_usd": settled}])
        self.assertEqual(micro, settled)
        image = transport.calls[0]["messages"][0]["content"][1]["image_url"]
        self.assertEqual((image["url"][:22], image["detail"]), ("data:image/png;base64,", "high"))
        self.assertTrue(transport.closed)

    def test_an_unknown_outcome_stays_charged_at_the_reservation_and_stops_further_reads(self):
        from rfp_assistant.gateway import budget, generation

        with ocr.RemoteReader(self.settings, Replies(generation.ProviderError("timeout", pre_execution=False))) as r:
            with self.assertRaisesRegex(ocr.OcrStop, "unknown billing"):
                r.read(b"png", "d1")
        (attempt,) = self.attempts()
        self.assertEqual(attempt["state"], "unknown")
        self.assertEqual(budget.snapshot(self.settings.db_path).pending_micro_usd, attempt["reserved_micro_usd"])
        transport = Replies(self.reply())
        with ocr.RemoteReader(self.settings, transport) as reader:
            with self.assertRaisesRegex(ocr.OcrStop, "refused"):
                reader.read(b"png", "d2")
        self.assertEqual(transport.calls, [])

    def test_a_response_without_usage_is_unknown_billing(self):
        with ocr.RemoteReader(self.settings, Replies(self.reply(usage=None))) as reader:
            with self.assertRaisesRegex(ocr.OcrStop, "no usage"):
                reader.read(b"png", "d1")
        self.assertEqual(self.attempts()[0]["state"], "unknown")

    def test_an_unfunded_envelope_refuses_before_any_call(self):
        self.fund(0)
        transport = Replies(self.reply())
        with ocr.RemoteReader(self.settings, transport) as reader:
            with self.assertRaisesRegex(ocr.OcrStop, "envelope_exhausted"):
                reader.read(b"png", "d1")
        self.assertEqual((transport.calls, self.attempts()), ([], []))

    def test_an_unfinished_or_refused_answer_is_paid_but_not_a_read(self):
        transport = Replies(self.reply(finish="length"), self.reply(content=None, refusal="no"))
        with ocr.RemoteReader(self.settings, transport) as reader:
            with self.assertRaisesRegex(ocr.OcrError, "finish length"):
                reader.read(b"png", "d1")
            with self.assertRaisesRegex(ocr.OcrError, "refused"):
                reader.read(b"png", "d2")
        self.assertEqual([a["state"] for a in self.attempts()], ["settled", "settled"])

    def test_the_sample_reads_gemini_read_regions_and_reports_cost_and_f1_without_touching_the_cache(self):
        import pymupdf

        from rfp_assistant.corpus.ingestion import printed_pdf_path
        from rfp_assistant.gateway import budget
        from rfp_assistant.storage.store import write_jsonl_atomic

        h = "a" * 64
        pdf = printed_pdf_path(self.settings, h)
        pdf.parent.mkdir(parents=True)
        with pymupdf.open() as doc:
            image(doc.new_page(), (72, 120, 472, 320))
            doc.save(pdf)
        (reg,) = ocr.regions(pdf)
        local = {"text": "제안", "mean_prob": 0.5, "looped": False, "expected": 3, "truncated": False}
        write_jsonl_atomic(ocr.cache_path(self.settings, h, ocr.PRINT_VERSION), [{
            "page": 1, "bbox": reg["bbox"], "digest": reg["digest"], "rendering": "hancom_print",
            "engine": "gemini-3.5-flash-lite", "text": "제안요청서", "local": local, "reasons": ["low_confidence"],
            "status": "gemini"}])
        transport = Replies(self.reply("제안요청서"))
        report = ocr.sample(self.settings, 50, transport)
        settled = budget.micro_cost(self.rates, 900, 0, 120)
        self.assertEqual((report["read"], report["f1_mean"], report["local_f1_mean"], report["settled_usd"]),
                         (1, 1.0, round(2 * 2 / (2 + 5), 4), settled / 1e6))
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(ocr.cache_path(self.settings, h).exists())  # reference only
        (region,) = json.loads((self.settings.data_dir / "ocr" / "sample-gpt-5-mini.json").read_text("utf-8"))["regions"]
        self.assertEqual((region["text"], region["reference"]), ("제안요청서", "제안요청서"))

    def test_another_gateway_owner_refuses_the_reader_and_a_key_is_required(self):
        with fixtures.foreign_gateway(self.settings):
            with self.assertRaisesRegex(ocr.OcrError, "owns the paid gateway"):
                ocr.RemoteReader(self.settings, Replies()).__enter__()
        with ocr.RemoteReader(self.settings, Replies()):  # nothing was left held
            pass
        from rfp_assistant.gateway import budget

        with mock.patch.object(budget, "ensure_generation_rate", side_effect=RuntimeError("rate")):
            with self.assertRaisesRegex(RuntimeError, "rate"):
                ocr.RemoteReader(self.settings, Replies()).__enter__()
        with ocr.RemoteReader(self.settings, Replies()):  # a reader that failed to start released the gateway
            pass
        with mock.patch.object(ocr, "read_api_key", return_value=None):
            with self.assertRaisesRegex(ocr.OcrError, "OPENAI_API_KEY"):
                ocr.RemoteReader(self.settings)


if __name__ == "__main__":
    unittest.main()
