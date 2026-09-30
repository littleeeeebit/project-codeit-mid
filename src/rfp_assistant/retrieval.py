"""One versioned Kiwi analyzer for indexing and queries; scoped BM25, exact requirement codes and evidence packing."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from rank_bm25 import BM25Okapi

from . import chunking
from .contracts import DocRef, EvidenceUnit, RetrievalResult
from .ingestion import CODE_RE, load_elements, nfc
from .settings import Settings
from .store import dumps, get_app_setting, open_db, read_jsonl, set_app_setting, tx, utcnow, write_jsonl_atomic, \
    write_text_atomic

ANALYZER_VERSION = "kiwi-bm25-1"
KEEP_TAGS = {"NNG", "NNP", "NNB", "NR", "NP", "SL", "SN", "SH", "XR", "VV", "VA", "XPN"}
NEGATIONS = {"않", "안", "못", "없", "아니", "불가", "금지"}
# Small reviewed domain dictionary; extend only from observed query failures.
USER_WORDS = ["제안요청서", "요구사항", "하자보수", "하자담보", "부가가치세", "공동수급", "분담이행", "공동이행",
              "입찰참가자격", "기술평가", "가격평가", "지체상금", "사업수행계획서", "산출물", "유지관리", "정보시스템"]
BM25_TOP_K = 20


class RetrievalError(RuntimeError):
    pass


class Analyzer:
    """Kiwi is loaded once and shared; tokenize calls are serialized."""

    def __init__(self) -> None:
        from kiwipiepy import Kiwi

        self._kiwi = Kiwi()
        for w in USER_WORDS:
            self._kiwi.add_user_word(w, "NNG")
        self._lock = threading.Lock()  # ponytail: one global lock; per-thread Kiwi instances if query volume grows
        self.version = f"{ANALYZER_VERSION}:kiwipiepy-{metadata.version('kiwipiepy')}"

    def tokens(self, text: str) -> list[str]:
        text = nfc(text)
        out = [f"code:{c}" for c in CODE_RE.findall(text)]
        with self._lock:
            toks = self._kiwi.tokenize(text)
        for t in toks:
            if t.tag in KEEP_TAGS or t.form in NEGATIONS:
                out.append(t.form.lower())
        return out


def analyzer_fingerprint(analyzer: Analyzer) -> str:
    info = {"analyzer": analyzer.version, "words": USER_WORDS, "tags": sorted(KEEP_TAGS)}
    return hashlib.sha256(json.dumps(info, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- index build


def build_keyword_index(settings: Settings, analyzer: Analyzer, include_unreviewed: bool = False) -> dict:
    allowed = ("auto_verified", "sample_checked", "reviewed") + (
        ("unreviewed", "auto_flagged") if include_unreviewed else ())
    with open_db(settings.db_path) as conn:
        sources = [dict(r) for r in conn.execute(
            f"SELECT source_hash, active_extraction_id, review_status FROM sources WHERE parse_status = 'parsed' "
            f"AND review_status IN ({','.join('?' * len(allowed))}) ORDER BY source_hash", allowed)]
    if not sources:
        raise RetrievalError("no parsed sources match the review policy; record reviews or pass --include-unreviewed")
    config = {"chunker": chunking.chunker_fingerprint(), "analyzer": analyzer_fingerprint(analyzer),
              "review_scope": "reviewed_only" if not include_unreviewed else "includes_unreviewed",
              "extractions": [s["active_extraction_id"] for s in sources]}
    source_set_hash = hashlib.sha256("".join(s["source_hash"] for s in sources).encode()).hexdigest()
    version = hashlib.sha256(dumps(config).encode()).hexdigest()[:16]
    final_dir = settings.data_dir / "indexes" / version
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT state FROM indexes WHERE index_version = ?", (version,)).fetchone()
    if row and row["state"] == "ready" and final_dir.exists():
        with open_db(settings.db_path) as conn, tx(conn, immediate=True):
            set_app_setting(conn, "active_index", version)
        return {"index_version": version, "reused": True}

    chunks, inventory = [], []
    for s in sources:
        c, inv = chunking.build_chunks(load_elements(settings, s["active_extraction_id"]), s["active_extraction_id"])
        chunks += c
        inventory += [{**r, "extraction_id": s["active_extraction_id"]} for r in inv]
    tokens = [{"chunk_id": c["chunk_id"], "tokens": analyzer.tokens(c["payload"])} for c in chunks]

    tmp = settings.data_dir / "indexes" / f".tmp-{uuid.uuid4().hex}"
    try:
        write_jsonl_atomic(tmp / "chunks.jsonl", chunks)
        write_jsonl_atomic(tmp / "tokens.jsonl", tokens)
        write_jsonl_atomic(tmp / "requirements.jsonl", inventory)
        files = {n: hashlib.sha256((tmp / n).read_bytes()).hexdigest()
                 for n in ("chunks.jsonl", "tokens.jsonl", "requirements.jsonl")}
        manifest = {"index_version": version, "created_at": utcnow(), "source_set_hash": source_set_hash,
                    "sources": sources, "config": config, "chunk_count": len(chunks), "files": files,
                    "row_order": "chunks.jsonl line order"}
        write_text_atomic(tmp / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        if final_dir.exists():
            shutil.rmtree(final_dir)  # a directory without a ready row is a failed earlier build
        tmp.rename(final_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    manifest_path = final_dir / "manifest.json"
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("DELETE FROM chunks WHERE index_version = ?", (version,))
        conn.execute("DELETE FROM requirements WHERE index_version = ?", (version,))
        conn.execute("DELETE FROM indexes WHERE index_version = ?", (version,))
        conn.execute(
            "INSERT INTO indexes(index_version, manifest_path, manifest_hash, source_set_hash, config_json, state, "
            "created_at) VALUES (?, ?, ?, ?, ?, 'ready', ?)",
            (version, str(manifest_path), manifest_hash, source_set_hash, dumps(config), utcnow()),
        )
        conn.executemany(
            "INSERT INTO chunks(index_version, chunk_id, extraction_id, row_order, spans_json, payload, token_count, "
            "chunk_type, requirement_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(version, c["chunk_id"], c["extraction_id"], i, dumps(c["spans"]), c["payload"], c["token_count"],
              c["chunk_type"], c["requirement_key"]) for i, c in enumerate(chunks)],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO requirements(index_version, extraction_id, requirement_key, source_form, kind, "
            "element_id, name) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(version, r["extraction_id"], r["requirement_key"], r["source_form"], r["kind"], r["element_id"],
              r["name"]) for r in inventory],
        )
        set_app_setting(conn, "active_index", version)
    return {"index_version": version, "reused": False, "chunks": len(chunks), "requirements": len(inventory),
            "review_scope": config["review_scope"], "sources": len(sources)}


# ---------------------------------------------------------------- loaded index


@dataclass
class KeywordIndex:
    version: str
    review_scope: str
    chunks: list[dict]
    bm25: BM25Okapi
    rows_by_extraction: dict[str, list[int]]
    elements: dict[tuple[str, str], dict]

    @classmethod
    def load(cls, settings: Settings, version: str | None = None) -> "KeywordIndex":
        with open_db(settings.db_path) as conn:
            version = version or get_app_setting(conn, "active_index")
            row = conn.execute("SELECT * FROM indexes WHERE index_version = ?", (version,)).fetchone() if version else None
        if row is None or row["state"] != "ready":
            raise RetrievalError("no ready keyword index; run build-keyword")
        manifest_path = Path(row["manifest_path"])
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != row["manifest_hash"]:
            raise RetrievalError("index manifest hash mismatch")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, digest in manifest["files"].items():
            if hashlib.sha256((manifest_path.parent / name).read_bytes()).hexdigest() != digest:
                raise RetrievalError(f"index file hash mismatch: {name}")
        chunks = read_jsonl(manifest_path.parent / "chunks.jsonl")
        tokens = read_jsonl(manifest_path.parent / "tokens.jsonl")
        if [t["chunk_id"] for t in tokens] != [c["chunk_id"] for c in chunks]:
            raise RetrievalError("token rows do not match chunk rows")
        rows: dict[str, list[int]] = {}
        for i, c in enumerate(chunks):
            rows.setdefault(c["extraction_id"], []).append(i)
        elements = {}
        for extraction_id in rows:
            for e in load_elements(settings, extraction_id):
                elements[(extraction_id, e["element_id"])] = e
        return cls(version, manifest["config"]["review_scope"], chunks,
                   BM25Okapi([t["tokens"] or ["∅"] for t in tokens]), rows, elements)


# ---------------------------------------------------------------- retrieval


def _core(spans: list[dict]) -> list[dict]:
    """A table chunk's own spans are its rows; the title lines before the table repeat in every row group."""
    return [s for s in spans if "rows" in s] or spans


def _location(index: KeywordIndex, chunk: dict) -> dict:
    first = _core(chunk["spans"])[0]
    el = index.elements[(chunk["extraction_id"], first["element_id"])]
    loc = dict(el["location"])
    if "rows" in first:
        loc["table_rows"] = first["rows"]
    pages = sorted({index.elements[(chunk["extraction_id"], s["element_id"])]["location"].get("page")
                    for s in chunk["spans"]} - {None})
    if pages:
        loc["pages"] = pages
    return loc


def _overlaps(a: dict, b: dict) -> bool:
    for sa in _core(a["spans"]):
        for sb in _core(b["spans"]):
            if sa["element_id"] != sb["element_id"]:
                continue
            if "rows" in sa and "rows" in sb:
                if set(sa["rows"]) & set(sb["rows"]) - {sa["rows"][0]}:  # shared header row alone is not overlap
                    return True
            elif "start" in sa and "start" in sb:
                if sa["start"] < sb["end"] and sb["start"] < sa["end"]:
                    return True
    return False


def retrieve(settings: Settings, index: KeywordIndex, analyzer: Analyzer, question: str,
             scope: list[tuple[DocRef, str]]) -> RetrievalResult:
    """scope pairs each selected DocRef with its active extraction ID; nothing outside it can be scored."""
    t0 = time.perf_counter()
    trace_id = str(uuid.uuid4())
    by_extraction = {extraction_id: ref for ref, extraction_id in scope}
    allowed = [i for extraction_id in by_extraction for i in index.rows_by_extraction.get(extraction_id, [])]
    limitations = []
    if index.review_scope != "reviewed_only":
        limitations.append("index_includes_unreviewed_sources")
    missing = [ref.doc_id for ref, x in scope if x not in index.rows_by_extraction]
    if missing:
        limitations.append("scope_not_indexed:" + ",".join(missing))

    codes = list(dict.fromkeys(CODE_RE.findall(nfc(question))))
    exact = []
    for code in codes:
        hits = [i for i in allowed if index.chunks[i]["requirement_key"] == code]
        hits.sort(key=lambda i: index.chunks[i]["chunk_type"] != "requirement_detail")
        exact += hits
        if not hits:
            limitations.append(f"code_not_found:{code}")
        elif index.chunks[hits[0]]["chunk_type"] != "requirement_detail":
            limitations.append(f"code_detail_unavailable:{code}")
    t1 = time.perf_counter()
    qtokens = analyzer.tokens(question)
    scores = index.bm25.get_batch_scores(qtokens, allowed) if allowed and qtokens else []
    ranked = sorted(zip(allowed, scores), key=lambda x: -x[1])
    bm25 = [(i, float(s)) for i, s in ranked if s > 0][:BM25_TOP_K]
    t2 = time.perf_counter()

    candidates = [{"chunk_id": index.chunks[i]["chunk_id"], "channel": "exact", "rank": r + 1, "score": None,
                   "row": i} for r, i in enumerate(exact)]
    candidates += [{"chunk_id": index.chunks[i]["chunk_id"], "channel": "bm25", "rank": r + 1, "score": round(s, 4),
                    "row": i} for r, (i, s) in enumerate(bm25)]
    evidence: list[EvidenceUnit] = []
    excluded = []
    used_tokens = 0
    picked: list[dict] = []
    seen_rows: set[int] = set()
    for cand in candidates:
        i = cand["row"]
        if i in seen_rows:
            continue
        seen_rows.add(i)
        chunk = index.chunks[i]
        if exact and chunk["requirement_key"] and chunk["requirement_key"] not in codes:
            # An explicit code was asked; another requirement (e.g. SFR-0010 for SFR-001) is a collision.
            excluded.append({"chunk_id": chunk["chunk_id"], "reason": "other_requirement_code"})
            continue
        if len(evidence) >= settings.evidence_max_units:
            excluded.append({"chunk_id": chunk["chunk_id"], "reason": "unit_limit"})
            continue
        if any(_overlaps(chunk, p) for p in picked):
            excluded.append({"chunk_id": chunk["chunk_id"], "reason": "duplicate_span"})
            continue
        limit = settings.evidence_target_tokens if evidence else settings.evidence_max_tokens
        if used_tokens + chunk["token_count"] > limit:
            excluded.append({"chunk_id": chunk["chunk_id"], "reason": "token_budget"})
            continue
        ref = by_extraction[chunk["extraction_id"]]
        evidence.append(EvidenceUnit(
            evidence_id=f"E{len(evidence) + 1}", doc_id=ref.doc_id, source_hash=ref.source_hash,
            extraction_id=chunk["extraction_id"], chunk_id=chunk["chunk_id"],
            element_ids=[s["element_id"] for s in chunk["spans"]], quote=chunk["body"],
            location=_location(index, chunk), token_count=chunk["token_count"]))
        picked.append(chunk)
        used_tokens += chunk["token_count"]
    for c in candidates:
        c.pop("row")
    return RetrievalResult(
        mode=settings.retrieval_mode, scope=[ref for ref, _ in scope], exact_matches=[c for c in candidates
                                                                                     if c["channel"] == "exact"],
        candidates=candidates, evidence=evidence, excluded=excluded,
        limitations=limitations + ["expansion:none(phase-1)"], evidence_tokens=used_tokens,
        timings_ms={"exact": round((t1 - t0) * 1000, 1), "bm25": round((t2 - t1) * 1000, 1),
                    "total": round((time.perf_counter() - t0) * 1000, 1)},
        trace_id=trace_id, index_version=index.version)


def best_chunk_per_extraction(index: KeywordIndex, analyzer: Analyzer, query: str) -> dict[str, tuple[float, str]]:
    """Free project search: best BM25 chunk per extraction across the whole index."""
    qtokens = analyzer.tokens(query)
    if not qtokens:
        return {}
    scores = index.bm25.get_scores(qtokens)
    best: dict[str, tuple[float, str]] = {}
    for i, s in enumerate(scores):
        if s <= 0:
            continue
        x = index.chunks[i]["extraction_id"]
        if x not in best or s > best[x][0]:
            best[x] = (float(s), index.chunks[i]["body"][:240])
    return best
