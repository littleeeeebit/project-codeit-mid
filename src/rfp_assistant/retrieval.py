"""One versioned Kiwi analyzer for indexing and queries; scoped BM25, exact requirement codes and evidence packing."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
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




class WhitespaceAnalyzer:
    """K0 baseline: NFC, lowercase, split on whitespace, strip surrounding punctuation. Requirement codes are
    served by the separate exact lookup, as in K1."""

    version = "whitespace-1"
    PUNCT = "\"'()[]{}<>.,:;!?·•○◦□■-–—「」『』《》〈〉“”‘’"

    def tokens(self, text: str) -> list[str]:
        return [t for t in (w.strip(self.PUNCT) for w in nfc(text).lower().split()) if t]


# Run labels of the phase-2 comparison -> retrieval modes
RUN_MODES = {"K0": "whitespace_bm25", "K1": "kiwi_bm25", "D": "dense", "H": "hybrid", "HR": "hybrid_rerank"}
MODES = tuple(RUN_MODES.values())
DENSE_MODES = ("dense", "hybrid", "hybrid_rerank")
LINKED_EXTRA_UNITS = 2  # sibling pieces of a split element packed with a selected piece
IDF_POLICY = "global"  # IDF over the whole index; rows outside the scope are masked before ranking


# ---------------------------------------------------------------- index build


def build_keyword_index(settings: Settings, analyzer: Analyzer, include_unreviewed: bool = False,
                        profile: str = "structural", activate: bool = True) -> dict:
    """Immutable index for one chunk profile. `activate` moves the keyword pointer (the phase-1 default path);
    once `activate-run` has recorded a selection, only another activation changes what serves users."""
    allowed = ("auto_verified", "sample_checked", "reviewed") + (
        ("unreviewed", "auto_flagged") if include_unreviewed else ())
    with open_db(settings.db_path) as conn:
        sources = [dict(r) for r in conn.execute(
            f"SELECT source_hash, active_extraction_id, review_status FROM sources WHERE parse_status = 'parsed' "
            f"AND review_status IN ({','.join('?' * len(allowed))}) ORDER BY source_hash", allowed)]
    if not sources:
        raise RetrievalError("no parsed sources match the review policy; record reviews or pass --include-unreviewed")
    config = {"chunker": chunking.chunker_fingerprint(profile), "profile": profile,
              "analyzer": analyzer_fingerprint(analyzer), "idf": IDF_POLICY,
              "review_scope": "reviewed_only" if not include_unreviewed else "includes_unreviewed",
              "extractions": [s["active_extraction_id"] for s in sources]}
    source_set_hash = hashlib.sha256("".join(s["source_hash"] for s in sources).encode()).hexdigest()
    version = hashlib.sha256(dumps(config).encode()).hexdigest()[:16]
    final_dir = settings.data_dir / "indexes" / version
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT state FROM indexes WHERE index_version = ?", (version,)).fetchone()
    if row and row["state"] == "ready" and final_dir.exists():
        if activate:
            with open_db(settings.db_path) as conn, tx(conn, immediate=True):
                set_app_setting(conn, "active_index", version)
        return {"index_version": version, "reused": True, "profile": profile, "activated": activate}

    chunks, inventory = [], []
    for s in sources:
        c, inv = chunking.build_profile(load_elements(settings, s["active_extraction_id"]), s["active_extraction_id"],
                                        profile)
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
        if activate:
            set_app_setting(conn, "active_index", version)
    return {"index_version": version, "reused": False, "chunks": len(chunks), "requirements": len(inventory),
            "review_scope": config["review_scope"], "sources": len(sources), "profile": profile,
            "activated": activate}


# ---------------------------------------------------------------- loaded index


@dataclass
class KeywordIndex:
    version: str
    review_scope: str
    chunks: list[dict]
    bm25: BM25Okapi  # K1: Kiwi tokens persisted with the index
    rows_by_extraction: dict[str, list[int]]
    elements: dict[tuple[str, str], dict]
    profile: str = "structural"
    manifest_hash: str = ""
    _extra_bm25: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.row_of = {c["chunk_id"]: i for i, c in enumerate(self.chunks)}

    def bm25_for(self, analyzer) -> BM25Okapi:
        """K0 and other comparison analyzers: built once per loaded index from the same payload rows."""
        if analyzer.version not in self._extra_bm25:
            self._extra_bm25[analyzer.version] = BM25Okapi([analyzer.tokens(c["payload"]) or ["∅"]
                                                            for c in self.chunks])
        return self._extra_bm25[analyzer.version]

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
        if manifest["config"].get("kind") == "dense":
            raise RetrievalError(f"{version} is a dense matrix, not a keyword index")
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
                   BM25Okapi([t["tokens"] or ["∅"] for t in tokens]), rows, elements,
                   manifest["config"].get("profile", "structural"), row["manifest_hash"])


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
                fa, fb = sa.get("fragment"), sb.get("fragment")
                if fa and fb and fa["row"] == fb["row"]:  # two pieces of one oversized row
                    if fa["start"] < fb["end"] and fb["start"] < fa["end"]:
                        return True
                    continue
                if set(sa["rows"]) & set(sb["rows"]) - {sa["rows"][0]}:  # shared header row alone is not overlap
                    return True
            elif "start" in sa and "start" in sb:
                if sa["start"] < sb["end"] and sb["start"] < sa["end"]:
                    return True
    return False


def scope_rows(index: KeywordIndex, scope: list[tuple[DocRef, str]]) -> list[int]:
    """Index rows the selected scope admits. Callers check this before paying for a query embedding."""
    return [i for extraction_id in dict.fromkeys(x for _, x in scope)
            for i in index.rows_by_extraction.get(extraction_id, [])]


def rank_lexical(index: KeywordIndex, analyzer, question: str, allowed: list[int], k: int,
                 whitespace: bool = False) -> list[tuple[int, float]]:
    """BM25 over admitted rows only. No shared token: empty, never arbitrary zero-score rows. Ties by chunk ID."""
    qtokens = analyzer.tokens(question)
    if not allowed or not qtokens:
        return []
    bm25 = index.bm25_for(analyzer) if whitespace else index.bm25
    scores = bm25.get_batch_scores(qtokens, allowed)
    ranked = sorted(((i, float(s)) for i, s in zip(allowed, scores) if s > 0),
                    key=lambda x: (-x[1], index.chunks[x[0]]["chunk_id"]))
    return ranked[:k]


def rrf_fuse(ranked_ids: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: sum(1 / (k + rank)), ranks from one; a list that lacks an ID contributes zero and
    an ID repeated inside one list votes once. Ties by chunk ID."""
    scores: dict[str, float] = {}
    for ids in ranked_ids:
        for rank, cid in enumerate(dict.fromkeys(ids), start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def retrieve(settings: Settings, index: KeywordIndex, analyzer: Analyzer, question: str,
             scope: list[tuple[DocRef, str]], *, mode: str | None = None, dense=None, query_vector=None,
             reranker=None, rerank_depth: int | None = None) -> RetrievalResult:
    """scope pairs each selected DocRef with its active extraction ID; nothing outside it can be scored.

    Modes: whitespace_bm25 (K0), kiwi_bm25 (K1), dense (D), hybrid (H: K1 + D by RRF), hybrid_rerank (HR). A mode
    whose dense matrix, query vector or reranker is unavailable falls back and says so in `fallback`."""
    t0 = time.perf_counter()
    trace_id = str(uuid.uuid4())
    mode = mode or settings.retrieval_mode
    if mode not in MODES:
        raise RetrievalError(f"unknown retrieval mode {mode!r}")
    fallback = None
    if mode in DENSE_MODES:
        problem = ("dense_index_unavailable" if dense is None else
                   "dense_index_mismatch" if dense.base_index_version != index.version else
                   "query_vector_unavailable" if query_vector is None else
                   "query_vector_dimension_mismatch" if len(query_vector) != dense.dims else None)
        if problem:
            fallback, mode = f"{mode}->kiwi_bm25:{problem}", "kiwi_bm25"
    if mode == "hybrid_rerank" and reranker is None:
        fallback, mode = "hybrid_rerank->hybrid:reranker_unavailable", "hybrid"

    by_extraction = {extraction_id: ref for ref, extraction_id in scope}
    allowed = scope_rows(index, scope)
    limitations = [f"idf:{IDF_POLICY}"]
    if index.review_scope != "reviewed_only":
        limitations.append("index_includes_unreviewed_sources")
    missing = [ref.doc_id for ref, x in scope if x not in index.rows_by_extraction]
    if missing:
        limitations.append("scope_not_indexed:" + ",".join(missing))

    codes = list(dict.fromkeys(CODE_RE.findall(nfc(question))))
    exact = []
    for code in codes:
        hits = sorted((i for i in allowed if index.chunks[i]["requirement_key"] == code),
                      key=lambda i: (index.chunks[i]["chunk_type"] != "requirement_detail", i))
        exact += hits
        details = {_core(index.chunks[i]["spans"])[0]["element_id"] for i in hits
                   if index.chunks[i]["chunk_type"] == "requirement_detail"}
        if not hits:
            limitations.append(f"code_not_found:{code}")
        elif not details:
            limitations.append(f"code_detail_unavailable:{code}")
        elif len(details) > 1:
            limitations.append(f"code_ambiguous:{code}")
    t1 = time.perf_counter()
    k = settings.channel_top_k
    lexical: list[tuple[int, float]] = []
    lexical_channel = "bm25_ws" if mode == "whitespace_bm25" else "bm25"
    if mode != "dense":
        lex_analyzer = WhitespaceAnalyzer() if mode == "whitespace_bm25" else analyzer
        lexical = rank_lexical(index, lex_analyzer, question, allowed, k, whitespace=mode == "whitespace_bm25")
    t2 = time.perf_counter()
    dense_ranked: list[tuple[int, float]] = []
    if mode in DENSE_MODES:
        dense_ranked = dense.search(query_vector, allowed, k)
    t3 = time.perf_counter()

    cid = lambda i: index.chunks[i]["chunk_id"]  # noqa: E731
    candidates = [{"chunk_id": cid(i), "channel": "exact", "rank": r + 1, "score": None} for r, i in enumerate(exact)]
    candidates += [{"chunk_id": cid(i), "channel": lexical_channel, "rank": r + 1, "score": round(s, 4)}
                   for r, (i, s) in enumerate(lexical)]
    candidates += [{"chunk_id": cid(i), "channel": "dense", "rank": r + 1, "score": round(s, 4)}
                   for r, (i, s) in enumerate(dense_ranked)]
    if mode in ("hybrid", "hybrid_rerank"):
        fused = rrf_fuse([[cid(i) for i, _ in lexical], [cid(i) for i, _ in dense_ranked]],
                         settings.rrf_k)[:settings.fused_top_k]
        candidates += [{"chunk_id": c, "channel": "rrf", "rank": r + 1, "score": round(s, 6)}
                       for r, (c, s) in enumerate(fused)]
        ordered = [index.row_of[c] for c, _ in fused]
    else:
        ordered = [i for i, _ in (dense_ranked if mode == "dense" else lexical)]
    t4 = time.perf_counter()
    rerank_info = None
    if mode == "hybrid_rerank":
        depth = min(rerank_depth or settings.fused_top_k, len(ordered))
        pool = ordered[:depth]
        scored, rerank_info = reranker.rerank(question, [index.chunks[i] for i in pool])
        admitted = set(pool)
        reranked = [pool[p] for p, _ in scored if 0 <= p < len(pool) and pool[p] in admitted]
        candidates += [{"chunk_id": cid(pool[p]), "channel": "rerank", "rank": r + 1, "score": round(s, 6)}
                       for r, (p, s) in enumerate(scored) if 0 <= p < len(pool)]
        ordered = list(dict.fromkeys(reranked + ordered[depth:]))
    t5 = time.perf_counter()
    ranking = list(dict.fromkeys(exact + ordered))  # exact identifier matches stay ahead of every ranker

    evidence: list[EvidenceUnit] = []
    excluded = []
    used_tokens = 0
    picked: list[dict] = []
    picked_ids: set[str] = set()

    def admit(chunk: dict, limit: int, linked: bool) -> str | None:
        nonlocal used_tokens
        if codes and chunk["requirement_key"] and chunk["requirement_key"] not in codes:
            return "other_requirement_code"  # e.g. SFR-0010 when SFR-001 was asked
        if len(evidence) >= settings.evidence_max_units:
            return "unit_limit"
        if not linked and any(_overlaps(chunk, p) for p in picked):
            return "duplicate_span"
        if used_tokens + chunk["token_count"] > limit:
            return "token_budget"
        ref = by_extraction[chunk["extraction_id"]]
        evidence.append(EvidenceUnit(
            evidence_id=f"E{len(evidence) + 1}", doc_id=ref.doc_id, source_hash=ref.source_hash,
            extraction_id=chunk["extraction_id"], chunk_id=chunk["chunk_id"],
            element_ids=[s["element_id"] for s in chunk["spans"]], quote=chunk["body"],
            location=_location(index, chunk), token_count=chunk["token_count"]))
        picked.append(chunk)
        picked_ids.add(chunk["chunk_id"])
        used_tokens += chunk["token_count"]
        return None

    for i in ranking:
        chunk = index.chunks[i]
        if chunk["chunk_id"] in picked_ids:
            continue
        if chunk["extraction_id"] not in by_extraction:  # defensive: a ranker returned an out-of-scope row
            excluded.append({"chunk_id": chunk["chunk_id"], "reason": "outside_scope"})
            continue
        limit = settings.evidence_target_tokens if evidence else settings.evidence_max_tokens
        reason = admit(chunk, limit, linked=False)
        if reason:
            excluded.append({"chunk_id": chunk["chunk_id"], "reason": reason})
            continue
        # The rest of a split element or row may carry its condition. Nearest pieces first, within the evidence
        # target and LINKED_EXTRA_UNITS, so one long paragraph cannot crowd out every other candidate; a piece
        # left out is reported rather than silently dropped.
        here = index.row_of[chunk["chunk_id"]]
        siblings = sorted((index.row_of[o] for o in chunk.get("linked") or [] if o in index.row_of),
                          key=lambda r: (abs(r - here), r))
        added = 0
        for r in siblings:
            other = index.chunks[r]
            if other["chunk_id"] in picked_ids:
                continue
            reason = "linked_unit_limit" if added >= LINKED_EXTRA_UNITS else admit(
                other, settings.evidence_target_tokens, linked=True)
            if reason:
                limitations.append(f"linked_evidence_missing:{chunk['chunk_id']}:{reason}")
                break
            added += 1
    t6 = time.perf_counter()
    ms = lambda a, b: round((b - a) * 1000, 1)  # noqa: E731
    timings = {"exact": ms(t0, t1), "lexical": ms(t1, t2), "dense": ms(t2, t3), "fuse": ms(t3, t4),
               "rerank": ms(t4, t5), "pack": ms(t5, t6), "total": ms(t0, t6)}
    if rerank_info:
        limitations += [f"rerank:{k}={v}" for k, v in rerank_info.items() if k in ("truncated", "windows")]
    return RetrievalResult(
        mode=mode, scope=[ref for ref, _ in scope],
        exact_matches=[c for c in candidates if c["channel"] == "exact"],
        candidates=candidates, evidence=evidence, excluded=excluded,
        limitations=limitations + ["expansion:linked_split_pieces"], evidence_tokens=used_tokens,
        timings_ms=timings, trace_id=trace_id, index_version=index.version,
        ranking=[cid(i) for i in ranking], fallback=fallback,
        dense_version=dense.version if mode in DENSE_MODES else None)


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
