"""Verified pgvector caches and exact retrieval over homogeneous embedding sets."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

import numpy as np
from pgvector import Vector

from ..storage.store import dumps, open_db, tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_payloads (
    payload_hash text PRIMARY KEY, model text NOT NULL, dimensions bigint NOT NULL,
    policy text NOT NULL, normalized_payload_sha256 text NOT NULL, vector_checksum text NOT NULL,
    provenance_json text NOT NULL, embedding vector NOT NULL,
    CHECK(vector_dims(embedding)=dimensions), CHECK(dimensions BETWEEN 1 AND 3072)
);
CREATE TABLE IF NOT EXISTS embedding_sets (
    set_version text PRIMARY KEY REFERENCES indexes(index_version) ON DELETE CASCADE,
    source_index text NOT NULL REFERENCES indexes(index_version), model text NOT NULL,
    dimensions bigint NOT NULL, policy text NOT NULL, expected_rows bigint NOT NULL,
    state text NOT NULL CHECK(state IN ('building','ready')), config_json text NOT NULL
);
CREATE TABLE IF NOT EXISTS embedding_set_rows (
    set_version text NOT NULL REFERENCES embedding_sets(set_version) ON DELETE CASCADE,
    row_order bigint NOT NULL, source_index text NOT NULL, chunk_id text NOT NULL,
    extraction_id text NOT NULL, payload_hash text NOT NULL REFERENCES embedding_payloads(payload_hash),
    PRIMARY KEY(set_version,row_order), UNIQUE(set_version,chunk_id),
    FOREIGN KEY(source_index,chunk_id) REFERENCES chunks(index_version,chunk_id)
);
CREATE INDEX IF NOT EXISTS embedding_scope ON embedding_set_rows(set_version,extraction_id,row_order);
CREATE INDEX IF NOT EXISTS embedding_set_payload ON embedding_set_rows(payload_hash,set_version);
"""
# Approximate index over one fixed dimension (standard HNSW accepts at most 2,000). Built by `build_hnsw`, not at
# startup: building takes longer than the application statement timeout.
HNSW_NAME = "embedding_payloads_hnsw_{d}"
HNSW_DDL = ("CREATE INDEX IF NOT EXISTS " + HNSW_NAME + " ON embedding_payloads USING hnsw "
            "((embedding::vector({d})) vector_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE dimensions = {d}")
HNSW_SQL = ("SELECT r.row_order, 1-(p.embedding::vector({d}) <=> ?) AS score FROM embedding_payloads p "
            "JOIN embedding_set_rows r USING(payload_hash) WHERE p.dimensions = {d} AND r.set_version = ? "
            "AND r.row_order = ANY(?) ORDER BY p.embedding::vector({d}) <=> ? LIMIT ?")
# Transaction-local. At this corpus size the planner prefers a scan plus sort (exact search); disabling those plans
# makes the ordered HNSW index scan the plan that actually runs, resumed past filtered rows in strict order.
HNSW_SESSION = ("SELECT set_config('hnsw.ef_search', ?, true), set_config('hnsw.iterative_scan', 'strict_order', true), "
                "set_config('enable_seqscan', 'off', true), set_config('enable_sort', 'off', true), "
                "set_config('enable_incremental_sort', 'off', true)")
EXACT_SQL = ("SELECT r.row_order,1-(p.embedding <=> ?) AS score FROM embedding_set_rows r "
             "JOIN embedding_payloads p USING(payload_hash) WHERE r.set_version=? "
             "AND r.row_order=ANY(?) ORDER BY p.embedding <=> ?,r.chunk_id COLLATE \"C\" LIMIT ?")


def build_hnsw(settings) -> dict:
    """Idempotent maintenance: the HNSW index for the serving dimension. Serving uses it only when an activated
    run selected `dense_search: hnsw` after its recall against exact search was measured."""
    d = int(settings.embedding_dimensions)
    started = time.perf_counter()
    with open_db(settings.db_path) as conn:
        conn.execute("SET statement_timeout = 0")
        conn.execute("SET maintenance_work_mem = '512MB'")
        # A parallel build places the graph in dynamic shared memory, which the container's 64 MB /dev/shm cannot
        # hold; a serial build keeps it in backend memory. ponytail: serial (~minutes at 19k vectors); raise
        # shm_size in compose.postgresql.yaml before re-enabling parallel workers for a larger corpus.
        conn.execute("SET max_parallel_maintenance_workers = 0")
        try:
            conn.execute(HNSW_DDL.format(d=d))
        finally:
            conn.execute("SET statement_timeout = '60s'")
            conn.execute("RESET maintenance_work_mem")
            conn.execute("RESET max_parallel_maintenance_workers")
        size = conn.execute("SELECT pg_relation_size(to_regclass(?))", (HNSW_NAME.format(d=d),)).fetchone()[0]
        rows = conn.execute("SELECT count(*) FROM embedding_payloads WHERE dimensions = ?", (d,)).fetchone()[0]
    return {"index": HNSW_NAME.format(d=d), "dimensions": d, "vectors": rows, "bytes": size,
            "parameters": {"m": 16, "ef_construction": 64}, "seconds": round(time.perf_counter() - started, 1)}


def checksum(vector):
    return hashlib.sha256(np.asarray(vector, dtype="<f4").tobytes()).hexdigest()


def verified(vector, dimensions, expected_checksum=None):
    from .dense import DenseError, NORM_TOLERANCE

    value = np.asarray(vector.to_numpy() if isinstance(vector, Vector) else vector, dtype=np.float32)
    if value.shape != (dimensions,) or not np.isfinite(value).all() or \
            abs(float(np.linalg.norm(value)) - 1) > NORM_TOLERANCE:
        raise DenseError("pgvector dimensions, finite values or unit normalization are invalid")
    if expected_checksum is not None and checksum(value) != expected_checksum:
        raise DenseError("pgvector vector checksum mismatch")
    return value


def _identity(settings):
    from .dense import embed_policy

    return settings.embedding_model, settings.embedding_dimensions, embed_policy(settings.embedding_model)


def cache_get(settings, payload_hash):
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM embedding_payloads WHERE payload_hash=?", (payload_hash,)).fetchone()
    if row is None or (row["model"], row["dimensions"], row["policy"]) != _identity(settings):
        return None
    return verified(row["embedding"], settings.embedding_dimensions, row["vector_checksum"])


def cached_hashes(settings, hashes):
    """The subset of `hashes` with a cache entry of this model, dimensions and policy (one query, not verified)."""
    model, dims, policy = _identity(settings)
    with open_db(settings.db_path) as conn:
        return {r[0] for r in conn.execute("SELECT payload_hash FROM embedding_payloads WHERE payload_hash = ANY(?) "
                                           "AND model=? AND dimensions=? AND policy=?",
                                           (list(hashes), model, dims, policy))}


def cache_put(settings, payload_hash, vector, provenance):
    cache_put_many(settings, [(payload_hash, vector, provenance)])


def cache_put_many(settings, entries):
    """Insert verified vectors in one transaction; an existing entry must be the same vector."""
    from .dense import DenseError

    model, dims, policy = _identity(settings)
    rows = []
    for payload_hash, vector, provenance in entries:
        vector = verified(vector, dims)
        text_hash = provenance.get("normalized_payload_sha256")
        if not isinstance(text_hash, str) or len(text_hash) != 64:
            raise DenseError("pgvector cache requires the normalized provider payload hash")
        rows.append((payload_hash, model, dims, policy, text_hash, checksum(vector), dumps(provenance), vector))
    if not rows:
        return
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.executemany("INSERT INTO embedding_payloads VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", rows)
        stored = {r["payload_hash"]: r for r in conn.execute(
            "SELECT payload_hash, model, dimensions, policy, normalized_payload_sha256, vector_checksum, embedding "
            "FROM embedding_payloads WHERE payload_hash = ANY(?)", ([r[0] for r in rows],))}
        for h, m, d, pol, text_hash, check, _, _ in rows:
            row = stored.get(h)
            if row is None or (row["model"], row["dimensions"], row["policy"], row["normalized_payload_sha256"],
                               row["vector_checksum"]) != (m, d, pol, text_hash, check):
                raise DenseError("refusing to overwrite a different verified pgvector cache entry")
            verified(row["embedding"], d, row["vector_checksum"])


def publish(settings, version, payloads, config):
    model, dims, policy = _identity(settings)
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("INSERT INTO embedding_sets VALUES (?,?,?,?,?,?,'building',?)",
                     (version, config["base_index_version"], model, dims, policy, len(payloads), dumps(config)))
        conn.executemany("INSERT INTO embedding_set_rows VALUES (?,?,?,?,?,?)",
                         [(version, i, config["base_index_version"], p["chunk_id"], p["extraction_id"], p["payload_hash"])
                          for i, p in enumerate(payloads)])
    PgDenseIndex.load(settings, version, require_ready=False)
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("UPDATE embedding_sets SET state='ready' WHERE set_version=?", (version,))


@dataclass
class PgDenseIndex:
    settings: object
    version: str
    base_index_version: str
    model: str
    dims: int
    chunk_ids: list[str]

    @classmethod
    def load(cls, settings, version, base=None, require_ready=True):
        from .dense import DenseError, embed_policy

        with open_db(settings.db_path) as conn:
            meta = conn.execute("SELECT * FROM embedding_sets WHERE set_version=?", (version,)).fetchone()
            if meta is None or (require_ready and meta["state"] != "ready"):
                raise DenseError("pgvector set is absent or incomplete; NumPy is not a serving fallback")
            config = json.loads(meta["config_json"])
            index = conn.execute("SELECT * FROM indexes WHERE index_version=?", (version,)).fetchone()
            source = conn.execute("SELECT manifest_hash FROM indexes WHERE index_version=?", (meta["source_index"],)).fetchone()
            if index is None or (require_ready and index["state"] != "ready") or source is None or \
                    source[0] != config["base_manifest_hash"] or json.loads(index["config_json"]) != config or \
                    (meta["model"], meta["dimensions"], meta["policy"]) != (config["model"], config["dimensions"], embed_policy(config["model"])):
                raise DenseError("pgvector set/source/configuration identity mismatch")
            rows = conn.execute("SELECT r.*,p.model,p.dimensions,p.policy,p.vector_checksum,p.embedding,c.extraction_id "
                                "AS expected_extraction FROM embedding_set_rows r JOIN embedding_payloads p USING(payload_hash) "
                                "JOIN chunks c ON c.index_version=r.source_index AND c.chunk_id=r.chunk_id "
                                "WHERE r.set_version=? ORDER BY r.row_order", (version,)).fetchall()
        if len(rows) != meta["expected_rows"] or [r["row_order"] for r in rows] != list(range(len(rows))):
            raise DenseError("pgvector set has incomplete or unordered row-to-chunk mappings")
        for row in rows:
            if (row["model"], row["dimensions"], row["policy"]) != (meta["model"], meta["dimensions"], meta["policy"]) or \
                    row["extraction_id"] != row["expected_extraction"]:
                raise DenseError("pgvector set mixes model, dimensions, policy or extraction scope")
            verified(row["embedding"], meta["dimensions"], row["vector_checksum"])
        ids = [r["chunk_id"] for r in rows]
        if base is not None and (base.version != meta["source_index"] or [c["chunk_id"] for c in base.chunks] != ids):
            raise DenseError("pgvector rows do not match the keyword source index")
        return cls(settings, version, meta["source_index"], meta["model"], meta["dimensions"], ids)

    def search(self, query, allowed, k, settings=None):
        """Top k admitted rows by cosine. `settings` (the run's) chooses exact search or the HNSW index with an
        iterative strict-order scan, so a filtered scope still fills k rows. Ties by chunk ID either way."""
        from .dense import DenseError

        settings = settings or self.settings
        if not allowed:
            return []
        if (settings.embedding_model, settings.embedding_dimensions) != (self.model, self.dims):
            raise DenseError("mixed model/dimension search was refused")
        vector = verified(query, self.dims)
        with open_db(self.settings.db_path) as conn:
            if settings.dense_search == "hnsw":
                with tx(conn):
                    conn.execute(HNSW_SESSION, (str(settings.hnsw_ef_search),))
                    rows = conn.execute(HNSW_SQL.format(d=self.dims), (vector, self.version, allowed, vector, k)).fetchall()
            else:
                rows = conn.execute(EXACT_SQL, (vector, self.version, allowed, vector, k)).fetchall()
        return sorted(((r[0], r[1]) for r in rows), key=lambda r: (-r[1], self.chunk_ids[r[0]]))
