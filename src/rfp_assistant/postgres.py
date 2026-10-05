"""Bounded PostgreSQL resources and the audited application SQL dialect boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool, PoolTimeout

SERVER_VERSION = 180006  # PostgreSQL 18.6, as libpq reports server_version_num
EXTENSION_VERSION = "0.8.6"
GATEWAY_LOCK = 0x4249444D415445
SCHEMA_LOCK = GATEWAY_LOCK + 1


@dataclass(frozen=True)
class Target:
    dsn_env: str = "RFP_DATABASE_DSN"
    max_connections: int = 8
    timeout: float = 5

    def dsn(self) -> str:
        value = os.environ.get(self.dsn_env)
        if not value:
            raise RuntimeError(f"{self.dsn_env} must contain the PostgreSQL connection string")
        return value


class Row(dict):
    """Named access and positional scalar access (`row["name"]`, `row[0]`)."""

    def __init__(self, names, values):
        self.values_tuple = values
        for name, value in zip(names, values):
            self.setdefault(name, value)

    def __getitem__(self, key):
        return self.values_tuple[key] if isinstance(key, int) else super().__getitem__(key)

    def __iter__(self):
        return iter(self.values_tuple)


def row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, tuple(int(v) if isinstance(v, Decimal) and v == v.to_integral_value()
                                          else v for v in values))


def bind_sql(sql: str) -> str:
    """Translate qmark parameters outside literals; escape literal percent signs for psycopg.

    Application SQL uses no dollar quoted strings. Refuse unhandled dialect features.
    Backup parity reads use psycopg.sql identifiers directly, outside this boundary.
    """
    if re.search(r"\b(PRAGMA|INSERT\s+OR|REPLACE\s+INTO)\b", sql, re.I):
        raise ValueError("non-PostgreSQL SQL (PRAGMA, INSERT OR, REPLACE INTO) reached the application boundary")
    parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/)", sql, flags=re.S)
    return "".join(part.replace("%", "%%") if i % 2 else part.replace("%", "%%").replace("?", "%s")
                   for i, part in enumerate(parts))


class Connection:
    def __init__(self, raw, target):
        self.raw = raw
        self.target = target

    def execute(self, sql, params=None):
        return self.raw.execute(bind_sql(sql), params or ())

    def executemany(self, sql, params):
        cursor = self.raw.cursor()
        cursor.executemany(bind_sql(sql), params)
        return cursor


_mutex = threading.RLock()
_pools: dict[Target, tuple[ConnectionPool, str, int]] = {}
_owners: dict[Target, "GatewayOwner"] = {}


def require_server(conn):
    """Every connection BidMate opens, pooled or direct, is to PostgreSQL 18.6 or is refused."""
    if conn.info.server_version != SERVER_VERSION:
        raise RuntimeError(f"PostgreSQL 18.6 is required; server reports {conn.info.server_version}")


def _configure(conn):
    require_server(conn)
    conn.execute("SET lock_timeout = '5s'")
    conn.execute("SET statement_timeout = '60s'")
    row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    version = row[0] if row else "absent"
    if version != EXTENSION_VERSION:
        raise RuntimeError(f"pgvector {EXTENSION_VERSION} is required; installed {version}")
    register_vector(conn)


@contextmanager
def lifecycle(target: Target):
    """One explicit owner per CLI or Resources; references share a bounded process pool.

    No implicit pool creation from a query. Checkout and startup timeouts are bounded.
    A connection belongs to one operation and is returned only after its transaction ends.
    """
    identity = hashlib.sha256(target.dsn().encode()).hexdigest()
    with _mutex:
        if target in _pools:
            pool, previous, references = _pools[target]
            if previous != identity:
                raise RuntimeError("PostgreSQL DSN changed while its pool was owned")
            _pools[target] = pool, identity, references + 1
        else:
            # A failing configure only times the pool out; probe once so a wrong server says why.
            with psycopg.connect(target.dsn(), autocommit=True, connect_timeout=int(target.timeout)) as probe:
                require_server(probe)
            pool = ConnectionPool(target.dsn(), min_size=1, max_size=target.max_connections,
                                  timeout=target.timeout, max_waiting=48, open=False,
                                  kwargs={"autocommit": True, "connect_timeout": int(target.timeout)},
                                  configure=_configure, check=ConnectionPool.check_connection)
            try:
                pool.open(wait=True, timeout=target.timeout)
            except BaseException:
                pool.close()
                raise RuntimeError("PostgreSQL pool startup failed; verify DSN, server and pgvector extension") from None
            _pools[target] = pool, identity, 1
    try:
        yield pool
    finally:
        with _mutex:
            pool, identity, references = _pools[target]
            if references == 1:
                del _pools[target]
                pool.close(timeout=target.timeout)
            else:
                _pools[target] = pool, identity, references - 1


@contextmanager
def open_db(target: Target):
    with _mutex:
        entry = _pools.get(target)
    if entry is None:
        raise RuntimeError("PostgreSQL pool has no resource owner; enter database_lifecycle first")
    if entry[1] != hashlib.sha256(target.dsn().encode()).hexdigest():
        raise RuntimeError("PostgreSQL DSN changed while its pool was owned")
    with entry[0].connection(timeout=target.timeout) as conn:
        conn.row_factory = row_factory
        yield Connection(conn, target)


def recovery_blocked(conn):
    exists = conn.execute("SELECT to_regclass('bidmate_recovery.control')").fetchone()[0]
    if not exists:
        return False
    row = conn.execute("SELECT state FROM bidmate_recovery.control WHERE id=1").fetchone()
    if row is None or row[0] != "verified":
        return True
    if not conn.execute("SELECT to_regclass('bidmate_recovery.receipt')").fetchone()[0]:
        return True
    receipt = conn.execute("SELECT report_json FROM bidmate_recovery.receipt WHERE id=1").fetchone()
    try:
        report = json.loads(receipt[0]) if receipt else {}
        return report.get("passed") is not True or report.get("restore_check_version") != "postgresql-restore-check-1"
    except (ValueError, TypeError, AttributeError):
        return True


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def references_valid(references):
    """Every recorded original, extraction and index manifest (with the files it lists) is present and unchanged."""
    try:
        for reference in references:
            path = Path(reference["path"])
            if not path.is_file() or file_hash(path) != reference["expected_sha256"]:
                return False
            if reference.get("kind") == "index":
                manifest = json.loads(path.read_text(encoding="utf-8"))
                for name, digest in manifest["files"].items():
                    artifact = path.parent / name
                    if not artifact.is_file() or file_hash(artifact) != digest:
                        return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def record_validation(conn, snapshot_sha256, references, passed):
    """The validated-database marker startup requires; a failed validation also closes paid admission."""
    conn.execute("CREATE TABLE IF NOT EXISTS migration_validation (id bigint PRIMARY KEY CHECK(id=1), "
                 "snapshot_sha256 text NOT NULL, references_json text NOT NULL, passed boolean NOT NULL)")
    conn.execute("INSERT INTO migration_validation VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                 "snapshot_sha256=excluded.snapshot_sha256,references_json=excluded.references_json,passed=excluded.passed",
                 (snapshot_sha256, json.dumps(references, ensure_ascii=False), passed))
    if not passed:
        conn.execute("UPDATE database_control SET paid_admission=false WHERE id=1")


def validation_ready(conn):
    exists = conn.execute("SELECT to_regclass(current_schema() || '.migration_validation')").fetchone()[0]
    if not exists:
        return False
    row = conn.execute("SELECT v.passed AND v.snapshot_sha256=m.snapshot_sha256 AND m.state='complete' "
                       "FROM migration_validation v JOIN migration_import m ON m.id=v.id WHERE v.id=1").fetchone()
    return row is not None and row[0]


def require_imported_database(target: Target):
    """Reject application/maintenance startup on an empty, partial or unvalidated database. There is no fallback:
    the validated database is the final import or a restore of its verified dump that passed restore-check."""
    with open_db(target) as conn:
        if recovery_blocked(conn):
            raise RuntimeError("PostgreSQL recovery is incomplete or failed; startup and paid work are blocked")
        exists = conn.execute("SELECT to_regclass(current_schema() || '.migration_import')").fetchone()[0]
        imported = conn.execute("SELECT state FROM migration_import WHERE id=1").fetchone() if exists else None
        if imported is None or imported[0] != "complete" or not validation_ready(conn):
            raise RuntimeError("this PostgreSQL database has no validated import: point RFP_DATABASE_DSN at the "
                               "validated application database, or restore the verified dump with restore-check")
        references = json.loads(conn.execute("SELECT references_json FROM migration_validation WHERE id=1").fetchone()[0])
        if not references_valid(references):
            raise RuntimeError("PostgreSQL validated artifacts are missing or changed; validation and startup are blocked")


@contextmanager
def tx(conn: Connection, immediate=False):
    with conn.raw.transaction():
        if immediate:
            # ponytail: one write mutex serializes admission/idempotency;
            # use per-account locks only if measured throughput requires them.
            conn.execute("SELECT id FROM application_mutex WHERE id = 1 FOR UPDATE")
        yield conn


@contextmanager
def read_snapshot(conn: Connection):
    """One read-only view: every read inside sees the same committed state (pooled connections autocommit)."""
    with conn.raw.transaction():
        conn.raw.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield conn


def init_schema(target: Target, schema: str, version: int):
    with open_db(target) as conn, conn.raw.transaction():
        conn.raw.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        conn.raw.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version bigint PRIMARY KEY)")
        current = conn.raw.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        if current is not None and current > version:
            raise RuntimeError("PostgreSQL schema is newer than this application")
        conn.raw.execute(schema.replace("INTEGER", "BIGINT").replace("TEXT", 'TEXT COLLATE "C"'))
        from .vector_store import SCHEMA as vector_schema
        conn.raw.execute(vector_schema)
        conn.raw.execute("CREATE TABLE IF NOT EXISTS application_mutex (id bigint PRIMARY KEY CHECK(id=1))")
        conn.raw.execute("INSERT INTO application_mutex VALUES (1) ON CONFLICT DO NOTHING")
        conn.raw.execute("INSERT INTO schema_migrations VALUES (%s) ON CONFLICT DO NOTHING", (version,))
        conn.raw.execute("CREATE TABLE IF NOT EXISTS database_control (id bigint PRIMARY KEY CHECK(id=1), "
                         "identity text NOT NULL, paid_admission boolean NOT NULL DEFAULT false)")
        conn.raw.execute("INSERT INTO database_control VALUES (1,%s,false) ON CONFLICT DO NOTHING", (str(uuid.uuid4()),))
    return version


def schema_version(conn):
    return conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]


class GatewayOwner:
    """Database-wide session advisory lock, held on one dedicated connection until shutdown.

    It consumes one connection in addition to the bounded operation pool. Never reconnect
    after loss. A new owner recovers dispatched work as unknown before accepting calls.
    """

    def __init__(self, target: Target):
        from .store import LockHeld

        self.target = target
        self.lock = threading.Lock()
        self.lost = False
        self.users = 1  # this creator plus every borrow_owner(); the lock is released with the last user
        self.conn = psycopg.connect(target.dsn(), autocommit=True, connect_timeout=int(target.timeout))
        try:
            require_server(self.conn)
            self.conn.execute("SET statement_timeout='5s'")
            if not self.conn.execute("SELECT pg_try_advisory_lock(%s)", (GATEWAY_LOCK,)).fetchone()[0]:
                raise LockHeld("PostgreSQL paid gateway already has an owner")
            with _mutex:
                _owners[target] = self
        except BaseException:
            self.conn.close()
            raise

    def check(self):
        with self.lock:
            if self.lost or self.conn.closed:
                raise RuntimeError("PostgreSQL paid gateway ownership was lost; no further dispatch is allowed")
            try:
                held = self.conn.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() "
                                         "AND locktype='advisory' AND classid=%s AND objid=%s AND granted)",
                                         (GATEWAY_LOCK >> 32, GATEWAY_LOCK & 0xFFFFFFFF)).fetchone()[0]
                if not held:
                    self.lost = True
                    raise RuntimeError("PostgreSQL paid gateway ownership was lost")
                if recovery_blocked(self.conn) or not validation_ready(self.conn):
                    raise RuntimeError("PostgreSQL recovery or artifact validation is not ready; no paid dispatch allowed")
            except psycopg.Error:
                self.lost = True
                raise RuntimeError("PostgreSQL paid gateway ownership was lost; restart and reconcile unknown billing") from None

    def release(self):
        """Drop one user; the last one closes the session and so releases the advisory lock."""
        with _mutex:
            self.users -= 1
            if self.users > 0:
                return
            if _owners.get(self.target) is self:
                del _owners[self.target]
        with self.lock:
            self.lost = True
            self.conn.close()


def process_owner(target):
    """This process's live gateway owner for the target, if any (several Resources in one process share it)."""
    with _mutex:
        owner = _owners.get(target)
    return owner if owner is not None and not owner.lost and not owner.conn.closed else None


def borrow_owner(target):
    """Share this process's live owner: the caller becomes one more user and must release() it."""
    with _mutex:
        owner = _owners.get(target)
        if owner is None or owner.lost or owner.conn.closed:
            return None
        owner.users += 1
        return owner


def require_owner(target):
    if isinstance(target, Target):
        with _mutex:
            owner = _owners.get(target)
        if owner is None:
            raise RuntimeError("PostgreSQL paid gateway has no owner")
        owner.check()  # paid admission itself is refused inside the ledger transaction, as a budget reason


def owner_guard(conn):
    """Check the owning session at the durable admission/dispatch boundary as well as before checkout."""
    if not isinstance(conn, Connection):
        return None
    if recovery_blocked(conn) or not validation_ready(conn):
        return "postgresql_recovery_or_validation"
    with _mutex:
        owner = _owners.get(conn.target)
    if owner is None or owner.lost or owner.conn.closed:
        return "gateway_ownership_lost"
    held = conn.raw.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' "
                            "AND classid=%s AND objid=%s AND granted)",
                            (owner.conn.info.backend_pid, GATEWAY_LOCK >> 32, GATEWAY_LOCK & 0xFFFFFFFF)).fetchone()[0]
    return None if held else "gateway_ownership_lost"


def gateway_lock(target, data_dir=None):
    return GatewayOwner(target)
