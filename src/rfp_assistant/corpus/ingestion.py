"""CSV manifest, converter invocation, HWP/PDF element extraction and review records."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import shutil
import tempfile
import time
import traceback
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from importlib import metadata
from pathlib import Path
from zoneinfo import ZoneInfo

from ..storage.hwp_loader import LOADER_ADAPTER_VERSION, LOADER_PACKAGES, load_hwp_hwpx
from ..storage.postgres import host_path
from ..settings import Settings
from ..storage.store import dumps, open_db, read_jsonl, tx, utcnow, write_jsonl_atomic, write_text_atomic

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
    "hwp_loader_failed": "HWP 변환기와 보조 파서 모두 문서를 읽지 못했습니다.",
    "hwp_loader_result_invalid": "HWP 보조 파서의 결과 형식이 올바르지 않습니다.",
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


def _collect(node: ET.Element, texts: list[str], tables: list, boxes: list, shapes: list | None = None) -> None:
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
            if shapes is not None:
                shapes.append(ch)
        elif tag in ("Header", "Footer", "HeaderParagraphList", "FooterParagraphList"):
            continue  # page chrome repeats on every page
        else:
            _collect(ch, texts, tables, boxes, shapes)


def _paragraphs(container: ET.Element):
    for ch in container:
        if ch.tag == "Paragraph":
            yield ch
        elif ch.tag == "ColumnSet":
            yield from _paragraphs(ch)


def _plain(p: ET.Element) -> str:
    """A paragraph's own text, without the tables and boxes it holds."""
    texts: list[str] = []
    _collect(p, texts, [], [])
    return "".join(texts)


def _table_cells(tbody: ET.Element | None, path: str) -> tuple[list[dict], list[tuple], list[tuple]]:
    """Each cell with its paragraphs' and text boxes' text, the tables nested in the cells (table, path,
    [row, col]) and the shape controls in the cells (control, path), in source order."""
    cells, nested, shapes = [], [], []
    for tr in (tbody.findall("TableRow") if tbody is not None else []):
        for cell in tr.findall("TableCell"):
            r, c = int(cell.get("row", 0)), int(cell.get("col", 0))
            parts = []
            for pi, p in enumerate(_paragraphs(cell)):
                texts, tables, boxes, controls = [], [], [], []
                _collect(p, texts, tables, boxes, controls)
                parts.append("".join(texts))
                parts += [_plain(bp) for box in boxes for bp in _paragraphs(box)]
                nested += [(t, f"{path}/r{r}c{c}/p{pi}/t{ti}", [r, c]) for ti, t in enumerate(tables)]
                shapes += [(g, f"{path}/r{r}c{c}/p{pi}/g{gi}") for gi, g in enumerate(controls)]
            cells.append({"row": r, "col": c, "rowspan": int(cell.get("rowspan", 1)),
                          "colspan": int(cell.get("colspan", 1)), "text": "\n".join(parts).strip()})
    return cells, nested, shapes


def bindata_names(root: ET.Element) -> list[str | None]:
    """Stream name under `BinData/` per `bindata-id` (1-based, in DocInfo order); None for a linked file."""
    names = []
    for b in root.iter("BinData"):
        emb = b.find("BinDataEmbedding")
        names.append(f"{emb.get('storage-id')}.{emb.get('ext')}" if emb is not None else None)
    return names


def shape_objects(gso: ET.Element) -> list[tuple[str, ET.Element]]:
    """What a shape control shows that text extraction cannot: each picture, else an OLE object, else a drawing.
    A lone line and a plain text box (its text is already extracted) are neither."""
    pictures = [sc for sc in gso.iter("ShapeComponent") if sc.find("ShapePicture/PictureInfo") is not None]
    if pictures:
        return [("picture", sc) for sc in pictures]
    chids = [sc.get("chid") for sc in gso.iter("ShapeComponent")]
    if "$ole" in chids:
        return [("ole", gso)]
    # ponytail: any group, or a shape that is neither a line nor a text box, counts as a drawing, decorative
    # frames included. Tighten when the ocr_unavailable drawing counts prove noisy.
    if "$con" in chids or chids and chids[0] != "$lin" and gso.find(".//TextboxParagraphList") is None:
        return [("drawing", gso)]
    return []


def walk_hwp(root: ET.Element, markers: bool = False) -> list[dict]:
    """Walk HWP XML in source order. Every text span is owned by exactly one element:
    body paragraph, table cell (nested tables excluded) or nested table. With `markers`, a `picture` element (no
    text) stands where a picture, OLE object or drawing sits, a picture in a cell after its table;
    `ocr.merge_hwp` turns each into image_text or drops it, so no marker is ever stored."""
    body = root.find("BodyText")
    if body is None:
        raise IngestionError("hwp_empty_output")
    out: list[dict] = []
    tracker = _SectionTracker()
    table_counter = [0]
    names = bindata_names(root)
    page_area = [1]

    def emit_shapes(shapes: list[tuple], section: int) -> None:
        for gso, path in shapes if markers else ():
            for k, (obj, node) in enumerate(shape_objects(gso)):
                location = {"format": "hwp", "section": section, "path": f"{path}/i{k}",
                            "section_path": tracker.path, "object": obj,
                            "share": round(int(node.get("width", 0)) * int(node.get("height", 0)) / page_area[0], 4)}
                if obj == "picture":
                    ref = int(node.find("ShapePicture/PictureInfo").get("bindata-id", 0))
                    location["bindata"] = names[ref - 1] if 0 < ref <= len(names) else None
                out.append({"path": f"{path}/i{k}", "kind": "picture", "parent": None, "raw_text": "",
                            "location": location})

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
        cap = tc.find("TableCaption")
        caption = [_plain(p) for p in _paragraphs(cap)] if cap is not None else []
        cells, nested, shapes = _table_cells(tbody, path)
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
        emit_shapes(shapes, section)
        for t, npath, ref in nested:
            emit_table(t, npath, section, path, ref)

    for si, sec in enumerate(body.findall("SectionDef")):
        page = sec.find("PageDef")
        page_area[0] = int(page.get("width", 0)) * int(page.get("height", 0)) if page is not None else 0
        page_area[0] = page_area[0] or 59528 * 84188  # A4 in HWPUNIT
        for pi, p in enumerate(_paragraphs(sec)):
            texts, tables, boxes, controls = [], [], [], []
            _collect(p, texts, tables, boxes, controls)
            path = f"s{si}/p{pi}"
            emit_paragraph("".join(texts), path, si, None)
            for bi, box in enumerate(boxes):
                for bpi, bp in enumerate(_paragraphs(box)):
                    emit_paragraph(_plain(bp), f"{path}/box{bi}/p{bpi}", si, None)
            emit_shapes([(g, f"{path}/g{gi}") for gi, g in enumerate(controls)], si)
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
    """Returns (elements with picture markers, warnings, reason_code). `ocr.merge_hwp` removes the markers."""
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
    try:  # a document pyhwp cannot walk is a failure reason, so ingestion can fall back to the loader
        slots = list(root.iter("EqEdit"))
        if slots:
            scripts = equation_scripts(original)
            if len(scripts) == len(slots):
                for slot, script in zip(slots, scripts):
                    slot.text = equation_text(script)
            else:
                warnings.append({"code": "equation_count_mismatch", "detail": f"{len(slots)} in XML, {len(scripts)} records"})
        elements = walk_hwp(root, markers=True)
    except IngestionError as exc:  # walk_hwp's own reason code
        return [], warnings, str(exc)
    except (ValueError, LookupError) as exc:  # a malformed attribute, or a record the equation reader cannot decode
        return [], warnings + [{"code": "hwp_walk_error", "detail": f"{type(exc).__name__}: {exc}"[:STDERR_LIMIT]}], \
            "hwp_walk_failed"
    if not any(e["raw_text"].strip() for e in elements):
        return [], warnings, "hwp_empty_output"
    return elements, warnings, None


# ---------------------------------------------------------------- PDF


def _page_labels(page, blocks: list, height: float, column) -> dict[int, str]:
    """The printed page label of each column: the PDF's own label, else the page number in the footer."""
    if page.get_label():
        return {0: page.get_label(), 1: page.get_label()}
    labels: dict[int, str] = {}
    for b in blocks:  # printed page numbers sit in the footer, e.g. "-201-"
        m = FOOTER_PAGE_RE.match(b[4].strip())
        if m and b[1] > height * 0.85:
            labels[column(b[0])] = m.group(1)
    return labels


def _page_items(page, blocks: list, column) -> list[tuple]:
    """The page's tables and the text blocks outside them, in reading order: (column, top, left, kind, object,
    cells)."""
    import pymupdf

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
    return items


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
            labels = _page_labels(page, blocks, height, column)
            if sum(len(b[4].strip()) for b in blocks) < 10:
                warnings.append({"code": "page_blank_or_image", "page": pno})
            for n, (col, _, _, kind, obj, cells) in enumerate(_page_items(page, blocks, column)):
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
    elif fmt == "hwp_loader":
        info = {"adapter": LOADER_ADAPTER_VERSION, **{p: metadata.version(p) for p in LOADER_PACKAGES}}
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


def input_key(settings: Settings, src) -> str:
    """Everything a parse depends on: original bytes, parser revision and the OCR cache (the Hancom print is only
    the fidelity witness). An unchanged key means a rerun would reproduce the same extraction, so it is reused."""
    from .ocr import cache_path  # ocr imports this module

    ocr_rows = cache_path(settings, src["source_hash"])
    info = {"source": src["source_hash"], "parser": parser_fingerprint(src["format"]),
            "ocr": sha256_file(ocr_rows) if ocr_rows.exists() else None}
    if src["format"] == "hwp":  # installing or replacing the converter retries a failed conversion
        info["loader"] = parser_fingerprint("hwp_loader")  # the fallback when the converter fails
        conv = settings.hwp_converter
        info["converter"] = [str(conv), conv.stat().st_size, conv.stat().st_mtime_ns] \
            if conv is not None and Path(conv).is_file() else None
        info["converter_timeout"] = settings.converter_timeout_seconds
    return hashlib.sha256(dumps(info).encode()).hexdigest()


def diagnose(elements: list[dict], warnings: list[dict]) -> dict:
    """Automatic content checks. They point a reviewer at problems; none of them is a fidelity pass."""
    kinds: dict[str, int] = {}
    for e in elements:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    tables = [e for e in elements if e["kind"] == "table"]
    chars = [len(e["raw_text"].strip()) for e in elements]
    total = sum(chars)
    codes = {c for e in elements for c in CODE_RE.findall(e["raw_text"])}
    pages = {e["location"].get("page") for e in elements} - {None}
    sections = {e["location"].get("section") for e in elements} - {None}
    body = [e for e in elements if e["raw_text"].strip() and e["kind"] != "toc"]

    def probe(e: dict) -> dict:
        return {"element_id": e["element_id"], "kind": e["kind"], "text": search_text(e["raw_text"])[:80]}

    tail = sum(chars[len(chars) - max(1, len(chars) // 10):]) if chars else 0
    flags = []
    if total < 2000:
        flags.append("short_output")
    if elements and not tables:
        flags.append("no_tables")
    if total and tail / total < 0.01:
        flags.append("thin_tail")
    blank = [w.get("page") for w in warnings if w.get("code") == "page_blank_or_image"]
    if blank:
        flags.append("blank_pages")
    if any(e.get("replacement_chars") for e in elements):
        flags.append("replacement_characters")
    if any(w.get("code") == "mupdf_warnings" for w in warnings):
        flags.append("parser_warnings")
    return {
        "elements": len(elements), "kinds": kinds, "tables": len(tables),
        "cells": sum(len(t["table"]["cells"]) for t in tables if t.get("table")), "chars": total,
        "requirement_codes": len(codes), "pages": max(pages) if pages else None,
        "sections": len(sections) if sections else None, "blank_pages": blank[:50],
        "probes": {"start": probe(body[0]), "middle": probe(body[len(body) // 2]), "end": probe(body[-1])}
        if body else {},
        "flags": flags,
    }


def ingest_source(settings: Settings, source_hash: str, force: bool = False) -> dict:
    with open_db(settings.db_path) as conn:
        src = conn.execute("SELECT * FROM sources WHERE source_hash = ?", (source_hash,)).fetchone()
        recovery = conn.execute(
            "SELECT recovery_json FROM extractions WHERE extraction_id = ?", (src["active_extraction_id"],)
        ).fetchone() if src is not None and src["active_extraction_id"] else None
        prior = conn.execute("SELECT * FROM extraction_inputs WHERE source_hash = ?", (source_hash,)).fetchone()
    if src is None:
        raise IngestionError(f"unknown source {source_hash}")
    original = host_path(src["original_path"])
    if sha256_file(original) != source_hash:
        raise IngestionError(f"original bytes changed for {original.name}; rerun manifest")
    if recovery is not None and recovery["recovery_json"]:
        # An owner-registered recovery artifact replaces a failed conversion; reparsing would only fail again.
        return {"source_hash": source_hash, "status": "recovered", "extraction_id": src["active_extraction_id"]}
    key = input_key(settings, src)
    if not force and prior is not None and prior["input_key"] == key:
        reused = _reused(settings, src, prior)
        if reused is not None:
            return reused
    started = time.perf_counter()
    raw, warnings, reason, fp = _parse_source(settings, src, original)
    seconds = round(time.perf_counter() - started, 2)
    if reason:
        with open_db(settings.db_path) as conn, tx(conn, immediate=True):
            conn.execute(
                "UPDATE sources SET parse_status = 'quarantined', review_status = 'needs_recovery', reason_code = ?, "
                "warnings_json = ? WHERE source_hash = ?",
                (reason, dumps(warnings), source_hash),
            )
            _record_input(conn, source_hash, key, "", "", {"seconds": seconds, "reason": reason})
        return {"source_hash": source_hash, "status": "quarantined", "reason": reason, "seconds": seconds}
    extraction_id, elements, artifact = assign_revision(settings, source_hash, fp, raw)
    if any(e["replacement_chars"] for e in elements):
        warnings.append({"code": "replacement_characters",
                         "count": sum(e["replacement_chars"] for e in elements)})
    diagnostics = diagnose(elements, warnings)
    stats = {"seconds": seconds, "diagnostics": diagnostics, "cues": original_cues(elements),
             "warnings": [w["code"] for w in warnings]}
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        _record_input(conn, source_hash, key, extraction_id, sha256_file(artifact), stats)
        conn.execute(
            "INSERT INTO extractions(extraction_id, source_hash, parser_fingerprint, artifact_path, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (extraction_id, source_hash, fp, str(artifact), utcnow()),
        )
        _store_elements(conn, extraction_id, elements)
        # A new revision (parser or output) resets review: parsed does not mean faithful.
        review = src["review_status"] if src["active_extraction_id"] == extraction_id else "unreviewed"
        conn.execute(
            "UPDATE sources SET parse_status = 'parsed', active_extraction_id = ?, reason_code = NULL, "
            "review_status = ?, warnings_json = ? WHERE source_hash = ?",
            (extraction_id, review, dumps(warnings), source_hash),
        )
    return {"source_hash": source_hash, "status": "parsed", "extraction_id": extraction_id,
            "elements": len(elements), "tables": sum(e["kind"] == "table" for e in elements),
            "warnings": [w["code"] for w in warnings], "seconds": seconds,
            "diagnostic_flags": diagnostics["flags"]}


def _reused(settings: Settings, src, prior) -> dict | None:
    """The recorded outcome of the same input: its quarantine, or its parse while the artifact is intact."""
    source_hash = src["source_hash"]
    stats = json.loads(prior["stats_json"])
    if src["parse_status"] == "quarantined" and not prior["extraction_id"]:
        return {"source_hash": source_hash, "status": "quarantined", "reason": src["reason_code"], "reused": True}
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT artifact_path FROM extractions WHERE extraction_id = ?",
                           (prior["extraction_id"],)).fetchone()
    artifact = host_path(row["artifact_path"]) if row else None
    if (src["parse_status"] == "parsed" and src["active_extraction_id"] == prior["extraction_id"]
            and artifact is not None and artifact.exists() and sha256_file(artifact) == prior["artifact_sha256"]):
        return {"source_hash": source_hash, "status": "parsed", "extraction_id": prior["extraction_id"],
                "reused": True, "diagnostic_flags": stats.get("diagnostics", {}).get("flags", [])}
    return None


def _parse_source(settings: Settings, src, original: Path) -> tuple[list[dict], list[dict], str | None, str]:
    """(elements, warnings, reason_code, parser fingerprint) of an original, with OCR text merged: for HWP at the
    picture markers pyhwp's walk left, for PDF next to the original's text around each image."""
    from . import ocr  # ocr imports this module

    source_hash = src["source_hash"]
    fp = parser_fingerprint(src["format"])
    suffix = ""
    if src["format"] == "hwp":
        raw, warnings, reason = parse_hwp(settings, original)
        if not reason:
            raw, more, suffix = ocr.merge_hwp(settings, source_hash, raw)
            warnings += more
        else:
            # pyhwp cannot read it: the HWP loader parses the original. The print is only the fidelity witness.
            # The loader gives no picture positions, so this document gets no image text.
            raw, more, failed = load_hwp_hwpx(original)
            warnings += [{"code": "pyhwp_failed", "detail": reason}] + more
            reason, fp = failed, parser_fingerprint("hwp_loader")
    else:
        raw, warnings, reason = parse_pdf(original)
        if not reason:
            raw, more, suffix = ocr.merge(settings, source_hash, raw, original)
            warnings += more
    return raw, warnings, reason, f"{fp}-{suffix}" if suffix else fp


def _store_elements(conn, extraction_id: str, elements: list[dict]) -> None:
    """Replaces an extraction's element rows."""
    conn.execute("DELETE FROM elements WHERE extraction_id = ?", (extraction_id,))
    conn.executemany(
        "INSERT INTO elements(extraction_id, element_id, source_order, kind, parent_id, raw_text, search_text, "
        "location_json, table_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(extraction_id, e["element_id"], e["source_order"], e["kind"], e["parent_id"], e["raw_text"],
          e["search_text"], dumps(e["location"]), dumps(e["table"]) if e["table"] else None) for e in elements])


def assign_revision(settings: Settings, source_hash: str, fp: str, raw: list[dict]) -> tuple[str, list[dict], Path]:
    """Extraction revision bound to the actual output, not only to the parser fingerprint: a different converter
    or input can produce different text under the same fingerprint. Identical output keeps its revision (and its
    review); different output gets a revision of its own beside the old one, whose artifact and element rows stay
    for the traces, gold rows and citations pinned to them. Returns (extraction_id, elements, artifact path)."""
    base = hashlib.sha256(f"{source_hash}:{fp}".encode()).hexdigest()[:24]
    root = settings.data_dir / "extracted" / source_hash / fp
    candidates = [(base, root / "elements.jsonl")]
    elements = finalize_elements(raw, base)
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT artifact_path FROM extractions WHERE extraction_id = ?", (base,)).fetchone()
    if row is not None:
        stored = host_path(row["artifact_path"])
        if not (stored.exists() and read_jsonl(stored) == json.loads(json.dumps(elements, ensure_ascii=False))):
            digest = hashlib.sha256(dumps(elements).encode()).hexdigest()
            content_id = hashlib.sha256(f"{source_hash}:{fp}:{digest}".encode()).hexdigest()[:24]
            candidates = [(content_id, root / content_id / "elements.jsonl")]
            elements = finalize_elements(raw, content_id)
    extraction_id, artifact = candidates[0]
    with open_db(settings.db_path) as conn:
        known = conn.execute("SELECT artifact_path FROM extractions WHERE extraction_id = ?",
                             (extraction_id,)).fetchone()
    if known is not None:
        artifact = host_path(known["artifact_path"])
    if not artifact.exists() or read_jsonl(artifact) != json.loads(json.dumps(elements, ensure_ascii=False)):
        write_jsonl_atomic(artifact, elements)
    return extraction_id, elements, artifact


def _record_input(conn, source_hash: str, key: str, extraction_id: str, artifact_sha: str, stats: dict) -> None:
    conn.execute(
        "INSERT INTO extraction_inputs(source_hash, input_key, extraction_id, artifact_sha256, stats_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source_hash) DO UPDATE SET input_key = excluded.input_key, "
        "extraction_id = excluded.extraction_id, artifact_sha256 = excluded.artifact_sha256, "
        "stats_json = excluded.stats_json, updated_at = excluded.updated_at",
        (source_hash, key, extraction_id, artifact_sha, dumps(stats), utcnow()))


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


def ingest(settings: Settings, doc_ids: list[str] | None = None, force: bool = False) -> list[dict]:
    """Every unique original once. One failing file is recorded with its error and the run continues; the
    caller reports failures (the CLI exits nonzero) rather than dropping them."""
    results = []
    done: dict[str, dict] = {}
    for d in select_documents(settings, doc_ids):
        h = d["active_source_hash"]
        if h not in done:  # identical bytes share one extraction
            try:
                done[h] = ingest_source(settings, h, force=force)
            except IngestionError as exc:
                done[h] = {"source_hash": h, "status": "error", "reason": str(exc)}
            except Exception as exc:  # noqa: BLE001 - an unexpected parser crash stays visible per source
                done[h] = {"source_hash": h, "status": "error", "reason": f"{type(exc).__name__}: {exc}"[:500],
                           "traceback": traceback.format_exc()[-STDERR_LIMIT:]}
        results.append({"doc_id": d["doc_id"], "filename": d["filename"], **done[h]})
    return results


def write_ingest_report(settings: Settings, results: list[dict]) -> Path:
    counts: dict[str, int] = {}
    for r in results:
        key = r["status"] + ("_reused" if r.get("reused") else "")
        counts[key] = counts.get(key, 0) + 1
    report = {"created_at": utcnow(), "counts": counts, "results": results}
    path = settings.data_dir / "reports" / f"ingest-{utcnow()[:19].replace(':', '')}.json"
    write_text_atomic(path, json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return path


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


def extraction_review_status(conn, extraction_id: str) -> str:
    """The review state of one extraction from its own records, replayed the way the writers apply them: a
    person's review sets it, a fidelity verdict sets it unless a person's positive review stands. For an extraction
    that is no longer its source's active one, whose `sources` row now describes a newer revision."""
    events = sorted([(r["created_at"], True, r["status"]) for r in conn.execute(
        "SELECT created_at, status FROM reviews WHERE extraction_id = ?", (extraction_id,))]
        + [(r["created_at"], False, r["verdict"]) for r in conn.execute(
            "SELECT created_at, verdict FROM fidelity_checks WHERE extraction_id = ?", (extraction_id,))])
    status = "unreviewed"
    for _, human, value in events:
        if human or status not in ("sample_checked", "reviewed"):
            status = value
    return status


def _load_review_records(path: Path) -> list[dict]:
    if not path.is_absolute():
        raise IngestionError("review files are given by absolute path")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise IngestionError("review files must be UTF-8 without BOM")
    text = raw.decode("utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def _check_review(conn, rec: dict, tag: str) -> list[str]:
    """A positive review names its reviewer, the exact extraction revision it compared and the element
    locations that exist in it. Converter success alone never passes."""
    errors = []
    source_hash = rec.get("source_hash")
    if not source_hash and rec.get("doc_id"):
        row = conn.execute("SELECT active_source_hash FROM documents WHERE doc_id = ?", (rec["doc_id"],)).fetchone()
        source_hash = row[0] if row else None
    src = conn.execute("SELECT * FROM sources WHERE source_hash = ?", (source_hash,)).fetchone() if source_hash \
        else None
    if src is None:
        return [f"{tag}: unknown document/source"]
    rec["source_hash"] = source_hash
    status = rec.get("status")
    if status not in REVIEW_STATUSES:
        errors.append(f"{tag}: status must be one of {REVIEW_STATUSES}")
    if not str(rec.get("reviewer", "")).strip():
        errors.append(f"{tag}: reviewer identity is required")
    if rec.get("extraction_id") != src["active_extraction_id"]:
        errors.append(f"{tag}: extraction_id is not the source's active revision (stale or missing)")
    if status in ("sample_checked", "reviewed"):
        if src["parse_status"] != "parsed":
            errors.append(f"{tag}: only parsed sources can be marked checked")
        locations = rec.get("locations") or []
        if not locations:
            errors.append(f"{tag}: a positive review must name the inspected locations")
        for loc in locations:
            eid = loc.get("element_id") if isinstance(loc, dict) else None
            if not eid or conn.execute("SELECT 1 FROM elements WHERE extraction_id = ? AND element_id = ?",
                                       (rec.get("extraction_id"), eid)).fetchone() is None:
                errors.append(f"{tag}: location {loc!r} is not an element of the reviewed extraction")
        if not isinstance(rec.get("checks"), dict) or not rec["checks"]:
            errors.append(f"{tag}: a positive review records its checks")
        if "limitations" not in rec:
            errors.append(f"{tag}: record limitations (an empty list states that none were found)")
    if status == "reviewed" and not (rec.get("coverage") or {}).get("sections"):
        errors.append(f"{tag}: 'reviewed' declares the inspected sections in coverage.sections")
    return errors


def import_reviews(settings: Settings, path: Path) -> dict:
    """Validate every record first, then append them all; one invalid record imports nothing."""
    records = _load_review_records(path)
    if not records:
        raise IngestionError("review file holds no records")
    with open_db(settings.db_path) as conn:
        errors = [e for i, rec in enumerate(records, 1) for e in _check_review(conn, rec, f"record {i}")]
    if errors:
        raise IngestionError("; ".join(errors[:20]))
    ids = []
    for rec in records:
        findings = {"checks": rec.get("checks", {}), "coverage": rec.get("coverage", {}),
                    "limitations": rec.get("limitations", []), "findings": rec.get("findings", {}),
                    "imported_from": path.name}
        ids.append(record_review(settings, rec["source_hash"], rec["reviewer"], rec["status"],
                                 rec.get("locations", []), findings))
    return {"imported": len(ids), "review_ids": ids}


def review_coverage(settings: Settings) -> list[dict]:
    """Per source: current status and the latest human review's declared coverage and limitations."""
    with open_db(settings.db_path) as conn:
        sources = conn.execute(
            "SELECT s.source_hash, s.format, s.parse_status, s.review_status, s.reason_code, s.active_extraction_id, "
            "string_agg(d.filename, '; ' ORDER BY d.filename) AS filenames FROM sources s JOIN documents d "
            "ON d.active_source_hash = s.source_hash GROUP BY s.source_hash ORDER BY filenames").fetchall()
        out = []
        for s in sources:
            review = conn.execute(
                "SELECT reviewer, status, locations_json, findings_json, created_at, extraction_id FROM reviews "
                "WHERE source_hash = ? ORDER BY created_at DESC LIMIT 1", (s["source_hash"],)).fetchone()
            findings = json.loads(review["findings_json"]) if review else {}
            out.append({
                "source_hash": s["source_hash"], "filenames": s["filenames"], "format": s["format"],
                "parse_status": s["parse_status"], "review_status": s["review_status"], "reason_code": s["reason_code"],
                "reviewer": review["reviewer"] if review else None,
                "reviewed_at": review["created_at"] if review else None,
                "review_is_current": bool(review and review["extraction_id"] == s["active_extraction_id"]),
                "locations": len(json.loads(review["locations_json"])) if review else 0,
                "coverage": findings.get("coverage"), "limitations": findings.get("limitations"),
            })
    return out


# ---------------------------------------------------------------- recovery of failed conversions


def recovered_dir(settings: Settings, source_hash: str) -> Path:
    return settings.data_dir / "recovered" / source_hash


def _recovery_review(converted_file: Path, review_file: Path) -> dict:
    """The owner's review of a recovery artifact, once the artifact is an absolute, existing PDF and the review
    names who compared what, how and with which result."""
    if not converted_file.is_absolute():
        raise IngestionError("--converted-file must be an absolute path")
    if not converted_file.is_file():
        raise IngestionError("converted file not found")
    suffix = converted_file.suffix.lower()
    if suffix == ".hwpx":
        raise IngestionError("HWPX recovery is not implemented: convert to PDF, or add an HWPX parser once a real "
                             "recovery artifact needs it")
    if suffix != ".pdf":
        raise IngestionError("recovery artifacts must be PDF")
    (review,) = _load_review_records(review_file)[:1] or [None]
    if not isinstance(review, dict):
        raise IngestionError("review file holds no record")
    for key in ("reviewer", "method", "compared_locations", "mapping_limitations"):
        if not review.get(key):
            raise IngestionError(f"recovery review requires {key!r}")
    if not isinstance(review.get("fidelity_passed"), bool):
        raise IngestionError("recovery review requires fidelity_passed: true|false")
    if not isinstance(review["compared_locations"], list):
        raise IngestionError("compared_locations must be a list of the places compared (pages, tables, ...)")
    return review


def _managed_copy(settings: Settings, source_hash: str, converted_file: Path) -> tuple[str, Path]:
    """(hash, path) of the recovery artifact's copy under the source's recovered folder, verified."""
    converted_hash = sha256_file(converted_file)
    managed = recovered_dir(settings, source_hash) / f"{converted_hash}.pdf"
    if not managed.exists():
        managed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(converted_file, managed)
    if sha256_file(managed) != converted_hash:
        raise IngestionError("managed copy of the converted file does not match its hash")
    return converted_hash, managed


def recover_source(settings: Settings, doc_id: str, converted_file: Path, review_file: Path) -> dict:
    """Registers an owner-approved conversion (PDF) of a quarantined original as another extraction revision.
    The original and its failure stay recorded; a failed fidelity comparison keeps the quarantine."""
    review = _recovery_review(converted_file, review_file)
    with open_db(settings.db_path) as conn:
        doc = conn.execute("SELECT active_source_hash FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        if doc is None:
            raise IngestionError("unknown doc_id")
        src = conn.execute("SELECT * FROM sources WHERE source_hash = ?", (doc[0],)).fetchone()
    source_hash = src["source_hash"]
    if src["parse_status"] != "quarantined":
        raise IngestionError("only quarantined sources take a recovery artifact")
    if sha256_file(host_path(src["original_path"])) != source_hash:
        raise IngestionError("original bytes changed; rerun manifest")
    converted_hash, managed = _managed_copy(settings, source_hash, converted_file)
    previous = {"reason_code": src["reason_code"], "warnings": json.loads(src["warnings_json"])}
    recovery = {"original_hash": source_hash, "converted_hash": converted_hash, "converted_path": str(managed),
                "method": review["method"], "reviewer": review["reviewer"],
                "mapping_limitations": review["mapping_limitations"], "previous_failure": previous}
    if not review["fidelity_passed"]:
        record_review(settings, source_hash, review["reviewer"], "needs_recovery",
                      review["compared_locations"], {"recovery_rejected": recovery,
                                                     "findings": review.get("findings", {})})
        return {"source_hash": source_hash, "status": "quarantined", "reason": "recovery_fidelity_failed"}
    raw, warnings, reason = parse_pdf(managed)
    if reason:
        record_review(settings, source_hash, review["reviewer"], "needs_recovery", review["compared_locations"],
                      {"recovery_unreadable": reason, "recovery": recovery})
        return {"source_hash": source_hash, "status": "quarantined", "reason": f"recovery_{reason}"}
    for e in raw:
        e["location"]["format"] = "hwp_recovered_pdf"  # pages of the converted PDF, not of the original HWP
    fp = f"{parser_fingerprint('pdf')}-recovered-{converted_hash[:12]}"
    extraction_id, elements, artifact = assign_revision(settings, source_hash, fp, raw)
    warnings = previous["warnings"] + warnings + [{"code": "recovered_from_artifact", "detail": previous["reason_code"],
                                                   "converted_sha256": converted_hash}]
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute(
            "INSERT INTO extractions(extraction_id, source_hash, parser_fingerprint, artifact_path, "
            "created_at, recovery_json) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (extraction_id, source_hash, fp, str(artifact), utcnow(), dumps(recovery)))
        _store_elements(conn, extraction_id, elements)
        conn.execute(
            "UPDATE sources SET parse_status = 'parsed', active_extraction_id = ?, reason_code = NULL, "
            "review_status = 'unreviewed', warnings_json = ? WHERE source_hash = ?",
            (extraction_id, dumps(warnings), source_hash))
        _record_input(conn, source_hash, "recovery:" + converted_hash, extraction_id, sha256_file(artifact),
                      {"diagnostics": diagnose(elements, warnings), "cues": original_cues(elements),
                       "recovery": {k: recovery[k] for k in ("converted_hash", "method")}})
        # The conversion comparison is recorded as an audit entry. Coverage of the new revision is claimed
        # only by `import-reviews` against its own element IDs, so the source stays unreviewed until then.
        conn.execute(
            "INSERT INTO reviews(review_id, source_hash, extraction_id, reviewer, status, locations_json, "
            "findings_json, created_at) VALUES (?, ?, ?, ?, 'unreviewed', ?, ?, ?)",
            (review_id := str(uuid.uuid4()), source_hash, extraction_id, review["reviewer"],
             dumps(review["compared_locations"]), dumps({"recovery": recovery, "findings": review.get("findings", {}),
                                                         "limitations": [review["mapping_limitations"]]}), utcnow()))
    return {"source_hash": source_hash, "status": "parsed", "extraction_id": extraction_id,
            "elements": len(elements), "review_status": "unreviewed", "review_id": review_id,
            "next": "import-reviews against this extraction before it enters --reviewed-only indexes"}


# ---------------------------------------------------------------- identity cues and metadata provenance

INSTITUTION_LABELS = ("발주기관", "수요기관", "주관기관", "발주처", "기관명", "발 주 기 관", "수 요 기 관")
TITLE_LABELS = ("사업명", "용역명", "과제명", "사 업 명", "용 역 명")
CUE_SCAN_ELEMENTS = 120
LABEL_VALUE_RE = re.compile(r"^\s*[○◦·•\-\d.]*\s*(?P<label>[가-힣 ]{2,8}?)\s*[:：]\s*(?P<value>.{2,80})$")


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", "", nfc(text or ""))


def original_cues(elements: list[dict]) -> dict:
    """Institution and title as the original states them near its start (label: value lines and label | value
    table rows). These are provenance for comparison with the CSV row, never a silent replacement."""
    cues: dict[str, list[dict]] = {"institution": [], "title": []}

    def add(field: str, value: str, element_id: str) -> None:
        value = search_text(value).strip(" :：")
        if 2 <= len(value) <= 80 and value not in {c["value"] for c in cues[field]} and len(cues[field]) < 3:
            cues[field].append({"value": value, "element_id": element_id})

    def classify(label: str) -> str | None:
        squashed = _squash(label)
        if any(squashed == _squash(x) for x in INSTITUTION_LABELS):
            return "institution"
        if any(squashed == _squash(x) for x in TITLE_LABELS):
            return "title"
        return None

    for e in elements[:CUE_SCAN_ELEMENTS]:
        if e["kind"] == "table" and e.get("table"):
            rows: dict[int, list[dict]] = {}
            for c in e["table"]["cells"]:
                rows.setdefault(c["row"], []).append(c)
            for cells in rows.values():
                cells = sorted(cells, key=lambda c: c["col"])
                for a, b in zip(cells, cells[1:]):
                    field = classify(a["text"])
                    if field and b["text"].strip():
                        add(field, b["text"], e["element_id"])
        else:
            for line in e["raw_text"].splitlines():
                m = LABEL_VALUE_RE.match(line)
                field = classify(m.group("label")) if m else None
                if field:
                    add(field, m.group("value"), e["element_id"])
    return cues


def _agrees(csv_value: str | None, cues: list[dict]) -> bool | None:
    if not cues or not csv_value:
        return None
    a = _squash(csv_value)
    return any(a in _squash(c["value"]) or _squash(c["value"]) in a for c in cues)


def identity_report(settings: Settings) -> list[dict]:
    """Per association: CSV values, the original's own cues, agreement, provenance conflicts and resolutions.
    Byte-identical associations share one set of cues but keep their own CSV values."""
    with open_db(settings.db_path) as conn:
        docs = conn.execute(
            "SELECT d.doc_id, d.filename, d.active_source_hash, d.normalized_metadata_json, d.quality_json, "
            "i.stats_json FROM documents d LEFT JOIN extraction_inputs i ON i.source_hash = d.active_source_hash "
            "ORDER BY d.csv_row_id").fetchall()
        resolutions = resolutions_by_doc(conn)
    out = []
    for d in docs:
        meta = json.loads(d["normalized_metadata_json"])
        quality = json.loads(d["quality_json"])
        cues = json.loads(d["stats_json"]).get("cues", {}) if d["stats_json"] else {}
        out.append({
            "doc_id": d["doc_id"], "filename": d["filename"], "source_hash": d["active_source_hash"],
            "csv": {"institution": meta.get("institution"), "title": meta.get("title")},
            "original": cues,
            "agreement": {f: _agrees(meta.get(f), cues.get(f, [])) for f in ("institution", "title")},
            "shared_source_with": quality.get("shared_source_with", []),
            "provenance_conflicts": quality.get("provenance_conflicts", []),
            "resolutions": resolutions.get(d["doc_id"], {}),
        })
    return out


def resolutions_by_doc(conn) -> dict[str, dict[str, dict]]:
    """Latest owner/reviewer resolution per (doc, field). History stays in the append-only table."""
    out: dict[str, dict[str, dict]] = {}
    for r in conn.execute("SELECT * FROM metadata_resolutions ORDER BY created_at"):
        out.setdefault(r["doc_id"], {})[r["field"]] = {
            "value": json.loads(r["value_json"]), "rationale": r["rationale"], "evidence": r["evidence"],
            "actor": r["actor"], "at": r["created_at"]}
    return out


def _valid_when(v) -> bool:
    """Same shape as normalized CSV dates: ISO date for date precision, ISO timestamp with offset otherwise."""
    if not isinstance(v, dict) or not isinstance(v.get("value"), str):
        return False
    try:
        if v.get("precision") == "date":
            return date.fromisoformat(v["value"]).isoformat() == v["value"]
        if v.get("precision") == "timestamp":
            return datetime.fromisoformat(v["value"]).tzinfo is not None
    except ValueError:
        return False
    return False


def resolve_metadata(settings: Settings, doc_id: str, field: str, value, rationale: str, evidence: str,
                     actor: str) -> str:
    """Canonical value for one association's conflicting field, with a written source/notice rationale. The
    competing CSV values stay recorded and visible; CSV order is never authority."""
    if field not in CONFLICT_FIELDS:
        raise IngestionError(f"field must be one of {CONFLICT_FIELDS}")
    if not rationale.strip() or not evidence.strip() or not actor.strip():
        raise IngestionError("a resolution needs a rationale, the source/notice evidence and the actor")
    valid = {"amount_krw": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
             "bid_close": _valid_when}.get(field, lambda v: isinstance(v, str) and v.strip() != "")
    if not valid(value):
        raise IngestionError(f"value has the wrong shape for {field} (amount_krw: integer KRW; bid_close: "
                             '{"value": ISO text, "precision": "date"|"timestamp"}; others: text)')
    resolution_id = str(uuid.uuid4())
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        doc = conn.execute("SELECT quality_json FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        if doc is None:
            raise IngestionError("unknown doc_id")
        conflicts = {c["field"] for c in json.loads(doc["quality_json"]).get("provenance_conflicts", [])}
        if field not in conflicts:
            raise IngestionError(f"{field} has no recorded provenance conflict for this document")
        conn.execute(
            "INSERT INTO metadata_resolutions(resolution_id, doc_id, field, value_json, rationale, evidence, actor, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (resolution_id, doc_id, field, dumps(value), rationale, evidence, actor, utcnow()))
    return resolution_id


def printed_pdf_path(settings: Settings, source_hash: str) -> Path:
    return settings.data_dir / "reviews" / "printed" / f"{source_hash[:16]}.pdf"


def render_pages(settings: Settings, source_hash: str, pdf: Path | None = None, dpi: int = 110) -> Path:
    """One PNG per page for visual comparison: the PDF original itself, or the native-printed PDF of an HWP."""
    import pymupdf

    with open_db(settings.db_path) as conn:
        src = conn.execute("SELECT format, original_path FROM sources WHERE source_hash = ?", (source_hash,)).fetchone()
    if src is None:
        raise IngestionError("unknown source")
    pdf = pdf or (host_path(src["original_path"]) if src["format"] == "pdf" else printed_pdf_path(settings, source_hash))
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
    return read_jsonl(host_path(row["artifact_path"]))
