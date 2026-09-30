"""CSV manifest, converter invocation, HWP/PDF element extraction and review records."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import tempfile
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from importlib import metadata
from pathlib import Path
from zoneinfo import ZoneInfo

from .settings import Settings
from .store import dumps, open_db, read_jsonl, tx, utcnow, write_jsonl_atomic, write_text_atomic

# Committed namespace for doc_id = uuid5(NAMESPACE, NFC(csv filename)). Never change it.
DOC_NAMESPACE = uuid.UUID("5b0f4c1e-8a57-4f0e-9d3c-2a6e1f7b9c42")
SEOUL = ZoneInfo("Asia/Seoul")
HWP_WALKER_VERSION = "hwp-xml-walker-3"
PDF_WALKER_VERSION = "pdf-blocks-tables-2"
STDERR_LIMIT = 4000

CSV_COLUMNS = {
    "공고 번호": "notice",
    "공고 차수": "revision",
    "사업명": "title",
    "사업 금액": "amount",
    "발주 기관": "institution",
    "공개 일자": "published_at",
    "입찰 참여 시작일": "bid_start",
    "입찰 참여 마감일": "bid_close",
    "사업 요약": "summary",
    "파일형식": "format",
    "파일명": "filename",
    "텍스트": "text",
}
AUDIT_EXPECTED = {"associations": 100, "hwp": 96, "pdf": 4}

# Requirement codes in this corpus end in R (SFR, ECR, PER, COR, DAR, ...). Keep the source spelling;
# the look-arounds stop SFR-001 from matching inside SFR-0010.
CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,5}R-\d{1,4})(?![0-9])")

# Stable reason codes -> safe user-facing Korean text. Forensic detail stays in warnings_json.
QUARANTINE_TEXT = {
    "hwp_xml_malformed": "HWP 구조 변환 결과가 올바른 XML이 아니어서 원문 전체를 확인할 수 없습니다.",
    "hwp_converter_decode_error": "HWP 변환기가 문서 내부 문자열을 해석하지 못했습니다.",
    "hwp_converter_failed": "HWP 변환기가 실패했습니다.",
    "hwp_converter_timeout": "HWP 변환 시간이 초과되었습니다.",
    "hwp_converter_missing": "HWP 변환기가 설정되지 않았습니다.",
    "hwp_empty_output": "HWP 변환 결과에 본문이 없습니다.",
    "pdf_open_failed": "PDF 파일을 열 수 없습니다.",
    "pdf_no_text": "PDF에서 텍스트를 추출하지 못했습니다.",
}

HEADING_PATTERNS = [
    (1, re.compile(r"^\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\s*[.．]?\s*\S")),
    (1, re.compile(r"^\s*제\s*\d+\s*[장편]\s")),
    (2, re.compile(r"^\s*\d{1,2}\s*[.．]\s*[^\d\s]")),
    (3, re.compile(r"^\s*[가나다라마바사아자차카타파하]\s*[.．)]\s*\S")),
]
TOC_RE = re.compile(r"^.{2,80}?(?:\s{3,}|\t|\.{3,}|·{3,}|…{2,}|\s-\s).*?\d{1,3}\s*$")
FOOTER_PAGE_RE = re.compile(r"^[-–—\s]*(\d{1,4})[-–—\s]*$")


class IngestionError(RuntimeError):
    pass


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def search_text(raw: str) -> str:
    return re.sub(r"\s+", " ", nfc(raw)).strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def doc_id_for(filename: str) -> str:
    return str(uuid.uuid5(DOC_NAMESPACE, nfc(filename)))


# ---------------------------------------------------------------- metadata normalization


def _parse_when(raw: str, flags: list[str], name: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt, precision in (("%Y-%m-%d %H:%M:%S", "timestamp"), ("%Y-%m-%d %H:%M", "timestamp"), ("%Y-%m-%d", "date")):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if precision == "date":
            return {"value": parsed.date().isoformat(), "precision": "date"}
        return {"value": parsed.replace(tzinfo=SEOUL).isoformat(), "precision": "timestamp"}
    flags.append(f"{name}_unparsed")
    return None


def _parse_integral(raw: str, flags: list[str], name: str) -> int | None:
    raw = raw.strip().replace(",", "")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        flags.append(f"{name}_unparsed")
        return None
    if value != value.to_integral_value():
        flags.append(f"{name}_fractional")
        return None
    return int(value)


def normalize_row(row: dict[str, str]) -> tuple[dict, list[str]]:
    flags: list[str] = []
    amount = _parse_integral(row["amount"], flags, "amount")
    if amount == 0:
        flags.append("amount_zero_review")
    if amount is not None and amount < 0:
        flags.append("amount_negative")
        amount = None
    if not row["amount"].strip():
        flags.append("amount_missing")
    normalized = {
        "notice": row["notice"].strip() or None,
        "revision": _parse_integral(row["revision"], flags, "revision"),
        "title": nfc(row["title"].strip()),
        "amount_krw": amount,
        "institution": nfc(row["institution"].strip()) or None,
        "published_at": _parse_when(row["published_at"], flags, "published_at"),
        "bid_start": _parse_when(row["bid_start"], flags, "bid_start"),
        "bid_close": _parse_when(row["bid_close"], flags, "bid_close"),
        "format": row["format"].strip().lower(),
    }
    for key in ("notice", "bid_start", "bid_close"):
        if normalized[key] is None and f"{key}_unparsed" not in flags:
            flags.append(f"{key}_missing")
    return normalized, flags


def read_manifest_csv(settings: Settings) -> list[dict]:
    """Read every CSV association and validate its original path without touching the database."""
    with open(settings.csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if set(reader.fieldnames or []) != set(CSV_COLUMNS):
            raise IngestionError(f"unexpected CSV columns: {reader.fieldnames}")
        rows = [{CSV_COLUMNS[k]: (v or "") for k, v in raw.items()} for raw in reader]
    files_dir = settings.files_dir.resolve()
    on_disk = {nfc(p.name): p for p in files_dir.iterdir() if p.is_file()}
    seen: set[str] = set()
    out = []
    for i, row in enumerate(rows, start=1):
        name = nfc(row["filename"].strip())
        if not name or "/" in name or "\\" in name or name in (".", "..") or Path(name).name != name:
            raise IngestionError(f"CSV row {i}: unsafe filename {name!r}")
        suffix = Path(name).suffix.lower().lstrip(".")
        if suffix not in ("hwp", "pdf") or suffix != row["format"].strip().lower():
            raise IngestionError(f"CSV row {i}: unexpected extension/format for {name!r}")
        if name in seen:
            raise IngestionError(f"CSV row {i}: duplicate association {name!r}")
        seen.add(name)
        path = on_disk.get(name)
        if path is None or path.resolve().parent != files_dir:
            raise IngestionError(f"CSV row {i}: original file not found beneath the source directory: {name!r}")
        normalized, flags = normalize_row(row)
        raw = {k: v for k, v in row.items() if k != "text"}
        raw["csv_text_chars"] = len(row["text"])
        out.append(
            {
                "csv_row_id": i,
                "filename": name,
                "path": path.resolve(),
                "format": suffix,
                "doc_id": doc_id_for(name),
                "raw": raw,
                "normalized": normalized,
                "flags": flags,
            }
        )
    return out


CONFLICT_FIELDS = ("institution", "title", "notice", "bid_close", "amount_krw")


def import_manifest(settings: Settings) -> dict:
    rows = read_manifest_csv(settings)
    for r in rows:
        r["source_hash"] = sha256_file(r["path"])
    by_hash: dict[str, list[dict]] = {}
    for r in rows:
        by_hash.setdefault(r["source_hash"], []).append(r)
    duplicates = []
    for h, group in by_hash.items():
        if len(group) < 2:
            continue
        conflicts = []
        for field in CONFLICT_FIELDS:
            values = {g["doc_id"]: g["normalized"][field] for g in group}
            if len({dumps(v) for v in values.values()}) > 1:
                conflicts.append({"field": field, "values": values})
        for g in group:
            g["shared_with"] = [o["doc_id"] for o in group if o is not g]
            g["conflicts"] = conflicts
        duplicates.append({"source_hash": h, "doc_ids": [g["doc_id"] for g in group], "conflicts": conflicts})

    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        for r in rows:
            conn.execute(
                "INSERT INTO sources(source_hash, format, original_path) VALUES (?, ?, ?) "
                "ON CONFLICT(source_hash) DO UPDATE SET original_path = excluded.original_path",
                (r["source_hash"], r["format"], str(r["path"])),
            )
            quality = {
                "flags": r["flags"],
                "shared_source_with": r.get("shared_with", []),
                "provenance_conflicts": r.get("conflicts", []),
            }
            existing = conn.execute("SELECT filename FROM documents WHERE doc_id = ?", (r["doc_id"],)).fetchone()
            if existing and existing["filename"] != r["filename"]:
                raise IngestionError(f"doc_id collision for {r['filename']!r}; identity migration required")
            conn.execute(
                "INSERT INTO documents(doc_id, csv_row_id, filename, active_source_hash, raw_metadata_json, "
                "normalized_metadata_json, quality_json) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET csv_row_id = excluded.csv_row_id, "
                "active_source_hash = excluded.active_source_hash, raw_metadata_json = excluded.raw_metadata_json, "
                "normalized_metadata_json = excluded.normalized_metadata_json, quality_json = excluded.quality_json",
                (r["doc_id"], r["csv_row_id"], r["filename"], r["source_hash"], dumps(r["raw"]),
                 dumps(r["normalized"]), dumps(quality)),
            )
    return manifest_report(settings, duplicates)


def manifest_report(settings: Settings, duplicates: list | None = None) -> dict:
    with open_db(settings.db_path) as conn:
        docs = conn.execute(
            "SELECT d.doc_id, d.filename, s.format, s.parse_status, s.review_status, s.reason_code "
            "FROM documents d JOIN sources s ON s.source_hash = d.active_source_hash ORDER BY d.csv_row_id"
        ).fetchall()
    counts = {"associations": len(docs), "hwp": 0, "pdf": 0, "unique_sources": 0}
    parse: dict[str, int] = {}
    review: dict[str, int] = {}
    for d in docs:
        counts[d["format"]] += 1
        parse[d["parse_status"]] = parse.get(d["parse_status"], 0) + 1
        review[d["review_status"]] = review.get(d["review_status"], 0) + 1
    with open_db(settings.db_path) as conn:
        counts["unique_sources"] = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    differences = {k: (counts[k], v) for k, v in AUDIT_EXPECTED.items() if counts[k] != v}
    return {
        "counts": counts,
        "parse_status": parse,
        "review_status": review,
        "duplicates": duplicates,
        "audit_differences": differences,
        "quarantined": [dict(d) for d in docs if d["parse_status"] == "quarantined"],
    }


# ---------------------------------------------------------------- shared element helpers


def heading_level(text: str) -> int | None:
    stripped = text.strip()
    # "1. 명  칭 : ○○○" is a form field, not a section
    if not stripped or len(stripped) > 60 or TOC_RE.match(stripped) or re.search(r"\s[:：]\s", stripped):
        return None
    for level, pattern in HEADING_PATTERNS:
        if pattern.match(stripped):
            # numbered list items look like "1." / "가." headings; long ones are sentences
            return level if level == 1 or len(stripped) <= 40 else None
    return None


class _SectionTracker:
    def __init__(self) -> None:
        self.stack: list[tuple[int, str]] = []

    def update(self, text: str) -> bool:
        level = heading_level(text)
        if level is None:
            return False
        self.stack = [(lv, t) for lv, t in self.stack if lv < level] + [(level, search_text(text))]
        return True

    @property
    def path(self) -> list[str]:
        return [t for _, t in self.stack]


def render_table(cells: list[dict]) -> str:
    rows: dict[int, list[dict]] = {}
    for c in cells:
        rows.setdefault(c["row"], []).append(c)
    lines = []
    for r in sorted(rows):
        texts = [c["text"].strip() for c in sorted(rows[r], key=lambda c: c["col"])]
        lines.append(" | ".join(t for t in texts if t))
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------- HWP


# Hancom equation script commands, written as the symbols the printed formula shows; braces only group.
EQ_WORDS = {"TIMES": "×", "times": "×", "over": "/", "OVER": "/", "DIVIDE": "÷", "div": "÷", "cdot": "·",
            "CDOT": "·", "pm": "±", "PM": "±", "le": "≤", "leq": "≤", "LEQ": "≤", "ge": "≥", "geq": "≥", "GEQ": "≥",
            "ne": "≠", "NEQ": "≠", "sqrt": "√", "SQRT": "√", "sum": "Σ", "SUM": "Σ", "LEFT": "", "left": "",
            "RIGHT": "", "right": "", "rm": "", "it": "", "bold": "", "BULLET": "•", "CDOTS": "⋯", "cdots": "⋯"}


def equation_text(script: str) -> str:
    text = re.sub(r"[A-Za-z]+", lambda m: EQ_WORDS.get(m.group(), m.group()), script)
    text = re.sub(r"[{}]", "", text.replace("`", " ").replace("~", " ").replace("#", "\n"))
    return re.sub(r"[ \t]+", " ", text).strip()


def equation_scripts(original: Path) -> list[str]:
    """Equation scripts in document order. pyhwp's XML keeps an empty <EqEdit/> per equation; the script is in
    the record payload: UINT32 attributes, WORD length, WCHAR[length]."""
    from hwp5.xmlmodel import Hwp5File

    f = Hwp5File(str(original))
    out = []
    for si in f.bodytext.section_indexes():
        for m in f.bodytext.section(si).models():
            if m["type"].__name__ == "EqEdit":
                p = m["payload"]
                n = int.from_bytes(p[4:6], "little") if len(p) >= 6 else 0
                out.append(p[6:6 + 2 * n].decode("utf-16-le", errors="replace"))
    return out


def _collect(node: ET.Element, texts: list[str], tables: list, boxes: list) -> None:
    for ch in node:
        tag = ch.tag
        if tag == "Text":
            texts.append(ch.text or "")
        elif tag == "EqEdit":
            texts.append(f" {ch.text} " if ch.text else "")
        elif tag == "ControlChar":
            name = ch.get("name")
            if name == "LINE_BREAK":
                texts.append("\n")
            elif name == "TAB":
                texts.append("\t")
            elif name in ("FIXWIDTH_SPACE", "NONBREAK_SPACE"):
                texts.append(" ")
        elif tag == "TableControl":
            tables.append(ch)
        elif tag == "GShapeObjectControl":  # text boxes and picture captions ("그림. ...")
            boxes.extend(x for x in ch.iter() if x.tag in ("TextboxParagraphList", "GShapeObjectCaption"))
        elif tag in ("Header", "Footer", "HeaderParagraphList", "FooterParagraphList"):
            continue  # page chrome repeats on every page
        else:
            _collect(ch, texts, tables, boxes)


def _paragraphs(container: ET.Element):
    for ch in container:
        if ch.tag == "Paragraph":
            yield ch
        elif ch.tag == "ColumnSet":
            yield from _paragraphs(ch)


def walk_hwp(root: ET.Element) -> list[dict]:
    """Walk HWP XML in source order. Every text span is owned by exactly one element:
    body paragraph, table cell (nested tables excluded) or nested table."""
    body = root.find("BodyText")
    if body is None:
        raise IngestionError("hwp_empty_output")
    out: list[dict] = []
    tracker = _SectionTracker()
    table_counter = [0]

    def emit_paragraph(text: str, path: str, section: int, parent: str | None, kind: str = "paragraph") -> None:
        if not text.strip():
            return
        if kind == "paragraph":
            if tracker.update(text):
                kind = "heading"
            elif TOC_RE.match(text.strip()):
                kind = "toc"
        out.append({"path": path, "kind": kind, "parent": parent, "raw_text": text,
                    "location": {"format": "hwp", "section": section, "path": path, "section_path": tracker.path}})

    def emit_table(tc: ET.Element, path: str, section: int, parent: str | None, cell_ref: list | None) -> None:
        table_counter[0] += 1
        ordinal = table_counter[0]
        tbody = tc.find("TableBody")
        caption = []
        cap = tc.find("TableCaption")
        if cap is not None:
            for p in _paragraphs(cap):
                t: list[str] = []
                _collect(p, t, [], [])
                caption.append("".join(t))
        cells, nested = [], []
        for tr in (tbody.findall("TableRow") if tbody is not None else []):
            for cell in tr.findall("TableCell"):
                r, c = int(cell.get("row", 0)), int(cell.get("col", 0))
                parts = []
                for pi, p in enumerate(_paragraphs(cell)):
                    texts, tables, boxes = [], [], []
                    _collect(p, texts, tables, boxes)
                    parts.append("".join(texts))
                    for box in boxes:
                        for bp in _paragraphs(box):
                            bt: list[str] = []
                            _collect(bp, bt, [], [])
                            parts.append("".join(bt))
                    for ti, t in enumerate(tables):
                        nested.append((t, f"{path}/r{r}c{c}/p{pi}/t{ti}", [r, c]))
                cells.append({"row": r, "col": c, "rowspan": int(cell.get("rowspan", 1)),
                              "colspan": int(cell.get("colspan", 1)), "text": "\n".join(parts).strip()})
        rows = int(tbody.get("rows", 0)) if tbody is not None else 0
        cols = int(tbody.get("cols", 0)) if tbody is not None else 0
        # One-row title boxes ("Ⅰ | | 사업 안내", "1 | | 사업개요 |") are headings laid out as tables.
        kind = "table"
        texts = [c["text"].strip() for c in sorted(cells, key=lambda c: c["col"]) if c["text"].strip()]
        if rows == 1 and 1 <= len(texts) <= 2 and cell_ref is None:
            joined = f"{texts[0]}. {texts[1]}" if len(texts) == 2 and 0 < len(texts[0]) <= 4 else texts[0]
            if tracker.update(joined):
                kind = "heading"
        location = {"format": "hwp", "section": section, "path": path, "section_path": tracker.path,
                    "table_ordinal": ordinal}
        if cell_ref is not None:
            location["parent_cell"] = cell_ref
        out.append({"path": path, "kind": kind, "parent": parent, "raw_text": render_table(cells),
                    "location": location,
                    "table": {"rows": rows, "cols": cols, "caption": "\n".join(caption).strip(), "cells": cells}})
        for t, npath, ref in nested:
            emit_table(t, npath, section, path, ref)

    for si, sec in enumerate(body.findall("SectionDef")):
        for pi, p in enumerate(_paragraphs(sec)):
            texts, tables, boxes = [], [], []
            _collect(p, texts, tables, boxes)
            path = f"s{si}/p{pi}"
            emit_paragraph("".join(texts), path, si, None)
            for bi, box in enumerate(boxes):
                for bpi, bp in enumerate(_paragraphs(box)):
                    bt: list[str] = []
                    _collect(bp, bt, [], [])
                    emit_paragraph("".join(bt), f"{path}/box{bi}/p{bpi}", si, None)
            for ti, t in enumerate(tables):
                emit_table(t, f"{path}/t{ti}", si, None, None)
    return out


def run_hwp_converter(settings: Settings, original: Path, out_xml: Path) -> tuple[str | None, str]:
    """Returns (reason_code or None, bounded stderr). Output is validated independently of the return code."""
    conv = settings.hwp_converter
    if conv is None or not Path(conv).is_file():
        return "hwp_converter_missing", ""
    try:
        proc = subprocess.run(
            [str(conv), "xml", "--output", str(out_xml), str(original)],
            shell=False, capture_output=True, timeout=settings.converter_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return "hwp_converter_timeout", ""
    stderr = proc.stderr.decode("utf-8", errors="replace")
    # xmllint is an optional pretty-printer the converter tries; its absence is not a conversion failure.
    stderr = "\n".join(line for line in stderr.splitlines() if "xmllint" not in line)[-STDERR_LIMIT:]
    if "UnicodeDecodeError" in stderr:
        return "hwp_converter_decode_error", stderr
    if proc.returncode != 0:
        return "hwp_converter_failed", stderr
    return None, stderr


def parse_hwp(settings: Settings, original: Path) -> tuple[list[dict], list[dict], str | None]:
    """Returns (elements, warnings, reason_code)."""
    with tempfile.TemporaryDirectory(dir=settings.data_dir) as tmp:
        out_xml = Path(tmp) / "out.xml"
        reason, stderr = run_hwp_converter(settings, original, out_xml)
        warnings = [{"code": "converter_stderr", "detail": stderr}] if stderr.strip() else []
        if reason:
            return [], warnings, reason
        if not out_xml.exists() or out_xml.stat().st_size == 0:
            return [], warnings, "hwp_empty_output"
        try:
            root = ET.parse(out_xml).getroot()
        except ET.ParseError as exc:
            return [], warnings + [{"code": "xml_parse_error", "detail": str(exc)}], "hwp_xml_malformed"
    slots = list(root.iter("EqEdit"))
    if slots:
        scripts = equation_scripts(original)
        if len(scripts) == len(slots):
            for slot, script in zip(slots, scripts):
                slot.text = equation_text(script)
        else:
            warnings.append({"code": "equation_count_mismatch", "detail": f"{len(slots)} in XML, {len(scripts)} records"})
    elements = walk_hwp(root)
    if not any(e["raw_text"].strip() for e in elements):
        return [], warnings, "hwp_empty_output"
    return elements, warnings, None


# ---------------------------------------------------------------- PDF


def parse_pdf(original: Path) -> tuple[list[dict], list[dict], str | None]:
    import pymupdf

    try:
        doc = pymupdf.open(original)
    except Exception as exc:  # noqa: BLE001 - reported as a quarantine reason
        return [], [{"code": "pdf_open_error", "detail": str(exc)[:STDERR_LIMIT]}], "pdf_open_failed"
    pymupdf.TOOLS.mupdf_warnings()  # clear
    out: list[dict] = []
    warnings: list[dict] = []
    tracker = _SectionTracker()
    with doc:
        for index, page in enumerate(doc):
            pno = index + 1
            height, width = page.rect.height, page.rect.width
            # Landscape sheets in this corpus carry two printed pages side by side ("- 78 -" | "- 79 -").
            two_up = width > height

            def column(x0: float) -> int:
                return int(x0 >= width / 2) if two_up else 0

            blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
            labels: dict[int, str] = {}
            if page.get_label():
                labels = {0: page.get_label(), 1: page.get_label()}
            else:  # printed page numbers sit in the footer, e.g. "-201-"
                for b in blocks:
                    m = FOOTER_PAGE_RE.match(b[4].strip())
                    if m and b[1] > height * 0.85:
                        labels[column(b[0])] = m.group(1)
            if sum(len(b[4].strip()) for b in blocks) < 10:
                warnings.append({"code": "page_blank_or_image", "page": pno})
            tables = page.find_tables().tables
            items = []
            for t in tables:
                grid = t.extract()
                cells = [{"row": r, "col": c, "rowspan": 1, "colspan": 1, "text": v or "", "merged": v is None}
                         for r, row in enumerate(grid) for c, v in enumerate(row)]
                items.append((column(t.bbox[0]), t.bbox[1], t.bbox[0], "table", t, cells))
            tboxes = [pymupdf.Rect(t.bbox) for t in tables]
            for b in blocks:
                rect = pymupdf.Rect(b[:4])
                center = pymupdf.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
                if any(center in tb for tb in tboxes):
                    continue  # owned by the table
                items.append((column(b[0]), b[1], b[0], "block", b, None))
            items.sort(key=lambda it: it[:3])
            for n, (col, _, _, kind, obj, cells) in enumerate(items):
                path = f"p{pno}/i{n}"
                label = labels.get(col) or (next(iter(labels.values())) if len(labels) == 1 else None)
                loc = {"format": "pdf", "page": pno, "page_label": label, "path": path}
                if kind == "table":
                    loc.update(bbox=[round(v, 1) for v in obj.bbox], section_path=tracker.path)
                    out.append({"path": path, "kind": "table", "parent": None, "raw_text": render_table(cells),
                                "location": loc,
                                "table": {"rows": obj.row_count, "cols": obj.col_count, "caption": "",
                                          "cells": cells, "merged_spans": "unavailable_from_pdf"}})
                else:
                    text = obj[4].rstrip("\n")
                    if FOOTER_PAGE_RE.match(text.strip()) and obj[1] > height * 0.85:
                        continue  # printed page number, kept as page_label
                    ekind = "heading" if tracker.update(text) else ("toc" if TOC_RE.match(text.strip()) else "paragraph")
                    loc.update(bbox=[round(v, 1) for v in obj[:4]], section_path=tracker.path)
                    out.append({"path": path, "kind": ekind, "parent": None, "raw_text": text, "location": loc})
    mupdf_warnings = pymupdf.TOOLS.mupdf_warnings()
    if mupdf_warnings:
        warnings.append({"code": "mupdf_warnings", "detail": mupdf_warnings[:STDERR_LIMIT]})
    if not out:
        return [], warnings, "pdf_no_text"
    return out, warnings, None


# ---------------------------------------------------------------- extraction records


def parser_fingerprint(fmt: str) -> str:
    if fmt == "hwp":
        info = {"walker": HWP_WALKER_VERSION, "pyhwp": metadata.version("pyhwp")}
    else:
        info = {"walker": PDF_WALKER_VERSION, "pymupdf": metadata.version("pymupdf")}
    return hashlib.sha256(json.dumps(info, sort_keys=True).encode()).hexdigest()[:16]


def finalize_elements(raw_elements: list[dict], extraction_id: str) -> list[dict]:
    ids = {e["path"]: hashlib.sha256(f"{extraction_id}:{e['path']}".encode()).hexdigest()[:20] for e in raw_elements}
    out = []
    for order, e in enumerate(raw_elements):
        replacement = e["raw_text"].count("�")
        out.append({
            "element_id": ids[e["path"]],
            "source_order": order,
            "kind": e["kind"],
            "parent_id": ids.get(e["parent"]) if e["parent"] else None,
            "raw_text": e["raw_text"],
            "search_text": search_text(e["raw_text"]),
            "location": e["location"],
            "table": e.get("table"),
            "replacement_chars": replacement,
        })
    return out


def ingest_source(settings: Settings, source_hash: str) -> dict:
    with open_db(settings.db_path) as conn:
        src = conn.execute("SELECT * FROM sources WHERE source_hash = ?", (source_hash,)).fetchone()
    if src is None:
        raise IngestionError(f"unknown source {source_hash}")
    original = Path(src["original_path"])
    if sha256_file(original) != source_hash:
        raise IngestionError(f"original bytes changed for {original.name}; rerun manifest")
    fp = parser_fingerprint(src["format"])
    if src["format"] == "hwp":
        raw, warnings, reason = parse_hwp(settings, original)
        printed = printed_pdf_path(settings, source_hash)
        if reason and printed.exists():
            # pyhwp cannot read it, but Hancom could print it (review print): read the print's text layer instead.
            # No independent witness remains for this text, so it stays unreviewed.
            raw, more, failed = parse_pdf(printed)
            if not failed:
                for e in raw:
                    e["location"]["format"] = "hwp_print"
                warnings += more + [{"code": "recovered_from_native_print", "detail": reason,
                                     "rendering_sha256": sha256_file(printed)}]
                reason, fp = None, parser_fingerprint("pdf") + "-hancom-print"
    else:
        raw, warnings, reason = parse_pdf(original)
    rendering = printed_pdf_path(settings, source_hash) if src["format"] == "hwp" else original
    if not reason and rendering.exists():
        from .ocr import merge  # ocr imports this module

        raw, more, suffix = merge(settings, source_hash, raw, rendering)
        warnings += more
        if suffix:
            fp = f"{fp}-{suffix}"
    if reason:
        with open_db(settings.db_path) as conn, tx(conn, immediate=True):
            conn.execute(
                "UPDATE sources SET parse_status = 'quarantined', review_status = 'needs_recovery', reason_code = ?, "
                "warnings_json = ? WHERE source_hash = ?",
                (reason, dumps(warnings), source_hash),
            )
        return {"source_hash": source_hash, "status": "quarantined", "reason": reason}
    extraction_id = hashlib.sha256(f"{source_hash}:{fp}".encode()).hexdigest()[:24]
    elements = finalize_elements(raw, extraction_id)
    if any(e["replacement_chars"] for e in elements):
        warnings.append({"code": "replacement_characters",
                         "count": sum(e["replacement_chars"] for e in elements)})
    artifact = settings.data_dir / "extracted" / source_hash / fp / "elements.jsonl"
    write_jsonl_atomic(artifact, elements)
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute(
            "INSERT OR IGNORE INTO extractions(extraction_id, source_hash, parser_fingerprint, artifact_path, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (extraction_id, source_hash, fp, str(artifact), utcnow()),
        )
        conn.execute("DELETE FROM elements WHERE extraction_id = ?", (extraction_id,))
        conn.executemany(
            "INSERT INTO elements(extraction_id, element_id, source_order, kind, parent_id, raw_text, search_text, "
            "location_json, table_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(extraction_id, e["element_id"], e["source_order"], e["kind"], e["parent_id"], e["raw_text"],
              e["search_text"], dumps(e["location"]), dumps(e["table"]) if e["table"] else None) for e in elements],
        )
        # A new parser revision resets review: parsed does not mean faithful.
        review = src["review_status"] if src["active_extraction_id"] == extraction_id else "unreviewed"
        conn.execute(
            "UPDATE sources SET parse_status = 'parsed', active_extraction_id = ?, reason_code = NULL, "
            "review_status = ?, warnings_json = ? WHERE source_hash = ?",
            (extraction_id, review, dumps(warnings), source_hash),
        )
    return {"source_hash": source_hash, "status": "parsed", "extraction_id": extraction_id,
            "elements": len(elements), "tables": sum(e["kind"] == "table" for e in elements),
            "warnings": [w["code"] for w in warnings]}


def select_documents(settings: Settings, doc_ids: list[str] | None = None) -> list[dict]:
    """Every manifest document, or exactly the named ones."""
    with open_db(settings.db_path) as conn:
        docs = [dict(r) for r in conn.execute("SELECT doc_id, filename, active_source_hash FROM documents")]
    if not docs:
        raise IngestionError("manifest is empty; run the manifest command first")
    if not doc_ids:
        return docs
    chosen = [d for d in docs if d["doc_id"] in set(doc_ids)]
    if len(chosen) != len(set(doc_ids)):
        raise IngestionError("unknown doc_id in selection")
    return chosen


def ingest(settings: Settings, doc_ids: list[str] | None = None) -> list[dict]:
    results = []
    done: dict[str, dict] = {}
    for d in select_documents(settings, doc_ids):
        h = d["active_source_hash"]
        if h not in done:  # identical bytes share one extraction
            try:
                done[h] = ingest_source(settings, h)
            except IngestionError as exc:
                done[h] = {"source_hash": h, "status": "error", "reason": str(exc)}
        results.append({"doc_id": d["doc_id"], "filename": d["filename"], **done[h]})
    return results


# ---------------------------------------------------------------- reviews


REVIEW_STATUSES = ("unreviewed", "sample_checked", "reviewed", "needs_recovery")
MONEY_RE = re.compile(r"(\d[\d,]{3,}\s*원|부가(가치)?세|VAT)")
DATE_RE = re.compile(r"(20\d\d\s*[.\-년]\s*\d{1,2}\s*[.\-월]\s*\d{1,2}|마감|제출\s*기한)")


def review_sheet(settings: Settings, source_hash: str) -> Path:
    """Writes a checklist of exact locations for a reviewer to compare against the original."""
    with open_db(settings.db_path) as conn:
        src = conn.execute("SELECT * FROM sources WHERE source_hash = ?", (source_hash,)).fetchone()
        if src is None or src["parse_status"] != "parsed":
            raise IngestionError("source is not parsed")
        docs = [r["filename"] for r in conn.execute(
            "SELECT filename FROM documents WHERE active_source_hash = ?", (source_hash,))]
        els = [dict(r) for r in conn.execute(
            "SELECT element_id, kind, raw_text, location_json FROM elements WHERE extraction_id = ? "
            "ORDER BY source_order", (src["active_extraction_id"],))]
    n = len(els)
    picks: list[tuple[str, dict]] = []

    def pick(label: str, predicate, start: int = 0) -> None:
        for e in els[start:]:
            if predicate(e):
                picks.append((label, e))
                return

    picks += [("start", els[0]), ("middle", els[n // 2]), ("end", els[-1])]
    pick("detailed requirement", lambda e: e["kind"] == "table" and len(set(CODE_RE.findall(e["raw_text"]))) == 1,
         n // 4)
    pick("money/VAT condition", lambda e: MONEY_RE.search(e["raw_text"]) is not None)
    pick("date/deadline condition", lambda e: DATE_RE.search(e["raw_text"]) is not None, n // 3)
    pick("table (multi-row)", lambda e: e["kind"] == "table" and e["raw_text"].count("\n") >= 3, n // 2)
    lines = [f"# Fidelity review sheet — {', '.join(docs)}", "",
             f"- source_hash: `{source_hash}`", f"- extraction_id: `{src['active_extraction_id']}`",
             f"- elements: {n}", "",
             "Open the original in its native viewer. For each item compare text, numbers, units, "
             "table header/cell relationships and order. Record findings with `review record`.", ""]
    for label, e in picks:
        loc = json.loads(e["location_json"])
        lines += [f"## {label}", "", f"- element_id: `{e['element_id']}` ({e['kind']})",
                  f"- location: `{json.dumps(loc, ensure_ascii=False)}`", "", "```text",
                  e["raw_text"][:1500], "```", "", "- [ ] matches original", ""]
    path = settings.data_dir / "reviews" / f"{source_hash[:16]}-sheet.md"
    write_text_atomic(path, "\n".join(lines))
    return path


def record_review(settings: Settings, source_hash: str, reviewer: str, status: str,
                  locations: list, findings: dict) -> str:
    if status not in REVIEW_STATUSES:
        raise IngestionError(f"status must be one of {REVIEW_STATUSES}")
    if not reviewer.strip():
        raise IngestionError("reviewer identity is required")
    if status in ("sample_checked", "reviewed") and not locations:
        raise IngestionError("a positive review must name the inspected locations")
    review_id = str(uuid.uuid4())
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        src = conn.execute("SELECT * FROM sources WHERE source_hash = ?", (source_hash,)).fetchone()
        if src is None:
            raise IngestionError("unknown source")
        if status in ("sample_checked", "reviewed") and src["parse_status"] != "parsed":
            raise IngestionError("only parsed sources can be marked checked")
        conn.execute(
            "INSERT INTO reviews(review_id, source_hash, extraction_id, reviewer, status, locations_json, "
            "findings_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (review_id, source_hash, src["active_extraction_id"], reviewer, status, dumps(locations),
             dumps(findings), utcnow()),
        )
        conn.execute("UPDATE sources SET review_status = ? WHERE source_hash = ?", (status, source_hash))
    return review_id


def printed_pdf_path(settings: Settings, source_hash: str) -> Path:
    return settings.data_dir / "reviews" / "printed" / f"{source_hash[:16]}.pdf"


def render_pages(settings: Settings, source_hash: str, pdf: Path | None = None, dpi: int = 110) -> Path:
    """One PNG per page for visual comparison: the PDF original itself, or the native-printed PDF of an HWP."""
    import pymupdf

    with open_db(settings.db_path) as conn:
        src = conn.execute("SELECT format, original_path FROM sources WHERE source_hash = ?", (source_hash,)).fetchone()
    if src is None:
        raise IngestionError("unknown source")
    pdf = pdf or (Path(src["original_path"]) if src["format"] == "pdf" else printed_pdf_path(settings, source_hash))
    if not pdf.exists():
        raise IngestionError("no rendering to split: print the HWP first (review print or fidelity run)")
    out = settings.data_dir / "reviews" / "rendered" / source_hash[:16]
    out.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc, 1):
            page.get_pixmap(dpi=dpi).save(out / f"page-{i:03d}.png")
    return out


def load_elements(settings: Settings, extraction_id: str) -> list[dict]:
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT artifact_path FROM extractions WHERE extraction_id = ?", (extraction_id,)).fetchone()
    if row is None:
        raise IngestionError(f"unknown extraction {extraction_id}")
    return read_jsonl(Path(row["artifact_path"]))


def today_seoul() -> date:
    return datetime.now(SEOUL).date()
