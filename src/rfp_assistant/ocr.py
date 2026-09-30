"""Text inside raster images: PaddleOCR-VL reads every region, Gemini re-reads only the regions it fails on.

Regions come from the Hancom print (HWP) or the original (PDF). The fallback test uses only signals available
without a truth text; it was calibrated on 80 print regions (docs/rag/preprocessing.md, "Text inside images").
Gemini spend has its own append-only ledger and hard cap, separate from the OpenAI budget.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import uuid
from contextlib import nullcontext
from pathlib import Path

from .fidelity import IMAGE_MIN_AREA, norm
from .ingestion import printed_pdf_path
from .settings import Settings, read_api_key
from .store import LockHeld, ProcessLock, dumps, open_db, utcnow, write_jsonl_atomic

LOCAL_MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"
LOCAL_REVISION = "c5630abae1d940eafe0697512a0325494b02ab42"
LOCAL_ENGINE = "paddleocr-vl-1.6"
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_MODEL
# Micro-dollars per token = USD per 1M tokens, from https://ai.google.dev/gemini-api/docs/pricing checked 2026-09-30.
GEMINI_RATES = {"input": 0.30, "output": 2.50}
GEMINI_CAP_MICRO = 500_000  # $0.50, decided 2026-09-30
GEMINI_MAX_OUTPUT = 4096  # includes thinking tokens, so it bounds the worst case
GEMINI_PROMPT = ("Transcribe all text in this image exactly as written, in reading order. Keep Korean as Korean. "
                 "Output only the text: no commentary, no markdown, no translation.")
OCR_VERSION = f"{LOCAL_ENGINE}@{LOCAL_REVISION[:8]}+{GEMINI_MODEL}-fallback-1"
DPI = 150
MAX_NEW_TOKENS = 2048
LOOP_WINDOW, LOOP_DISTINCT, LOOP_SAME = 48, 8, 12  # real tables repeat one cell up to ~9 times
# Calibrated on 50 random real image regions against Gemini's read (docs/rag/preprocessing.md). A shortfall
# against the ink estimate is not a signal: dotted contents leaders and touching glyphs swing it both ways.
MIN_MEAN_PROB = 0.90
INVENTED = 20.0  # output longer than 20x the ink estimate
RUNAWAY = (5.0, 500)  # or longer than 5x and over 500 characters: a repeated phrase that never breaks a line
CHARS_PER_BLOB = 0.39  # characters per glyph-sized ink component: median 0.39, p10 0.33, p90 0.44


class OcrError(RuntimeError):
    pass


def looping(lines: list[str]) -> bool:
    """True once the output cycles: 12 identical lines, or 48 lines with at most 8 distinct ones."""
    lines = [x for x in lines if x.strip()]
    if len(lines) >= LOOP_SAME and len(set(lines[-LOOP_SAME:])) == 1:
        return True
    return len(lines) >= LOOP_WINDOW and len(set(lines[-LOOP_WINDOW:])) <= LOOP_DISTINCT


def expected_chars(image) -> float:
    """Text length the ink suggests: glyph-sized connected components (rules and frames are longer)."""
    import numpy as np
    from scipy import ndimage

    labels, _ = ndimage.label(np.asarray(image.convert("L")) < 128)
    glyphs = sum(1 for s in ndimage.find_objects(labels)
                 if 2 < s[0].stop - s[0].start < 60 and s[1].stop - s[1].start < 60)
    return glyphs * CHARS_PER_BLOB


def fallback_reasons(text: str, mean_prob: float, looped: bool, expected: float, truncated: bool = False) -> list[str]:
    reasons = []
    if looped:
        reasons.append("loop")
    if truncated:
        reasons.append("truncated")  # ran out of tokens: the rest of the image was never read
    if mean_prob < MIN_MEAN_PROB:
        reasons.append("low_confidence")
    length = len(norm(text))
    coverage = length / max(expected, 1.0)
    if coverage > INVENTED or coverage > RUNAWAY[0] and length > RUNAWAY[1]:
        reasons.append("text_invented")
    return reasons


def regions(pdf: Path) -> list[dict]:
    """Raster images covering at least IMAGE_MIN_AREA of their page, rendered at DPI as PNG."""
    import pymupdf

    out, seen = [], set()
    with pymupdf.open(pdf) as doc:
        for page in doc:
            for info in page.get_image_info():
                rect = pymupdf.Rect(info["bbox"]) & page.rect
                key = (page.number, *(round(v) for v in rect))
                if rect.is_empty or key in seen or abs(rect) / abs(page.rect) < IMAGE_MIN_AREA:
                    continue
                seen.add(key)
                png = page.get_pixmap(clip=rect, dpi=DPI).tobytes("png")
                out.append({"page": page.number + 1, "bbox": [round(v, 1) for v in rect],
                            "digest": hashlib.sha256(png).hexdigest()[:24], "png": png})
    return out


class LocalOCR:
    """Owns PaddleOCR-VL on the GPU for one run: created on enter, freed on exit, never shared across runs."""

    def __enter__(self) -> "LocalOCR":
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor, StoppingCriteria

        self.torch = torch
        self.model = AutoModelForImageTextToText.from_pretrained(
            LOCAL_MODEL, revision=LOCAL_REVISION, dtype=torch.bfloat16).to("cuda").eval()
        self.proc = AutoProcessor.from_pretrained(LOCAL_MODEL, revision=LOCAL_REVISION)
        proc = self.proc

        class Loop(StoppingCriteria):
            def __init__(self, start: int):
                self.start = start

            def __call__(self, ids, scores, **kw) -> bool:
                if (ids.shape[-1] - self.start) % 16:
                    return False
                return looping(proc.decode(ids[0][self.start:], skip_special_tokens=True).splitlines())

        self.loop = Loop
        return self

    def __exit__(self, *exc) -> None:
        del self.model, self.proc
        self.torch.cuda.empty_cache()

    def read(self, image) -> dict:
        from transformers import StoppingCriteriaList

        torch = self.torch
        messages = [{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": "OCR:"}]}]
        inputs = self.proc.apply_chat_template(messages, add_generation_prompt=True, tokenize=True, return_dict=True,
                                               return_tensors="pt").to(self.model.device)
        start = inputs["input_ids"].shape[-1]
        with torch.inference_mode():
            g = self.model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS, do_sample=False, use_cache=True,
                                    return_dict_in_generate=True, output_scores=True,
                                    stopping_criteria=StoppingCriteriaList([self.loop(start)]))
        new = g.sequences[0][start:]
        text = self.proc.decode(new, skip_special_tokens=True)
        probs = [torch.softmax(s[0].float(), -1)[t].item() for s, t in zip(g.scores, new)]
        # The loop stop fires on a multiple of 16 before the limit; only running out of tokens reaches it.
        return {"text": text.strip(), "mean_prob": round(sum(probs) / max(len(probs), 1), 4),
                "looped": looping(text.splitlines()), "truncated": len(new) >= MAX_NEW_TOKENS}


# ---------------------------------------------------------------- Gemini fallback


def _micro(input_tokens: int, output_tokens: int) -> int:
    return math.ceil(input_tokens * GEMINI_RATES["input"] + output_tokens * GEMINI_RATES["output"])


def ledger_path(settings: Settings) -> Path:
    return settings.data_dir / "ocr" / "gemini-ledger.jsonl"


def spent_micro(ledger: Path) -> int:
    """Settled calls at their actual cost; reserved calls that never settled (unknown outcome) at worst case."""
    if not ledger.exists():
        return 0
    reserved, settled = {}, {}
    for row in map(json.loads, ledger.read_text(encoding="utf-8").splitlines()):
        (settled if row["event"] == "settled" else reserved)[row["call_id"]] = row["micro"]
    return sum(settled.values()) + sum(v for k, v in reserved.items() if k not in settled)


def _append(ledger: Path, row: dict) -> None:
    # Only a GeminiReader inside its `with` block appends, and it holds the ledger lock for that whole time.
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(dumps({**row, "at": utcnow()}) + "\n")


class GeminiReader:
    """Owns, for one `with` block, the ledger lock and one HTTP client. The lock makes the cap check and the
    reservation one step across processes: a second reader on the same data directory is refused. Every call is
    reserved at worst case before dispatch and settled after."""

    def __init__(self, settings: Settings):
        self.key = read_api_key("GEMINI_API_KEY")
        if not self.key:
            raise OcrError("GEMINI_API_KEY is not configured")
        self.ledger = ledger_path(settings)
        self.lock: ProcessLock | None = None

    def __enter__(self) -> "GeminiReader":
        import httpx

        try:
            self.lock = ProcessLock(self.ledger.parent / "gemini-ledger.lock")
        except LockHeld:
            raise OcrError("another process is spending from the Gemini ledger") from None
        try:
            self.http = httpx.Client(timeout=120, headers={"x-goog-api-key": self.key})
        except BaseException:  # __exit__ never runs when __enter__ fails
            self.lock.release()
            self.lock = None
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.http.close()
        finally:
            self.lock.release()
            self.lock = None

    def _post(self, action: str, body: dict) -> dict:
        r = self.http.post(f"{GEMINI_URL}:{action}", json=body)
        if r.status_code != 200:
            raise OcrError(f"gemini {action} HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def read(self, png: bytes, region: str) -> str:
        if self.lock is None:
            raise OcrError("GeminiReader.read outside its with block holds no ledger lock")
        parts = [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png).decode()}},
                 {"text": GEMINI_PROMPT}]
        contents = [{"role": "user", "parts": parts}]
        input_tokens = self._post("countTokens", {"contents": contents})["totalTokens"]
        worst = _micro(input_tokens, GEMINI_MAX_OUTPUT)
        if spent_micro(self.ledger) + worst > GEMINI_CAP_MICRO:
            raise OcrError("gemini cap reached")
        call_id = uuid.uuid4().hex
        _append(self.ledger, {"event": "reserved", "call_id": call_id, "region": region, "model": GEMINI_MODEL,
                              "input_tokens": input_tokens, "micro": worst})
        # Any failure after this point leaves the reservation standing: an unknown outcome is charged at worst.
        data = self._post("generateContent", {"contents": contents, "generationConfig": {
            "temperature": 0, "maxOutputTokens": GEMINI_MAX_OUTPUT, "thinkingConfig": {"thinkingLevel": "minimal"}}})
        usage = data.get("usageMetadata", {})
        out_tokens = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
        _append(self.ledger, {"event": "settled", "call_id": call_id, "usage": usage,
                              "micro": _micro(usage.get("promptTokenCount", input_tokens), out_tokens)})
        # A blocked prompt has no candidate; only a finished one is a read (an image with no text finishes empty).
        candidates = data.get("candidates") or []
        if not candidates:
            raise OcrError(f"gemini blocked {data.get('promptFeedback', {}).get('blockReason')}")
        candidate = candidates[0]
        if candidate.get("finishReason") != "STOP":
            raise OcrError(f"gemini finish {candidate.get('finishReason')}")
        return "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", [])).strip()


# ---------------------------------------------------------------- runs


def cache_path(settings: Settings, source_hash: str) -> Path:
    return settings.data_dir / "ocr" / source_hash / f"{hashlib.sha256(OCR_VERSION.encode()).hexdigest()[:12]}.jsonl"


def rendering_for(settings: Settings, src) -> Path:
    return printed_pdf_path(settings, src["source_hash"]) if src["format"] == "hwp" else Path(src["original_path"])


def load(settings: Settings, source_hash: str) -> list[dict]:
    path = cache_path(settings, source_hash)
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


ANCHOR_MIN = 6  # normalized characters of nearby print text needed to place an image
MERGE_VERSION = "anchor-2"  # placement algorithm; part of the extraction fingerprint


def _anchors(doc, pno: int, bbox: list[float]) -> tuple[str, str]:
    """Normalized print text right above and right below the image: within its column, else anywhere on the
    page, else the end of an earlier page and the start of a later one (a page that is only a picture)."""
    x0, y0, x1, y1 = bbox

    def lines(page, column: bool) -> tuple[list[str], list[str]]:
        rows: dict[int, list] = {}
        for w in page.get_text("words"):
            cy = (w[1] + w[3]) / 2
            if (not column or x0 - 30 <= (w[0] + w[2]) / 2 <= x1 + 30) and not (y0 - 2 < cy < y1 + 2):
                rows.setdefault(round(cy / 6), []).append(w)
        text = {k: norm(" ".join(w[4] for w in sorted(v, key=lambda w: w[0]))) for k, v in rows.items()}
        keys = [k for k in sorted(text) if len(text[k]) >= ANCHOR_MIN]
        return [text[k] for k in keys if rows[k][0][3] <= y0 + 2], [text[k] for k in keys if rows[k][0][1] >= y1 - 2]

    page = doc[pno - 1]
    for column in (True, False):
        above, below = lines(page, column)
        if above or below:
            return (above[-1] if above else ""), (below[0] if below else "")
    up = next((t[-1] for t in (doc[k].get_text().split("\n") for k in range(pno - 2, max(pno - 5, -1), -1))
               for t in [[norm(x) for x in t if len(norm(x)) >= ANCHOR_MIN]] if t), "")
    down = next((t[0] for t in (doc[k].get_text().split("\n") for k in range(pno, min(pno + 3, len(doc))))
                 for t in [[norm(x) for x in t if len(norm(x)) >= ANCHOR_MIN]] if t), "")
    return up, down


def _find(texts: list[str], anchor: str, tail: bool, near: float) -> int | None:
    """Index of the element holding the anchor (its tail or head, longest first); of several, the one whose
    place in the document is nearest the image's (a heading also appears in the contents)."""
    for n in (40, 20, 10):
        piece = anchor[-n:] if tail else anchor[:n]
        if len(piece) < ANCHOR_MIN:
            return None
        hits = [k for k, t in enumerate(texts) if piece in t]
        if hits:
            return min(hits, key=lambda k: abs(k / len(texts) - near))
    return None


def merge(settings: Settings, source_hash: str, raw: list[dict], rendering: Path) -> tuple[list[dict], list[dict], str]:
    """Raw elements with an `image_text` element per finished OCR region, placed after the element holding the
    print text just above the image (or before the one holding the text below). Returns (elements, warnings,
    fingerprint suffix); the suffix is empty when there is nothing to add."""
    import pymupdf

    rows = load(settings, source_hash)
    ready = [r for r in rows if _final(r) and r["text"].strip()]
    warnings = []
    unresolved = sum(not _final(r) for r in rows)
    if unresolved:
        warnings.append({"code": "ocr_unresolved", "count": unresolved})
    if not ready:
        return raw, warnings, ""
    texts = [norm(e["raw_text"]) for e in raw]
    after: dict[int, list] = {}  # insert position -> elements
    unplaced = 0
    with pymupdf.open(rendering) as doc:
        for i, r in enumerate(ready):
            up, down = _anchors(doc, r["page"], r["bbox"])
            near = (r["page"] - 0.5) / len(doc)
            k = _find(texts, up, True, near)
            pos = k + 1 if k is not None else _find(texts, down, False, near)
            if pos is None:
                pos, unplaced = len(raw), unplaced + 1
            before = raw[min(max(pos - 1, 0), len(raw) - 1)]["location"] if raw else {}
            after.setdefault(pos, []).append({
                "path": f"ocr/p{r['page']}/i{i}", "kind": "image_text", "parent": None, "raw_text": r["text"],
                "location": {"format": "image_ocr", "rendering": r["rendering"], "page": r["page"], "bbox": r["bbox"],
                             "engine": r["engine"], "digest": r["digest"],
                             "section_path": before.get("section_path", [])}})
    if unplaced:
        warnings.append({"code": "ocr_unplaced", "count": unplaced})
    out = []
    for k in range(len(raw) + 1):
        out += after.get(k, [])
        if k < len(raw):
            out.append(raw[k])
    # The revision identity covers what was added and where: the same text placed elsewhere is another revision.
    added = sorted([k, e["path"], e["raw_text"], e["location"]] for k, es in after.items() for e in es)
    digest = hashlib.sha256(dumps([MERGE_VERSION, OCR_VERSION, added]).encode()).hexdigest()
    return out, warnings, f"ocr-{digest[:12]}"


def _reasons(local: dict) -> list[str]:
    if "truncated" not in local:  # read before truncation was recorded: not trusted, read again locally
        return ["unrecorded_truncation"]
    return fallback_reasons(local["text"], local["mean_prob"], local["looped"], local["expected"], local["truncated"])


def _final(row: dict) -> bool:
    """Gemini's read, or a local read that passes the current fallback test (thresholds may have moved)."""
    return row["status"] == "gemini" or row["status"] == "local" and not _reasons(row["local"])


def _known(settings: Settings) -> dict[str, dict]:
    """Local reads by image digest across every source: a logo repeated on 40 pages is read once."""
    out = {}
    for path in (settings.data_dir / "ocr").glob(f"*/{cache_path(settings, 'x').name}"):
        for r in map(json.loads, path.read_text(encoding="utf-8").splitlines()):
            if r["digest"] not in out or _final(r):
                out[r["digest"]] = r
    return out


def run(settings: Settings, source_hashes: list[str] | None = None, gemini: bool = True) -> dict:
    """OCR every image region of every parsed source (or the named ones); resumable per region."""
    with open_db(settings.db_path) as conn:
        srcs = [dict(r) for r in conn.execute(
            "SELECT source_hash, format, original_path FROM sources WHERE parse_status = 'parsed' "
            "ORDER BY source_hash")]
    if source_hashes:
        srcs = [s for s in srcs if s["source_hash"] in set(source_hashes)]
    try:  # every cache writer, local-only included, or one run's checkpoint overwrites another's paid reads
        run_lock = ProcessLock(settings.data_dir / "ocr" / "run.lock")
    except LockHeld:
        raise OcrError("another OCR run is writing the cache") from None
    try:
        return _run(settings, srcs, gemini)
    finally:
        run_lock.release()


def _run(settings: Settings, srcs: list[dict], gemini: bool) -> dict:
    from PIL import Image

    known = _known(settings)
    totals = {"sources": 0, "regions": 0, "local": 0, "gemini": 0, "reused": 0, "unresolved": 0, "no_rendering": 0}
    reader = GeminiReader(settings) if gemini else None
    with LocalOCR() as local, (reader or nullcontext()) as remote:
        for src in srcs:
            pdf = rendering_for(settings, src)
            if not pdf.exists():
                totals["no_rendering"] += 1
                continue
            totals["sources"] += 1
            path = cache_path(settings, src["source_hash"])
            rendering = "hancom_print" if src["format"] == "hwp" else "original"
            done = {(r["page"], tuple(r["bbox"])): r for r in load(settings, src["source_hash"])}
            visited: set[tuple] = set()
            rows = []
            for reg in regions(pdf):
                totals["regions"] += 1
                key = (reg["page"], tuple(reg["bbox"]))
                visited.add(key)
                cached = done.get(key)
                # Same place is not same picture: a regenerated rendering can hold other pixels there.
                same = cached if cached and cached["digest"] == reg["digest"] else None
                other = known.get(reg["digest"])
                # A finished read of this picture anywhere beats an unfinished one here: no second paid read.
                prev = next((r for r in (same, other) if r and _final(r)), same or other)
                if prev and _final(prev):
                    rows.append({**prev, "page": reg["page"], "bbox": reg["bbox"], "rendering": rendering})
                    totals["reused"] += 1
                    continue
                row = {"page": reg["page"], "bbox": reg["bbox"], "digest": reg["digest"], "rendering": rendering}
                if prev and "truncated" in prev["local"]:  # a local read that failed the test: no GPU again
                    first = prev["local"]
                else:
                    image = Image.open(io.BytesIO(reg["png"])).convert("RGB")
                    first = {**local.read(image), "expected": round(expected_chars(image), 1)}
                reasons = _reasons(first)
                row.update(engine=LOCAL_ENGINE, text=first["text"], local=first, reasons=reasons, status="local")
                if reasons and remote is not None:
                    try:
                        row.update(engine=GEMINI_MODEL, text=remote.read(reg["png"], reg["digest"]), status="gemini")
                    except OcrError as exc:  # keeps the local text, marked for the next run
                        row.update(status="unresolved", error=str(exc)[:300])
                elif reasons:
                    row["status"] = "unresolved"
                known[reg["digest"]] = row
                totals[row["status"]] += 1
                rows.append(row)
                # A checkpoint keeps the cached rows not reached yet: an interrupted run must not lose paid reads.
                write_jsonl_atomic(path, rows + [r for k, r in done.items() if k not in visited])
            write_jsonl_atomic(path, rows)
            print(dumps({"source_hash": src["source_hash"][:16], "regions": len(rows),
                         "statuses": sorted(r["status"] for r in rows)}), flush=True)
    totals["gemini_spent_usd"] = spent_micro(ledger_path(settings)) / 1_000_000
    return totals
