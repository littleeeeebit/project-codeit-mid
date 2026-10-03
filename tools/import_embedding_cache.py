"""Explicit provider-free transfer of settled large/1536 corpus caches into a paid-disabled PG import.

python tools/import_embedding_cache.py --plan <final-source-plan.json> --source-runtime <absolute-runtime>
    --index <keyword-index> --out <private-receipt.json>

The SQLite plan/snapshot supplies historical billing evidence only. It is opened read-only;
all target writes use PostgreSQL. Repeating this transfer reuses verified immutable entries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rfp_assistant import dense, migration, store, vector_store
from rfp_assistant.settings import load_settings


def transfer(plan_path, source_runtime, index, target_settings):
    source_runtime = Path(source_runtime)
    if not source_runtime.is_absolute():
        raise ValueError("source runtime must be absolute")
    plan = migration.load_plan(plan_path)
    settings = target_settings
    if (settings.database_backend, settings.embedding_model, settings.embedding_dimensions) != \
            ("postgresql", "text-embedding-3-large", 1536):
        raise ValueError("this transfer requires PostgreSQL and the owner-selected large/1536 identity")
    source_settings = settings.with_(database_backend="sqlite", data_dir=source_runtime, provider="fake")
    with closing(migration.read_source(plan["snapshot"])) as source, store.database_lifecycle(settings.db_path):
        source.row_factory = migration.sqlite3.Row
        origin = source.execute("SELECT * FROM indexes WHERE index_version=?", (index,)).fetchone()
        if origin is None or origin["state"] != "ready":
            raise ValueError("source keyword index is missing or incomplete")
        with store.open_db(settings.db_path) as conn:
            identity = conn.execute("SELECT snapshot_sha256,state FROM migration_import WHERE id=1").fetchone()
            if identity is None or (identity[0], identity[1]) != (plan["snapshot_sha256"], "complete") or \
                    conn.execute("SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0]:
                raise ValueError("target must be the matching complete paid-disabled source import")
        row, payloads = dense.index_payloads(settings, index)
        if (row["manifest_hash"], row["source_set_hash"]) != (origin["manifest_hash"], origin["source_set_hash"]):
            raise ValueError("source/target index identities differ")
        unique = {item["payload_hash"]: item for item in payloads}
        source_attempts = {item["attempt_id"]: dict(item) for item in source.execute(
            "SELECT * FROM attempts WHERE model='text-embedding-3-large' AND stage='embedding' "
            "AND purpose='embedding' AND state='settled'")}
        imported, reused, attempt_ids = 0, 0, set()
        for key, item in unique.items():
            vector = dense.cache_get(source_settings, key)
            if vector is None:
                raise ValueError("source corpus cache is missing or invalid; transfer never calls a provider")
            metadata = json.loads((dense.cache_dir(source_settings) / (key + ".json")).read_text(encoding="utf-8"))
            expected_text = hashlib.sha256(item["text"].encode()).hexdigest()
            if metadata.get("normalized_payload_sha256") != expected_text or \
                    metadata.get("construction") != "provider-explicit-dimensions:l2-float32":
                raise ValueError("source cache payload or provider construction provenance is invalid")
            attempt_id = metadata.get("attempt_id")
            attempt = source_attempts.get(attempt_id)
            if attempt is None:
                raise ValueError("source cache lacks a settled matching large-model corpus attempt")
            if attempt_id not in attempt_ids:
                with store.open_db(settings.db_path) as conn:
                    target_attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
                if target_attempt is None or dict(target_attempt) != attempt:
                    raise ValueError("source/target attempt records differ; import the complete ledger first")
                attempt_ids.add(attempt_id)
            cached = vector_store.cache_get(settings, key)
            if cached is not None:
                with store.open_db(settings.db_path) as conn:
                    text_hash = conn.execute("SELECT normalized_payload_sha256 FROM embedding_payloads "
                                             "WHERE payload_hash=?", (key,)).fetchone()[0]
                if text_hash != expected_text or vector_store.checksum(cached) != vector_store.checksum(vector):
                    raise ValueError("target cache differs from the immutable source; refusing overwrite")
                reused += 1
            else:
                vector_store.cache_put(settings, key, vector, {**metadata,
                    "transfer": "explicit-verified-settled-cache-1", "source_snapshot_sha256": plan["snapshot_sha256"],
                    "source_index": index, "source_index_manifest_sha256": row["manifest_hash"]})
                imported += 1
            if (imported + reused) % 1000 == 0:
                print(json.dumps({"verified_payloads": imported + reused, "total": len(unique)}), flush=True)
        result = dense.publish_dense(settings, row, payloads)
        verified = dense.DenseIndex.load(settings, result["dense_version"])
        if len(verified.chunk_ids) != len(payloads):
            raise ValueError("published PostgreSQL row-to-chunk coverage is incomplete")
        return {"pass": True, "model": settings.embedding_model, "dimensions": settings.embedding_dimensions,
                "policy": dense.EMBED_POLICY, "source_snapshot_sha256": plan["snapshot_sha256"],
                "source_index": index, "unique_payloads": len(unique), "rows": len(payloads),
                "imported_payloads": imported, "reused_payloads": reused, "settled_attempts": len(attempt_ids),
                "dense_version": result["dense_version"], "paid_admission": False,
                "provider_calls": 0, "serving_activation": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--source-runtime", required=True, type=Path)
    parser.add_argument("--index", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        settings = load_settings(database_backend="postgresql", provider="fake",
                                 embedding_model="text-embedding-3-large", embedding_dimensions=1536)
        result = transfer(args.plan, args.source_runtime, args.index, settings)
        store.write_text_atomic(args.out, json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except (ValueError, RuntimeError, OSError, dense.DenseError, *store.DATABASE_ERRORS) as exc:
        print(f"embedding transfer failed: {type(exc).__name__}; no provider calls or automatic overwrite", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
