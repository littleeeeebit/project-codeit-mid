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
* Every re-parse runs in a child process (`ParseWorker`) killed after `parse_timeout_seconds`: that original keeps
  its extraction, the run goes on and ends `unverified`, naming it. A cancel kills a running parse at once and stops
  every other step at its next boundary (`cancelled`); the controlled stop kills a running parse too.
* Nothing here activates anything: the activated run, its index and its vectors keep serving, which the report
  checks. A rebuilt index serves only after the person activates its regression row.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..settings import Settings
from .store import open_db, utcnow, write_text_atomic

STEPS = ("backup", "restore_check", "ingest", "fidelity", "keyword", "embedding", "regression", "report")
ACTOR_NOTE = "maintenance"
# Every restore check creates `rfp_mrestore_<uuid>` and drops only that database afterwards. A run killed mid-check
# leaves its scratch database behind under this prefix for the owner to drop by name.
SCRATCH_PREFIX = "rfp_mrestore_"
# Tables a no-change rerun still writes (the backup's own audit event); they do not make a new backup necessary.
VOLATILE_TABLES = ("audit_events",)
# run_id -> {"by", "at"}: a cancel the live run in this process accepted (`request_cancel`). Under CANCEL_LOCK, a
# cancel is admitted only while its run is in LIVE, and `run` leaves LIVE and takes its entry in one step, so every
# admitted cancel is in the run's final state.
CANCELS: dict[str, dict] = {}
LIVE: set[str] = set()
CANCEL_LOCK = threading.Lock()


def request_cancel(run_id: str, by: str) -> bool:
    """Admits a cancel of this live run; False once the run has finished its steps (its outcome is being written)."""
    with CANCEL_LOCK:
        if run_id not in LIVE:
            return False
        CANCELS.setdefault(run_id, {"by": by, "at": utcnow()})
        return True


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


def artifact_problem(manifest_path: Path) -> str | None:
    """The first file a restore of this backup depends on that no longer matches its manifest, else None: the
    dump, a copied runtime file, or an original, extraction or index file it references (the restore check's
    immutable-artifact check, `references_valid`, one reference at a time so the answer names it)."""
    from .postgres import file_hash, references_valid

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if file_hash(manifest_path.parent / "database.dump") != manifest["dump_sha256"]:
            return "database.dump"
        for rel, expected in sorted(manifest["copied"].items()):
            if file_hash(manifest_path.parent / "files" / rel) != expected:
                return f"copied file {rel}"
        return next((f"{r.get('kind')} {r.get('path')}" for r in manifest["references"]
                     if not references_valid([r])), None)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"{manifest_path.name}: {type(exc).__name__}"


def artifacts_digest(manifest_path: Path) -> str | None:
    """One digest over everything a restore of this backup depends on, after checking each against its manifest
    (`artifact_problem`); None when anything is missing or changed. A restore check is bound to this digest."""
    if artifact_problem(manifest_path) is not None:
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parts = [manifest["dump_sha256"], *(f"{rel}:{h}" for rel, h in sorted(manifest["copied"].items())),
             *(f"{r['kind']}:{r['path']}:{r['expected_sha256']}" for r in manifest["references"])]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def step_backup(settings: Settings, ctx: dict, actor: str, **_) -> dict:
    """Reuses the newest backup whose table digests still equal the database's and whose dump, copied files and
    referenced originals, extractions and index files still match its manifest; otherwise takes a new one."""
    from ..evaluation import release
    from . import postgres_backup
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
    if ctx["backup_artifacts"] is None:  # a restore of it would fail its artifact check: nothing to trust
        raise StepFailed(f"the new backup would not restore: {artifact_problem(Path(result['manifest']))} is "
                         f"missing or changed ({result['manifest']})")
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
    from ..evaluation import release

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


def _parse_child(conn, target) -> None:
    """The child process: parses each (settings, src, original) the parent sends and returns the result, or the
    exception with its traceback, through the pipe's pickling (no text decoding, so no filename can break it)."""
    if os.name != "nt":
        os.setsid()  # its own process group, so `ParseWorker.close` kills the converter it starts along with it
    conn.send(("ready", None, ""))
    while True:
        try:
            job = conn.recv()
        except EOFError:
            return
        try:
            out = ("ok", target(*job), "")
        except Exception as exc:  # noqa: BLE001 - re-raised in the parent, where ingest records it per source
            out = ("error", exc, traceback.format_exc()[-1500:])
        try:
            conn.send(out)
        except Exception:  # noqa: BLE001 - an exception that does not pickle
            conn.send(("error", RuntimeError(f"{type(out[1]).__name__}: {out[1]}"[:500]), out[2]))


class ParseWorker:
    """`ingest`'s parse callable for maintenance: each parse runs in one spawned child process (reused across files,
    restarted after a kill), killed when it passes `parse_timeout_seconds` (`ParseTimedOut`) or as soon as `stop()`
    is true (`Stopped`). A Python thread cannot be killed; a process can. Every DB write stays in the parent."""

    POLL_SECONDS = 0.2
    STARTUP_SECONDS = 120  # a spawned child imports the parsers before the first file's clock starts

    def __init__(self, settings: Settings, stop=lambda: False, target=None) -> None:
        from ..corpus import ingestion

        self.limit, self.stop = settings.parse_timeout_seconds, stop
        self.target = target or ingestion._parse_source
        self.proc = self.conn = None

    def _wait(self, deadline: float):
        from ..corpus import ingestion

        while not self.conn.poll(self.POLL_SECONDS):
            if self.stop():
                self.close()
                raise ingestion.Stopped()
            if time.monotonic() > deadline:
                self.close()
                raise ingestion.ParseTimedOut()
        try:
            kind, value, tb = self.conn.recv()
        except EOFError:  # the child died on its own (a crash inside a parser library)
            code = self.proc.exitcode
            self.close()
            raise ingestion.IngestionError(f"the parse process exited with code {code}") from None
        if kind == "error":
            value.add_note(f"in the parse process:\n{tb}")
            raise value
        return value

    def __call__(self, settings: Settings, src: dict, original: Path):
        if self.proc is None:
            ctx = multiprocessing.get_context("spawn")  # never fork the serving process's threads and locks
            self.conn, child = ctx.Pipe()
            self.proc = ctx.Process(target=_parse_child, args=(child, self.target), daemon=True,
                                    name="rfp-maintenance-parse")
            self.proc.start()
            child.close()
            self._wait(time.monotonic() + self.STARTUP_SECONDS)
        self.conn.send((settings, src, original))
        return self._wait(time.monotonic() + self.limit)

    def close(self) -> None:
        """Kills the child with every process it started (a converter's own timeout dies with the child)."""
        if self.proc is not None:
            if os.name == "nt":  # while the child lives, so its descendants still name it as their parent
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True, timeout=30)
            else:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.proc.kill()
            self.proc.join(10)
            self.conn.close()
        self.proc = self.conn = None


def step_ingest(settings: Settings, ctx: dict, target=None, **_) -> dict:
    """Imports the manifest, then parses every original whose bytes, parser or inputs changed; the rest is reused.
    An original counts as changed only when its active extraction differs afterwards: a re-parse whose output is
    identical keeps its extraction (and its review), so nothing downstream changes. Each parse runs in a
    `ParseWorker` child (`target` replaces `_parse_source` there); one that times out keeps its extraction and is
    listed in `timed_out`. Before each original the run state records the progress."""
    from ..corpus import ingestion
    from . import store

    stop, progress = ctx.get("stop", lambda: False), ctx.get("progress", lambda **_: None)

    def on_file(done: int, total: int, filename: str) -> None:
        if stop():
            raise ingestion.Stopped()
        progress(done=done, total=total, current=filename, current_started_at=utcnow())

    store.init_schema(settings.db_path)

    def active() -> dict:
        with open_db(settings.db_path) as conn:
            return {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT source_hash, active_extraction_id, parse_status FROM sources")}

    before = active()
    manifest = ingestion.import_manifest(settings)
    worker = ParseWorker(settings, stop, target)
    try:
        results = ingestion.ingest(settings, parse=worker, progress=on_file)
    finally:
        worker.close()
    ingestion.write_ingest_report(settings, results)
    ctx["timed_out"] = sorted({r["filename"] for r in results if r["status"] == "timed_out"})
    errors = [r for r in results if r["status"] == "error"]
    if errors:
        raise StepFailed(f"{len(errors)} originals failed to parse: "
                         + "; ".join(f"{r['filename']}: {r.get('reason')}" for r in errors)[:400])
    after = active()
    changed = [r for r in results if r["status"] != "timed_out"  # a new original that timed out is still unread
               and before.get(r["source_hash"]) != after.get(r["source_hash"])]
    ctx["changed_sources"] = sorted({r["source_hash"] for r in changed})
    return {"status": "done" if changed or ctx["timed_out"] else "reused", "documents": len(results),
            "reparsed": sum(not r.get("reused") and r["status"] not in ("recovered", "timed_out") for r in results),
            "changed": [{"filename": r["filename"], "status": r["status"]} for r in changed],
            "timed_out": ctx["timed_out"], "time_limit_seconds": settings.parse_timeout_seconds,
            "audit_differences": manifest.get("audit_differences") or {}}


def step_fidelity(settings: Settings, ctx: dict, **_) -> dict:
    """Checks every HWP extraction that has no fidelity verdict yet: the ones ingest just created or changed."""
    from ..corpus import fidelity
    from ..corpus.ingestion import IngestionError

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
    from ..retrieval.retrieval import KeywordIndex, build_keyword_index
    from ..service.service import active_serving

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
    from ..evaluation import compare
    from ..evaluation import evaluation as ev
    from ..retrieval.retrieval import KeywordIndex
    from ..service.service import active_serving

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
    from ..evaluation import compare

    table = compare.regression(settings, analyzer, transport, ctx["rebuilt_index"])
    rows = [_row_view(r) for r in table["rows"]]
    for r in table["rows"]:
        if r["status"] == "needs_approval":
            raise NeedsApproval(r["estimate"], f"{r['name']}: {r.get('reason')}")
    failed = [r for r in table["rows"] if r["status"] != "complete"]
    if failed:
        raise StepFailed("; ".join(f"{r['name']}: {r.get('reason')}" for r in failed)[:400])
    reused = all((r.get("measured_at") or "") < started for r in table["rows"])
    return {"status": "reused" if reused else "done", "table": "regression", "rows": rows,
            "needs_evidence_review": table["needs_evidence_review"]}


STEP_FUNCTIONS = {"backup": step_backup, "restore_check": step_restore_check, "ingest": step_ingest,
                  "fidelity": step_fidelity, "keyword": step_keyword, "embedding": step_embedding,
                  "regression": step_regression}


# ---------------------------------------------------------------- the sequence


def _serving(settings: Settings) -> dict:
    from ..service.service import active_serving

    cfg = active_serving(settings)
    return {k: cfg.get(k) for k in ("run_id", "mode", "index_version", "dense_version")}


def _paid_attempts(settings: Settings, since: str) -> int:
    """Provider attempts the maintenance members recorded since `since`: OpenAI ledger and Gemini's own."""
    from ..evaluation import compare

    with open_db(settings.db_path) as conn:
        openai = conn.execute("SELECT COUNT(*) FROM attempts WHERE member_id = ? AND created_at >= ?",
                              (compare.MEMBER, since)).fetchone()[0]
        gemini = conn.execute("SELECT COUNT(*) FROM external_attempts WHERE created_at >= ?", (since,)).fetchone()[0]
    return int(openai) + int(gemini)


def _write(settings: Settings, state: dict) -> None:
    write_text_atomic(root(settings) / "state.json", json.dumps(state, ensure_ascii=False, indent=1))


def start_state(actor: str) -> dict:
    """A new run's state; the run is live (it admits a cancel) from here until `run` finishes its steps."""
    now = utcnow()
    run_id = "M-" + now[:19].replace("-", "").replace(":", "").replace("T", "-")
    with CANCEL_LOCK:
        LIVE.add(run_id)
    return {"run_id": run_id, "actor": actor,
            "started_at": now, "finished_at": None, "status": "running", "stopped_at": None, "reason": None,
            "steps": [{"name": n, "status": "pending", "started_at": None, "finished_at": None, "detail": {},
                       "reason": None} for n in STEPS]}


def run(settings: Settings, analyzer, transport, actor: str, *, closing=lambda: False,
        state: dict | None = None) -> dict:
    """Runs every step in order and writes one report. Stops at the first failed step or paid estimate, or at a
    cancel (`request_cancel`): a running parse is killed, any other step finishes first. A cancel admitted during
    the last step still ends the run `cancelled`. Nothing already written is rolled back."""
    from ..corpus import ingestion
    from ..evaluation import compare

    state = state or start_state(actor)
    started = state["started_at"]

    def cancelled() -> dict | None:
        return CANCELS.get(state["run_id"])

    ingest_step = next(s for s in state["steps"] if s["name"] == "ingest")

    def progress(**p) -> None:
        ingest_step["detail"] = {"progress": p}
        _write(settings, state)

    ctx: dict = {"stop": lambda: closing() or bool(cancelled()), "progress": progress}
    _write(settings, state)
    serving_before = _serving(settings)
    for step in state["steps"]:
        name = step["name"]
        step.update(status="running", started_at=utcnow())
        _write(settings, state)
        try:
            if cancelled():
                raise ingestion.Stopped()
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
        except ingestion.Stopped:  # the detail keeps the last progress: where ingest stopped
            if who := cancelled():
                reason = f"cancelled by {who['by']} at {who['at']}"
                step.update(status="cancelled", reason=reason)
                state.update(status="cancelled", stopped_at=name, reason=reason, cancelled_by=who["by"],
                             cancelled_at=who["at"])
            else:  # the controlled stop killed a running parse
                reason = "the service is stopping; rerun maintenance to continue"
                step.update(status="failed", reason=reason)
                state.update(status="failed", stopped_at=name, reason=reason)
        except Exception as exc:  # noqa: BLE001 - a failed step stops the sequence with its reason
            reason = str(exc) if isinstance(exc, StepFailed) else f"{type(exc).__name__}: {exc}"
            step.update(status="failed", reason=reason[:600], detail={"traceback": traceback.format_exc()[-1500:]})
            state.update(status="failed", stopped_at=name, reason=reason[:600])
        step["finished_at"] = utcnow()
        _write(settings, state)
        if state["status"] != "running":
            break
    with CANCEL_LOCK:  # no cancel is admitted after this, so the one taken here is the last
        LIVE.discard(state["run_id"])
        who = CANCELS.pop(state["run_id"], None)
    if who and not state.get("cancelled_by"):  # admitted after the last check: during the last step, or as it stopped
        late = f"cancelled by {who['by']} at {who['at']}"
        if state["status"] == "running":
            last_step = next(s["name"] for s in reversed(state["steps"]) if s["started_at"])
            state.update(status="cancelled", stopped_at=last_step, reason=f"{late}, during its last step, which "
                                                                          "finished")
        else:
            state["reason"] = f"{state['reason']}; {late}, as it stopped"
        state.update(cancelled_by=who["by"], cancelled_at=who["at"])
    serving_after = _serving(settings)
    lost = next((s["detail"].get("needs_evidence_review") for s in state["steps"] if s["name"] == "regression"), None)
    timed_out = ctx.get("timed_out") or []
    unverified = []  # every step ran, but not every original or the comparison is a verified one
    if timed_out:
        unverified.append(f"{len(timed_out)} original(s) did not finish parsing within "
                          f"{settings.parse_timeout_seconds:g} s and keep their previous extraction; the next run "
                          f"retries them: {', '.join(timed_out)}")
    if lost:
        unverified.append(f"{len(lost)} question(s) left out of the rebuilt rows: a re-parse replaced the extraction "
                          f"their evidence is pinned to; review it again ({', '.join(lost[:10])}"
                          f"{' …' if len(lost) > 10 else ''})")
    if state["status"] == "running" and unverified:
        state.update(status="unverified", reason="; ".join(unverified))
    state.update(finished_at=utcnow(), status="complete" if state["status"] == "running" else state["status"],
                 timed_out=timed_out,
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
                                          else f": {state['reason']}" if state.get("reason") else "") + ".", "",
             f"Serving unchanged: {state.get('serving', {}).get('unchanged')} "
             f"(`{(state.get('serving') or {}).get('after', {}).get('run_id')}`). Provider calls during the run: "
             f"{state.get('provider_calls')}. Everything reused: {state.get('reused')}.", "",
             "| step | status | detail |", "| --- | --- | --- |"]
    for s in state["steps"]:
        detail = s["reason"] or json.dumps({k: v for k, v in s["detail"].items() if k not in ("rows", "traceback")},
                                           ensure_ascii=False)
        lines.append(f"| {LABELS[s['name']]} | {s['status']} | {detail[:300].replace('|', '/')} |")
    if state.get("timed_out"):
        lines += ["", "## Timed out (previous extraction kept; retried on the next run)", "",
                  *(f"- {f}" for f in state["timed_out"])]
    rows = next((s["detail"].get("rows") for s in state["steps"] if s["name"] == "regression"), None)
    if rows:
        lines += ["", "## Regression table", "", "| row | nDCG@5 dev | support dev | nDCG@5 whole | needle top-5 | "
                  "critical dev |", "| --- | --- | --- | --- | --- | --- |"]
        lines += [f"| {r['name']} | {r['dev_ndcg']} | {r['dev_support']} | {r['whole_ndcg']} | {r['needle_top5']} | "
                  f"{r['dev_critical']} |" for r in rows]
    return "\n".join(lines) + "\n"
