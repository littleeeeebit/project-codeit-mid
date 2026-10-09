"""Text inside images: PaddleOCR-VL reads every region, gpt-5-mini re-reads only the regions it fails on.

HWP regions are the pictures embedded in the file (BinData), and their text goes where pyhwp's walk met the
picture. PDF regions are the original's raster images, placed next to the text around them. The fallback test uses
only signals available without a truth text (docs/rag/preprocessing.md, "Text inside images"). gpt-5-mini is paid
with OPENAI_API_KEY from the local environment, through the budget gateway into the shared ledger.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import random
import struct
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

from ..settings import Settings, read_api_key
from ..storage.postgres import host_path
from ..storage.store import LockHeld, ProcessLock, dumps, open_db, utcnow, write_jsonl_atomic, write_text_atomic
from .fidelity import IMAGE_MIN_AREA, norm
from .ingestion import parse_hwp, printed_pdf_path

LOCAL_MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"
LOCAL_REVISION = "c5630abae1d940eafe0697512a0325494b02ab42"
LOCAL_ENGINE = "paddleocr-vl-1.6"
REMOTE_MODEL = "gpt-5-mini"
REMOTE_PURPOSE = "ocr"  # a ledger envelope the owner funds with set-envelopes
REMOTE_MEMBER = "ocr"
REMOTE_MAX_OUTPUT = 4096  # completion tokens, reasoning included, so it bounds the worst case
# Image input never bills more than this, whatever the size: patch-priced models stop at 1,536 patches times the
# largest multiplier OpenAI lists (2.46), tile-priced ones at 85 + 170 x 16 tiles.
IMAGE_TOKENS_MAX = 4000
REMOTE_PROMPT = ("Transcribe all text in this image exactly as written, in reading order. Keep Korean as Korean. "
                 "Output only the text: no commentary, no markdown, no translation.")
OCR_VERSION = f"{LOCAL_ENGINE}@{LOCAL_REVISION[:8]}+{REMOTE_MODEL}-fallback-1"
# The cache of the Hancom-print regions with their Gemini fallback (2026-09-30): the coverage baseline (463
# distinct images) and the reference reads of the sample. Read only; no Gemini call is made any more.
PRINT_VERSION = f"{LOCAL_ENGINE}@{LOCAL_REVISION[:8]}+gemini-3.5-flash-lite-fallback-1"
DPI = 150
MAX_SIDE = 2048  # long side of an embedded picture as OCR reads it; metafiles are rendered at this size
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


class OcrStop(OcrError):
    """The paid reads stop here: a budget refusal, unknown billing or lost gateway. Finished reads stay cached."""


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
    """Raster images covering at least IMAGE_MIN_AREA of their page, rendered at DPI as PNG. Boxes are in unrotated
    page coordinates, like the words the anchors come from; only the render clip is rotated."""
    import pymupdf

    out, seen = [], set()
    with pymupdf.open(pdf) as doc:
        for page in doc:
            for info in page.get_image_info():
                rect = pymupdf.Rect(info["bbox"]) & (page.rect * page.derotation_matrix)
                key = (page.number, *(round(v) for v in rect))
                if rect.is_empty or key in seen or abs(rect) / abs(page.rect) < IMAGE_MIN_AREA:
                    continue
                seen.add(key)
                png = page.get_pixmap(clip=rect * page.rotation_matrix, dpi=DPI).tobytes("png")
                out.append({"page": page.number + 1, "bbox": [round(v, 1) for v in rect],
                            "digest": hashlib.sha256(png).hexdigest()[:24], "png": png})
    return out


def _placeable(wmf: bytes) -> bytes:
    """A standard WMF behind the Aldus placeable header Pillow needs. Its box is the first SETWINDOWORG and
    SETWINDOWEXT, in logical units at 72 to the inch: only the aspect matters, the render is scaled."""
    org, ext, pos = (0, 0), None, struct.unpack_from("<H", wmf, 2)[0] * 2
    while ext is None and pos + 10 <= len(wmf):
        size, func = struct.unpack_from("<IH", wmf, pos)
        if func in (0x020B, 0x020C):  # parameters y, x
            xy = struct.unpack_from("<hh", wmf, pos + 6)[::-1]
            org, ext = (xy, ext) if func == 0x020B else (org, xy)
        if size < 3:
            break
        pos += size * 2
    if ext is None or min(ext) <= 0:
        raise ValueError("WMF without a window extent")
    head = struct.pack("<IHhhhhHI", 0x9AC6CDD7, 0, org[0], org[1], org[0] + ext[0], org[1] + ext[1], 72, 0)
    checksum = 0
    for (word,) in struct.iter_unpack("<H", head):
        checksum ^= word
    return head + struct.pack("<H", checksum) + wmf


def to_png(data: bytes) -> bytes:
    """An embedded picture as OCR reads it: a metafile (WMF, EMF) rendered to a bitmap, transparency on white, the
    long side at most MAX_SIDE pixels. Pillow renders metafiles only on Windows; elsewhere they raise OSError."""
    from PIL import Image

    if data[:2] in (b"\x01\x00", b"\x02\x00") and data[2:4] == b"\x09\x00":  # WMF without the placeable header
        data = _placeable(data)
    image = Image.open(io.BytesIO(data))
    if image.format == "WMF":  # Pillow's name for WMF and EMF alike
        scale = MAX_SIDE / max(image.size)
        dpi = image.info["dpi"]
        image.load(dpi=(dpi[0] * scale, dpi[1] * scale) if isinstance(dpi, tuple) else dpi * scale)
    image = image.convert("RGBA")
    image = Image.alpha_composite(Image.new("RGBA", image.size, "white"), image).convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def hwp_regions(settings: Settings, original: Path) -> tuple[list[dict], list[dict]] | None:
    """The pictures pyhwp's walk placed that cover at least IMAGE_MIN_AREA of their page, each embedded image once:
    (regions {bindata, digest, png}, unavailable {bindata, reason}). None when pyhwp cannot walk the file: the
    loader that parses it then gives no picture positions."""
    from hwp5.xmlmodel import Hwp5File
    from PIL import Image

    raw, _, reason = parse_hwp(settings, original)
    if reason:
        return None
    wanted = list(dict.fromkeys(e["location"]["bindata"] for e in raw if e["kind"] == "picture"
                                and e["location"]["object"] == "picture" and e["location"]["share"] >= IMAGE_MIN_AREA))
    f = Hwp5File(str(original))
    streams = {n.lower(): n for n in f["BinData"]} if "BinData" in f else {}
    out, unavailable = [], []
    for name in wanted:
        stream = streams.get((name or "").lower())
        if stream is None:  # a linked file, or a reference to nothing
            unavailable.append({"bindata": name, "reason": "missing"})
            continue
        try:
            png = to_png(f["BinData"][stream].open().read())
        except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError):
            unavailable.append({"bindata": name, "reason": "format"})
            continue
        out.append({"bindata": name, "digest": hashlib.sha256(png).hexdigest()[:24], "png": png})
    return out, unavailable


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


# ---------------------------------------------------------------- gpt-5-mini fallback


def remote_input_tokens(settings: Settings) -> int:
    """Reserved input: the prompt counted like any request, plus the most an image can bill."""
    from ..gateway import generation

    return generation.count_request_tokens([{"role": "user", "content": REMOTE_PROMPT}], {"type": "text"},
                                           settings.framing_margin_tokens) + IMAGE_TOKENS_MAX


class RemoteReader:
    """Owns, for one `with` block, the paid gateway of the ledger database and one OpenAI client on
    OPENAI_API_KEY (the owner's personal key in the local environment); both are released on exit. Every call is
    reserved at worst case before dispatch and settled on its reported usage. A call whose outcome or usage is
    unknown is marked unknown and stays charged at its reservation; the run then stops."""

    def __init__(self, settings: Settings, transport=None, job: str = "ocr"):
        self.settings = settings
        self.job = job
        self.key = read_api_key("OPENAI_API_KEY") if transport is None else None
        if transport is None and not self.key:
            raise OcrError("OPENAI_API_KEY is not configured")
        self.transport = transport
        self.lock = None

    def __enter__(self) -> "RemoteReader":
        from ..gateway import budget, generation
        from ..retrieval import dense
        from ..storage.postgres import gateway_lock

        try:
            self.lock = gateway_lock(self.settings.db_path)
        except LockHeld:
            raise OcrError("another process owns the paid gateway of this ledger") from None
        try:
            budget.ensure_generation_rate(self.settings.db_path, REMOTE_MODEL, REMOTE_MEMBER)
            self.request_id = dense.ensure_job_request(self.settings, REMOTE_MEMBER, f"{self.job}:{OCR_VERSION}",
                                                       {"job": self.job, "version": OCR_VERSION})
            if self.transport is None:
                self.transport = generation.OpenAITransport(self.key, self.settings.request_timeout_seconds,
                                                            owner_check=getattr(self.lock, "check", None))
        except BaseException:  # __exit__ never runs when __enter__ fails
            self.lock.release()
            self.lock = None
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.transport.close()
        finally:
            self.lock.release()
            self.lock = None

    def read(self, png: bytes, region: str) -> tuple[str, int]:
        """(text, settled micro-USD). OcrError leaves the region unresolved; OcrStop ends the paid reads."""
        from ..gateway import budget, generation

        if self.lock is None:
            raise OcrError("RemoteReader.read outside its with block owns no gateway")
        db = self.settings.db_path
        admission = budget.reserve(db, request_id=self.request_id, member_id=REMOTE_MEMBER, stage=f"ocr:{region}",
                                   purpose=REMOTE_PURPOSE, model=REMOTE_MODEL,
                                   input_tokens=remote_input_tokens(self.settings),
                                   max_output_tokens=REMOTE_MAX_OUTPUT, count_method=generation.COUNT_METHOD)
        if not admission["admitted"]:
            raise OcrStop(f"budget refused: {admission['reason']}")
        aid = admission["attempt_id"]
        try:
            budget.mark_dispatching(db, aid)
        except budget.DispatchRefused as exc:
            raise OcrStop(f"dispatch refused: {exc}") from None
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        messages = [{"role": "user", "content": [{"type": "text", "text": REMOTE_PROMPT},
                                                 {"type": "image_url", "image_url": {"url": url, "detail": "high"}}]}]
        try:
            response = self.transport.chat(model=REMOTE_MODEL, messages=messages, response_format={"type": "text"},
                                           max_completion_tokens=REMOTE_MAX_OUTPUT, reasoning_effort="minimal")
        except generation.ProviderError as exc:
            if exc.pre_execution:
                budget.release(db, aid, "provider rejected before execution", confirmed_pre_execution=True)
                raise OcrError(f"{REMOTE_MODEL} rejected: {str(exc)[:200]}") from None
            budget.mark_unknown(db, aid, str(exc))
            raise OcrStop("unknown billing: reconcile the attempt before reading again") from None
        except Exception as exc:  # noqa: BLE001 - whatever failed after dispatch may have been billed
            budget.mark_unknown(db, aid, type(exc).__name__)
            raise OcrStop("unknown billing: reconcile the attempt before reading again") from None
        if response.usage is None:
            budget.mark_unknown(db, aid, "response missing usage")
            raise OcrStop("unknown billing: the response reported no usage")
        settlement = budget.settle(db, aid, response.usage, response.response_id)
        if settlement.get("overrun"):
            raise OcrStop("settled above the reservation; the ledger is frozen for inspection")
        # Only a finished answer is a read (an image with no text finishes empty).
        if response.refusal or response.finish_reason != "stop" or response.content is None:
            raise OcrError(f"{REMOTE_MODEL} finish {response.finish_reason}" + (" (refused)" if response.refusal else ""))
        return response.content.strip(), settlement["settled_micro_usd"]


# ---------------------------------------------------------------- runs


def cache_path(settings: Settings, source_hash: str, version: str = OCR_VERSION) -> Path:
    return settings.data_dir / "ocr" / source_hash / f"{hashlib.sha256(version.encode()).hexdigest()[:12]}.jsonl"


def load(settings: Settings, source_hash: str, version: str = OCR_VERSION) -> list[dict]:
    path = cache_path(settings, source_hash, version)
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _key(row: dict) -> tuple:
    """A region's place: the embedded image (HWP) or the page and box (PDF)."""
    return ("bindata", row["bindata"]) if "bindata" in row else (row["page"], tuple(row["bbox"]))


def _place(region: dict) -> dict:
    return {k: region[k] for k in ("bindata", "page", "bbox") if k in region}


ANCHOR_MIN = 6  # normalized characters of nearby text needed to place a PDF image
MERGE_VERSION = "anchor-5+marker-1"  # placement algorithms; part of the extraction fingerprint


def merge_hwp(settings: Settings, source_hash: str, raw: list[dict]) -> tuple[list[dict], list[dict], str]:
    """pyhwp's elements with each picture marker replaced by the image_text of its picture's finished read, or
    removed. Pictures below IMAGE_MIN_AREA of the page are not read; OLE objects, drawings and images that cannot
    be decoded are counted as `ocr_unavailable`. Returns (elements, warnings, fingerprint suffix); the suffix is
    empty when nothing was added."""
    rows = {r["bindata"]: r for r in load(settings, source_hash) if "bindata" in r}
    out, added, unavailable, unresolved = [], [], Counter(), 0
    for e in raw:
        if e["kind"] != "picture":
            out.append(e)
            continue
        loc = e["location"]
        row = rows.get(loc.get("bindata"))
        if loc["share"] < IMAGE_MIN_AREA or loc["object"] == "picture" and row is None:
            continue  # too small to hold text, or not read yet
        if loc["object"] != "picture" or row["status"] == "unavailable":
            unavailable[loc["object"] if loc["object"] != "picture" else row["reason"]] += 1
            continue
        if not _final(row):
            unresolved += 1
            continue
        if not row["text"].strip():
            continue
        out.append({"path": e["path"], "kind": "image_text", "parent": None, "raw_text": row["text"],
                    "location": {"format": "image_ocr", "rendering": "hwp_bindata", "bindata": loc["bindata"],
                                 "section": loc["section"], "path": e["path"], "engine": row["engine"],
                                 "digest": row["digest"], "section_path": loc["section_path"]}})
        added.append([len(out) - 1, e["path"], row["text"], out[-1]["location"]])
    warnings = []
    if unavailable:
        warnings.append({"code": "ocr_unavailable", "count": sum(unavailable.values()),
                         "by": dict(sorted(unavailable.items()))})
    if unresolved:
        warnings.append({"code": "ocr_unresolved", "count": unresolved})
    if not added:
        return out, warnings, ""
    # The revision identity covers what was added and where: the same text placed elsewhere is another revision.
    digest = hashlib.sha256(dumps([MERGE_VERSION, OCR_VERSION, added]).encode()).hexdigest()
    return out, warnings, f"ocr-{digest[:12]}"


def _anchors(doc, pno: int, bbox: list[float]) -> tuple[tuple[str, int], tuple[str, int]]:
    """(text, page) of the normalized text right above and right below the image: within its column, else
    anywhere on the page, else the end of an earlier page and the start of a later one (a page that is only a
    picture)."""
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
            return ((above[-1] if above else ""), pno), ((below[0] if below else ""), pno)

    def text(k: int) -> list[str]:
        return [norm(x) for x in doc[k].get_text().split("\n") if len(norm(x)) >= ANCHOR_MIN]

    up = next(((t[-1], k + 1) for k in range(pno - 2, max(pno - 5, -1), -1) for t in [text(k)] if t), ("", 0))
    down = next(((t[0], k + 1) for k in range(pno, min(pno + 3, len(doc))) for t in [text(k)] if t), ("", 0))
    return up, down


def _find(texts: list[str], pages: list[int | None], anchor: tuple[str, int], tail: bool) -> int | None:
    """Index of the element holding the anchor (its tail or head, longest first). It must be the only one on the
    anchor's page: elements keep no box, so two hits there are ambiguous and give none."""
    text, page = anchor
    for n in (40, 20, 10):
        piece = text[-n:] if tail else text[:n]
        if len(piece) < ANCHOR_MIN:
            return None
        hits = [k for k, t in enumerate(texts) if piece in t and pages[k] == page]
        if len(hits) > 1:
            return None
        if hits:
            return hits[0]
    return None


def merge(settings: Settings, source_hash: str, raw: list[dict], rendering: Path) -> tuple[list[dict], list[dict], str]:
    """PDF elements with an `image_text` element per finished OCR region of the original, placed after the element
    holding the text just above the image (or before the one holding the text below). Returns (elements, warnings,
    fingerprint suffix); the suffix is empty when there is nothing to add."""
    import pymupdf

    # Only reads of a region this rendering still has, same pixels at the same place: a reprint can move pages.
    current = {(r["page"], tuple(r["bbox"]), r["digest"]) for r in regions(rendering)}
    rows = [r for r in load(settings, source_hash) if "page" in r]
    live = [r for r in rows if (r["page"], tuple(r["bbox"]), r["digest"]) in current]
    ready = [r for r in live if _final(r) and r["text"].strip()]
    warnings = []
    for code, count in (("ocr_stale", len(rows) - len(live)), ("ocr_unresolved", sum(not _final(r) for r in live))):
        if count:
            warnings.append({"code": code, "count": count})
    if not ready:
        return raw, warnings, ""
    texts = [norm(e["raw_text"]) for e in raw]
    pages = [e["location"].get("page") for e in raw]
    after: dict[int, list] = {}  # insert position -> elements
    unplaced = 0
    with pymupdf.open(rendering) as doc:
        for i, r in enumerate(ready):
            up, down = _anchors(doc, r["page"], r["bbox"])
            k = _find(texts, pages, up, True)
            pos = k + 1 if k is not None else _find(texts, pages, down, False)
            # Unplaced (appended at the end) or before the first element: no section is known for it.
            before = raw[pos - 1]["location"] if pos else {}
            if pos is None:
                pos, unplaced = len(raw), unplaced + 1
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
    """gpt-5-mini's read, or a local read that passes the current fallback test (thresholds may have moved)."""
    return row["status"] == "remote" or row["status"] == "local" and not _reasons(row["local"])


def _known(settings: Settings) -> dict[str, dict]:
    """Reads by image digest across every source: a logo repeated in 40 documents is read once."""
    out = {}
    for path in (settings.data_dir / "ocr").glob(f"*/{cache_path(settings, 'x').name}"):
        for r in map(json.loads, path.read_text(encoding="utf-8").splitlines()):
            if "digest" in r and (r["digest"] not in out or _final(r)):
                out[r["digest"]] = r
    return out


def run(settings: Settings, source_hashes: list[str] | None = None, remote: bool = True,
        ledger: Settings | None = None) -> dict:
    """OCR every image region of every parsed source (or the named ones); resumable per region. Paid reads go to
    `ledger`'s database (the shared ledger) when given, else to the corpus database."""
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
        return _run(settings, srcs, remote, ledger)
    finally:
        run_lock.release()


def _source_regions(settings: Settings, src) -> tuple[list[dict], list[dict], str] | None:
    """(regions, unavailable, rendering name) of a source; None for an HWP pyhwp cannot walk."""
    original = host_path(src["original_path"])
    if src["format"] != "hwp":
        return regions(original), [], "original"
    found = hwp_regions(settings, original)
    return None if found is None else (*found, "hwp_bindata")


def _run(settings: Settings, srcs: list[dict], remote_reads: bool, ledger: Settings | None) -> dict:
    from PIL import Image

    known = _known(settings)
    totals = {"sources": 0, "regions": 0, "local": 0, "remote": 0, "reused": 0, "unresolved": 0, "unavailable": 0,
              "no_structure": 0, "remote_micro_usd": 0}
    reader = RemoteReader(ledger or settings) if remote_reads else None
    with LocalOCR() as local, (reader or nullcontext()) as remote:
        for src in srcs:
            found = _source_regions(settings, src)
            if found is None:  # parsed by the HWP loader: no picture positions to place text at
                totals["no_structure"] += 1
                continue
            found, unavailable, rendering = found
            totals["sources"] += 1
            totals["unavailable"] += len(unavailable)
            path = cache_path(settings, src["source_hash"])
            done = {_key(r): r for r in load(settings, src["source_hash"]) if r["status"] != "unavailable"}
            visited: set[tuple] = set()
            rows = [{**u, "rendering": rendering, "status": "unavailable"} for u in unavailable]
            stop = None
            for reg in found:
                totals["regions"] += 1
                key = _key(reg)
                visited.add(key)
                cached = done.get(key)
                # Same place is not same picture: a regenerated rendering can hold other pixels there.
                same = cached if cached and cached["digest"] == reg["digest"] else None
                other = known.get(reg["digest"])
                # A finished read of this picture anywhere beats an unfinished one here: no second paid read.
                prev = next((r for r in (same, other) if r and _final(r)), same or other)
                if prev and _final(prev):
                    kept = {k: v for k, v in prev.items() if k not in ("bindata", "page", "bbox")}
                    rows.append({**kept, **_place(reg), "rendering": rendering})
                    totals["reused"] += 1
                    continue
                row = {**_place(reg), "digest": reg["digest"], "rendering": rendering}
                if prev and "truncated" in prev["local"]:  # a local read that failed the test: no GPU again
                    first = prev["local"]
                else:
                    image = Image.open(io.BytesIO(reg["png"])).convert("RGB")
                    first = {**local.read(image), "expected": round(expected_chars(image), 1)}
                reasons = _reasons(first)
                row.update(engine=LOCAL_ENGINE, text=first["text"], local=first, reasons=reasons, status="local")
                if reasons and remote is not None and stop is None:
                    try:
                        text, micro = remote.read(reg["png"], reg["digest"])
                        row.update(engine=REMOTE_MODEL, text=text, status="remote")
                        totals["remote_micro_usd"] += micro
                    except OcrError as exc:  # keeps the local text, marked for the next run
                        row.update(status="unresolved", error=str(exc)[:300])
                        stop = exc if isinstance(exc, OcrStop) else None
                elif reasons:
                    row["status"] = "unresolved"
                known[reg["digest"]] = row
                totals[row["status"]] += 1
                rows.append(row)
                # A checkpoint keeps the cached rows not reached yet: an interrupted run must not lose paid reads.
                write_jsonl_atomic(path, rows + [r for k, r in done.items() if k not in visited])
                if stop is not None:
                    raise stop
            write_jsonl_atomic(path, rows)
            print(dumps({"source_hash": src["source_hash"][:16], "regions": len(rows),
                         "statuses": sorted(r["status"] for r in rows)}), flush=True)
    totals["remote_spent_usd"] = totals.pop("remote_micro_usd") / 1_000_000
    return totals


# ---------------------------------------------------------------- reports


def coverage(settings: Settings) -> dict:
    """Per document, the embedded images read against the distinct images of its Hancom print (the PRINT_VERSION
    cache: 463 across the corpus, PDF originals included); PDF regions come from the original both times. The
    `unavailable` counts are the latest ingest's. Written to <data>/ocr/coverage.json."""
    with open_db(settings.db_path) as conn:
        srcs = [dict(r) for r in conn.execute(
            "SELECT s.source_hash, s.format, s.parse_status, s.warnings_json, MIN(d.filename) AS filename "
            "FROM sources s JOIN documents d ON d.active_source_hash = s.source_hash "
            "GROUP BY s.source_hash, s.format, s.parse_status, s.warnings_json ORDER BY filename")]
    docs, union = [], {"print": set(), "embedded": set(), "read": set()}
    for src in srcs:
        h = src["source_hash"]
        base = {r["digest"] for r in load(settings, h, PRINT_VERSION)}
        rows = [r for r in load(settings, h) if r["status"] != "unavailable"]
        images = {r["digest"] for r in rows}
        read = {r["digest"] for r in rows if _final(r)}
        warnings = {w["code"]: w for w in json.loads(src["warnings_json"] or "[]")}
        docs.append({"filename": src["filename"], "source_hash": h[:16], "format": src["format"],
                     "print_images": len(base), "embedded_images": len(images), "read": len(read),
                     "with_text": len({r["digest"] for r in rows if _final(r) and r["text"].strip()}),
                     "unresolved": len(images - read),
                     "unavailable": warnings.get("ocr_unavailable", {}).get("by", {}),
                     "no_structure": "pyhwp_failed" in warnings})
        union["print"] |= base
        union["embedded"] |= images
        union["read"] |= read
    unavailable = Counter()
    for d in docs:
        unavailable.update(d["unavailable"])
    report = {"generated_at": utcnow(), "ocr_version": OCR_VERSION, "baseline_version": PRINT_VERSION,
              "totals": {"documents": len(docs), "print_images": len(union["print"]),
                         "embedded_images": len(union["embedded"]), "read": len(union["read"]),
                         "unavailable": dict(sorted(unavailable.items())),
                         "no_structure": [d["filename"] for d in docs if d["no_structure"]]},
              "documents": docs}
    write_text_atomic(settings.data_dir / "ocr" / "coverage.json", json.dumps(report, ensure_ascii=False, indent=1))
    return report


def char_f1(text: str, reference: str) -> float:
    """Order-free character F1 of the normalized texts (the calibration's measure of a read)."""
    a, b = Counter(norm(text)), Counter(norm(reference))
    if not a and not b:
        return 1.0
    overlap = sum((a & b).values())
    return 0.0 if not overlap else round(2 * overlap / (sum(a.values()) + sum(b.values())), 4)


SAMPLE_SEED = 20261009


def sample(settings: Settings, n: int = 50, transport=None, ledger: Settings | None = None) -> dict:
    """Paid, reference only: gpt-5-mini reads `n` Hancom-print and PDF regions that Gemini read on 2026-09-30 (a
    seeded draw from the PRINT_VERSION cache, whose 50-region calibration list was never recorded), and the report
    gives the settled cost per image and the character F1 against Gemini's cached read. It makes no Gemini call and
    writes nothing to the OCR cache. The projection prices the regions the current cache leaves unresolved."""
    name = cache_path(settings, "x", PRINT_VERSION).name
    pool: dict[str, tuple[str, dict]] = {}
    for path in sorted((settings.data_dir / "ocr").glob(f"*/{name}")):
        for r in map(json.loads, path.read_text(encoding="utf-8").splitlines()):
            if r["status"] == "gemini" and r["digest"] not in pool:
                pool[r["digest"]] = (path.parent.name, r)
    picked = random.Random(SAMPLE_SEED).sample(sorted(pool), min(n, len(pool)))
    with open_db(settings.db_path) as conn:
        originals = {r["source_hash"]: r["original_path"] for r in conn.execute(
            "SELECT source_hash, original_path FROM sources")}
    pngs = {}
    for h in sorted({pool[d][0] for d in picked}):
        rendering = {pool[d][1]["rendering"] for d in picked if pool[d][0] == h}.pop()
        pdf = printed_pdf_path(settings, h) if rendering == "hancom_print" else host_path(originals[h])
        pngs.update({r["digest"]: r["png"] for r in regions(pdf) if r["digest"] in picked})
    results, stopped = [], None
    ledger = ledger or settings
    with RemoteReader(ledger, transport, job="ocr-sample") as reader:
        for d in picked:
            h, row = pool[d]
            item = {"digest": d, "source_hash": h[:16], "page": row["page"], "rendering": row["rendering"],
                    "local_f1": char_f1(row["local"]["text"], row["text"])}
            if d not in pngs:
                results.append({**item, "error": "region no longer in its rendering"})
                continue
            try:
                text, _ = reader.read(pngs[d], d)
                # Both texts are kept: the Gemini reference itself invents text on illegible screenshots.
                results.append({**item, "f1": char_f1(text, row["text"]), "chars": len(norm(text)),
                                "reference_chars": len(norm(row["text"])), "text": text, "reference": row["text"]})
            except OcrStop as exc:
                stopped = str(exc)
                break
            except OcrError as exc:
                results.append({**item, "error": str(exc)[:200]})
        request_id = reader.request_id
    with open_db(ledger.db_path) as conn:
        attempts = {r["stage"]: dict(r) for r in conn.execute(
            "SELECT stage, state, reserved_micro_usd, settled_micro_usd, raw_usage_json FROM attempts "
            "WHERE request_id = ? ORDER BY created_at", (request_id,))}
    for item in results:
        a = attempts.get(f"ocr:{item['digest']}")
        if a:
            usage = json.loads(a["raw_usage_json"] or "{}")
            item.update(state=a["state"], settled_micro_usd=a["settled_micro_usd"],
                        reserved_micro_usd=a["reserved_micro_usd"], prompt_tokens=usage.get("prompt_tokens"),
                        completion_tokens=usage.get("completion_tokens"))
    settled = [i["settled_micro_usd"] for i in results if i.get("state") == "settled"]
    scored = [i["f1"] for i in results if "f1" in i]
    flagged = {r["digest"] for p in (settings.data_dir / "ocr").glob(f"*/{cache_path(settings, 'x').name}")
               for r in map(json.loads, p.read_text(encoding="utf-8").splitlines()) if r["status"] == "unresolved"}
    per_image = sum(settled) / len(settled) if settled else None
    report = {
        "generated_at": utcnow(), "model": REMOTE_MODEL, "seed": SAMPLE_SEED, "requested": n, "pool": len(pool),
        "read": len(scored), "errors": sum("error" in i for i in results), "stopped": stopped,
        "settled_usd": sum(settled) / 1e6, "per_image_usd": None if per_image is None else per_image / 1e6,
        "max_per_image_usd": max(settled) / 1e6 if settled else None,
        "f1_mean": round(sum(scored) / len(scored), 4) if scored else None,
        "f1_below_0_8": sum(f < 0.8 for f in scored),
        "local_f1_mean": round(sum(i["local_f1"] for i in results) / len(results), 4) if results else None,
        "unresolved_distinct_images": len(flagged),
        "projected_usd": None if per_image is None else round(per_image * len(flagged) / 1e6, 4),
        "regions": results}
    write_text_atomic(settings.data_dir / "ocr" / f"sample-{REMOTE_MODEL}.json",
                      json.dumps(report, ensure_ascii=False, indent=1))
    return {k: v for k, v in report.items() if k != "regions"}
