"""Read-only Phase 2 inventory and selected retrieval-run export; no application imports."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACE_FIELDS = ("id", "type", "critical", "question", "metrics", "code_check", "wrong_scope",
                "ranking", "packed", "fallback", "limitations", "timings_ms", "evidence_tokens")
CONFIG_FIELDS = ("eval_version", "label", "mode", "dataset", "dataset_sha256", "index_version",
                 "index_manifest_hash", "profile", "analyzer", "dense_version", "embedding", "limits",
                 "reranker", "rerank_depth", "h_run")
SCORE_FIELDS = ("status", "reason", "aggregate", "profile_stats", "query_embedding", "gate", "load",
                "latency_ms", "trial", "created_at")
PRIVATE_FIELDS = {"raw_text", "search_text", "payload", "text", "quote", "evidence", "context",
                  "prompt", "messages", "raw_usage_json", "error_json", "original_path", "artifact_path",
                  "api_key", "authorization", "access_token", "secret", "traceback"}
SECRET_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b|(?i:Bearer\s+)[A-Za-z0-9_.-]{12,}")


def clean(value):
    """Drop source-text/credential fields recursively; redact recognizable credentials in remaining strings."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k.lower() not in PRIVATE_FIELDS}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str):
        return SECRET_PATTERN.sub("[redacted]", value)
    return value


def select(value, fields):
    return clean({k: value[k] for k in fields if k in value})


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def snapshot(runtime: Path, out: Path):
    """Query an existing SQLite database in one read transaction; never initialize or migrate it."""
    db = runtime / "rfp.sqlite3"
    if not db.is_file():
        raise ValueError("runtime database does not exist; no empty database will be created")
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        documents = read_rows(conn, "SELECT doc_id, filename, active_source_hash FROM documents ORDER BY doc_id")
        sources = read_rows(conn, "SELECT source_hash, format, active_extraction_id, parse_status, review_status, "
                           "reason_code FROM sources ORDER BY source_hash")
        for source in sources:
            extraction = source["active_extraction_id"]
            sample = read_rows(conn, "SELECT element_id, source_order, kind FROM elements WHERE extraction_id=? "
                               "ORDER BY source_order", (extraction,))
            source["element_count"] = len(sample)
            # These are navigation anchors, never evidence that a person inspected them.
            source["navigation_anchors"] = [sample[i] for i in sorted({0, len(sample)//2, len(sample)-1})] if sample else []
            review = conn.execute("SELECT reviewer, status, extraction_id, locations_json, created_at FROM reviews "
                                  "WHERE source_hash=? ORDER BY created_at DESC LIMIT 1", (source["source_hash"],)).fetchone()
            source["latest_review"] = None if review is None else {
                "reviewer": review["reviewer"], "status": review["status"], "created_at": review["created_at"],
                "review_is_current": review["extraction_id"] == extraction,
                "location_count": len(json.loads(review["locations_json"]))}
        gold = read_rows(conn, "SELECT dataset, status, COUNT(*) AS count FROM gold_candidates "
                         "GROUP BY dataset, status ORDER BY dataset, status")
        indexes = read_rows(conn, "SELECT index_version, state, config_json, created_at FROM indexes ORDER BY created_at")
        for index in indexes:
            cfg = json.loads(index.pop("config_json"))
            index["config"] = select(cfg, ("kind", "profile", "chunker_version", "base_index_version", "model", "dimensions"))
        active = {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('active_index','active_run')")}
        if active.get("active_run"):
            active["active_run"] = select(json.loads(active["active_run"]), CONFIG_FIELDS + ("run_id", "activated_at"))
        fidelity = read_rows(conn, "SELECT f.extraction_id, f.source_hash, f.method, f.verdict, f.findings_json, f.created_at "
                            "FROM fidelity_checks f JOIN sources s ON s.source_hash=f.source_hash "
                            "AND s.active_extraction_id=f.extraction_id ORDER BY f.source_hash") if "fidelity_checks" in tables else []
        for record in fidelity:
            findings = json.loads(record.pop("findings_json"))
            record["is_current"] = any(s["source_hash"] == record["source_hash"] and
                                       s["active_extraction_id"] == record["extraction_id"] for s in sources)
            record["finding_counts"] = ({k: len(v) if isinstance(v, list) else None for k, v in findings.items()}
                                        if isinstance(findings, dict) else {"total": len(findings)})
            record["locations"] = clean_locations(findings)
        budget = read_rows(conn, "SELECT allowance_micro_usd, cap_micro_usd, project_start, project_end, "
                          "paid_enabled, prior_use_recorded, rate_version, revision FROM budget_settings")
        totals = read_rows(conn, "SELECT purpose, stage, state, COUNT(*) AS count, "
                          "SUM(reserved_micro_usd) AS reserved_micro_usd, SUM(settled_micro_usd) AS settled_micro_usd "
                          "FROM attempts GROUP BY purpose, stage, state ORDER BY purpose, stage, state")
        adjustments = read_rows(conn, "SELECT correction_key, amount_micro_usd, scope, created_at FROM adjustments")
        open_attempts = read_rows(conn, "SELECT attempt_id, state, purpose, stage, model, reserved_micro_usd, created_at "
                                 "FROM attempts WHERE state IN ('reserved','dispatching','unknown')")
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
    dataset = runtime / "datasets" / "dev-pilot.jsonl"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    packages = {}
    for name in ("pyhwp", "pymupdf", "kiwipiepy", "openai", "torch", "sentence-transformers"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    write_json(out / "manifest.json", {
        "package_version": 1, "status": "inventory_only", "collected_at": datetime.now(timezone.utc).isoformat(),
        "commit": commit, "schema_user_version": schema,
        "host": {"os": platform.platform(), "python": platform.python_version(), "cpu": platform.processor(),
                 "gpu": None, "ram_gb": None}, "packages": packages,
        "decisions": {"D1": "A: local runtime retained", "D2": "see budget-status.json; existing configuration, no change",
                      "D3": "pending owner decision; no paid work performed", "D4": "pending owner decision; no model loaded"},
        "gold": {"counts": gold, "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest() if dataset.exists() else None},
        "active": active, "steps": [],
        "not_run": [{"step": step, "reason": reason} for step, reason in (
            ("phase2_ingest_and_fidelity", "inventory collection does not reparse or print originals"),
            ("human_source_review_and_gold_approval", "requires an independent person's actual inspection"),
            ("K0_K1_comparisons", "requires a validated reviewed dataset and frozen Phase 2 indexes"),
            ("dense_and_reranker", "no owner authorization or real measurement recorded by this collector"),
            ("activation_and_phase2_report", "requires completed comparisons and an owner selection"))]})
    write_json(out / "manifest-report.json", {"documents": documents, "sources": sources, "indexes": indexes})
    write_json(out / "review-coverage.json", [{k: s[k] for k in (
        "source_hash", "active_extraction_id", "review_status", "latest_review")} for s in sources])
    write_json(out / "fidelity-summary.json", fidelity)
    write_json(out / "budget-status.json", {"status": "single_read_only_snapshot", "settings": budget,
        "attempt_totals": totals, "adjustments": adjustments, "open_attempts": open_attempts,
        "note": "Micro-USD integers; reservations are not settled spend. Reconciliation adjustments are separate."})


def clean_locations(value):
    """Retain only positional references from fidelity findings, including nested findings."""
    out = []
    if isinstance(value, dict):
        location = {k: value[k] for k in ("element_id", "page", "pages", "code") if k in value}
        if location:
            out.append(clean(location))
        for item in value.values():
            out.extend(clean_locations(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(clean_locations(item))
    return out


def export_run(runtime: Path, out: Path, run_id: str):
    if not re.fullmatch(r"[A-Za-z0-9-]+", run_id):
        raise ValueError("invalid run ID")
    src = runtime / "runs" / run_id
    if not src.is_dir() or src.is_symlink():
        raise ValueError("run directory missing or is a symlink")
    files = [src / name for name in ("config.json", "scores.json", "traces.jsonl")]
    if any(not path.is_file() or path.is_symlink() for path in files):
        raise ValueError("run requires regular config.json, scores.json and traces.jsonl files")
    config, scores = read_json(files[0]), read_json(files[1])
    traces = []
    for line in files[2].read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        trace = json.loads(line)
        result = select(trace, TRACE_FIELDS)
        result["candidates"] = [select(c, ("chunk_id", "channel", "rank", "score")) for c in trace.get("candidates", [])]
        traces.append(result)
    dst = out / "runs" / run_id
    write_json(dst / "config.json", select(config, CONFIG_FIELDS))
    write_json(dst / "scores.json", select(scores, SCORE_FIELDS))
    (dst / "traces.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in traces), encoding="utf-8")
    # Do not copy an unrestricted Markdown artifact containing possible source excerpts.
    (dst / "report.md").write_text(
        f"# Exported retrieval run {run_id}\n\nStatus: `{scores.get('status')}`. "
        "See `config.json`, `scores.json` and `traces.jsonl` for comparison inputs. "
        "Source bodies and unknown trace fields were omitted. This summary is not a release report.\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="new snapshot directory; existing directories are refused")
    parser.add_argument("--run-id", action="append", default=[], help="explicitly select a retrieval run to export")
    args = parser.parse_args()
    runtime, out = args.runtime.resolve(), args.out.resolve()
    if out.is_relative_to(runtime) or runtime.is_relative_to(out):
        parser.error("output and runtime directories must not contain each other")
    if out.exists():
        parser.error("output already exists; choose a new directory to preserve earlier snapshots")
    try:
        snapshot(runtime, out)
        for run_id in args.run_id:
            export_run(runtime, out, run_id)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Collection failed; any partial output is incomplete: {exc}\n")
    print(f"Inventory written to {out}; no API calls, migrations, reviews or activations performed.")


if __name__ == "__main__":
    main()
