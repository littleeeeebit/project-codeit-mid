"""Hash-keyed embedding cache, metered embedding batches, dense matrix build/verification/scoring and the
optional local reranker.

Every paid embedding goes through the shared ledger: count -> reserve -> mark dispatching -> transport.embed
(generation.py is the only SDK call site) -> settle once. An unknown outcome is never resent automatically.
A matrix becomes `ready` only after every row is verified; nothing here changes what serves users, which is
`activate-run`'s job.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import budget
from .generation import ProviderError, Transport
from .ingestion import nfc
from .settings import EMBEDDING_MAX_TOKENS_PER_INPUT, EMBEDDING_TOKENIZER, Settings
from .store import dumps, open_db, read_jsonl, tx, utcnow, write_bytes_atomic, write_jsonl_atomic, write_text_atomic

EMBED_POLICY = "nfc-strip:l2-unit:float32"
DENSE_VERSION = "dense-matrix-1"
COUNT_METHOD = "tiktoken:cl100k_base exact per input"
QUERY_MARGIN_TOKENS = 8
NORM_TOLERANCE = 1e-3


class DenseError(RuntimeError):
    pass


# ---------------------------------------------------------------- payload identity and counting


@lru_cache(maxsize=2)
def _encoding(model: str):
    import tiktoken

    return tiktoken.get_encoding(EMBEDDING_TOKENIZER[model])


def count_embedding_tokens(text: str, model: str = "text-embedding-3-small") -> int:
    return len(_encoding(model).encode(text))


def normalize_payload(text: str) -> str:
    """The exact text sent to the embedding endpoint."""
    return nfc(text).strip()


def model_fingerprint(model: str, dims: int) -> str:
    return hashlib.sha256(dumps({"model": model, "dims": dims, "policy": EMBED_POLICY}).encode()).hexdigest()[:16]


def payload_hash(text: str, model: str, dims: int) -> str:
    """Identity of one vector: normalized text, model, dimensions and normalization policy. Chunk payloads come
    from the original only (no CSV metadata), so byte-identical sources share every vector."""
    return hashlib.sha256(dumps({"text": normalize_payload(text), "model": model, "dims": dims,
                                 "policy": EMBED_POLICY}).encode()).hexdigest()


def unit_vector(values, dims: int) -> np.ndarray:
    v = np.asarray(values, dtype=np.float64)
    if v.shape != (dims,):
        raise DenseError(f"vector has shape {v.shape}, expected ({dims},)")
    if not np.all(np.isfinite(v)):
        raise DenseError("vector has nonfinite values")
    norm = float(np.linalg.norm(v))
    if norm == 0.0:
        raise DenseError("vector is zero")
    return (v / norm).astype(np.float32)


# ---------------------------------------------------------------- cache


def cache_dir(settings: Settings) -> Path:
    return settings.data_dir / "embedding-cache" / model_fingerprint(settings.embedding_model,
                                                                      settings.embedding_dimensions)


def _npy_bytes(vec: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, vec, allow_pickle=False)
    return buf.getvalue()


def cache_get(settings: Settings, h: str) -> np.ndarray | None:
    """A verified cached vector, or None. A corrupt entry is a miss, never a silently used vector."""
    d = cache_dir(settings)
    npy, meta = d / f"{h}.npy", d / f"{h}.json"
    if not (npy.exists() and meta.exists()):
        return None
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
        data = npy.read_bytes()
        if info.get("payload_hash") != h or hashlib.sha256(data).hexdigest() != info.get("npy_sha256"):
            return None
        vec = np.load(io.BytesIO(data), allow_pickle=False)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if vec.dtype != np.float32 or vec.shape != (settings.embedding_dimensions,) or not np.all(np.isfinite(vec)) \
            or abs(float(np.linalg.norm(vec)) - 1.0) > NORM_TOLERANCE:
        return None
    return vec


def cache_put(settings: Settings, h: str, vec: np.ndarray, meta: dict) -> None:
    data = _npy_bytes(vec)
    d = cache_dir(settings)
    write_bytes_atomic(d / f"{h}.npy", data)
    write_text_atomic(d / f"{h}.json", json.dumps(
        {**meta, "payload_hash": h, "model": settings.embedding_model, "dims": settings.embedding_dimensions,
         "policy": EMBED_POLICY, "npy_sha256": hashlib.sha256(data).hexdigest(), "created_at": utcnow()},
        ensure_ascii=False))


# ---------------------------------------------------------------- metered calls


def ensure_job_request(settings: Settings, member_id: str, key: str, description: dict) -> str:
    """Maintenance and evaluation jobs attach their attempts to one request row, reused when a job resumes."""
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        row = conn.execute("SELECT request_id FROM requests WHERE member_id = ? AND idempotency_key = ?",
                           (member_id, key)).fetchone()
        if row is not None:
            conn.execute("UPDATE requests SET status = 'running', updated_at = ? WHERE request_id = ?",
                         (utcnow(), row[0]))
            return row[0]
        request_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO requests(request_id, member_id, idempotency_key, generation_id, input_hash, config_hash, "
            "scope_json, status, trace_json, created_at, updated_at) VALUES (?, ?, ?, NULL, ?, ?, '[]', 'running', ?, "
            "?, ?)",
            (request_id, member_id, key, hashlib.sha256(key.encode()).hexdigest(),
             hashlib.sha256(dumps(description).encode()).hexdigest(), dumps(description), utcnow(), utcnow()))
        return request_id


def finish_job_request(settings: Settings, request_id: str, status: str, result: dict) -> None:
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("UPDATE requests SET status = ?, result_json = ?, updated_at = ? WHERE request_id = ?",
                     (status, dumps(result), utcnow(), request_id))


def unresolved_attempts(settings: Settings, purpose: str) -> list[str]:
    """Embedding attempts of a purpose whose billing is still open (unknown or dispatching). A job must not
    resend their inputs until they are reconciled: the first call may already have been billed."""
    with open_db(settings.db_path) as conn:
        return [r[0] for r in conn.execute(
            "SELECT attempt_id FROM attempts WHERE stage = 'embedding' AND purpose = ? "
            "AND state IN ('unknown', 'dispatching') ORDER BY created_at", (purpose,))]


def metered_embed(settings: Settings, transport: Transport | None, texts: list[str], input_tokens: int, *,
                  request_id: str, member_id: str, purpose: str, guard=None) -> dict:
    """One bounded embedding call through the ledger. Returns {'status': ok|blocked|failed|unknown, ...}.
    `guard` is the owning request's dispatch check (see budget.mark_dispatching)."""
    if transport is None:
        return {"status": "blocked", "reason": "provider_unavailable", "vectors": None, "attempt_id": None}
    db = settings.db_path
    admission = budget.reserve(db, request_id=request_id, member_id=member_id, stage="embedding", purpose=purpose,
                               model=settings.embedding_model, input_tokens=input_tokens, max_output_tokens=0,
                               count_method=COUNT_METHOD)
    if not admission["admitted"]:
        return {"status": "blocked", "reason": admission["reason"], "vectors": None, "attempt_id": None,
                "reserved_micro_usd": admission.get("reserved_micro_usd")}
    attempt_id = admission["attempt_id"]
    try:
        budget.mark_dispatching(db, attempt_id, guard)
    except budget.DispatchRefused as exc:
        return {"status": "blocked", "reason": f"stopped_before_dispatch:{exc}", "vectors": None,
                "attempt_id": attempt_id, "billing": "released"}
    try:
        response = transport.embed(model=settings.embedding_model, inputs=texts,
                                   dimensions=settings.embedding_dimensions)
    except ProviderError as exc:
        if exc.pre_execution:
            budget.release(db, attempt_id, str(exc)[:300], confirmed_pre_execution=True)
            return {"status": "failed", "reason": str(exc)[:300], "vectors": None, "attempt_id": attempt_id,
                    "billing": "released"}
        budget.mark_unknown(db, attempt_id, str(exc))
        return {"status": "unknown", "reason": str(exc)[:300], "vectors": None, "attempt_id": attempt_id,
                "billing": "unknown"}
    if response.usage is None:
        budget.mark_unknown(db, attempt_id, "provider returned no usage")
        billing, settled = "unknown", None
    else:
        settled = budget.settle(db, attempt_id, response.usage, response.response_id)["settled_micro_usd"]
        billing = "settled"
    if len(response.vectors) != len(texts):
        return {"status": "failed", "reason": f"{len(response.vectors)} vectors for {len(texts)} inputs",
                "vectors": None, "attempt_id": attempt_id, "billing": billing, "settled_micro_usd": settled}
    try:
        vectors = [unit_vector(v, settings.embedding_dimensions) for v in response.vectors]
    except DenseError as exc:
        return {"status": "failed", "reason": str(exc), "vectors": None, "attempt_id": attempt_id,
                "billing": billing, "settled_micro_usd": settled}
    return {"status": "ok", "vectors": vectors, "attempt_id": attempt_id, "billing": billing,
            "settled_micro_usd": settled, "usage": response.usage}


def query_vector(settings: Settings, transport: Transport | None, question: str, *, request_id: str | None,
                 member_id: str, purpose: str, allow_paid: bool, guard=None) -> tuple[np.ndarray | None, dict]:
    """Cached by normalized query, model and dimensions. A miss is paid only when `allow_paid`."""
    text = normalize_payload(question)
    h = payload_hash(text, settings.embedding_model, settings.embedding_dimensions)
    vec = cache_get(settings, h)
    if vec is not None:
        return vec, {"cache": "hit", "payload_hash": h}
    if not allow_paid or request_id is None:
        return None, {"cache": "miss", "payload_hash": h, "reason": "paid_query_embedding_not_allowed"}
    tokens = count_embedding_tokens(text, settings.embedding_model) + QUERY_MARGIN_TOKENS
    result = metered_embed(settings, transport, [text], tokens, request_id=request_id, member_id=member_id,
                           purpose=purpose, guard=guard)
    info = {"cache": "miss", "payload_hash": h, **{k: v for k, v in result.items() if k != "vectors"}}
    if result["status"] != "ok":
        return None, info
    cache_put(settings, h, result["vectors"][0], {"kind": "query", "attempt_id": result["attempt_id"]})
    return result["vectors"][0], info


# ---------------------------------------------------------------- embedding plan


def _keyword_index_row(settings: Settings, index_version: str) -> dict:
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM indexes WHERE index_version = ?", (index_version,)).fetchone()
    if row is None or row["state"] != "ready":
        raise DenseError(f"keyword index {index_version} is not ready")
    config = json.loads(row["config_json"])
    if config.get("kind") == "dense":
        raise DenseError(f"{index_version} is a dense matrix; pass its keyword index")
    return dict(row)


def index_payloads(settings: Settings, index_version: str) -> tuple[dict, list[dict]]:
    """(index row, [{chunk_id, text, payload_hash, tokens}]) in chunk row order, from verified index files."""
    row = _keyword_index_row(settings, index_version)
    manifest_path = Path(row["manifest_path"])
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != row["manifest_hash"]:
        raise DenseError("keyword index manifest hash mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chunks_path = manifest_path.parent / "chunks.jsonl"
    if hashlib.sha256(chunks_path.read_bytes()).hexdigest() != manifest["files"]["chunks.jsonl"]:
        raise DenseError("keyword index chunks hash mismatch")
    out = []
    for c in read_jsonl(chunks_path):
        text = normalize_payload(c["payload"])
        out.append({"chunk_id": c["chunk_id"], "text": text,
                    "payload_hash": payload_hash(text, settings.embedding_model, settings.embedding_dimensions)})
    return row, out


def _unique_missing(settings: Settings, payloads: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    unique: dict[str, dict] = {}
    for p in payloads:
        unique.setdefault(p["payload_hash"], p)
    missing = {h: p for h, p in unique.items() if cache_get(settings, h) is None}
    return unique, missing


def plan_batches(settings: Settings, items: list[dict]) -> list[list[dict]]:
    """Bounded batches by input count and summed tokens, in stable payload-hash order."""
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_tokens = 0
    for it in sorted(items, key=lambda x: x["payload_hash"]):
        if it["tokens"] > EMBEDDING_MAX_TOKENS_PER_INPUT:
            raise DenseError(f"payload {it['payload_hash'][:12]} has {it['tokens']} tokens, above the per-input limit")
        if cur and (len(cur) >= settings.embedding_batch_inputs
                    or cur_tokens + it["tokens"] > settings.embedding_batch_tokens):
            batches.append(cur)
            cur, cur_tokens = [], 0
        cur.append(it)
        cur_tokens += it["tokens"]
    if cur:
        batches.append(cur)
    return batches


def _fingerprint(settings: Settings, row: dict, unique: dict, rate_version: str) -> str:
    return hashlib.sha256(dumps({"index": row["index_version"], "manifest": row["manifest_hash"],
                                 "model": settings.embedding_model, "dims": settings.embedding_dimensions,
                                 "policy": EMBED_POLICY, "payloads": sorted(unique), "rates": rate_version,
                                 "batch": [settings.embedding_batch_inputs, settings.embedding_batch_tokens]}
                                ).encode()).hexdigest()


def _ledger_view(settings: Settings, purpose: str) -> dict:
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM budget_settings WHERE id = 1").fetchone()
        totals = budget._totals(conn)
        used = budget._purpose_used(conn, purpose)
    envelopes = json.loads(row["envelopes_json"])
    rates = json.loads(row["rates_json"]).get(settings.embedding_model)
    return {"rates": rates, "rate_version": row["rate_version"], "paid_enabled": bool(row["paid_enabled"]),
            "envelope_micro_usd": envelopes.get(purpose), "envelope_remaining_micro_usd":
                envelopes.get(purpose, 0) - used,
            "available_micro_usd": row["cap_micro_usd"] - totals["spent"] - totals["pending"]}


def plan_embeddings(settings: Settings, index_version: str) -> dict:
    """Counts unique exact payload tokens, subtracts verified cache hits and states the maximum spend. No
    provider call. The stored estimate binds index, manifest, model, dimensions, payload set and rates."""
    row, payloads = index_payloads(settings, index_version)
    unique, missing = _unique_missing(settings, payloads)
    for p in missing.values():
        p["tokens"] = count_embedding_tokens(p["text"], settings.embedding_model)
    batches = plan_batches(settings, list(missing.values()))
    ledger = _ledger_view(settings, "embedding")
    if ledger["rates"] is None:
        raise DenseError(f"no configured rate for {settings.embedding_model}")
    batch_costs = [budget.max_cost(ledger["rates"], sum(p["tokens"] for p in b), 0) for b in batches]
    max_cost = sum(batch_costs)
    fits = max_cost <= min(ledger["envelope_remaining_micro_usd"], ledger["available_micro_usd"])
    now = datetime.now(timezone.utc)
    estimate = {
        "estimate_id": uuid.uuid4().hex[:12], "index_version": index_version, "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions, "policy": EMBED_POLICY, "chunks": len(payloads),
        "unique_payloads": len(unique), "cached_payloads": len(unique) - len(missing),
        "payloads_to_embed": len(missing), "tokens_to_embed": sum(p["tokens"] for p in missing.values()),
        "batches": len(batches), "max_cost_micro_usd": max_cost, "rate_version": ledger["rate_version"],
        "envelope_remaining_micro_usd": ledger["envelope_remaining_micro_usd"],
        "available_micro_usd": ledger["available_micro_usd"], "paid_enabled": ledger["paid_enabled"],
        "fits_envelope": fits, "count_method": COUNT_METHOD,
        "fingerprint": _fingerprint(settings, row, unique, ledger["rate_version"]),
        "created_at": now.isoformat(), "expires_at": (now + timedelta(hours=settings.embedding_estimate_ttl_hours)
                                                      ).isoformat(),
        "invalidated_by": "any change to index/manifest, model, dimensions, payload set, rates or batch limits; "
                          "expiry",
    }
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("INSERT INTO embedding_estimates(estimate_id, index_version, fingerprint, estimate_json, "
                     "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (estimate["estimate_id"], index_version, estimate["fingerprint"], dumps(estimate),
                      estimate["created_at"], estimate["expires_at"]))
    return estimate


# ---------------------------------------------------------------- build


def dense_version_for(settings: Settings, row: dict) -> str:
    return "d" + hashlib.sha256(dumps({"v": DENSE_VERSION, "index": row["index_version"],
                                       "manifest": row["manifest_hash"], "model": settings.embedding_model,
                                       "dims": settings.embedding_dimensions, "policy": EMBED_POLICY}
                                      ).encode()).hexdigest()[:15]


def build_dense(settings: Settings, transport: Transport | None, index_version: str, estimate_id: str,
                member_id: str = "owner-cli") -> dict:
    """Owner maintenance job. Embeds only cache misses in bounded, individually reserved batches; settled
    batches survive a failure. Publishes an immutable matrix only when every row is verified."""
    with open_db(settings.db_path) as conn:
        est = conn.execute("SELECT * FROM embedding_estimates WHERE estimate_id = ?", (estimate_id,)).fetchone()
    if est is None or est["index_version"] != index_version:
        raise DenseError("unknown estimate for this index; run plan-embeddings")
    estimate = json.loads(est["estimate_json"])
    if datetime.fromisoformat(est["expires_at"]) < datetime.now(timezone.utc):
        raise DenseError("estimate expired; run plan-embeddings again")
    row, payloads = index_payloads(settings, index_version)
    unique, missing = _unique_missing(settings, payloads)
    ledger = _ledger_view(settings, "embedding")
    if _fingerprint(settings, row, unique, ledger["rate_version"]) != est["fingerprint"]:
        raise DenseError("stale estimate: index, model, dimensions, payloads, rates or batch limits changed")
    if missing and not estimate["fits_envelope"]:
        raise DenseError("the estimate exceeds the embedding envelope or the remaining cap; reduce chunk profiles "
                         "or stay with K1 (an owner reallocation keeps the global cap)")
    open_attempts = unresolved_attempts(settings, "embedding")
    if missing and open_attempts:
        raise DenseError(f"embedding attempts with unknown billing are unresolved ({', '.join(open_attempts)}); "
                         "reconcile them with the provider usage before embedding again")
    request_id = ensure_job_request(settings, member_id, f"build-dense:{estimate_id}",
                                    {"job": "build-dense", "index": index_version, "estimate": estimate_id})
    for p in missing.values():
        p["tokens"] = count_embedding_tokens(p["text"], settings.embedding_model)
    done_batches, spent = 0, 0
    for batch in plan_batches(settings, list(missing.values())):
        result = metered_embed(settings, transport, [p["text"] for p in batch], sum(p["tokens"] for p in batch),
                               request_id=request_id, member_id=member_id, purpose="embedding")
        if result.get("settled_micro_usd"):
            spent += result["settled_micro_usd"]
        if result["status"] != "ok":
            out = {"status": result["status"], "reason": result["reason"], "embedded_batches": done_batches,
                   "attempt_id": result.get("attempt_id"), "settled_micro_usd": spent, "published": False,
                   "note": "cached vectors remain valid; no automatic resend of an unknown outcome"}
            finish_job_request(settings, request_id, "failed", out)
            return out
        for p, vec in zip(batch, result["vectors"]):
            cache_put(settings, p["payload_hash"], vec, {"kind": "chunk", "attempt_id": result["attempt_id"]})
        done_batches += 1
        if result.get("billing") == "unknown":
            # Vectors arrived but the provider reported no usage: keep them, spend nothing more until reconciled.
            out = {"status": "unknown", "reason": "provider returned no usage", "embedded_batches": done_batches,
                   "attempt_id": result["attempt_id"], "settled_micro_usd": spent, "published": False,
                   "note": "vectors are cached; reconcile the unknown attempt before embedding again"}
            finish_job_request(settings, request_id, "failed", out)
            return out
    out = publish_dense(settings, row, payloads)
    out.update(embedded_batches=done_batches, settled_micro_usd=spent)
    finish_job_request(settings, request_id, "completed", out)
    return out


def publish_dense(settings: Settings, row: dict, payloads: list[dict]) -> dict:
    """Assemble the matrix from verified cache entries, write it beside a manifest in a temporary sibling,
    verify it back, then register it `ready`. Never overwrites a published version."""
    version = dense_version_for(settings, row)
    final_dir = settings.data_dir / "indexes" / version
    with open_db(settings.db_path) as conn:
        existing = conn.execute("SELECT state FROM indexes WHERE index_version = ?", (version,)).fetchone()
    if existing and existing["state"] == "ready" and final_dir.exists():
        DenseIndex.load(settings, version)  # verify before reporting reuse
        return {"status": "ready", "dense_version": version, "reused": True, "published": False}
    matrix = np.zeros((len(payloads), settings.embedding_dimensions), dtype=np.float32)
    for i, p in enumerate(payloads):
        vec = cache_get(settings, p["payload_hash"])
        if vec is None:
            raise DenseError(f"row {i} has no verified cached vector; the matrix is not published")
        matrix[i] = vec
    tmp = settings.data_dir / "indexes" / f".tmp-{uuid.uuid4().hex}"
    try:
        write_bytes_atomic(tmp / "embeddings.npy", _npy_bytes(matrix))
        write_jsonl_atomic(tmp / "rows.jsonl", [{"row": i, "chunk_id": p["chunk_id"],
                                                 "payload_hash": p["payload_hash"]} for i, p in enumerate(payloads)])
        files = {n: hashlib.sha256((tmp / n).read_bytes()).hexdigest() for n in ("embeddings.npy", "rows.jsonl")}
        config = {"kind": "dense", "dense_version": DENSE_VERSION, "base_index_version": row["index_version"],
                  "base_manifest_hash": row["manifest_hash"], "model": settings.embedding_model,
                  "dimensions": settings.embedding_dimensions, "policy": EMBED_POLICY, "rows": len(payloads),
                  "unique_payloads": len({p["payload_hash"] for p in payloads})}
        manifest = {"index_version": version, "created_at": utcnow(), "config": config, "files": files,
                    "row_order": "keyword index chunks.jsonl line order"}
        write_text_atomic(tmp / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        if final_dir.exists():
            shutil.rmtree(final_dir)  # no ready row: a failed earlier publish
        tmp.rename(final_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    manifest_path = final_dir / "manifest.json"
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("DELETE FROM indexes WHERE index_version = ?", (version,))
        conn.execute(
            "INSERT INTO indexes(index_version, manifest_path, manifest_hash, source_set_hash, config_json, state, "
            "created_at) VALUES (?, ?, ?, ?, ?, 'building', ?)",
            (version, str(manifest_path), hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
             row["source_set_hash"], dumps(config), utcnow()))
    try:
        DenseIndex.load(settings, version, require_ready=False)
    except DenseError:
        with open_db(settings.db_path) as conn, tx(conn, immediate=True):
            conn.execute("UPDATE indexes SET state = 'failed' WHERE index_version = ?", (version,))
        raise
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("UPDATE indexes SET state = 'ready' WHERE index_version = ?", (version,))
    return {"status": "ready", "dense_version": version, "rows": len(payloads), "reused": False, "published": True}


@dataclass
class DenseIndex:
    version: str
    base_index_version: str
    model: str
    dims: int
    matrix: np.ndarray
    chunk_ids: list[str]

    @classmethod
    def load(cls, settings: Settings, version: str, base=None, require_ready: bool = True) -> "DenseIndex":
        """Verifies manifest, file hashes, dtype, shape, finiteness, unit norms and row order. `base`, when
        given, must be the keyword index the matrix was built from, row for row."""
        with open_db(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM indexes WHERE index_version = ?", (version,)).fetchone()
            base_row = None
            if row is not None:
                cfg = json.loads(row["config_json"])
                base_row = conn.execute("SELECT manifest_hash FROM indexes WHERE index_version = ?",
                                        (cfg.get("base_index_version"),)).fetchone()
        if row is None or (require_ready and row["state"] != "ready"):
            raise DenseError(f"dense matrix {version} is not ready")
        manifest_path = Path(row["manifest_path"])
        if not manifest_path.exists() or hashlib.sha256(manifest_path.read_bytes()).hexdigest() != row["manifest_hash"]:
            raise DenseError("dense manifest hash mismatch")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        config = manifest["config"]
        if config.get("kind") != "dense":
            raise DenseError(f"{version} is not a dense matrix")
        if base_row is None or base_row["manifest_hash"] != config["base_manifest_hash"]:
            raise DenseError("the keyword index this matrix was built from is missing or changed")
        for name, digest in manifest["files"].items():
            path = manifest_path.parent / name
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise DenseError(f"dense file hash mismatch: {name}")
        matrix = np.load(manifest_path.parent / "embeddings.npy", allow_pickle=False)
        rows = read_jsonl(manifest_path.parent / "rows.jsonl")
        dims = int(config["dimensions"])
        if matrix.dtype != np.float32 or matrix.shape != (config["rows"], dims) or len(rows) != config["rows"]:
            raise DenseError("dense matrix shape, dtype or row count does not match its manifest")
        if [r["row"] for r in rows] != list(range(len(rows))):
            raise DenseError("dense row mapping is not in row order")
        if matrix.size and (not np.all(np.isfinite(matrix))
                            or np.max(np.abs(np.linalg.norm(matrix, axis=1) - 1.0)) > NORM_TOLERANCE):
            raise DenseError("dense matrix has nonfinite or non-unit rows")
        chunk_ids = [r["chunk_id"] for r in rows]
        if base is not None and (base.version != config["base_index_version"]
                                 or [c["chunk_id"] for c in base.chunks] != chunk_ids):
            raise DenseError("dense rows do not match the keyword index rows")
        return cls(version, config["base_index_version"], config["model"], dims, matrix, chunk_ids)

    def search(self, query: np.ndarray, allowed: list[int], k: int) -> list[tuple[int, float]]:
        """Cosine by dot product over unit rows, restricted to admitted rows. Ties by chunk ID."""
        if not allowed:
            return []
        rows = np.asarray(allowed, dtype=np.int64)
        scores = self.matrix[rows] @ np.asarray(query, dtype=np.float32)
        order = sorted(range(len(rows)), key=lambda j: (-float(scores[j]), self.chunk_ids[rows[j]]))
        return [(int(rows[j]), float(scores[j])) for j in order[:k]]


# ---------------------------------------------------------------- optional local reranker


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    try:
        import psutil  # optional; Windows

        return round(psutil.Process(os.getpid()).memory_info().rss / 2 ** 20, 1)
    except Exception:  # noqa: BLE001 - memory is reported as unknown
        return None


class LocalReranker:
    """Cross-encoder loaded once per process; inference is bounded by a semaphore. Scores are raw model
    logits used only for ordering, never presented as a probability or confidence."""

    def __init__(self, settings: Settings, device: str | None = None) -> None:
        if not settings.reranker_revision:
            raise DenseError("reranker_revision is not pinned; set the model card's commit hash in the config file")
        if settings.reranker_max_concurrency != 1:
            raise DenseError("reranker_max_concurrency must be 1: the shared tokenizer is not safe for parallel "
                             "preprocessing (a concurrency-2 run crashed in CrossEncoder.predict)")
        import importlib.metadata as md

        from sentence_transformers import CrossEncoder

        rss0 = _rss_mb()
        t0 = time.perf_counter()
        self.model = CrossEncoder(settings.reranker_model, revision=settings.reranker_revision,
                                  max_length=settings.reranker_max_length, device=device)
        self.precision = settings.reranker_precision
        if self.precision == "fp16":
            if "cuda" not in str(getattr(self.model, "device", "")):
                raise DenseError("fp16 reranking is only supported on a CUDA device")
            inner = getattr(self.model, "model", self.model)  # the Hugging Face module inside the cross-encoder
            inner.half()
        self.cold_load_seconds = round(time.perf_counter() - t0, 2)
        self.max_length = settings.reranker_max_length
        self._sem = threading.BoundedSemaphore(settings.reranker_max_concurrency)
        versions = {}
        for pkg in ("torch", "transformers", "sentence-transformers"):
            try:
                versions[pkg] = md.version(pkg)
            except md.PackageNotFoundError:
                versions[pkg] = None
        self.info = {"model": settings.reranker_model, "revision": settings.reranker_revision,
                     "device": str(getattr(self.model, "device", device)), "max_length": self.max_length,
                     "max_concurrency": settings.reranker_max_concurrency, "precision": self.precision,
                     "cold_load_seconds": self.cold_load_seconds, "rss_before_mb": rss0, "rss_after_mb": _rss_mb(),
                     "versions": versions, "license": _model_license(settings)}
        try:
            import torch

            if torch.cuda.is_available():
                self.info["cuda_max_allocated_mb"] = round(torch.cuda.max_memory_allocated() / 2 ** 20, 1)
        except Exception:  # noqa: BLE001
            pass

    def rerank(self, question: str, chunks: list[dict]) -> tuple[list[tuple[int, float]], dict]:
        pairs = [(question, c["payload"]) for c in chunks]
        t0 = time.perf_counter()
        # Every use of the shared tokenizer (truncation counting and the prediction's own preprocessing) happens
        # under the one model lock: the tokenizer keeps mutable padding/truncation state.
        with self._sem:
            t1 = time.perf_counter()
            tok = self.model.tokenizer
            truncated = sum(len(tok(q, p)["input_ids"]) > self.max_length for q, p in pairs)
            scores = [float(s) for s in self.model.predict(pairs, show_progress_bar=False)] if pairs else []
            t2 = time.perf_counter()
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], chunks[i]["chunk_id"]))
        return [(i, scores[i]) for i in order], {"truncated": truncated, "queue_ms": round((t1 - t0) * 1000, 1),
                                                 "infer_ms": round((t2 - t1) * 1000, 1)}


def _model_license(settings: Settings) -> str | None:
    """The license the downloaded model card declares (front matter `license:`), read from local files."""
    try:
        from huggingface_hub import hf_hub_download

        card = Path(hf_hub_download(settings.reranker_model, "README.md", revision=settings.reranker_revision))
        for line in card.read_text(encoding="utf-8").splitlines()[:40]:
            if line.startswith("license:"):
                return line.split(":", 1)[1].strip()
    except Exception:  # noqa: BLE001 - reported as unknown
        return None
    return None


def load_reranker(settings: Settings) -> tuple[LocalReranker | None, dict]:
    """(reranker, info) or (None, failure info): a missing optional dependency, an unpinned revision, a download
    or memory failure all mean the bypass stays active."""
    try:
        r = LocalReranker(settings)
        return r, r.info
    except Exception as exc:  # noqa: BLE001 - any load failure keeps the bypass
        return None, {"error": f"{type(exc).__name__}: {exc}"[:500], "model": settings.reranker_model,
                      "revision": settings.reranker_revision or None}


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 2)
