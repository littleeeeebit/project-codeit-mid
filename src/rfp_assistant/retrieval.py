"""One versioned Kiwi analyzer for indexing and queries; scoped BM25, exact requirement codes and evidence packing."""

from __future__ import annotations

import hashlib
import json
import math
import re
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

ANALYZER_VERSION = "kiwi-bm25-2"  # 2: spacing normalization of the analysis copy, scope-redundant query terms
KEEP_TAGS = {"NNG", "NNP", "NNB", "NR", "NP", "SL", "SN", "SH", "XR", "VV", "VA", "XPN"}
NEGATIONS = {"않", "안", "못", "없", "아니", "불가", "금지"}
CONTENT_TAGS = {"NNG", "NNP", "NR", "SL", "SN", "SH", "XR"}
# Small reviewed domain dictionary; extend only from observed query failures.
USER_WORDS = ["제안요청서", "요구사항", "하자보수", "하자담보", "부가가치세", "공동수급", "분담이행", "공동이행",
              "입찰참가자격", "기술평가", "가격평가", "지체상금", "사업수행계획서", "산출물", "유지관리", "정보시스템"]
BM25_TOP_K = 20
# Spelling variants observed in this corpus, applied to the analysis copy of both indexed and query text (never to
# stored evidence). Extend only from an observed retrieval failure with a fixture.
SPACING_ALIASES = {"지체 상금": "지체상금", "사업 비": "사업비", "계약 보증금": "계약보증금", "하자 보수": "하자보수",
                   "부가세": "부가가치세"}  # last: abbreviation observed in dp-020 (rank 7 -> 1 when expanded)
HANGUL_SYLLABLE = re.compile(r"^[가-힣]$")
PLAIN_HANGUL_WORD = re.compile(r"^[가-힣]+$")
LETTER_SPACED_MAX = 6


def normalize_for_analysis(text: str) -> str:
    """Analysis copy only. Collapses letter-spaced headings ("사 업 비 :" -> "사업비 :"): a run of 2..6
    single-syllable tokens whose neighbours are not plain Hangul words, so ordinary prose ("그 외 사항") stays as
    written. Then applies SPACING_ALIASES. Requirement codes, digits and negations are unaffected."""
    out_lines = []
    for line in text.split("\n"):
        toks = line.split(" ")
        res, i = [], 0
        while i < len(toks):
            j = i
            while j < len(toks) and HANGUL_SYLLABLE.match(toks[j]):
                j += 1
            run = j - i
            before = res[-1] if res else ""
            after = toks[j] if j < len(toks) else ""
            if (2 <= run <= LETTER_SPACED_MAX and not PLAIN_HANGUL_WORD.match(before)
                    and not PLAIN_HANGUL_WORD.match(after)):
                res.append("".join(toks[i:j]))
                i = j
            elif run:
                res.extend(toks[i:j])
                i = j
            else:
                res.append(toks[i])
                i += 1
        out_lines.append(" ".join(res))
    text = "\n".join(out_lines)
    for variant, canonical in SPACING_ALIASES.items():
        text = text.replace(variant, canonical)
    return text


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
        out = [f"code:{c}" for c in CODE_RE.findall(text)]  # from the text as written
        with self._lock:
            toks = self._kiwi.tokenize(normalize_for_analysis(text))
        for t in toks:
            if t.tag in KEEP_TAGS or t.form in NEGATIONS:
                out.append(t.form.lower())
        return out

    def content_terms(self, text: str) -> set[str]:
        """Query-side only: the nouns, foreign words, numbers and roots a question asks about. Verbs, adjectives,
        dependent nouns and pronouns ("알려줘", "하는 거", "이것") carry no fact to search for."""
        with self._lock:
            toks = self._kiwi.tokenize(normalize_for_analysis(nfc(text)))
        return {t.form.lower() for t in toks if t.tag in CONTENT_TAGS} | {f"code:{c}" for c in CODE_RE.findall(nfc(text))}


def analyzer_fingerprint(analyzer: Analyzer | None = None) -> str:
    """Analyzer version, dictionary, tags and query policy. Without an instance it describes the current code."""
    version = analyzer.version if analyzer is not None else \
        f"{ANALYZER_VERSION}:kiwipiepy-{metadata.version('kiwipiepy')}"
    info = {"analyzer": version, "words": USER_WORDS, "tags": sorted(KEEP_TAGS),
            "aliases": SPACING_ALIASES, "letter_spaced_max": LETTER_SPACED_MAX, "query_policy": QUERY_POLICY,
            "scope_min_rows": SCOPE_MIN_ROWS, "metadata_restatement_min": METADATA_RESTATEMENT_MIN}
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
    with open_db(settings.db_path) as conn:
        hashes = {s["source_hash"] for s in sources}
        doc_ids = [r["doc_id"] for r in conn.execute("SELECT doc_id, active_source_hash FROM documents "
                                                     "ORDER BY doc_id") if r["active_source_hash"] in hashes]
    # The query policy reads each document's title/institution terms; freezing them here makes the index (and every
    # run bound to its manifest) reproduce retrieval exactly. A later metadata correction needs a rebuild.
    scope_terms = metadata_term_snapshot(settings, analyzer, doc_ids)
    config = {"chunker": chunking.chunker_fingerprint(profile), "profile": profile,
              "analyzer": analyzer_fingerprint(analyzer), "idf": IDF_POLICY,
              "review_scope": "reviewed_only" if not include_unreviewed else "includes_unreviewed",
              "extractions": [s["active_extraction_id"] for s in sources],
              "metadata_terms": hashlib.sha256(dumps(scope_terms).encode()).hexdigest()}
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
        write_text_atomic(tmp / "scope-terms.json", json.dumps(scope_terms, ensure_ascii=False, sort_keys=True))
        files = {n: hashlib.sha256((tmp / n).read_bytes()).hexdigest()
                 for n in ("chunks.jsonl", "tokens.jsonl", "requirements.jsonl", "scope-terms.json")}
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
            "INSERT INTO requirements(index_version, extraction_id, requirement_key, source_form, kind, "
            "element_id, name) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
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
    scope_terms: dict = field(default_factory=dict)  # doc_id -> title/institution terms frozen at build
    analyzer_fp: str | None = None  # analyzer fingerprint the index tokens were built with
    has_metadata_snapshot: bool = True
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
        terms_path = manifest_path.parent / "scope-terms.json"
        scope_terms = json.loads(terms_path.read_text(encoding="utf-8")) if "scope-terms.json" in manifest["files"] \
            else {}
        return cls(version, manifest["config"]["review_scope"], chunks,
                   BM25Okapi([t["tokens"] or ["∅"] for t in tokens]), rows, elements,
                   manifest["config"].get("profile", "structural"), row["manifest_hash"], scope_terms,
                   manifest["config"].get("analyzer"), "metadata_terms" in manifest["config"])


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


def index_compatibility(index: KeywordIndex, analyzer=None) -> str | None:
    """Why this index cannot reproduce the current query policy, or None. An index built by another analyzer or
    query policy, or before title/institution terms were frozen into it, would be queried with different
    inputs than it was built (and measured) with."""
    if not index.has_metadata_snapshot:
        return (f"keyword index {index.version} predates the frozen title/institution snapshot; rebuild it with "
                "build-keyword (cached embeddings are reused)")
    if index.analyzer_fp is not None and index.analyzer_fp != analyzer_fingerprint(analyzer):
        return (f"keyword index {index.version} was built with analyzer/query policy {index.analyzer_fp}, not the "
                f"current {analyzer_fingerprint(analyzer)}; rebuild it with build-keyword (cached embeddings are "
                "reused)")
    return None


def corpus_scope(settings: Settings, index: KeywordIndex) -> list[tuple[DocRef, str]]:
    """The scope of an all-documents question: every indexed active extraction once, cited under the first
    document (CSV order) associated with that original."""
    with open_db(settings.db_path) as conn:
        rows = conn.execute("SELECT d.doc_id, d.active_source_hash, s.active_extraction_id FROM documents d "
                            "JOIN sources s ON s.source_hash = d.active_source_hash ORDER BY d.csv_row_id").fetchall()
    scope: dict[str, tuple[DocRef, str]] = {}
    for r in rows:
        x = r["active_extraction_id"]
        if x in index.rows_by_extraction and x not in scope:
            scope[x] = (DocRef(r["doc_id"], r["active_source_hash"]), x)
    return list(scope.values())


def scope_rows(index: KeywordIndex, scope: list[tuple[DocRef, str]]) -> list[int]:
    """Index rows the selected scope admits. Callers check this before paying for a query embedding."""
    return [i for extraction_id in dict.fromkeys(x for _, x in scope)
            for i in index.rows_by_extraction.get(extraction_id, [])]


# 2: dropping only while a kept term still matches the scope; title/institution terms frozen into the index.
QUERY_POLICY = "scope-redundant-2"
SCOPE_MIN_ROWS = 8  # below this a scope-local frequency says nothing
METADATA_RESTATEMENT_MIN = 2  # a question restating the selected project name repeats at least two of its terms


def _protected(token: str) -> bool:
    return token.startswith("code:") or any(ch.isdigit() for ch in token) or token in NEGATIONS


def metadata_term_snapshot(settings: Settings, analyzer, doc_ids: list[str]) -> dict[str, list[str]]:
    """doc_id -> analyzer tokens of that document's own title and institution (CSV values and recorded
    resolutions). Built once into the index; retrieval never reads mutable metadata at query time."""
    if not doc_ids:
        return {}
    marks = ",".join("?" * len(doc_ids))
    with open_db(settings.db_path) as conn:
        rows = conn.execute(f"SELECT doc_id, normalized_metadata_json FROM documents WHERE doc_id IN ({marks})",
                            doc_ids).fetchall()
        resolved = conn.execute(
            f"SELECT doc_id, value_json FROM metadata_resolutions WHERE field IN ('title', 'institution') "
            f"AND doc_id IN ({marks}) ORDER BY created_at", doc_ids).fetchall()
    texts: dict[str, list[str]] = {}
    for r in rows:
        meta = json.loads(r["normalized_metadata_json"])
        texts.setdefault(r["doc_id"], []).extend([meta.get("title") or "", meta.get("institution") or ""])
    for r in resolved:
        texts.setdefault(r["doc_id"], []).append(str(json.loads(r["value_json"])))
    return {d: sorted({t for text in ts for t in analyzer.tokens(text) if not _protected(t)})
            for d, ts in sorted(texts.items())}


ROUTE_SHARE = 0.5  # a routed document carries at least half the best-matching document's name weight
ROUTE_MAX = 4  # more matching documents than this: the question names no particular project
ROUTE_RULE = "greedy-rare-term-1"  # recorded in frozen run configurations; change it whenever route_corpus changes


def route_corpus(index: KeywordIndex, analyzer, question: str,
                 scope: list[tuple[DocRef, str]]) -> list[tuple[DocRef, str]]:
    """An all-documents question that names a project keeps the documents whose own title/institution terms it
    restates (two or more, weighted by inverse document frequency across the scope). Chunks rarely repeat the
    project name, so otherwise a named project's requirement code or clause competes with the same wording in
    every other RFP. Returns [] (no routing) when no document, or too many, match.

    Documents are taken best first, and each is weighed only on the terms no document taken before it already
    explains, so a document sharing just the generic words of a better match ("대학교", "사업") stays out. Two named
    projects both carry their own terms; another edition with the very same matched name is kept."""
    terms = {t for t in analyzer.tokens(question) if not _protected(t)}
    names = {ref.doc_id: set(index.scope_terms.get(ref.doc_id, [])) & terms for ref, _ in scope}
    df: dict[str, int] = {}
    for matched in names.values():
        for t in matched:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(len(names) / n) for t, n in df.items()}
    # A term in ROUTE_MAX or more titles names a kind of project ("대학교", "교육"), not one: a document is a
    # candidate only when the question also restates one of its rarer terms.
    weight = {d: sum(idf[t] for t in matched) for d, matched in names.items()
              if len(matched) >= METADATA_RESTATEMENT_MIN and any(df[t] < ROUTE_MAX for t in matched)}
    top = max(weight.values(), default=0.0)
    keep: set[str] = set()
    explained: set[str] = set()
    for d in sorted(weight, key=lambda d: (-weight[d], d)):
        own = sum(idf[t] for t in names[d] - explained)
        if top > 0 and (own >= ROUTE_SHARE * top or any(names[d] == names[k] for k in keep)):
            keep.add(d)
            explained |= names[d]
    if not keep or len(keep) > ROUTE_MAX:
        return []
    return [(ref, x) for ref, x in scope if ref.doc_id in keep]


def scope_redundant_terms(index: KeywordIndex, qtokens: list[str], allowed: list[int],
                          metadata_terms: set[str]) -> set[str]:
    """Query terms that cannot discriminate inside the selected scope and only drown the asked fact:
    - terms present in more than half of the scope's chunks (their scope-local BM25 IDF is not positive);
    - the selected project's own title/institution terms, when the question restates that name (two or more).
    Codes, digits and negations are never dropped, and nothing is dropped unless a kept term still matches a chunk
    of the scope."""
    terms = {t for t in qtokens if not _protected(t)}
    drop: set[str] = set()
    if len(allowed) >= SCOPE_MIN_ROWS:
        freqs = index.bm25.doc_freqs
        for t in terms:
            if sum(1 for i in allowed if t in freqs[i]) * 2 > len(allowed):
                drop.add(t)
    restated = terms & metadata_terms
    if len(restated) >= METADATA_RESTATEMENT_MIN:
        drop |= restated
    kept = [t for t in qtokens if t not in drop]
    freqs = index.bm25.doc_freqs
    # Only drop when what is left still finds something in the scope; otherwise a common fact term ("하자보수" in a
    # warranty-only scope) would leave an unmatched interrogative and report false evidence absence.
    if drop and any(t in freqs[i] for i in allowed for t in kept):
        return drop
    return set()


def rank_lexical(index: KeywordIndex, analyzer, question: str, allowed: list[int], k: int,
                 whitespace: bool = False, drop: set[str] | None = None) -> list[tuple[int, float]]:
    """BM25 over admitted rows only. No shared token: empty, never arbitrary zero-score rows. Ties by chunk ID."""
    qtokens = [t for t in analyzer.tokens(question) if t not in (drop or ())]
    if not allowed or not qtokens:
        return []
    bm25 = index.bm25_for(analyzer) if whitespace else index.bm25
    scores = bm25.get_batch_scores(qtokens, allowed)
    ranked = sorted(((i, float(s)) for i, s in zip(allowed, scores) if s > 0),
                    key=lambda x: (-x[1], index.chunks[x[0]]["chunk_id"]))
    return ranked[:k]


def rrf_fuse(ranked_ids: list[list[str]], k: int = 60, weights: list[float] | None = None) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: sum(weight / (k + rank)), ranks from one; a list that lacks an ID contributes zero and
    an ID repeated inside one list votes once. Ties by chunk ID."""
    scores: dict[str, float] = {}
    for ids, weight in zip(ranked_ids, weights or [1.0] * len(ranked_ids)):
        for rank, cid in enumerate(dict.fromkeys(ids), start=1):
            scores[cid] = scores.get(cid, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def fuse(settings: Settings, lexical_ids: list[str], dense_ids: list[str]) -> list[tuple[str, float]]:
    """Hybrid order. keyword_first keeps the leading `keyword_head` BM25 rows in BM25 order and orders everything
    else by weighted RRF, so a dense row can never displace a protected keyword match."""
    fused = rrf_fuse([lexical_ids, dense_ids], settings.rrf_k, [1.0, settings.dense_weight])
    if settings.fusion == "keyword_first":
        score = dict(fused)
        head = list(dict.fromkeys(lexical_ids))[:settings.keyword_head]
        fused = [(c, score[c]) for c in head] + [x for x in fused if x[0] not in head]
    return fused[:settings.fused_top_k]


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

    limitations = [f"idf:{IDF_POLICY}"]
    routed: list[tuple[DocRef, str]] = []
    if len(scope) > 2:  # only the all-documents scope is larger than a selection
        routed = route_corpus(index, analyzer, question, scope)
        if routed:
            scope = routed
            limitations.append("corpus_routed:" + ",".join(ref.doc_id for ref, _ in routed))
    by_extraction = {extraction_id: ref for ref, extraction_id in scope}
    allowed = scope_rows(index, scope)
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
        drop: set[str] = set()
        if mode != "whitespace_bm25":  # K0 stays the plain documented baseline
            # A selected project's restated name says nothing inside its own scope; across the corpus it is what
            # finds the project, so the restatement rule applies to one or two selected documents only.
            metadata_terms = {t for ref, _ in scope for t in index.scope_terms.get(ref.doc_id, [])} \
                if len(scope) <= 2 else set()
            drop = scope_redundant_terms(index, analyzer.tokens(question), allowed, metadata_terms)
            if drop:
                limitations.append("scope_redundant_terms:" + ",".join(sorted(drop)))
        lexical = rank_lexical(index, lex_analyzer, question, allowed, k, whitespace=mode == "whitespace_bm25",
                               drop=drop)
    # "한영대학교 사업은 어떤 사업이야?": the question names its documents and asks nothing else. The remaining words
    # ("알려줘", "하는 거") would only match arbitrary passages, so the named documents are ranked by meaning instead.
    name_only = False
    if mode in ("hybrid", "hybrid_rerank") and not exact and len(scope) <= ROUTE_MAX:
        names = {t for ref, _ in scope for t in index.scope_terms.get(ref.doc_id, [])}
        named = bool(routed) or len(set(analyzer.tokens(question)) & names) >= METADATA_RESTATEMENT_MIN
        title_df: dict[str, int] = {}
        for terms in index.scope_terms.values():
            for t in set(terms):
                title_df[t] = title_df.get(t, 0) + 1
        project_words = {t for t, n in title_df.items() if n > ROUTE_MAX}  # "사업", "구축", "시스템": name a project
        name_only = named and not (analyzer.content_terms(question) - names - drop - project_words)
    if name_only:
        lexical = []
        limitations.append("name_only_question:dense_within_named_documents")
        qset = set(analyzer.tokens(question))
        named_rows = scope_rows(index, [(ref, x) for ref, x in scope if len(qset & set(
            index.scope_terms.get(ref.doc_id, []))) >= METADATA_RESTATEMENT_MIN])
        allowed = named_rows or allowed  # a routed scope is already named; a selection keeps the named one
    t2 = time.perf_counter()
    dense_ranked: list[tuple[int, float]] = []
    if mode in ("hybrid", "hybrid_rerank") and not lexical and not exact and not name_only:
        # Nearest neighbours always exist; without one lexical match they are arbitrary passages, not evidence.
        limitations.append("no_lexical_match:dense_not_used")
    elif mode in DENSE_MODES:
        dense_ranked = dense.search(query_vector, allowed, k, settings)
    t3 = time.perf_counter()

    cid = lambda i: index.chunks[i]["chunk_id"]  # noqa: E731
    candidates = [{"chunk_id": cid(i), "channel": "exact", "rank": r + 1, "score": None} for r, i in enumerate(exact)]
    candidates += [{"chunk_id": cid(i), "channel": lexical_channel, "rank": r + 1, "score": round(s, 4)}
                   for r, (i, s) in enumerate(lexical)]
    candidates += [{"chunk_id": cid(i), "channel": "dense", "rank": r + 1, "score": round(s, 4)}
                   for r, (i, s) in enumerate(dense_ranked)]
    if mode in ("hybrid", "hybrid_rerank"):
        fused = fuse(settings, [cid(i) for i, _ in lexical], [cid(i) for i, _ in dense_ranked])
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
        try:
            scored, rerank_info = reranker.rerank(question, [index.chunks[i] for i in pool])
        except Exception as exc:  # noqa: BLE001 - an inference failure serves the H order, visibly
            scored, rerank_info = None, None
            fallback, mode = f"hybrid_rerank->hybrid:reranker_error:{type(exc).__name__}: {exc}"[:300], "hybrid"
        if scored is not None:
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
        # queue time (waiting for the bounded model) and inference time, so latency gates show where time goes
        for key in ("queue_ms", "infer_ms", "truncated"):
            if rerank_info.get(key) is not None:
                timings[f"rerank_{key.removesuffix('_ms')}"] = rerank_info[key]
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
