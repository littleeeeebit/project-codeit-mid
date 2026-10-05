"""The DB and vector maintenance sequence: one command (`maintain`) or the 검증 button, never a schedule.

In order: back up the database, check the backup restores, import the manifest and ingest new or changed
originals, check the fidelity of changed HWP extractions, rebuild the keyword index, embed the new chunks for the
activated embedding model, run the regression table (`compare.regression`) and write one status report.

* Every step reuses what is already there: a backup whose tables match the database, its passed restore check,
  unchanged extractions, an existing index, cached vectors and cached comparison cells. Rerunning with no changed
  input therefore makes no provider call.
* A paid embedding stops the sequence at its priced estimate (`needs_approval`) until the person approves it
  (`compare --approve`, or 실험 비교); the rerun then continues from where it reuses everything before.
* A failed step stops the sequence, and the report names the step and the reason.
* Nothing here activates anything: the activated run, its index and its vectors keep serving, which the report
  checks. A rebuilt index serves only after the person activates its regression row.
"""

from __future__ import annotations

import hashlib
import json
import os
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .settings import Settings
from .store import open_db, utcnow, write_text_atomic

STEPS = ("backup", "restore_check", "ingest", "fidelity", "keyword", "embedding", "regression", "report")
ACTOR_NOTE = "maintenance"
# Every restore check creates `rfp_mrestore_<uuid>` and drops only that database afterwards. A run killed mid-check
# leaves its scratch database behind under this prefix for the owner to drop by name.
SCRATCH_PREFIX = "rfp_mrestore_"
# Tables a no-change rerun still writes (the backup's own audit event); they do not make a new backup necessary.
VOLATILE_TABLES = ("audit_events",)


class StepFailed(RuntimeError):
    pass


class NeedsApproval(RuntimeError):
    def __init__(self, estimate: dict, what: str) -> None:
        super().__init__(what)
        self.estimate = estimate


def root(settings: Settings) -> Path:
    return settings.data_dir / "maintenance"


def backup_root(settings: Settings) -> Path:
    """`RFP_BACKUP_DIR`, or a `-backups` sibling of the data directory: never inside the runtime or the originals."""
    configured = os.environ.get("RFP_BACKUP_DIR")
    return Path(configured) if configured else settings.data_dir.parent / f"{settings.data_dir.name}-backups"


def last(settings: Settings) -> dict | None:
    path = root(settings) / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _backups(settings: Settings) -> list[dict]:
    path = root(settings) / "backups.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _save_backups(settings: Settings, rows: list[dict]) -> None:
    write_text_atomic(root(settings) / "backups.json", json.dumps(rows, ensure_ascii=False, indent=1))


def _stable(tables: dict) -> dict:
    return {k: v for k, v in tables.items() if k not in VOLATILE_TABLES}


# ---------------------------------------------------------------- steps


def artifacts_digest(manifest_path: Path) -> str | None:
    """One digest over a backup's dump and copied files, after checking each against its manifest; None when the
    manifest is unreadable or any artifact is missing or changed. A restore check is bound to this digest."""
    from .postgres import file_hash

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        parts = [file_hash(manifest_path.parent / "database.dump")]
        if parts[0] != manifest["dump_sha256"]:
            return None
        for rel, expected in sorted(manifest["copied"].items()):
            if file_hash(manifest_path.parent / "files" / rel) != expected:
                return None
            parts.append(f"{rel}:{expected}")
    except (OSError, ValueError, KeyError):
        return None
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def step_backup(settings: Settings, ctx: dict, actor: str, **_) -> dict:
    """Reuses the newest backup whose table digests still equal the database's and whose dump and copied files
    still match its manifest; otherwise takes a new one."""
    from . import postgres_backup, release
    from .store import open_db as db

    with db(settings.db_path) as conn:
        current = _stable(postgres_backup._table_manifest(conn.raw))
    history = _backups(settings)
    for rec in reversed(history):
        if _stable(rec["tables"]) == current and (digest := artifacts_digest(Path(rec["manifest"]))):
            ctx["backup"], ctx["backup_artifacts"] = rec, digest
            return {"status": "reused", "manifest": rec["manifest"], "taken_at": rec["taken_at"]}
    destination = backup_root(settings) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result = release.backup(settings, destination, actor, share_owner=True)
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    rec = {"manifest": result["manifest"], "taken_at": manifest["created_at"], "tables": manifest["tables"],
           "ledger": result["ledger"], "restore_check": None}
    _save_backups(settings, history + [rec])
    ctx["backup"], ctx["backup_artifacts"] = rec, artifacts_digest(Path(result["manifest"]))
    if ctx["backup_artifacts"] is None:
        raise StepFailed(f"the new backup's files do not match its manifest: {result['manifest']}")
    return {"manifest": rec["manifest"], "tables": result["tables"], "spent_micro_usd": result["ledger"].get(
        "spent_micro_usd")}


def scratch(settings: Settings) -> tuple[str, str, str]:
    """(admin DSN, a new unique scratch database name, its DSN) on the application database's server."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    live = os.environ.get(settings.database_dsn_env)
    if not live:
        raise StepFailed(f"{settings.database_dsn_env} is not set")
    parts = conninfo_to_dict(live)
    name = SCRATCH_PREFIX + uuid.uuid4().hex  # 45 characters: never another database's name, never truncated
    return make_conninfo(**{**parts, "dbname": "postgres"}), name, make_conninfo(**{**parts, "dbname": name})


def _create_scratch(admin: str, name: str) -> None:
    """Creates the scratch database; an existing database of that name is refused, never replaced."""
    import psycopg
    from psycopg import errors, sql

    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        try:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except errors.DuplicateDatabase:
            raise StepFailed(f"database {name} already exists; the restore check uses only a database it "
                             "creates") from None


def _drop_scratch(admin: str, name: str) -> None:
    """Drops the scratch database this run created; called only after `_create_scratch(admin, name)` succeeded."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def step_restore_check(settings: Settings, ctx: dict, **_) -> dict:
    """Restores the backup into a fresh scratch database with paid admission off and checks it; a backup whose
    restore check already passed is not checked again. The scratch database is dropped afterwards."""
    from . import release

    rec, digest = ctx["backup"], ctx["backup_artifacts"]
    checked = rec.get("restore_check") or {}
    if checked.get("passed") and checked.get("artifacts") == digest:  # passed on exactly these files
        return {"status": "reused", "checked_at": rec["restore_check"]["checked_at"],
                "checks": rec["restore_check"]["checks"]}
    admin, name, dsn = scratch(settings)
    _create_scratch(admin, name)
    previous = os.environ.get("RFP_RESTORE_DATABASE_DSN")
    os.environ["RFP_RESTORE_DATABASE_DSN"] = dsn
    try:
        report = release.restore_check(settings, Path(rec["manifest"]))
    finally:
        if previous is None:
            os.environ.pop("RFP_RESTORE_DATABASE_DSN", None)
        else:
            os.environ["RFP_RESTORE_DATABASE_DSN"] = previous
        _drop_scratch(admin, name)
    failed = [k for k, ok in report["checks"].items() if not ok]
    rec["restore_check"] = {"passed": report["passed"], "checked_at": report["checked_at"],
                            "checks": len(report["checks"]), "failed": failed, "artifacts": digest}
    _save_backups(settings, [rec if r["manifest"] == rec["manifest"] else r for r in _backups(settings)])
    if not report["passed"]:
        raise StepFailed(f"the backup did not restore cleanly: {', '.join(failed)[:400]}")
    return {"checked_at": report["checked_at"], "checks": len(report["checks"])}


def step_ingest(settings: Settings, ctx: dict, **_) -> dict:
    """Imports the manifest, then parses every original whose bytes, parser or inputs changed; the rest is reused.
    An original counts as changed only when its active extraction differs afterwards: a re-parse whose output is
    identical keeps its extraction (and its review), so nothing downstream changes."""
    from . import ingestion, store

    store.init_schema(settings.db_path)

    def active() -> dict:
        with open_db(settings.db_path) as conn:
            return {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT source_hash, active_extraction_id, parse_status FROM sources")}

    before = active()
    manifest = ingestion.import_manifest(settings)
    results = ingestion.ingest(settings)
    ingestion.write_ingest_report(settings, results)
    errors = [r for r in results if r["status"] == "error"]
    if errors:
        raise StepFailed(f"{len(errors)} originals failed to parse: "
                         + "; ".join(f"{r['filename']}: {r.get('reason')}" for r in errors)[:400])
    after = active()
    changed = [r for r in results if before.get(r["source_hash"]) != after.get(r["source_hash"])]
    ctx["changed_sources"] = sorted({r["source_hash"] for r in changed})
    return {"status": "done" if changed else "reused", "documents": len(results),
            "reparsed": sum(not r.get("reused") and r["status"] != "recovered" for r in results),
            "changed": [{"filename": r["filename"], "status": r["status"]} for r in changed],
            "audit_differences": manifest.get("audit_differences") or {}}


def step_fidelity(settings: Settings, ctx: dict, **_) -> dict:
    """Checks every HWP extraction that has no fidelity verdict yet: the ones ingest just created or changed."""
    from . import fidelity
    from .ingestion import IngestionError

    with open_db(settings.db_path) as conn:
        todo = [r[0] for r in conn.execute(
            "SELECT s.source_hash FROM sources s WHERE s.format = 'hwp' AND s.parse_status = 'parsed' "
            "AND s.warnings_json NOT LIKE ? AND NOT EXISTS (SELECT 1 FROM fidelity_checks f WHERE "
            "f.extraction_id = s.active_extraction_id AND f.method = ?) ORDER BY s.source_hash",
            (f"%{fidelity.RECOVERED}%", fidelity.FIDELITY_VERSION))]
    if not todo:
        return {"status": "reused", "checked": 0}
    verdicts = {}
    for h in todo:
        try:
            verdicts[h] = fidelity.verify_source(settings, h)["verdict"]
        except IngestionError as exc:
            raise StepFailed(f"fidelity check of {h[:12]} failed: {exc}") from None
    return {"checked": len(verdicts), "verdicts": verdicts}


def step_keyword(settings: Settings, ctx: dict, analyzer, **_) -> dict:
    """Builds (or reuses) the serving profile's keyword index over the current extractions, without activating it."""
    from .retrieval import KeywordIndex, build_keyword_index
    from .service import active_serving

    served = active_serving(settings).get("index_version")
    if not served:
        raise StepFailed("nothing is activated yet: activate a run before maintenance")
    index = KeywordIndex.load(settings, served)
    result = build_keyword_index(settings, analyzer, include_unreviewed=index.review_scope != "reviewed_only",
                                 profile=index.profile, activate=False)
    ctx["rebuilt_index"] = result["index_version"]
    return {"status": "reused" if result.get("reused") else "done", "index_version": result["index_version"],
            "served_index_version": served, "same_as_served": result["index_version"] == served}


def step_embedding(settings: Settings, ctx: dict, transport, **_) -> dict:
    """Embeds the rebuilt index's uncached chunks with the activated embedding model: free for a local model,
    stopped at its estimate for a paid one until the person approves it."""
    from . import compare
    from . import evaluation as ev
    from .retrieval import KeywordIndex
    from .service import active_serving

    cfg = active_serving(settings)
    if not cfg.get("embedding") or cfg.get("mode") not in ("dense", "hybrid", "hybrid_rerank"):
        return {"status": "skipped", "reason": "the activated run is keyword-only"}
    s = compare.row_settings(settings, compare.serving_base(settings))
    index = KeywordIndex.load(settings, ctx["rebuilt_index"])
    existing = ev._ready_dense_for(s, index.version)
    dense, facts = compare.ensure_dense(settings, s, index, compare.approvals(settings), transport)
    if dense is None:
        if facts.get("needs_approval"):
            est = facts["needs_approval"]
            raise NeedsApproval(est, f"{s.embedding_model}: new chunks need a paid embedding of at most "
                                     f"${est['total_micro_usd'] / 1e6:.4f}; approve estimate {est['estimate_id']}")
        raise StepFailed(str(facts.get("error") or "the embedding build stopped"))
    if existing:  # the facts describe the vector set's original build, not this run
        return {"status": "reused", "model": s.embedding_model, "dense_version": dense.version}
    return {"model": s.embedding_model, "dense_version": dense.version,
            "embedded": facts.get("embedded"), "cost_usd": round((facts.get("settled_micro_usd") or 0) / 1e6, 6)}


def _row_view(r: dict) -> dict:
    return {"name": r["name"], "status": r["status"], "reason": r.get("reason"), "run_id": r.get("run_id"),
            **{k: (r.get(p) or {}).get(m) for k, (p, m) in {
                "dev_ndcg": ("dev", "ndcg"), "dev_support": ("dev", "support"), "whole_ndcg": ("whole", "ndcg"),
                "needle_top5": ("needle", "top5"), "dev_critical": ("dev", "critical")}.items()}}


def step_regression(settings: Settings, ctx: dict, analyzer, transport, started: str, **_) -> dict:
    """The regression table: K1 and the serving configuration on the served and the rebuilt index."""
    from . import compare

    table = compare.regression(settings, analyzer, transport, ctx["rebuilt_index"])
    rows = [_row_view(r) for r in table["rows"]]
    for r in table["rows"]:
        if r["status"] == "needs_approval":
            raise NeedsApproval(r["estimate"], f"{r['name']}: {r.get('reason')}")
    failed = [r for r in table["rows"] if r["status"] != "complete"]
    if failed:
        raise StepFailed("; ".join(f"{r['name']}: {r.get('reason')}" for r in failed)[:400])
    reused = all((r.get("measured_at") or "") < started for r in table["rows"])
    return {"status": "reused" if reused else "done", "table": "regression", "rows": rows}


STEP_FUNCTIONS = {"backup": step_backup, "restore_check": step_restore_check, "ingest": step_ingest,
                  "fidelity": step_fidelity, "keyword": step_keyword, "embedding": step_embedding,
                  "regression": step_regression}


# ---------------------------------------------------------------- the sequence


def _serving(settings: Settings) -> dict:
    from .service import active_serving

    cfg = active_serving(settings)
    return {k: cfg.get(k) for k in ("run_id", "mode", "index_version", "dense_version")}


def _paid_attempts(settings: Settings, since: str) -> int:
    """Provider attempts the maintenance members recorded since `since`: OpenAI ledger and Gemini's own."""
    from . import compare

    with open_db(settings.db_path) as conn:
        openai = conn.execute("SELECT COUNT(*) FROM attempts WHERE member_id = ? AND created_at >= ?",
                              (compare.MEMBER, since)).fetchone()[0]
        gemini = conn.execute("SELECT COUNT(*) FROM external_attempts WHERE created_at >= ?", (since,)).fetchone()[0]
    return int(openai) + int(gemini)


def _write(settings: Settings, state: dict) -> None:
    write_text_atomic(root(settings) / "state.json", json.dumps(state, ensure_ascii=False, indent=1))


def start_state(actor: str) -> dict:
    now = utcnow()
    return {"run_id": "M-" + now[:19].replace("-", "").replace(":", "").replace("T", "-"), "actor": actor,
            "started_at": now, "finished_at": None, "status": "running", "stopped_at": None, "reason": None,
            "steps": [{"name": n, "status": "pending", "started_at": None, "finished_at": None, "detail": {},
                       "reason": None} for n in STEPS]}


def run(settings: Settings, analyzer, transport, actor: str, *, closing=lambda: False,
        state: dict | None = None) -> dict:
    """Runs every step in order and writes one report. Stops at the first failed step or paid estimate."""
    from . import compare

    state = state or start_state(actor)
    started = state["started_at"]
    ctx: dict = {}
    _write(settings, state)
    serving_before = _serving(settings)
    for step in state["steps"]:
        name = step["name"]
        step.update(status="running", started_at=utcnow())
        _write(settings, state)
        try:
            if closing():
                raise StepFailed("the service is stopping; rerun maintenance to continue")
            if name == "report":
                golden = compare.golden_counts(settings)
                detail = {"judge_set": golden.get("judge_set"),
                          "development_rows": golden["development_count"]["live_approved"]}
            else:
                detail = STEP_FUNCTIONS[name](settings, ctx, analyzer=analyzer, transport=transport, actor=actor,
                                              started=started)
            step.update(status=detail.pop("status", "done"), detail=detail)
        except NeedsApproval as exc:
            step.update(status="needs_approval", reason=str(exc), detail={"estimate": exc.estimate})
            state.update(status="needs_approval", stopped_at=name, reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - a failed step stops the sequence with its reason
            reason = str(exc) if isinstance(exc, StepFailed) else f"{type(exc).__name__}: {exc}"
            step.update(status="failed", reason=reason[:600], detail={"traceback": traceback.format_exc()[-1500:]})
            state.update(status="failed", stopped_at=name, reason=reason[:600])
        step["finished_at"] = utcnow()
        _write(settings, state)
        if state["status"] != "running":
            break
    serving_after = _serving(settings)
    state.update(finished_at=utcnow(), status="complete" if state["status"] == "running" else state["status"],
                 serving={"before": serving_before, "after": serving_after,
                          "unchanged": serving_before == serving_after},
                 provider_calls=_paid_attempts(settings, started),
                 reused=all(s["status"] in ("reused", "skipped") for s in state["steps"] if s["name"] != "report"))
    _write(settings, state)
    write_text_atomic(root(settings) / "reports" / f"{state['run_id']}.json",
                      json.dumps(state, ensure_ascii=False, indent=1))
    write_text_atomic(root(settings) / "reports" / f"{state['run_id']}.md", report_md(state))
    return state


LABELS = {"backup": "Backup", "restore_check": "Restore check", "ingest": "Manifest and ingest",
          "fidelity": "HWP fidelity (changed only)", "keyword": "Keyword index", "embedding": "Embedding (new chunks)",
          "regression": "Regression table", "report": "Status report"}


def report_md(state: dict) -> str:
    lines = [f"# Maintenance {state['run_id']}", "",
             f"Started {state['started_at']} by {state['actor']}; finished {state['finished_at']}. Status: "
             f"**{state['status']}**" + (f" at `{state['stopped_at']}`: {state['reason']}" if state["stopped_at"]
                                          else "") + ".", "",
             f"Serving unchanged: {state.get('serving', {}).get('unchanged')} "
             f"(`{(state.get('serving') or {}).get('after', {}).get('run_id')}`). Provider calls during the run: "
             f"{state.get('provider_calls')}. Everything reused: {state.get('reused')}.", "",
             "| step | status | detail |", "| --- | --- | --- |"]
    for s in state["steps"]:
        detail = s["reason"] or json.dumps({k: v for k, v in s["detail"].items() if k not in ("rows", "traceback")},
                                           ensure_ascii=False)
        lines.append(f"| {LABELS[s['name']]} | {s['status']} | {detail[:300].replace('|', '/')} |")
    rows = next((s["detail"].get("rows") for s in state["steps"] if s["name"] == "regression"), None)
    if rows:
        lines += ["", "## Regression table", "", "| row | nDCG@5 dev | support dev | nDCG@5 whole | needle top-5 | "
                  "critical dev |", "| --- | --- | --- | --- | --- | --- |"]
        lines += [f"| {r['name']} | {r['dev_ndcg']} | {r['dev_support']} | {r['whole_ndcg']} | {r['needle_top5']} | "
                  f"{r['dev_critical']} |" for r in rows]
    return "\n".join(lines) + "\n"
