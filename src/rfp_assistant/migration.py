"""Provider-free SQLite snapshot planning, bounded PostgreSQL import and parity validation.

All plans and snapshots contain private application data and belong outside Git.
The imported ledger retains its original values; an independent database control
blocks paid admission during rehearsal, without rewriting billing history.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import re
import sqlite3
import sys
import uuid
import numpy as np
from pgvector import Vector
from contextlib import closing
from pathlib import Path

from psycopg import sql

from . import postgres, store

IMPORT_LOCK = postgres.SCHEMA_LOCK + 2
CONTROL_DDL = """
CREATE TABLE IF NOT EXISTS database_control (
    id bigint PRIMARY KEY CHECK(id=1), identity text NOT NULL,
    paid_admission boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS migration_import (
    id bigint PRIMARY KEY CHECK(id=1), snapshot_sha256 text NOT NULL,
    plan_sha256 text NOT NULL, state text NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_checkpoints (
    table_name text PRIMARY KEY, imported_rows bigint NOT NULL
);
"""


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_source(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def record_hash(values):
    """Lossless typed values: preserve JSON bytes, null/zero, Unicode and binary values."""
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
            raise ValueError(f"unsupported migration value type: {type(value).__name__}")
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode()).digest()


def table_digest(cursor):
    # Only fixed-width hashes are retained; source payloads stream in bounded batches.
    hashes = []
    while batch := cursor.fetchmany(1000):
        hashes.extend(record_hash(tuple(row)) for row in batch)
    return {"rows": len(hashes), "canonical_sha256": hashlib.sha256(b"".join(sorted(hashes))).hexdigest()}


def postgres_digest(raw, name, columns):
    with raw.transaction(), raw.cursor(name="parity_" + uuid.uuid4().hex) as cursor:
        cursor.execute(sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(name)))
        return table_digest(cursor)


def _name(name):
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"unsupported SQLite identifier: {name!r}")
    return '"' + name + '"'


def dependency_order(tables):
    pending = {name: set(item["references"]) for name, item in tables.items()}
    result = []
    while pending:
        ready = sorted(name for name, dependencies in pending.items() if not dependencies - set(result) - {name})
        if not ready:
            raise ValueError("source schema has unresolved/cyclic foreign keys; explicit migration is required")
        result.extend(ready)
        for name in ready:
            del pending[name]
    return result


def sql_inventory(root):
    inventory = []
    methods = {"execute", "executemany", "executescript", "connect", "open_db", "tx", "load", "save"}
    paths = list((root / "src" / "rfp_assistant").glob("*.py")) + list((root / "tools").rglob("*.py"))
    for path in sorted(paths):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                method = node.func.attr if isinstance(node.func, ast.Attribute) else \
                    node.func.id if isinstance(node.func, ast.Name) else ""
                if method in methods:
                    inventory.append({"file": str(path.relative_to(root)).replace("\\", "/"),
                                      "line": node.lineno, "method": method})
    return inventory


def _references(conn):
    references = []
    queries = [("original", "SELECT original_path, source_hash FROM sources"),
               ("extraction", "SELECT artifact_path, NULL FROM extractions"),
               ("index", "SELECT manifest_path, manifest_hash FROM indexes")]
    for kind, query in queries:
        for path, expected in conn.execute(query):
            actual = file_hash(path) if Path(path).is_file() else None
            references.append({"kind": kind, "path": path, "expected_sha256": expected or actual,
                               "accessible": actual is not None, "matches": actual is not None and
                               (expected is None or actual == expected)})
    return references


def plan(source: Path, output: Path):
    """Read-only source backup includes committed WAL contents. Never initialize the source."""
    source, output = source.resolve(), output.resolve()
    if not source.is_file() or source == output or output.exists():
        raise ValueError("source must exist and --output must be a new plan file")
    if output.is_relative_to(store.Path(__file__).resolve().parents[2]) and not \
            output.is_relative_to(store.Path(__file__).resolve().parents[2] / ".runtime"):
        raise ValueError("private migration plans inside this repository must stay under .runtime")
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = output.with_suffix(".sqlite3")
    if snapshot.exists():
        raise ValueError("snapshot destination already exists; select a new output name")
    with closing(read_source(source)) as live, closing(sqlite3.connect(snapshot)) as frozen:
        live.backup(frozen, pages=2048, sleep=0.01)
    with closing(read_source(snapshot)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != store.SCHEMA_VERSION:
            raise ValueError(f"source schema {version} must be explicitly migrated to {store.SCHEMA_VERSION} first")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or \
                conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("SQLite source integrity/referential checks failed")
        if conn.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('view','trigger')").fetchone()[0]:
            raise ValueError("source views/triggers require an explicit PostgreSQL migration")
        tables = {}
        for name, ddl in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"):
            _name(name)
            columns = [tuple(r) for r in conn.execute(f"PRAGMA table_info({_name(name)})")]
            if any(r[2].upper() not in ("TEXT", "INTEGER", "REAL", "BLOB") for r in columns):
                raise ValueError(f"unsupported declared column type in {name}")
            if any(conn.execute(f"SELECT 1 FROM {_name(name)} WHERE {_name(r[1])} IS NULL LIMIT 1").fetchone()
                   for r in columns if r[5]):
                raise ValueError(f"nullable SQLite primary key in {name} requires an explicit identity repair")
            tables[name] = {**table_digest(conn.execute(f"SELECT * FROM {_name(name)}")), "ddl": ddl,
                            "columns": [r[1] for r in columns], "column_info": columns,
                            "references": sorted({r[2] for r in conn.execute(f"PRAGMA foreign_key_list({_name(name)})")}),
                            "indexes": [r[0] for r in conn.execute(
                                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                                (name,))]}
        active = conn.execute("SELECT value FROM app_settings WHERE key='active_index'").fetchone()
        active_chunks = conn.execute("SELECT count(*) FROM chunks WHERE index_version=?", (active[0],)).fetchone()[0] \
            if active else 0
        result = {"format": "sqlite-postgresql-plan-1", "created_at": store.utcnow(), "source": str(source),
                  "snapshot": str(snapshot), "snapshot_sha256": file_hash(snapshot), "schema_version": version,
                  "tables": tables, "order": dependency_order(tables), "active_chunks": active_chunks,
                  "references": _references(conn), "callers": sql_inventory(Path(__file__).resolve().parents[2])}
    store.write_text_atomic(output, json.dumps(result, ensure_ascii=False, indent=2))
    return {"plan": str(output), "snapshot_sha256": result["snapshot_sha256"],
            "tables": len(tables), "rows": sum(t["rows"] for t in tables.values()), "active_chunks": active_chunks,
            "inaccessible_or_mismatched_artifacts": sum(not r["matches"] for r in result["references"]),
            "provider_calls": 0}


def load_plan(path):
    result = json.loads(Path(path).read_text(encoding="utf-8"))
    if result.get("format") != "sqlite-postgresql-plan-1" or \
            file_hash(result["snapshot"]) != result["snapshot_sha256"]:
        raise ValueError("source snapshot identity changed or plan format is unsupported")
    if dependency_order(result["tables"]) != result["order"]:
        raise ValueError("source dependency plan changed")
    return result


def translate_ddl(ddl):
    if re.search(r"\b(AUTOINCREMENT|WITHOUT\s+ROWID|VIRTUAL|COLLATE|STRICT)\b", ddl, re.I):
        raise ValueError("unsupported SQLite schema feature; explicit translation required")
    parts = re.split(r"('(?:''|[^'])*')", ddl)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"\bINTEGER\b", "BIGINT", parts[index], flags=re.I)
        parts[index] = re.sub(r"\bTEXT\b", 'TEXT COLLATE "C"', parts[index], flags=re.I)
        parts[index] = re.sub(r"\bREAL\b", "DOUBLE PRECISION", parts[index], flags=re.I)
        parts[index] = re.sub(r"\bBLOB\b", "BYTEA", parts[index], flags=re.I)
    return "".join(parts)


def import_snapshot(plan_path, target: postgres.Target, batch_size=1000, max_batches=None):
    if not 1 <= batch_size <= 5000:
        raise ValueError("batch size must be 1..5000")
    manifest = load_plan(plan_path)
    plan_sha = file_hash(plan_path)
    with store.database_lifecycle(target), store.open_db(target) as conn, closing(read_source(manifest["snapshot"])) as source:
        raw = conn.raw
        if postgres.recovery_blocked(conn):
            raise ValueError("cannot import into an incomplete or failed recovery target; use a fresh isolated database")
        if not raw.execute("SELECT pg_try_advisory_lock(%s)", (IMPORT_LOCK,)).fetchone()[0]:
            raise ValueError("another import owns this PostgreSQL target")
        try:
            table_names = {r[0] for r in raw.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")}
            if "migration_import" not in table_names:
                if table_names:
                    raise ValueError("target is unrelated or already initialized; import requires an empty database")
                with raw.transaction():
                    raw.execute(CONTROL_DDL)
                    raw.execute("INSERT INTO database_control VALUES (1,%s,false)", (manifest["snapshot_sha256"],))
                    raw.execute("INSERT INTO migration_import VALUES (1,%s,%s,'loading')",
                                (manifest["snapshot_sha256"], plan_sha))
                    for name in manifest["order"]:
                        raw.execute(translate_ddl(manifest["tables"][name]["ddl"]))
                        for index in manifest["tables"][name]["indexes"]:
                            raw.execute(translate_ddl(index))
            postgres.init_schema(target, store.SCHEMA, store.SCHEMA_VERSION)
            state = raw.execute("SELECT snapshot_sha256,plan_sha256,state FROM migration_import WHERE id=1").fetchone()
            if (state[0], state[1]) != (manifest["snapshot_sha256"], plan_sha):
                raise ValueError("resume refuses a different source snapshot or changed plan")
            if state[2] == "complete":
                return {"status": "already_complete", "overwritten_rows": 0, "provider_calls": 0}
            if raw.execute("SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0]:
                raise ValueError("import cannot resume into a serving/spendable database")
            batches = 0
            for name in manifest["order"]:
                item = manifest["tables"][name]
                checkpoint = raw.execute("SELECT imported_rows FROM migration_checkpoints WHERE table_name=%s", (name,)).fetchone()
                offset = checkpoint[0] if checkpoint else 0
                if raw.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0] != offset:
                    raise ValueError(f"target table {name} changed outside this import")
                # Frozen SQLite rowid gives an unambiguous order even for legacy tables without primary keys.
                cursor = source.execute(f"SELECT * FROM {_name(name)} ORDER BY rowid LIMIT -1 OFFSET ?", (offset,))
                insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(name), sql.SQL(",").join(map(sql.Identifier, item["columns"])),
                    sql.SQL(",").join(sql.Placeholder() for _ in item["columns"]))
                while rows := cursor.fetchmany(batch_size):
                    with raw.transaction(), raw.cursor() as writer:
                        writer.executemany(insert, rows)
                        offset += len(rows)
                        raw.execute("INSERT INTO migration_checkpoints VALUES (%s,%s) ON CONFLICT(table_name) "
                                    "DO UPDATE SET imported_rows=excluded.imported_rows", (name, offset))
                    batches += 1
                    if max_batches is not None and batches >= max_batches:
                        return {"status": "checkpointed", "batches": batches, "provider_calls": 0}
                with raw.transaction():
                    raw.execute("INSERT INTO migration_checkpoints VALUES (%s,%s) ON CONFLICT DO NOTHING", (name, offset))
                digest = postgres_digest(raw, name, item["columns"])
                if digest != {k: item[k] for k in ("rows", "canonical_sha256")}:
                    raise ValueError(f"canonical source/target parity failed for {name}")
            raw.execute("UPDATE migration_import SET state='complete' WHERE id=1")
        finally:
            raw.execute("SELECT pg_advisory_unlock(%s)", (IMPORT_LOCK,))
    return {"status": "complete", "tables": len(manifest["tables"]), "provider_calls": 0,
            "paid_admission": False}


def references_valid(references):
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
    conn.execute("CREATE TABLE IF NOT EXISTS migration_validation (id bigint PRIMARY KEY CHECK(id=1), "
                 "snapshot_sha256 text NOT NULL, references_json text NOT NULL, passed boolean NOT NULL)")
    conn.execute("INSERT INTO migration_validation VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                 "snapshot_sha256=excluded.snapshot_sha256,references_json=excluded.references_json,passed=excluded.passed",
                 (snapshot_sha256, json.dumps(references, ensure_ascii=False), passed))
    if not passed:
        conn.execute("UPDATE database_control SET paid_admission=false WHERE id=1")


def validate(plan_path, target):
    manifest = load_plan(plan_path)
    checks = {}
    with store.database_lifecycle(target), store.open_db(target) as conn:
        if postgres.recovery_blocked(conn):
            raise ValueError("recovery must finish before migration validation")
        identity = conn.execute("SELECT snapshot_sha256,plan_sha256,state FROM migration_import WHERE id=1").fetchone()
        if identity is None or tuple(identity) != (manifest["snapshot_sha256"], file_hash(plan_path), "complete"):
            raise ValueError("validation requires the matching complete import and unchanged plan")
        record_validation(conn, manifest["snapshot_sha256"], manifest["references"], False)
        for name, item in manifest["tables"].items():
            digest = postgres_digest(conn.raw, name, item["columns"])
            checks[name] = {"pass": digest == {k: item[k] for k in ("rows", "canonical_sha256")}, **digest}
        bad_files = sum(not references_valid([r]) for r in manifest["references"])
        passed = all(item["pass"] for item in checks.values()) and bad_files == 0
        record_validation(conn, manifest["snapshot_sha256"], manifest["references"], passed)
    return {"pass": passed,
            "tables": checks, "inaccessible_or_changed_artifacts": bad_files, "provider_calls": 0}


def embedding_preflight(target, dimensions=1536):
    """Proposed-rate estimate only. Neither approves rates nor dispatches provider requests."""
    from . import budget, dense, vector_store
    from .settings import DEFAULT_RATES, LARGE_RATE_VERSION, Settings

    if dimensions not in (768, 1024, 1280, 1536, 2000, 3072):
        raise ValueError("dimensions must be a specified reduced candidate or the 3072 reference")
    root = Path(__file__).resolve().parents[2]
    settings = Settings(source_dir=root / "원본 데이터", data_dir=root / ".runtime", hwp_converter=None,
                        database_dsn_env=target.dsn_env, database_pool_max=target.max_connections,
                        database_timeout_seconds=target.timeout, provider="fake", embedding_dimensions=dimensions)
    with store.database_lifecycle(target), store.open_db(target) as conn:
        version = store.get_app_setting(conn, "active_index")
        row, payloads = dense.index_payloads(settings, version)
        unique = {item["payload_hash"]: item for item in payloads}
        hits = set()
        for cached in conn.execute("SELECT * FROM embedding_payloads WHERE model=? AND dimensions=? AND policy=?",
                                   (settings.embedding_model, dimensions, dense.EMBED_POLICY)):
            if cached["payload_hash"] in unique:
                vector_store.verified(cached["embedding"], dimensions, cached["vector_checksum"])
                text_hash = hashlib.sha256(unique[cached["payload_hash"]]["text"].encode()).hexdigest()
                if text_hash == cached["normalized_payload_sha256"]:
                    hits.add(cached["payload_hash"])
        missing = [dict(item, tokens=dense.count_embedding_tokens(item["text"], settings.embedding_model))
                   for key, item in unique.items() if key not in hits]
        batches = dense.plan_batches(settings, missing)
        proposed = sum(budget.max_cost(DEFAULT_RATES[settings.embedding_model], sum(p["tokens"] for p in batch), 0)
                       for batch in batches)
        ledger = dense._ledger_view(settings, "embedding")
    return {"model": settings.embedding_model, "dimensions": dimensions, "index_version": version,
            "manifest_hash": row["manifest_hash"], "chunks": len(payloads), "unique_payloads": len(unique),
            "genuine_large_cache_hits": len(hits), "missing_tokens": sum(p["tokens"] for p in missing),
            "batches": len(batches), "proposed_rate_version": LARGE_RATE_VERSION,
            "proposed_max_cost_micro_usd": proposed,
            "envelope_remaining_micro_usd": ledger["envelope_remaining_micro_usd"],
            "cap_remaining_micro_usd": ledger["available_micro_usd"],
            "fits_existing_envelope": proposed <= min(ledger["envelope_remaining_micro_usd"], ledger["available_micro_usd"]),
            "configured_large_rate": ledger["rates"], "provider_calls": 0,
            "reference_and_evaluation_query_cost": "pending frozen independently reviewed development population"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan")
    planning.add_argument("--source", required=True, type=Path)
    planning.add_argument("--output", required=True, type=Path)
    embedding = commands.add_parser("embedding-preflight")
    embedding.add_argument("--dsn-env", default="RFP_DATABASE_DSN")
    embedding.add_argument("--dimensions", type=int, default=1536)
    for command in ("import", "validate"):
        sub = commands.add_parser(command)
        sub.add_argument("--plan", required=True, type=Path)
        sub.add_argument("--dsn-env", default="RFP_DATABASE_DSN")
        if command == "import":
            sub.add_argument("--batch-size", type=int, default=1000)
            sub.add_argument("--max-batches", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan(args.source, args.output)
        elif args.command == "embedding-preflight":
            result = embedding_preflight(postgres.Target(args.dsn_env), args.dimensions)
        elif args.command == "import":
            result = import_snapshot(args.plan, postgres.Target(args.dsn_env), args.batch_size, args.max_batches)
        else:
            result = validate(args.plan, postgres.Target(args.dsn_env))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("pass") is False else 0
    except (ValueError, RuntimeError, OSError, *store.DATABASE_ERRORS) as exc:
        # Driver connection errors can contain credentials; emit their class, never the DSN.
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        print(f"migration failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
