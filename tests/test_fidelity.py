import copy
import tempfile
import unittest
from pathlib import Path

import pymupdf

from rfp_assistant import fidelity

PARAGRAPHS = [
    "1.1 과업명 : 통합 정보시스템 구축 사업",
    "가. 계약금액은 금 1,200,000,000원(부가가치세 포함)으로 한다.",
    "나. 하자보수 기간은 검수 완료일로부터 12개월로 하며, 계약상대자는 장애 발생 시 4시간 이내에 조치하여야 한다.",
    "다. 제안서는 2024년 5월 20일 17시까지 전자조달시스템으로 제출하여야 한다.",
]
CELLS = [("요구사항 고유번호", "SFR-001"), ("요구사항 명칭", "통합 로그인 기능"),
         ("세부 내용", "사용자는 하나의 계정으로 모든 하위 시스템에 접근할 수 있어야 한다")]


def elements():
    out = [{"element_id": f"p{i}", "raw_text": t, "table": None} for i, t in enumerate(PARAGRAPHS)]
    cells = [{"row": r, "col": c, "text": text} for r, pair in enumerate(CELLS) for c, text in enumerate(pair)]
    out.append({"element_id": "t0", "raw_text": "", "table": {"caption": "", "cells": cells}})
    return out


def render(path: Path) -> Path:
    """A native-looking rendering: paragraphs as lines, the table as label/value columns, a page-number footer."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for t in PARAGRAPHS:
        page.insert_text((72, y), t[:45], fontname="korea", fontsize=9)
        if len(t) > 45:
            y += 14
            page.insert_text((72, y), t[45:], fontname="korea", fontsize=9)
        y += 20
    for label, value in CELLS:
        page.insert_text((72, y), label, fontname="korea", fontsize=9)
        page.insert_text((200, y), value, fontname="korea", fontsize=9)
        y += 16
    page.insert_text((290, 800), "- 1 -", fontname="korea", fontsize=9)
    doc.save(path)
    return path


class FidelityCompareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pages = fidelity.rendered_pages(render(Path(cls.tmp.name) / "r.pdf"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_faithful_extraction_is_auto_verified(self):
        result = fidelity.compare(elements(), self.pages)
        self.assertEqual(result["verdict"], "auto_verified", result["findings"])
        self.assertEqual(result["metrics"]["extraction_unmatched"], 0)

    def test_changed_digit_is_flagged_at_its_element(self):
        els = elements()
        els[1]["raw_text"] = els[1]["raw_text"].replace("1,200,000,000", "1,300,000,000")
        result = fidelity.compare(els, self.pages)
        self.assertEqual(result["verdict"], "auto_flagged")
        self.assertTrue(any(f.get("element_id") == "p1" and f["digits"] for f in result["findings"]))

    def test_dropped_paragraph_is_flagged_on_the_rendering_side(self):
        els = [e for e in elements() if e["element_id"] != "p2"]
        result = fidelity.compare(els, self.pages)
        self.assertEqual(result["verdict"], "auto_flagged")
        self.assertTrue(any(f["side"] == "rendering" and f["page"] == 1 for f in result["findings"]))

    def test_truncated_table_cell_is_flagged(self):
        els = copy.deepcopy(elements())
        els[-1]["table"]["cells"][-1]["text"] = "사용자는 하나의 계정으로"
        self.assertEqual(fidelity.compare(els, self.pages)["verdict"], "auto_flagged")

    def test_garbled_extraction_is_flagged(self):
        els = elements()
        els[2]["raw_text"] = els[2]["raw_text"].replace("하자보수 기간은", "��보수 기간은")
        result = fidelity.compare(els, self.pages)
        self.assertEqual(result["verdict"], "auto_flagged")
        self.assertTrue(any(f.get("element_id") == "p2" for f in result["findings"]))


if __name__ == "__main__":
    unittest.main()
