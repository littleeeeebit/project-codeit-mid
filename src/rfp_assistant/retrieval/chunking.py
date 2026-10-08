"""Structural chunks, exact requirement inventory and source-span mappings."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache

from ..corpus.ingestion import CODE_RE

CHUNKER_VERSION = "structural-5"  # 4: tokenizer-offset windows, sibling links; 5: row-fragment offsets
LABEL_LINES = 2  # title lines carried into the table below them
LABEL_CHARS = 60
TARGET_TOKENS = 700
HARD_TOKENS = 800
OVERLAP_TOKENS = 64
TOKENIZER = "o200k_base"  # gpt-4o-mini's encoding


@lru_cache(maxsize=1)
def _encoding():
    import tiktoken

    return tiktoken.get_encoding(TOKENIZER)


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text))


FIXED_VERSION = "fixed-1"
# name -> (window tokens, overlap tokens); None is the structural chunker
PROFILES: dict[str, tuple[int, int] | None] = {
    "structural": None, "fixed-256-32": (256, 32), "fixed-512-64": (512, 64), "fixed-800-96": (800, 96)}


def chunker_fingerprint(profile: str = "structural") -> str:
    if profile not in PROFILES:
        raise ValueError(f"unknown chunk profile {profile!r}; choose from {sorted(PROFILES)}")
    if PROFILES[profile] is None:
        info = {"v": CHUNKER_VERSION, "target": TARGET_TOKENS, "hard": HARD_TOKENS, "overlap": OVERLAP_TOKENS,
                "tokenizer": TOKENIZER}
    else:
        size, overlap = PROFILES[profile]
        info = {"v": FIXED_VERSION, "size": size, "overlap": overlap, "tokenizer": TOKENIZER}
    return hashlib.sha256(json.dumps(info, sort_keys=True).encode()).hexdigest()[:16]


def build_profile(elements: list[dict], extraction_id: str, profile: str) -> tuple[list[dict], list[dict]]:
    """Chunks for one extraction under a named profile. The requirement inventory always comes from the source
    structure, so exhaustive lists do not depend on the chunking under comparison."""
    chunks, inventory = build_chunks(elements, extraction_id)
    if PROFILES[profile] is None:
        return chunks, inventory
    size, overlap = PROFILES[profile]
    return build_fixed_chunks(elements, extraction_id, size, overlap, chunker_fingerprint(profile)), inventory


def build_fixed_chunks(elements: list[dict], extraction_id: str, size: int, overlap: int, fp: str) -> list[dict]:
    """Baseline token windows over the same element order as the structural chunker (contents entries
    excluded), within one extraction revision. No headings are added; every window maps back to raw spans."""
    parts: list[tuple[dict, int, int]] = []
    text = ""
    for el in elements:
        if el["kind"] == "toc" or not el["raw_text"].strip():
            continue
        if text:
            text += "\n"
        parts.append((el, len(text), len(text) + len(el["raw_text"])))
        text += el["raw_text"]
    b = _Builder(extraction_id, fp)
    for cs, ce in _windows(text, size, overlap):
        spans = [{"element_id": el["element_id"], "start": max(cs, es) - es, "end": min(ce, ee) - es}
                 for el, es, ee in parts if es < ce and cs < ee and min(ce, ee) > max(cs, es)]
        if not spans:
            continue
        first = next(el for el, es, ee in parts if el["element_id"] == spans[0]["element_id"])
        codes = list(dict.fromkeys(CODE_RE.findall(text[cs:ce])))
        b.add("", text[cs:ce], spans, "fixed", codes[0] if len(codes) == 1 else None,
              first["location"].get("section_path", []))
    return b.linked()


def _heading(section_path: list[str]) -> str:
    return "[" + " > ".join(section_path) + "]" if section_path else ""


def _is_label(el: dict) -> bool:
    """A title line, not a sentence: short and not ending like a Korean clause."""
    text = el["raw_text"].strip()
    return 0 < len(text) <= LABEL_CHARS and not re.search(r"(다|음|함|임)\s*[.。]?$", text)


def table_rows(el: dict) -> list[tuple[int, str]]:
    """(row index, rendered row). Merged cells carry their text into every row they span."""
    rows: dict[int, dict[int, str]] = {}
    for c in el["table"]["cells"]:
        for r in range(c["row"], c["row"] + max(1, c.get("rowspan", 1))):
            rows.setdefault(r, {})[c["col"]] = c["text"].strip()
    out = []
    for r in sorted(rows):
        seen: list[str] = []
        for col in sorted(rows[r]):
            t = rows[r][col]
            if t and (not seen or seen[-1] != t):
                seen.append(t)
        if seen:
            out.append((r, " | ".join(seen)))
    return out


def classify_table(el: dict) -> tuple[str, list[str]]:
    codes = list(dict.fromkeys(CODE_RE.findall(el["raw_text"])))
    if len(codes) == 1 and el["table"]["rows"] >= 2:
        return "requirement_detail", codes
    if len(codes) >= 2:
        return "requirement_summary", codes
    return "table", codes


def _requirement_name(el: dict) -> str | None:
    for _, line in table_rows(el):
        parts = line.split(" | ")
        if len(parts) >= 2 and ("명칭" in parts[0] or parts[0].strip() in ("요구사항명", "요구사항 명")):
            return parts[-1][:120]
    return None


class _Builder:
    def __init__(self, extraction_id: str, fp: str) -> None:
        self.extraction_id = extraction_id
        self.fp = fp
        self.chunks: list[dict] = []

    def add(self, heading: str, body: str, spans: list[dict], chunk_type: str, requirement_key: str | None,
            section_path: list[str], split_group: str | None = None) -> None:
        payload = f"{heading}\n{body}".strip() if heading else body.strip()
        if not payload:
            return
        key = json.dumps([self.extraction_id, self.fp, spans, payload], ensure_ascii=False)
        self.chunks.append({
            "chunk_id": hashlib.sha256(key.encode()).hexdigest()[:20],
            "extraction_id": self.extraction_id,
            "spans": spans,
            "payload": payload,
            "body": body.strip(),
            "token_count": count_tokens(payload),
            "chunk_type": chunk_type,
            "requirement_key": requirement_key,
            "section_path": section_path,
            "split_group": split_group,
        })

    def linked(self) -> list[dict]:
        """Pieces of one oversized element or row know each other, so packing can keep a condition with its
        fact or report that it could not."""
        groups: dict[str, list[str]] = {}
        for c in self.chunks:
            if c["split_group"]:
                groups.setdefault(c["split_group"], []).append(c["chunk_id"])
        for c in self.chunks:
            group = groups.get(c.pop("split_group") or "", [])
            c["linked"] = [x for x in group if x != c["chunk_id"]]
        return self.chunks


def _windows(text: str, size: int, overlap: int) -> list[tuple[int, int]]:
    """Character ranges of token windows over `text`. Offsets come from the tokenizer's own character map, so
    a window never starts inside a multi-byte character; each range is shrunk until it fits `size` tokens."""
    enc = _encoding()
    ids = enc.encode(text)
    if not ids:
        return []
    _, offsets = enc.decode_with_offsets(ids)
    ranges, start = [], 0
    step = max(1, size - overlap)
    while start < len(ids):
        end = min(start + size, len(ids))
        cs = offsets[start]
        ce = offsets[end] if end < len(ids) else len(text)
        while cs < ce and text[cs].isspace():  # payloads are stripped; count what is actually stored
            cs += 1
        while ce > cs and text[ce - 1].isspace():
            ce -= 1
        while ce > cs + 1 and count_tokens(text[cs:ce]) > size:
            ce -= 1
        if ce > cs:
            ranges.append((cs, ce))
        if end >= len(ids):
            break
        start += step
    return ranges


def _split_prose(text: str, budget: int) -> list[tuple[int, int]]:
    """Character ranges of token windows with overlap for one oversized element."""
    return _windows(text, budget, OVERLAP_TOKENS)


def build_chunks(elements: list[dict], extraction_id: str) -> tuple[list[dict], list[dict]]:
    """Returns (chunks, requirement inventory records) for one extraction revision."""
    b = _Builder(extraction_id, chunker_fingerprint())
    inventory: list[dict] = []
    buf: list[dict] = []
    buf_path: list[str] = []
    buf_tokens = 0

    def flush() -> None:
        nonlocal buf, buf_tokens
        # A buffer of bare titles ("Ⅰ | 사업 안내") carries no fact; its text lives on in chunk headings.
        if buf and not all(e["kind"] == "heading" and len(e["raw_text"].strip()) <= 20 for e in buf):
            body = "\n".join(e["raw_text"].strip() for e in buf)
            spans = [{"element_id": e["element_id"], "start": 0, "end": len(e["raw_text"])} for e in buf]
            b.add(_heading(buf_path), body, spans, "prose", None, buf_path)
        buf, buf_tokens = [], 0

    for el in elements:
        kind = el["kind"]
        path = el["location"].get("section_path", [])
        if kind == "toc":
            continue  # contents entries are not facts
        heading = _heading(path)
        head_tokens = count_tokens(heading) + 1 if heading else 0
        if kind == "heading":
            flush()
            buf_path = path
        if kind == "image_text":
            # Read from pixels by OCR: kept apart so an answer can say its evidence is machine-read.
            flush()
            _add_windows(b, heading, el, "image_text", path, HARD_TOKENS - head_tokens - 8)
            continue
        if kind in ("paragraph", "heading") or (kind == "table" and el["table"]["rows"] * el["table"]["cols"] == 1):
            text = el["raw_text"].strip()
            if not text:
                continue
            tokens = count_tokens(text)
            if path != buf_path or buf_tokens + tokens + head_tokens > TARGET_TOKENS:
                flush()
                buf_path = path
            if tokens + head_tokens > HARD_TOKENS:
                flush()
                _add_windows(b, heading, el, "prose", path, HARD_TOKENS - head_tokens - 8)
                continue
            buf.append(el)
            buf_tokens += tokens
            continue
        if kind != "table":
            continue
        # Short lines right before a table ("□ 조직별 역할", "[별표 2]") are its title: they go with every group of
        # its rows instead of standing alone as a chunk without facts.
        labels = _take_labels(buf)
        flush()
        rows = table_rows(el)
        if not rows:
            buf, buf_tokens = labels, sum(count_tokens(e["raw_text"].strip()) for e in labels)
            continue
        _add_table(b, el, rows, labels, heading, path, head_tokens, inventory)
    flush()
    return b.linked(), inventory


def _add_windows(b: _Builder, heading: str, el: dict, ctype: str, path: list[str], budget: int) -> None:
    """One oversized element as overlapping token windows, linked to each other."""
    for start, end in _split_prose(el["raw_text"], budget):
        b.add(heading, el["raw_text"][start:end],
              [{"element_id": el["element_id"], "start": start, "end": end}], ctype, None, path,
              el["element_id"])


def _take_labels(buf: list[dict]) -> list[dict]:
    """Pops the short title lines that end the buffer: they belong to the table that follows."""
    labels: list[dict] = []
    while buf and len(labels) < LABEL_LINES and _is_label(buf[-1]):
        labels.insert(0, buf.pop())
    return labels


def _add_table(b: _Builder, el: dict, rows: list[tuple[int, str]], labels: list[dict], heading: str,
               path: list[str], head_tokens: int, inventory: list[dict]) -> None:
    """A table's requirement records, and its rows in groups under the token ceiling with its title lines, caption
    and header repeated in every group; an oversized row is split into linked pieces."""
    ctype, codes = classify_table(el)
    label_spans = [{"element_id": e["element_id"], "start": 0, "end": len(e["raw_text"])} for e in labels]
    caption = "\n".join([e["raw_text"].strip() for e in labels] + [el["table"].get("caption") or ""]).strip()
    inventory += _inventory_records(el, ctype, codes, rows)
    # Row groups under the token ceiling; the header row repeats in every group.
    header_r, header = rows[0] if ctype != "requirement_detail" and len(rows) > 1 else (None, "")
    body_rows = rows[1:] if header_r is not None else rows
    fixed = "\n".join(x for x in (caption, header) if x)
    fixed_tokens = count_tokens(fixed) + head_tokens + 2
    group: list[tuple[int, str]] = []
    group_tokens = 0

    def emit(group: list[tuple[int, str]], split_group: str | None = None,
             fragment: tuple[int, int] | None = None) -> None:
        if not group:
            return
        body = "\n".join(x for x in (fixed, *[line for _, line in group]) if x)
        row_ids = ([header_r] if header_r is not None else []) + [r for r, _ in group]
        keys = list(dict.fromkeys(CODE_RE.findall("\n".join(line for _, line in group))))
        key = codes[0] if ctype == "requirement_detail" else (keys[0] if len(keys) == 1 else None)
        span = {"element_id": el["element_id"], "rows": row_ids}
        if fragment is not None:  # a piece of one oversized row: which characters of the rendered row it carries
            span["fragment"] = {"row": group[0][0], "start": fragment[0], "end": fragment[1]}
        b.add(heading, body, label_spans + [span], ctype, key, path, split_group)

    for r, line in body_rows:
        t = count_tokens(line) + 1
        if group and group_tokens + t + fixed_tokens > TARGET_TOKENS:
            emit(group)
            group, group_tokens = [], 0
        if t + fixed_tokens > HARD_TOKENS:  # one oversized row: split its text
            for start, end in _split_prose(line, HARD_TOKENS - fixed_tokens - 8):
                group = [(r, line[start:end])]
                emit(group, f"{el['element_id']}/r{r}", (start, end))
            group, group_tokens = [], 0
            continue
        group.append((r, line))
        group_tokens += t
    emit(group)


def _inventory_records(el: dict, ctype: str, codes: list[str], rows: list[tuple[int, str]]) -> list[dict]:
    """A requirement detail table's one code, or every code a summary table lists, row by row."""
    if ctype == "requirement_detail":
        return [{"requirement_key": codes[0], "source_form": codes[0], "kind": "detail",
                 "element_id": el["element_id"], "name": _requirement_name(el)}]
    if ctype != "requirement_summary":
        return []
    return [{"requirement_key": code, "source_form": code, "kind": "summary", "element_id": el["element_id"],
             "name": line[:120]} for _, line in rows for code in dict.fromkeys(CODE_RE.findall(line))]
