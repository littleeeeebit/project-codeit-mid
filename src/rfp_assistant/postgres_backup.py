"""PostgreSQL custom-format backup and isolated paid-disabled recovery checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from . import migration, postgres, store

METADATA_TABLES = {"database_control", "migration_import", "migration_checkpoints", "migration_validation", "schema_migrations",
                   "application_mutex"}


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
        tables[name] = {"columns": columns, **migration.postgres_digest(raw, name, columns)}
    return tables


def backup(settings, destination, actor):
    from . import evaluation, release

    destination = Path(destination)
    if not destination.is_absolute() or (destination.exists() and any(destination.iterdir())):
        raise ValueError("PostgreSQL backup destination must be an absolute new/empty directory")
    for live in (settings.data_dir, settings.source_dir):
        if destination.is_relative_to(live) or live.is_relative_to(destination):
            raise ValueError("backup destination must not overlap live runtime or originals")
    destination.mkdir(parents=True, exist_ok=True)
    target = settings.db_path
    lock = postgres.GatewayOwner(target)
    try:
        with store.open_db(target) as conn, store.tx(conn, immediate=True):
            if conn.execute("SELECT current_schema()").fetchone()[0] != "public":
                raise ValueError("backup supports the dedicated public application schema")
            tables = _table_manifest(conn.raw)
            references = migration._references(conn)
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
                        copied[rel.as_posix()] = migration.file_hash(output)
    finally:
        lock.release()
    manifest = {"backup_version": "postgresql-backup-1", "created_at": store.utcnow(), "actor": actor,
                "database_identity": identity, "dump_sha256": migration.file_hash(destination / "database.dump"),
                "tables": tables, "references": references, "copied": copied, "ledger": ledger,
                "code": evaluation.code_fingerprint()}
    path = destination / "manifest.json"
    store.write_text_atomic(path, json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"manifest": str(path), "tables": len(tables), "ledger": ledger, "provider_calls": 0}


def restore_check(settings, manifest_path, staging=None):
    from . import budget, release
    from .retrieval import KeywordIndex

    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dump = manifest_path.parent / "database.dump"
    if manifest.get("backup_version") != "postgresql-backup-1" or migration.file_hash(dump) != manifest["dump_sha256"]:
        raise ValueError("PostgreSQL backup format or dump hash is invalid")
    receipt = manifest_path.parent / "postgresql-restore-check.json"
    # This is the latest attempt's receipt; an earlier success must not describe a failed retry.
    receipt.unlink(missing_ok=True)
    target = postgres.Target("RFP_RESTORE_DATABASE_DSN")
    source_dsn = os.environ.get(settings.database_dsn_env)
    if source_dsn and conninfo_to_dict(target.dsn()) == conninfo_to_dict(source_dsn):
        raise ValueError("restore must use a different isolated database")
    with psycopg.connect(target.dsn(), autocommit=True, connect_timeout=5) as raw:
        if raw.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' LIMIT 1").fetchone():
            raise ValueError("restore target contains tables; use an empty isolated database")
        if not raw.execute("SELECT pg_try_advisory_lock(%s)", (postgres.GATEWAY_LOCK,)).fetchone()[0]:
            raise ValueError("restore target already has a paid gateway owner")
        if not raw.execute("SELECT pg_try_advisory_lock(%s)", (migration.IMPORT_LOCK,)).fetchone()[0]:
            raise ValueError("restore target already has a migration owner")
        # Persist outside pg_restore's transaction and exclude from all dumps/restores. A lost
        # connection or killed process therefore leaves a fence that copied control rows cannot clear.
        raw.execute("CREATE SCHEMA IF NOT EXISTS bidmate_recovery")
        raw.execute("CREATE TABLE IF NOT EXISTS bidmate_recovery.control "
                    "(id bigint PRIMARY KEY CHECK(id=1), state text NOT NULL)")
        raw.execute("INSERT INTO bidmate_recovery.control VALUES (1,'restoring') "
                    "ON CONFLICT(id) DO UPDATE SET state='restoring'")
        try:
            _run("pg_restore", target, ["--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction",
                                       "--exclude-schema=bidmate_recovery",
                                       "--dbname=" + conninfo_to_dict(target.dsn())["dbname"], str(dump)], manifest_path.parent)
            _disable_restored_control(raw)
            with store.database_lifecycle(target), store.open_db(target) as conn:
                actual = _table_manifest(conn.raw)
                checks = {"canonical application table parity": actual == manifest["tables"],
                          "staging paid admission disabled": not conn.execute(
                              "SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0],
                          "immutable originals/extractions/indexes": migration.references_valid(manifest["references"])}
                restored_ledger = release.ledger_summary(target)
                checks["ledger parity"] = restored_ledger == manifest["ledger"]
                for rel, expected in manifest["copied"].items():
                    checks[f"mutable file {rel}"] = migration.references_valid([
                        {"path": str(manifest_path.parent / "files" / rel), "expected_sha256": expected}])
                staged = settings.with_(database_backend="postgresql", database_dsn_env=target.dsn_env,
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
            # Finish pool shutdown before publishing on the session that still owns both locks.
            # The receipt and readiness become visible together; no filesystem export follows.
            with raw.transaction():
                raw.execute("SET LOCAL synchronous_commit='on'")
                if raw.execute("SHOW fsync").fetchone()[0] != "on":
                    raise RuntimeError("durable recovery publication requires PostgreSQL fsync=on")
                for lock in (postgres.GATEWAY_LOCK, migration.IMPORT_LOCK):
                    held = raw.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() "
                                       "AND locktype='advisory' AND classid=%s AND objid=%s AND granted)",
                                       (lock >> 32, lock & 0xFFFFFFFF)).fetchone()[0]
                    if not held:
                        raise RuntimeError("restore ownership was lost; target remains fenced")
                control = postgres.Connection(raw, target)
                migration.record_validation(control, snapshot, manifest["references"], report["passed"])
                if report["passed"]:
                    control.execute("UPDATE migration_import SET state='complete' WHERE id=1")
                control.execute("UPDATE bidmate_recovery.control SET state=? WHERE id=1",
                                ("verified" if report["passed"] else "failed",))
                _publish_restore_receipt(raw, report)
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
