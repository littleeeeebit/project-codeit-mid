"""SQLite initialization, bounded transactions and small shared queries.

Connections are opened per operation and closed explicitly; no transaction spans network inference.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 5  # 5: background requests, audit events, corrections, verifier runs
BUSY_TIMEOUT_MS = 5000

SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS sources (
    source_hash TEXT PRIMARY KEY,
    format TEXT NOT NULL CHECK (format IN ('hwp', 'pdf')),
    original_path TEXT NOT NULL,
    active_extraction_id TEXT,
    parse_status TEXT NOT NULL DEFAULT 'pending' CHECK (parse_status IN ('pending', 'parsed', 'quarantined')),
    review_status TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (review_status IN ('unreviewed', 'auto_verified', 'auto_flagged', 'sample_checked', 'reviewed',
                                 'needs_recovery')),
    reason_code TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]'
);"""

SCHEMA = SOURCES_DDL + """
CREATE TABLE IF NOT EXISTS fidelity_checks (
    extraction_id TEXT NOT NULL,
    method TEXT NOT NULL,
    source_hash TEXT NOT NULL REFERENCES sources(source_hash),
    verdict TEXT NOT NULL CHECK (verdict IN ('auto_verified', 'auto_flagged')),
    metrics_json TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    rendering_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (extraction_id, method)
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    csv_row_id INTEGER NOT NULL,
    filename TEXT NOT NULL UNIQUE,
    active_source_hash TEXT NOT NULL REFERENCES sources(source_hash),
    raw_metadata_json TEXT NOT NULL,
    normalized_metadata_json TEXT NOT NULL,
    quality_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_source ON documents(active_source_hash);
CREATE TABLE IF NOT EXISTS extractions (
    extraction_id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL REFERENCES sources(source_hash),
    parser_fingerprint TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    recovery_json TEXT
);
CREATE TABLE IF NOT EXISTS elements (
    extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id),
    element_id TEXT NOT NULL,
    source_order INTEGER NOT NULL,
    kind TEXT NOT NULL,
    parent_id TEXT,
    raw_text TEXT NOT NULL,
    search_text TEXT NOT NULL,
    location_json TEXT NOT NULL,
    table_json TEXT,
    PRIMARY KEY (extraction_id, element_id)
);
CREATE INDEX IF NOT EXISTS elements_order ON elements(extraction_id, source_order);
CREATE TABLE IF NOT EXISTS indexes (
    index_version TEXT PRIMARY KEY,
    manifest_path TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    source_set_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('building', 'ready', 'failed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    index_version TEXT NOT NULL REFERENCES indexes(index_version),
    chunk_id TEXT NOT NULL,
    extraction_id TEXT NOT NULL,
    row_order INTEGER NOT NULL,
    spans_json TEXT NOT NULL,
    payload TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    chunk_type TEXT NOT NULL,
    requirement_key TEXT,
    PRIMARY KEY (index_version, chunk_id)
);
CREATE INDEX IF NOT EXISTS chunks_extraction ON chunks(index_version, extraction_id);
CREATE INDEX IF NOT EXISTS chunks_requirement ON chunks(index_version, extraction_id, requirement_key);
CREATE TABLE IF NOT EXISTS requirements (
    index_version TEXT NOT NULL REFERENCES indexes(index_version),
    extraction_id TEXT NOT NULL,
    requirement_key TEXT NOT NULL,
    source_form TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('detail', 'summary')),
    element_id TEXT NOT NULL,
    name TEXT,
    PRIMARY KEY (index_version, extraction_id, requirement_key, element_id)
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL REFERENCES sources(source_hash),
    extraction_id TEXT,
    reviewer TEXT NOT NULL,
    status TEXT NOT NULL,
    locations_json TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    generation_id TEXT,
    input_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted')),
    trace_json TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    request_json TEXT,
    mode TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    UNIQUE (member_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS requests_status ON requests(status);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests(request_id),
    member_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'dispatching', 'settled', 'unknown', 'reconciled', 'released')),
    reserved_micro_usd INTEGER NOT NULL CHECK (reserved_micro_usd >= 0),
    settled_micro_usd INTEGER,
    estimated_input_tokens INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    count_method TEXT NOT NULL,
    raw_usage_json TEXT,
    price_json TEXT NOT NULL,
    response_id TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS attempts_response ON attempts(stage, model, response_id) WHERE response_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS attempts_state ON attempts(state);
CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id TEXT PRIMARY KEY,
    correction_key TEXT NOT NULL UNIQUE,
    amount_micro_usd INTEGER NOT NULL,
    interval_start TEXT,
    interval_end TEXT,
    scope TEXT NOT NULL,
    evidence TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    covered_attempts_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    allowance_micro_usd INTEGER NOT NULL,
    cap_micro_usd INTEGER NOT NULL,
    project_start TEXT,
    project_end TEXT,
    envelopes_json TEXT NOT NULL,
    rates_json TEXT NOT NULL,
    rate_version TEXT NOT NULL,
    paid_enabled INTEGER NOT NULL DEFAULT 0,
    prior_use_recorded INTEGER NOT NULL DEFAULT 0,
    frozen_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    history_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS gold_candidates (
    candidate_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    batch_sha256 TEXT NOT NULL,
    dataset TEXT NOT NULL,
    row_json TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    question_key TEXT NOT NULL,
    drafted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by TEXT,
    decided_at TEXT,
    reject_json TEXT,
    context_json TEXT,
    inference_json TEXT
);
CREATE INDEX IF NOT EXISTS gold_candidates_status ON gold_candidates(dataset, status);
CREATE TABLE IF NOT EXISTS extraction_inputs (
    source_hash TEXT PRIMARY KEY REFERENCES sources(source_hash),
    input_key TEXT NOT NULL,
    extraction_id TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metadata_resolutions (
    resolution_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id),
    field TEXT NOT NULL,
    value_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embedding_estimates (
    estimate_id TEXT PRIMARY KEY,
    index_version TEXT NOT NULL,  -- no FK: rebuilding an index version must not be blocked by old estimates
    fingerprint TEXT NOT NULL,
    estimate_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    reviewer TEXT NOT NULL,
    dataset TEXT,
    row_id TEXT,
    run_id TEXT,
    request_id TEXT,
    reason TEXT NOT NULL,
    quote TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verifier_runs (
    run_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    config_json TEXT NOT NULL,
    question TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    trace_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activations (
    activation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    config_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    previous_json TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


REQUEST_COLUMNS_V5 = {"request_json": "TEXT", "mode": "TEXT",
                      "cancel_requested": "INTEGER NOT NULL DEFAULT 0"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(db_path)) as conn:
        yield conn


@contextmanager
def tx(conn: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_schema(db_path: Path) -> int:
    """Idempotent: creates missing tables, never resets budget or ledger rows."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than this code ({SCHEMA_VERSION})")
        if 0 < version < 3:  # widen the review_status CHECK: SQLite rebuilds the table to change a constraint
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript(
                "BEGIN;" + SOURCES_DDL.replace("IF NOT EXISTS sources", "sources_v3")
                + "INSERT INTO sources_v3 SELECT * FROM sources; DROP TABLE sources;"
                  "ALTER TABLE sources_v3 RENAME TO sources; COMMIT;")
            conn.execute("PRAGMA foreign_keys = ON")
        if 0 < version < 5:  # phase 3 request columns; existing rows keep their history
            have = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
            for column, ddl in REQUEST_COLUMNS_V5.items():
                if column not in have:
                    conn.execute(f"ALTER TABLE requests ADD COLUMN {column} {ddl}")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return SCHEMA_VERSION


def get_app_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_app_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


class LockHeld(RuntimeError):
    pass


class ProcessLock:
    """Exclusive, non-blocking lock on one file for the life of one process's owner; released by `release()` or
    by the operating system when the process dies."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._f.close()
            raise LockHeld(str(path)) from None

    def release(self) -> None:
        if self._f.closed:
            return
        if os.name == "nt":
            import msvcrt

            self._f.seek(0)
            msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
        self._f.close()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def write_text_atomic(path: Path, text: str) -> None:
    """UTF-8 without BOM, written to a sibling temporary file then renamed."""
    write_bytes_atomic(path, text.encode("utf-8"))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Written to a sibling temporary file then renamed. On Windows the rename fails while any other process
    (a reader, an antivirus scan) has the target open, so it is retried for up to ~3 s."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        for attempt in range(16):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 15:
                    raise
                time.sleep(0.2)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_jsonl_atomic(path: Path, rows) -> None:
    write_text_atomic(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
