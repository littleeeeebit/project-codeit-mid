"""PostgreSQL custom-format backup and isolated paid-disabled recovery checks (the rollback path)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import numpy as np
import psycopg
from pgvector import Vector
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from . import postgres, store
from .postgres import file_hash, host_path, record_validation, references_valid

# migration_* tables hold the validated-import marker of the final import; restores carry them over.
METADATA_TABLES = {"database_control", "migration_import", "migration_checkpoints", "migration_validation", "schema_migrations",
                   "application_mutex"}
RESTORE_LOCK = postgres.SCHEMA_LOCK + 2


def record_hash(values):
    """Lossless typed values: preserve JSON bytes, null/zero, Unicode, binary values and float32 vectors."""
    canonical = []
    for value in values:
        if isinstance(value, Vector):
            value = value.to_numpy()
        if value is None:
            canonical.append(["null"])
        elif isinstance(value, bytes):
            canonical.append(["bytes", base64.b64encode(value).decode("ascii")])
        elif isinstance(value, float):
            canonical.append(["float", value.hex()])
        elif isinstance(value, int):
            canonical.append(["integer", str(value)])
        elif isinstance(value, str):
            canonical.append(["text", value])
        elif isinstance(value, np.ndarray) and value.dtype == np.float32:
            canonical.append(["vector-f32", base64.b64encode(value.astype("<f4").tobytes()).decode("ascii")])
        else:
            raise ValueError(f"unsupported backup value type: {type(value).__name__}")
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode()).digest()


def postgres_digest(raw, name, columns):
    """Row count and an order-independent digest of one table, streamed in bounded batches."""
    with raw.transaction(), raw.cursor(name="parity_" + uuid.uuid4().hex) as cursor:
        cursor.execute(sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(name)))
        hashes = []
        while batch := cursor.fetchmany(1000):
            hashes.extend(record_hash(tuple(row)) for row in batch)
    return {"rows": len(hashes), "canonical_sha256": hashlib.sha256(b"".join(sorted(hashes))).hexdigest()}


def references(conn):
    """Originals, extractions and index manifests the database points at, with their hashes."""
    found = []
    queries = [("original", "SELECT original_path, source_hash FROM sources"),
               ("extraction", "SELECT artifact_path, NULL FROM extractions"),
               ("index", "SELECT manifest_path, manifest_hash FROM indexes")]
    for kind, query in queries:
        for path, expected in conn.execute(query):
            actual = file_hash(host_path(path)) if host_path(path).is_file() else None
            found.append({"kind": kind, "path": path, "expected_sha256": expected or actual,
                          "accessible": actual is not None, "matches": actual is not None and
                          (expected is None or actual == expected)})
    return found


# A parallel HNSW build asks for a dynamic shared-memory segment as large as maintenance_work_mem, which the
# container's default 64 MB /dev/shm cannot hold; serial builds use local memory.
SESSION_OPTIONS = {"pg_restore": "-c max_parallel_maintenance_workers=0"}


def _run(program, target, args, log_dir):
    binary = shutil.which(program)
    if not binary:
        directory = Path(os.environ.get("RFP_PG_BIN", "C:/Program Files/PostgreSQL/18/bin"))
        binary = str(directory / (program + (".exe" if os.name == "nt" else "")))
    if not Path(binary).is_file():
        raise RuntimeError(f"PostgreSQL 18 {program} is required; set RFP_PG_BIN")
    environment = os.environ.copy()
    mapping = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD",
               "dbname": "PGDATABASE", "sslmode": "PGSSLMODE", "options": "PGOPTIONS"}
    for key, value in conninfo_to_dict(target.dsn()).items():
        if key not in mapping and key != "connect_timeout":
            raise ValueError(f"unsupported backup DSN option {key}")
        if key in mapping:
            environment[mapping[key]] = value
    if program in SESSION_OPTIONS:
        environment["PGOPTIONS"] = f"{environment.get('PGOPTIONS', '')} {SESSION_OPTIONS[program]}".strip()
    result = subprocess.run([binary, *args], env=environment, capture_output=True, timeout=600)
    if result.returncode:
        store.write_bytes_atomic(log_dir / f"{program}-failure.log", result.stderr)
        raise RuntimeError(f"{program} failed; inspect the private {program}-failure.log")


def _table_manifest(raw):
    tables = {}
    for name, in raw.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall():
        if name in METADATA_TABLES:
            continue
        columns = [r[0] for r in raw.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s "
            "ORDER BY ordinal_position", (name,))]
        tables[name] = {"columns": columns, **postgres_digest(raw, name, columns)}
    return tables


def backup(settings, destination, actor, share_owner=False):
    from ..evaluation import evaluation, release

    destination = Path(destination)
    if not destination.is_absolute() or (destination.exists() and any(destination.iterdir())):
        raise ValueError("PostgreSQL backup destination must be an absolute new/empty directory")
    for live in (settings.data_dir, settings.source_dir):
        if destination.is_relative_to(live) or live.is_relative_to(destination):
            raise ValueError("backup destination must not overlap live runtime or originals")
    destination.mkdir(parents=True, exist_ok=True)
    target = settings.db_path
    # The maintenance sequence shares its own process's gateway owner (the serving app's button, or the `maintain`
    # command's); everything else, and any other process's owner, is refused.
    lock = (share_owner and postgres.borrow_owner(target)) or postgres.GatewayOwner(target)
    try:
        with store.open_db(target) as conn, store.tx(conn, immediate=True):
            if conn.execute("SELECT current_schema()").fetchone()[0] != "public":
                raise ValueError("backup supports the dedicated public application schema")
            tables = _table_manifest(conn.raw)
            referenced = references(conn)
            ledger = release.ledger_summary(target)
            identity = conn.execute("SELECT identity FROM database_control WHERE id=1").fetchone()[0]
            _run("pg_dump", target, ["--format=custom", "--no-owner", "--no-privileges",
                                      "--exclude-schema=bidmate_recovery",
                                      "--file=" + str(destination / "database.dump")], destination)
            copied = {}
            for tree in release.COPIED_TREES:
                root = settings.data_dir / tree
                for path in sorted(root.rglob("*")) if root.exists() else []:
                    if path.is_file() and not path.name.startswith("."):
                        rel = path.relative_to(settings.data_dir)
                        output = destination / "files" / rel
                        output.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, output)
                        copied[rel.as_posix()] = file_hash(output)
    finally:
        lock.release()
    manifest = {"backup_version": "postgresql-backup-1", "created_at": store.utcnow(), "actor": actor,
                "database_identity": identity, "dump_sha256": file_hash(destination / "database.dump"),
                "tables": tables, "references": referenced, "copied": copied, "ledger": ledger,
                "code": evaluation.code_fingerprint()}
    path = destination / "manifest.json"
    store.write_text_atomic(path, json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"manifest": str(path), "tables": len(tables), "ledger": ledger, "provider_calls": 0}


def _claim_restore_target(raw) -> None:
    """An empty target this session owns as paid gateway and restorer, fenced as 'restoring'."""
    postgres.require_server(raw)
    if raw.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' LIMIT 1").fetchone():
        raise ValueError("restore target contains tables; use an empty isolated database")
    if not raw.execute("SELECT pg_try_advisory_lock(%s)", (postgres.GATEWAY_LOCK,)).fetchone()[0]:
        raise ValueError("restore target already has a paid gateway owner")
    if not raw.execute("SELECT pg_try_advisory_lock(%s)", (RESTORE_LOCK,)).fetchone()[0]:
        raise ValueError("restore target already has a restore owner")
    # Persist outside pg_restore's transaction and exclude from all dumps/restores. A lost
    # connection or killed process therefore leaves a fence that copied control rows cannot clear.
    raw.execute("CREATE SCHEMA IF NOT EXISTS bidmate_recovery")
    raw.execute("CREATE TABLE IF NOT EXISTS bidmate_recovery.control "
                "(id bigint PRIMARY KEY CHECK(id=1), state text NOT NULL)")
    raw.execute("INSERT INTO bidmate_recovery.control VALUES (1,'restoring') "
                "ON CONFLICT(id) DO UPDATE SET state='restoring'")


def _restored_report(settings, target, manifest: dict, manifest_path: Path) -> tuple[dict, str]:
    """The checks of the restored database against its manifest, after a restart recovery, and its snapshot hash."""
    from ..evaluation import release
    from ..gateway import budget
    from ..retrieval.retrieval import KeywordIndex

    with store.database_lifecycle(target), store.open_db(target) as conn:
        actual = _table_manifest(conn.raw)
        checks = {"canonical application table parity": actual == manifest["tables"],
                  "staging paid admission disabled": not conn.execute(
                      "SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0],
                  "immutable originals/extractions/indexes": references_valid(manifest["references"])}
        restored_ledger = release.ledger_summary(target)
        checks["ledger parity"] = restored_ledger == manifest["ledger"]
        for rel, expected in manifest["copied"].items():
            checks[f"mutable file {rel}"] = references_valid([
                {"path": str(manifest_path.parent / "files" / rel), "expected_sha256": expected}])
        staged = settings.with_(database_dsn_env=target.dsn_env,
                                database_pool_max=target.max_connections,
                                database_timeout_seconds=target.timeout, provider="fake")
        try:
            checks["historical evidence index"] = KeywordIndex.load(staged) is not None
        except Exception:
            checks["historical evidence index"] = False
        budget.recover(target)
        after = release.ledger_summary(target)
        report = {"restore_check_version": "postgresql-restore-check-1", "passed": all(checks.values()),
                  "checks": checks, "restored_ledger": restored_ledger, "after_restart_recovery": after,
                  "provider": "fake", "paid_admission": False, "provider_calls": 0,
                  "checked_at": store.utcnow(), "backup_dump_sha256": manifest["dump_sha256"],
                  "receipt": "bidmate_recovery.receipt/1"}
        snapshot = conn.execute("SELECT snapshot_sha256 FROM migration_import WHERE id=1").fetchone()[0]
    return report, snapshot


def _publish_verified(raw, target, manifest: dict, report: dict, snapshot: str) -> None:
    """Durably records the validation, the import state, the fence state and the receipt in one transaction, on
    the session that still owns both locks."""
    with raw.transaction():
        raw.execute("SET LOCAL synchronous_commit='on'")
        if raw.execute("SHOW fsync").fetchone()[0] != "on":
            raise RuntimeError("durable recovery publication requires PostgreSQL fsync=on")
        for lock in (postgres.GATEWAY_LOCK, RESTORE_LOCK):
            held = raw.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() "
                               "AND locktype='advisory' AND classid=%s AND objid=%s AND granted)",
                               (lock >> 32, lock & 0xFFFFFFFF)).fetchone()[0]
            if not held:
                raise RuntimeError("restore ownership was lost; target remains fenced")
        control = postgres.Connection(raw, target)
        record_validation(control, snapshot, manifest["references"], report["passed"])
        if report["passed"]:
            control.execute("UPDATE migration_import SET state='complete' WHERE id=1")
        control.execute("UPDATE bidmate_recovery.control SET state=? WHERE id=1",
                        ("verified" if report["passed"] else "failed",))
        _publish_restore_receipt(raw, report)


def restore_check(settings, manifest_path, staging=None):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dump = manifest_path.parent / "database.dump"
    if manifest.get("backup_version") != "postgresql-backup-1" or file_hash(dump) != manifest["dump_sha256"]:
        raise ValueError("PostgreSQL backup format or dump hash is invalid")
    receipt = manifest_path.parent / "postgresql-restore-check.json"
    # This is the latest attempt's receipt; an earlier success must not describe a failed retry.
    receipt.unlink(missing_ok=True)
    target = postgres.Target("RFP_RESTORE_DATABASE_DSN")
    for live in {settings.database_dsn_env, "RFP_DATABASE_DSN"} - {target.dsn_env}:
        source_dsn = os.environ.get(live)
        if source_dsn and conninfo_to_dict(target.dsn()) == conninfo_to_dict(source_dsn):
            raise ValueError("restore must use a different isolated database")
    with psycopg.connect(target.dsn(), autocommit=True, connect_timeout=5) as raw:
        _claim_restore_target(raw)
        try:
            _run("pg_restore", target, ["--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction",
                                       "--exclude-schema=bidmate_recovery",
                                       "--dbname=" + conninfo_to_dict(target.dsn())["dbname"], str(dump)], manifest_path.parent)
            _disable_restored_control(raw)
            report, snapshot = _restored_report(settings, target, manifest, manifest_path)
            # Finish pool shutdown before publishing on the session that still owns both locks.
            # The receipt and readiness become visible together; no filesystem export follows.
            _publish_verified(raw, target, manifest, report, snapshot)
            return report
        except BaseException:
            try:
                _disable_restored_control(raw)
                raw.execute("UPDATE bidmate_recovery.control SET state='failed' WHERE id=1")
            except psycopg.Error:
                pass  # The already-committed 'restoring' fence survives even when cleanup cannot connect.
            raise


def _publish_restore_receipt(raw, report):
    """The authoritative receipt and readiness share one durable PostgreSQL commit."""
    raw.execute("CREATE TABLE bidmate_recovery.receipt "
                "(id bigint PRIMARY KEY CHECK(id=1), report_json text NOT NULL)")
    raw.execute("INSERT INTO bidmate_recovery.receipt VALUES (1,%s)",
                (json.dumps(report, ensure_ascii=False, indent=2),))


def _disable_restored_control(raw):
    for table, change in (("database_control", "paid_admission=false"),
                          ("migration_import", "state='restore_pending'"),
                          ("migration_validation", "passed=false")):
        if raw.execute("SELECT to_regclass(%s)", ("public." + table,)).fetchone()[0]:
            raw.execute(f"UPDATE public.{table} SET {change} WHERE id=1")
