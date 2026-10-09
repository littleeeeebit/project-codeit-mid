"""Automatic extraction fidelity check against an independent native rendering.

An HWP original is printed by Hancom Viewer to "Microsoft Print to PDF". That PDF's text layer comes from Hancom's
own renderer, not from pyhwp, so it is an independent witness of the text. The check compares, in both directions:
every extracted unit (a paragraph, or one line of a table cell) must appear in the rendering, every rendered page must
appear in the extraction, and every run of 3+ digits must match exactly. Text inside images is in neither side, so
pages with images are listed separately. People inspect only the reported findings, never whole documents.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import time
import unicodedata
from collections import Counter
from pathlib import Path

from ..settings import Settings
from ..storage.store import dumps, open_db, tx, utcnow
from .ingestion import IngestionError, load_elements, printed_pdf_path, sha256_file

FIDELITY_VERSION = "hancom-print-textlayer-1"
VIEWER = Path(r"C:\Program Files (x86)\Hnc\Office 2024 Viewer\HOffice130\Bin\hwpviewer.exe")
PDF_PRINTER = "Microsoft Print to PDF"
SAVE_DIALOG_TITLE = "다음 이름으로 프린터 출력 저장"
N = 6  # shingle length in normalized characters
# Verdict, calibrated on the corpus (releases/phase-1/report.md). A rendered stretch of MIN_STRETCH+ characters
# that the extraction lacks was, on inspection, always real content (equations, diagram and shape text), so any
# such stretch flags the document. An extraction-side stretch at a unit's start or end that is rendered within
# WRAP_REACH characters of the rest of the unit is a wrap artifact of a table cell and is only listed; any other
# (a wrong or garbled character) flags it.
MIN_STRETCH = 8
WRAP_REACH = 60
# Running headers and footers are left out of the extraction on purpose (they repeat on every page): a line in the
# top or bottom CHROME_BAND of the page that recurs on CHROME_PAGES or more pages is not compared.
CHROME_BAND = 0.1
CHROME_PAGES = 3
MAX_EXTRACTION_UNMATCHED_SHARE = 0.001
IMAGE_MIN_AREA = 0.02
HUMAN_STATUSES = ("sample_checked", "reviewed")
# Warning of extractions from before the loader fallback, whose text was read from the print itself: the print
# cannot judge them. `ingest` replaces them with a loader parse, which `fidelity run` judges like any other HWP.
RECOVERED = "recovered_from_native_print"
_DROP = re.compile(r"[^0-9A-Za-z\uac00-\ud7a3\ufffd]")  # U+FFFD kept: a decode failure must never match
_DIGITS = re.compile(r"\d{3,}")
_PAGE_NUMBERS = re.compile(r"^\s*(?:\d{1,4}|(?:[-–—]\s*\d{1,4}\s*[-–—]\s*)+)\s*$")  # "- 10 -  - 11 -"
GUTTER_MIN = 12  # points
_TOC_PAGE = re.compile(r"\t[\s.·…]*\d{1,4}\s*$")
_EQUATION_SPLIT = re.compile(r" (?:/|×|÷|[()\[\]]) ")


class FidelityError(IngestionError):
    pass


def norm(text: str) -> str:
    """Letters and digits only: spacing, punctuation and bullet glyphs differ between renderers."""
    return _DROP.sub("", unicodedata.normalize("NFKC", text))


def _grams(s: str) -> list[str]:
    return [s[i:i + N] for i in range(len(s) - N + 1)]


# ---------------------------------------------------------------- native printing (Windows, Hancom Viewer)


def _default_printer() -> str:
    winspool = ctypes.WinDLL("winspool.drv")
    size = ctypes.c_ulong(0)
    winspool.GetDefaultPrinterW(None, ctypes.byref(size))
    buf = ctypes.create_unicode_buffer(max(size.value, 1))
    winspool.GetDefaultPrinterW(buf, ctypes.byref(size))
    return buf.value


def _save_dialog() -> int:
    return ctypes.windll.user32.FindWindowW("#32770", SAVE_DIALOG_TITLE)


def _filename_edit(dialog: int) -> int:
    """The dialog's filename field; 0 until the dialog has built its controls."""
    user32 = ctypes.windll.user32
    edits: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        cls = ctypes.create_unicode_buffer(32)
        user32.GetClassNameW(hwnd, cls, 32)
        if cls.value == "Edit":
            edits.append(hwnd)
        return True

    user32.EnumChildWindows(dialog, visit, 0)
    return edits[0] if edits else 0


def _complete_pdf(path: Path) -> bool:
    import pymupdf

    try:
        with pymupdf.open(path) as doc:
            return doc.page_count > 0
    except Exception:  # noqa: BLE001 - still being written by the spooler
        return False


def print_hwp(settings: Settings, source_hash: str, timeout: float = 600) -> Path:
    """Prints the original through Hancom Viewer and answers its save dialog. Nothing on the system is changed:
    the default printer must already be Microsoft Print to PDF. Do not print anything else while this runs."""
    with open_db(settings.db_path) as conn:
        src = conn.execute("SELECT format, original_path FROM sources WHERE source_hash = ?", (source_hash,)).fetchone()
    if src is None or src["format"] != "hwp":
        raise FidelityError("native printing applies to HWP sources")
    if not VIEWER.exists():
        raise FidelityError(f"Hancom Viewer not found at {VIEWER}")
    if _default_printer() != PDF_PRINTER:
        raise FidelityError(f"set the Windows default printer to '{PDF_PRINTER}' first")
    if _save_dialog():
        raise FidelityError("a print save dialog is already open; close it first")
    target = printed_pdf_path(settings, source_hash)
    partial = target.with_name(target.stem + ".partial.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial.unlink(missing_ok=True)
    user32 = ctypes.windll.user32
    proc = subprocess.Popen([str(VIEWER), "/p", src["original_path"]])
    deadline = time.monotonic() + timeout
    try:
        # The viewer may hand the job to a running instance, so the dialog is found by title, not by process.
        while not ((dialog := _save_dialog()) and (edit := _filename_edit(dialog))):
            if time.monotonic() > deadline:
                raise FidelityError("Hancom Viewer did not show the print save dialog")
            time.sleep(0.3)
        time.sleep(0.5)  # let the dialog finish initializing before it is answered
        user32.SendMessageW(edit, 0x000C, 0, ctypes.c_wchar_p(str(partial)))  # WM_SETTEXT
        user32.PostMessageW(dialog, 0x0111, 1, 0)  # WM_COMMAND IDOK
        # The spooler creates an empty file at once and writes the whole PDF some seconds later.
        last = -1
        while True:
            if time.monotonic() > deadline:
                raise FidelityError("the printed PDF was not written in time")
            time.sleep(1)
            size = partial.stat().st_size if partial.exists() else 0
            if size and size == last and _complete_pdf(partial):
                break
            last = size
    finally:
        if proc.poll() is None:
            proc.kill()
    partial.replace(target)
    return target


# ---------------------------------------------------------------- comparison


def _page_text(page) -> dict:
    """One rendered page: flow text outside ruled tables in visual and drawing order, table cell texts, and the
    whole page in visual order.

    Ruled tables are read cell by cell, because a row of multi-line cells reads across cells in visual order.
    Visual text is regrouped from characters, not words: Hancom draws some glyphs (field numbers, bullets) inside
    another run's word box. Page-number lines are dropped."""
    tables = page.find_tables().tables
    boxes = [t.bbox for t in tables]

    def outside(bbox) -> bool:
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        return not any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in boxes)

    every, flow, stream = [], [], []
    drawn: dict[tuple, list] = {}

    def first_draw(box, ch) -> bool:
        """Shadow and outline effects draw a glyph again a fraction of a point away; keep the first."""
        w, h = (box[2] - box[0]) * 0.4, (box[3] - box[1]) * 0.4
        band = round(box[1] / 20)
        if any(abs(box[0] - x) < w and abs(box[1] - y) < h
               for k in (band - 1, band, band + 1) for x, y in drawn.get((ch, k), ())):
            return False
        drawn.setdefault((ch, band), []).append((box[0], box[1]))
        return True

    for b in page.get_text("rawdict")["blocks"]:
        for ln in b.get("lines", ()):
            line = [(c["bbox"], c["c"]) for s in ln["spans"] for c in s["chars"] if first_draw(c["bbox"], c["c"])]
            every += line
            line = [c for c in line if outside(c[0])]
            flow += line
            text = "".join(ch for _, ch in line)
            if not _PAGE_NUMBERS.match(text):
                stream.append(text)
    # ponytail: text the table finder drops from a cell is matched (via "page") but not checked on the rendering
    # side; a stricter cell reader is the upgrade if dropped cells ever hide real losses.
    # Cell text from the deduplicated characters: the table finder's own extract repeats shadowed glyphs.
    cells = []
    for t in tables:
        for row in t.rows:
            for x0, y0, x1, y1 in filter(None, row.cells):
                inside = [c for c in every if x0 <= (c[0][0] + c[0][2]) / 2 <= x1 and y0 <= (c[0][1] + c[0][3]) / 2 <= y1]
                if inside:
                    cells.append(_lines(inside, page_numbers=False))
    height = page.rect.height
    margins = [c for c in every if not CHROME_BAND < (c[0][1] + c[0][3]) / 2 / height < 1 - CHROME_BAND]
    return {"visual": _columns(flow, page.rect.width), "stream": "\n".join(stream), "cells": cells,
            "page": _columns(every, page.rect.width), "margins": _columns(margins, page.rect.width)}


def _columns(chars: list, width: float) -> str:
    split = _gutter(chars, width)
    if split is None:
        return _lines(chars)
    return _lines([c for c in chars if c[0][2] <= split]) + "\n" + _lines([c for c in chars if c[0][2] > split])


def _gutter(chars: list, width: float) -> float | None:
    """x of an empty vertical band near the middle: two pages per sheet, or two text columns."""
    lo, hi = int(width * 0.35), int(width * 0.65)
    used = [False] * (hi - lo)
    for (x0, _, x1, _), ch in chars:
        if ch.strip():
            for x in range(max(lo, int(x0)), min(hi, int(x1) + 1)):
                used[x - lo] = True
    best, run = (0, None), 0
    for i, u in enumerate(used + [True]):
        if not u:
            run += 1
            continue
        if run > best[0]:
            best = (run, lo + i - run / 2)
        run = 0
    return best[1] if best[0] >= GUTTER_MIN else None


def _lines(chars: list, page_numbers: bool = True) -> str:
    """Characters regrouped into visual lines; lines that look like page numbers are dropped unless page_numbers
    is False (inside a table cell, where a lone "1" is a wrapped value)."""
    lines: list[list] = []
    for box, ch in sorted(chars, key=lambda c: ((c[0][1] + c[0][3]) / 2, c[0][0])):
        mid = (box[1] + box[3]) / 2
        if lines and abs(mid - lines[-1][0]) <= 3:
            lines[-1][1].append((box, ch))
        else:
            lines.append([mid, [(box, ch)]])
    out = []
    for _, line in lines:
        line.sort(key=lambda c: c[0][0])
        text = "".join(ch if i == 0 or box[0] - line[i - 1][0][2] < 2 else " " + ch
                       for i, (box, ch) in enumerate(line))
        if not (page_numbers and _PAGE_NUMBERS.match(text)):
            out.append(text)
    return "\n".join(out)


def rendered_pages(pdf: Path) -> list[dict]:
    """Per page, normalized: flow text in two orders, table cells, digit runs of the flow, and image area."""
    import pymupdf

    pages = []
    with pymupdf.open(pdf) as doc:
        for page in doc:
            area = abs(page.rect)
            image_area = sum(abs(pymupdf.Rect(i["bbox"]) & page.rect) for i in page.get_image_info())
            text = _page_text(page)
            # Digits per spaced token, so that neighboring numbers in a borderless table do not fuse.
            digits = [d for tok in text["visual"].split() for d in _DIGITS.findall(re.sub(r"[,.]", "", tok))]
            cell_words = [w for w in ([x for x in map(norm, c.split()) if x] for c in text["cells"]) if w]
            cells = ["".join(w) for w in cell_words]
            pages.append({"visual": norm(text["visual"]), "stream": norm(text["stream"]), "page": norm(text["page"]),
                          "lines": [x for x in map(norm, text["visual"].split("\n")) if x],
                          "stream_lines": [x for x in map(norm, text["stream"].split("\n")) if x],
                          "cells": cells, "cell_words": cell_words, "digits": digits,
                          "margins": [x for x in map(norm, text["margins"].split("\n")) if x],
                          "image_share": round(image_area / area, 3) if area else 0.0})
    return pages


def extraction_units(elements: list[dict]) -> list[dict]:
    """Paragraphs whole; tables per cell line, because a renderer interleaves the lines of neighboring cells."""
    units = []
    for e in elements:
        if e.get("kind") == "image_text":
            continue  # read from pixels by OCR: the text layer is no witness for it
        table = e.get("table")
        if table:
            pieces = [("caption", table.get("caption") or "")]
            pieces += [(f"r{c['row']}c{c['col']}", line) for c in table["cells"] for line in c["text"].split("\n")]
        else:
            # A TOC line's page number after a tab is cached in the HWP and recomputed by the printer. An equation
            # (ingestion.equation_text) is printed in two dimensions: a fraction's parts sit on separate lines, so
            # its pieces are compared one by one; its digits are drawn in a private-use equation font and stay
            # flagged for a person.
            # The HWP loader gives the whole body as one element: each of its lines is a unit aligned on its own
            # pages, or the pages around the longest stretch would be the only ones compared.
            grouped = str((e.get("location") or {}).get("format", "")).endswith("_loader")
            lines = e["raw_text"].split("\n") if grouped else [e["raw_text"]]
            pieces = [(f"l{k}" if grouped else "", p) for k, line in enumerate(lines)
                      for p in _EQUATION_SPLIT.split(_TOC_PAGE.sub("", line))]
        for where, text in pieces:
            n = norm(text)
            if n:
                units.append({"element_id": e["element_id"], "cell": where, "text": text.strip(), "norm": n})
    return units


def _unmatched(s: str, known: set[str], words: list[str] = (), text: str = "") -> tuple[int, list[str]]:
    """Characters of s not covered by any known shingle, and those stretches, longest first. With words (s split
    at spaces), a whole word found in text also counts as covered: the table finder can return an outer cell that
    reads a nested flowchart row line by line across its cells, which no shingle of the cells matches."""
    covered = [False] * len(s)
    for i, g in enumerate(_grams(s)):
        if g in known:
            for k in range(i, i + N):
                covered[k] = True
    at = 0
    for w in words:
        if not all(covered[at:at + len(w)]) and w in text:
            covered[at:at + len(w)] = [True] * len(w)
        at += len(w)
    mask = "".join("1" if c else "0" for c in covered)
    runs = [s[m.start():m.end()] for m in re.finditer("0+", mask)]
    return mask.count("0"), sorted(runs, key=len, reverse=True)


def _wrapped(n: str, x: str, whole: str) -> bool:
    """Whether a stretch at a unit's edge is a wrap fragment: rendered within a line's reach before (start) or
    after (end) the matched rest of the unit. A changed character near the edge leaves a stretch that is not,
    even when the same few characters occur elsewhere on the page ("제3" beside article 3)."""
    if len(x) >= len(n):
        return x in whole
    tries = []
    if n.startswith(x):
        tries.append((n[len(x):len(x) + N], lambda p, a: whole[max(0, p - WRAP_REACH):p]))
    if n.endswith(x):
        tries.append((n[-len(x) - N:-len(x)], lambda p, a: whole[p + len(a):p + len(a) + WRAP_REACH]))
    for anchor, span in tries:
        p = whole.find(anchor)
        while p >= 0:
            if x in span(p, anchor):
                return True
            p = whole.find(anchor, p + 1)
    return False


def _consume(pieces: list[str], window: str) -> tuple[int, list[str], str]:
    """Matches each rendered piece against the extraction window, longest match first from every position, and
    blanks what it used: one extracted copy cannot stand for two rendered copies of repeated text."""
    missing, stretches = 0, []
    for piece in pieces:
        i, run = 0, ""
        while i < len(piece):
            lo, hi = 0, len(piece) - i  # longest k with piece[i:i+k] still unused in the window
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if piece[i:i + mid] in window:
                    lo = mid
                else:
                    hi = mid - 1
            if lo and lo >= min(N, len(piece) - i):
                at = window.find(piece[i:i + lo])
                window = window[:at] + "\0" * lo + window[at + lo:]
                i += lo
                if run:
                    stretches.append(run)
                    run = ""
            else:
                missing += 1
                run += piece[i]
                i += 1
        if run:
            stretches.append(run)
    return missing, stretches, window


def _align(units: list[dict], page_text: list[str]) -> list[list[int]]:
    """Candidate pages of each unit. A unit found on one page anchors it; a repeated one (a label in every
    requirement table, boilerplate) keeps only the pages between the anchors before and after it in document
    order."""
    gram_pages: dict[str, list[int]] = {}
    for no, text in enumerate(page_text, 1):
        for g in set(_grams(text)):
            gram_pages.setdefault(g, []).append(no)
    goods = []
    for u in units:
        n = u["norm"]
        hits = (Counter(no for g in set(_grams(n)) for no in gram_pages.get(g, ())) if len(n) >= N
                else Counter(no for no, t in enumerate(page_text, 1) if n in t))
        top = max(hits.values(), default=0)
        goods.append(sorted(no for no, c in hits.items() if c >= 0.8 * top))
    anchors = [good[0] if len(good) == 1 else None for good in goods]
    before, last = [], 1
    for a in anchors:
        before.append(last)
        last = a or last
    after, nxt = [], len(page_text)
    for a in reversed(anchors):
        after.append(nxt)
        nxt = a or nxt
    after.reverse()
    out = []
    for good, lo, hi in zip(goods, before, after):
        lo, hi = min(lo, hi) - 1, max(lo, hi) + 1
        out.append([no for no in good if lo <= no <= hi] or good)
    return out


def _unmatched_text(s: str, grams: set[str], whole: str) -> tuple[int, list[str]]:
    """Unmatched characters and stretches of `s`; text shorter than a shingle is matched as a whole."""
    if len(s) < N:
        return (0, []) if s in whole else (len(s), [s])
    return _unmatched(s, grams)


def _extraction_findings(units: list[dict], unit_pages: list[list[int]], near, known) -> tuple[list[dict], int, int]:
    """Extraction side: each unit against the rendering around its pages. (findings, characters, unmatched)."""
    findings = []
    unit_chars = unit_unmatched = 0
    for u, nos in zip(units, unit_pages):
        n = u["norm"]
        unit_chars += len(n)
        grams, whole = known("r", near(nos))
        missing, stretches = _unmatched_text(n, grams, whole)
        digits = [d for d in _DIGITS.findall(n) if d not in whole]
        unit_unmatched += missing
        if digits or missing:  # one wrong character leaves exactly one character unmatched
            findings.append({"side": "extraction", "element_id": u["element_id"], "cell": u["cell"],
                             "page": nos[0] if nos else None, "unmatched_chars": missing, "digits": digits,
                             "stretch": stretches[0] if stretches else "", "text": u["text"][:300],
                             "edge": all(_wrapped(n, x, whole) for x in stretches)})
    return findings, unit_chars, unit_unmatched


def _rendering_findings(pages: list[dict], units: list[dict], unit_near: list[set[int]],
                        known) -> tuple[list[dict], int, int]:
    """Rendering side. Flow text: pages in order consume one extraction string, each within the span of its nearby
    units, so a dropped copy of repeated text is still missing. Table cells: shingles only, because the table
    finder can return an outer cell that repeats its nested table's cells. (findings, characters, unmatched)."""
    starts, pos = [], 0
    for u in units:
        starts.append(pos)
        pos += len(u["norm"])
    pool = "".join(u["norm"] for u in units)
    chrome = {line for line, k in Counter(x for p in pages for x in set(p["margins"])).items() if k >= CHROME_PAGES}
    findings = []
    page_chars = page_unmatched = 0
    for no, p in enumerate(pages, 1):
        idx = [i for i, around in enumerate(unit_near) if no in around]
        lo, hi = (starts[idx[0]], starts[idx[-1]] + len(units[idx[-1]]["norm"])) if idx else (0, 0)
        tries = [_consume([x for x in lines if x not in chrome], pool[lo:hi])
                 for lines in (p["lines"], p["stream_lines"])]
        missing, found, window = min(tries, key=lambda t: t[0])
        pool = pool[:lo] + window + pool[hi:]
        grams, extracted = known("e", (no,))
        for c, words in zip(p["cells"], p["cell_words"]):
            if c not in extracted:
                m, ss = _unmatched(c, grams, words, extracted)
                missing += m
                found += ss
        page_chars += len(p["visual"]) + sum(map(len, p["cells"]))
        page_unmatched += missing
        stretches = sorted((s for s in found if len(s) >= MIN_STRETCH), key=len, reverse=True)
        digits = sorted({d for d in p["digits"] if d not in extracted
                         and not any(d in x for x in p["margins"] if x in chrome)})
        if digits or stretches:
            findings.append({"side": "rendering", "page": no, "unmatched_chars": missing, "digits": digits,
                             "stretch": stretches[0] if stretches else "", "stretches": stretches[:20]})
    return findings, page_chars, page_unmatched


def compare(elements: list[dict], pages: list[dict]) -> dict:
    """Both directions are checked against the neighborhood (page ±1) only, so that a changed or missing copy of
    text repeated elsewhere in the document is still reported."""
    units = extraction_units(elements)
    page_text = ["\n".join([p["page"], p["visual"], p["stream"], *p["cells"]]) for p in pages]
    unit_pages = _align(units, page_text)
    count = len(pages)

    def near(nos: list[int]) -> tuple[int, ...]:
        return tuple(sorted({k for no in nos for k in range(max(1, no - 1), min(count, no + 1) + 1)}))

    rendered_all = "\n".join(page_text)
    unit_near = [set(near(nos)) for nos in unit_pages]
    cache: dict[tuple, tuple[set[str], str]] = {}

    def known(side: str, nos: tuple[int, ...]) -> tuple[set[str], str]:
        """Shingles and text of the rendering ("r") or of the extraction ("e") around the given pages."""
        if (side, nos) not in cache:
            if side == "r":
                text = "\n".join(page_text[k - 1] for k in nos) if nos else rendered_all
            else:
                text = "".join(u["norm"] for u, around in zip(units, unit_near) if around & set(nos))
            cache[side, nos] = ({g for piece in text.split("\n") for g in _grams(piece)}, text)
        return cache[side, nos]

    findings, unit_chars, unit_unmatched = _extraction_findings(units, unit_pages, near, known)
    rendering, page_chars, page_unmatched = _rendering_findings(pages, units, unit_near, known)
    findings += rendering
    image_pages = [no for no, p in enumerate(pages, 1) if p["image_share"] >= IMAGE_MIN_AREA]
    metrics = {"units": len(units), "extracted_chars": unit_chars, "rendered_chars": page_chars,
               "pages": len(pages), "extraction_unmatched": unit_unmatched, "rendering_unmatched": page_unmatched,
               "extraction_unmatched_share": round(unit_unmatched / max(unit_chars, 1), 5),
               "rendering_unmatched_share": round(page_unmatched / max(page_chars, 1), 5),
               "digit_findings": sum(bool(f["digits"]) for f in findings), "image_pages": image_pages}
    passed = (not metrics["digit_findings"]
              and metrics["extraction_unmatched_share"] <= MAX_EXTRACTION_UNMATCHED_SHARE
              and all(f["side"] == "extraction" and f["edge"] for f in findings))
    return {"verdict": "auto_verified" if passed else "auto_flagged", "metrics": metrics, "findings": findings}


# ---------------------------------------------------------------- recording


def verify_source(settings: Settings, source_hash: str, reprint: bool = False) -> dict:
    """Print (if needed), compare and record the verdict for the active extraction. Human review statuses win."""
    with open_db(settings.db_path) as conn:
        src = conn.execute("SELECT * FROM sources WHERE source_hash = ?", (source_hash,)).fetchone()
    if src is None or src["parse_status"] != "parsed":
        raise FidelityError("source is not parsed")
    if src["format"] != "hwp" or RECOVERED in src["warnings_json"]:
        raise FidelityError("the extraction reads this very text layer; an independent check needs OCR")
    pdf = printed_pdf_path(settings, source_hash)
    if reprint or not pdf.exists():
        print_hwp(settings, source_hash)
    result = compare(load_elements(settings, src["active_extraction_id"]), rendered_pages(pdf))
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute(
            "INSERT INTO fidelity_checks(extraction_id, method, source_hash, verdict, metrics_json, findings_json, "
            "rendering_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(extraction_id, method) DO UPDATE "
            "SET verdict = excluded.verdict, metrics_json = excluded.metrics_json, findings_json = excluded.findings_json, "
            "rendering_sha256 = excluded.rendering_sha256, created_at = excluded.created_at",
            (src["active_extraction_id"], FIDELITY_VERSION, source_hash, result["verdict"], dumps(result["metrics"]),
             dumps(result["findings"]), sha256_file(pdf), utcnow()),
        )
        conn.execute(
            f"UPDATE sources SET review_status = ? WHERE source_hash = ? AND active_extraction_id = ? "
            f"AND review_status NOT IN ({','.join('?' * len(HUMAN_STATUSES))})",
            (result["verdict"], source_hash, src["active_extraction_id"], *HUMAN_STATUSES),
        )
    return {"source_hash": source_hash, "verdict": result["verdict"], **result["metrics"],
            "findings": len(result["findings"])}


def verify_all(settings: Settings, reprint: bool = False) -> list[dict]:
    with open_db(settings.db_path) as conn:
        hashes = [r[0] for r in conn.execute(
            "SELECT source_hash FROM sources WHERE format = 'hwp' AND parse_status = 'parsed' "
            "AND warnings_json NOT LIKE ? ORDER BY source_hash", (f"%{RECOVERED}%",))]
    results = []
    for h in hashes:
        try:
            results.append(verify_source(settings, h, reprint))
        except IngestionError as exc:
            results.append({"source_hash": h, "verdict": "error", "reason": str(exc)})
        print(dumps(results[-1]), flush=True)
    return results


def latest(settings: Settings, source_hash: str) -> dict | None:
    with open_db(settings.db_path) as conn:
        row = conn.execute(
            "SELECT f.* FROM fidelity_checks f JOIN sources s ON s.active_extraction_id = f.extraction_id "
            "WHERE s.source_hash = ? AND f.method = ?", (source_hash, FIDELITY_VERSION)).fetchone()
    return dict(row) if row else None
