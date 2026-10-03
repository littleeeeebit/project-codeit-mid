"""Consistent backup, paid-disabled staged restore check, and the read-only phase-4 release report.

`backup` snapshots the live SQLite ledger with the online backup API (WAL-safe), copies the small mutable trees
(datasets, sealed, runs, releases) and records the hashes of the immutable artifacts they reference. `restore_check`
restores into a fresh staging directory, forces paid generation off, and proves that settled, prior-use and pending
amounts, the active index and the source extractions all come back unchanged. `write_release_report` only reads
recorded evidence: it never generates an answer or calls a provider.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path

from . import budget, evaluation, generation, ingestion
from .settings import REPO_ROOT, Settings
from .store import SCHEMA_VERSION, get_app_setting, open_db, utcnow, write_text_atomic

COPIED_TREES = ("datasets", "sealed", "runs", "releases")
MAX_COPY_BYTES = 512 * 1024 * 1024  # the mutable trees are small; refuse to copy a mistaken multi-GB folder


class ReleaseError(RuntimeError):
    pass


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _inside(a: Path, b: Path) -> bool:
    a, b = a.resolve(), b.resolve()
    return a == b or b in a.parents


def ledger_summary(db: Path) -> dict:
    """The amounts a restore must reproduce exactly."""
    snap = budget.snapshot(db)
    with open_db(db) as conn:
        by_state = {r[0]: {"attempts": r[1], "reserved_micro_usd": r[2], "settled_micro_usd": r[3]} for r in conn.execute(
            "SELECT state, COUNT(*), COALESCE(SUM(reserved_micro_usd), 0), COALESCE(SUM(settled_micro_usd), 0) "
            "FROM attempts GROUP BY state")}
        prior = conn.execute("SELECT amount_micro_usd FROM adjustments WHERE correction_key = 'prior-use-baseline'"
                             ).fetchone()
        adjustments = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_micro_usd), 0) FROM adjustments").fetchone()
        requests = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    return {"allowance_micro_usd": snap.allowance_micro_usd, "cap_micro_usd": snap.cap_micro_usd,
            "spent_micro_usd": snap.spent_micro_usd, "pending_micro_usd": snap.pending_micro_usd,
            "unknown_micro_usd": snap.unknown_micro_usd, "available_micro_usd": snap.available_micro_usd,
            "prior_use_micro_usd": prior[0] if prior else None,
            "adjustments": {"count": adjustments[0], "sum_micro_usd": adjustments[1]},
            "attempts_by_state": by_state, "requests": requests, "ledger_revision": snap.ledger_revision,
            "last_reconciliation": snap.last_reconciliation, "paid_enabled": snap.paid_enabled,
            "frozen_reason": snap.frozen_reason}


def _referenced(settings: Settings, db: Path) -> dict:
    """Immutable artifacts the ledger and datasets point at, with their hashes."""
    with open_db(db) as conn:
        indexes = [dict(r) for r in conn.execute("SELECT index_version, manifest_path, manifest_hash, state, "
                                                 "config_json FROM indexes ORDER BY created_at")]
        extractions = [dict(r) for r in conn.execute(
            "SELECT s.source_hash, s.active_extraction_id AS extraction_id, e.artifact_path FROM sources s "
            "JOIN extractions e ON e.extraction_id = s.active_extraction_id ORDER BY s.source_hash")]
        active_index = get_app_setting(conn, "active_index")
        active_run = get_app_setting(conn, "active_run")

    def rel(p: str) -> str:
        path = Path(p)
        return str(path.relative_to(settings.data_dir)) if _inside(path, settings.data_dir) else str(path)

    return {"active_index": active_index, "active_run": json.loads(active_run) if active_run else None,
            "indexes": [{"index_version": i["index_version"], "state": i["state"], "manifest": rel(i["manifest_path"]),
                         "manifest_hash": i["manifest_hash"],
                         "kind": json.loads(i["config_json"]).get("kind", "keyword")} for i in indexes],
            "extractions": [{"source_hash": x["source_hash"], "extraction_id": x["extraction_id"],
                             "artifact": rel(x["artifact_path"]),
                             "sha256": _sha_file(Path(x["artifact_path"])) if Path(x["artifact_path"]).exists()
                             else None} for x in extractions]}


def backup(settings: Settings, destination: Path, actor: str) -> dict:
    """Owner backup to an absolute directory outside the runtime and sources. Original state is not modified."""
    from .postgres import Target
    if isinstance(settings.db_path, Target):
        from .postgres_backup import backup as pg_backup
        return pg_backup(settings, destination, actor)
    if not destination.is_absolute():
        raise ReleaseError("--destination must be an absolute directory")
    for live in (settings.data_dir, settings.source_dir):
        if _inside(destination, live) or _inside(live, destination):
            raise ReleaseError(f"the destination overlaps {live}; choose a directory outside the runtime and sources")
    if destination.exists() and any(destination.iterdir()):
        raise ReleaseError("the destination is not empty; each backup gets a new directory")
    copy_bytes = sum(p.stat().st_size for t in COPIED_TREES if (settings.data_dir / t).exists()
                     for p in (settings.data_dir / t).rglob("*") if p.is_file())
    if copy_bytes > MAX_COPY_BYTES:
        raise ReleaseError(f"the mutable trees hold {copy_bytes} bytes, more than expected; inspect them first")
    destination.mkdir(parents=True, exist_ok=True)
    db_copy = destination / "rfp.sqlite3"
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(db_copy)
    try:
        src.backup(dst)  # consistent snapshot including committed WAL frames
    finally:
        dst.close()
        src.close()
    copied = {}
    for tree in COPIED_TREES:
        root = settings.data_dir / tree
        for path in sorted(root.rglob("*")) if root.exists() else []:
            if path.is_file() and not path.name.startswith("."):
                rel = path.relative_to(settings.data_dir)
                target = destination / "files" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                copied[str(rel).replace("\\", "/")] = _sha_file(target)
    manifest = {"backup_version": "backup-1", "created_at": utcnow(), "actor": actor, "schema_version": SCHEMA_VERSION,
                "data_dir_name": settings.data_dir.name, "db_sha256": _sha_file(db_copy),
                "ledger": ledger_summary(db_copy), "copied": copied, "referenced": _referenced(settings, db_copy),
                "code": evaluation.code_fingerprint(),
                "secrets": "not included: OPENAI_API_KEY and .env are backed up separately by the owner"}
    path = destination / "manifest.json"
    write_text_atomic(path, json.dumps(manifest, ensure_ascii=False, indent=1))
    evaluation.record_audit(settings, actor, "backup", str(destination.name), "owner backup",
                            {"db_sha256": manifest["db_sha256"], "files": len(copied),
                             "spent_micro_usd": manifest["ledger"]["spent_micro_usd"],
                             "pending_micro_usd": manifest["ledger"]["pending_micro_usd"]})
    return {"manifest": str(path), "db_sha256": manifest["db_sha256"], "files": len(copied),
            "ledger": manifest["ledger"]}


def restore_check(settings: Settings, manifest_path: Path, staging: Path | None = None) -> dict:
    """Restores the backup into a fresh staging directory with paid generation off and the fake provider, then
    compares the ledger amounts, the copied files and the referenced artifacts with the manifest. Nothing in the
    live runtime is changed and no provider is contacted."""
    if json.loads(manifest_path.read_text(encoding="utf-8")).get("backup_version") == "postgresql-backup-1":
        from .postgres_backup import restore_check as pg_restore_check
        return pg_restore_check(settings, manifest_path, staging)
    from . import dense
    from .retrieval import KeywordIndex

    if not manifest_path.is_absolute() or not manifest_path.exists():
        raise ReleaseError("--backup must be the absolute path of a backup's manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    src_dir = manifest_path.parent
    if staging is None:
        staging = Path(tempfile.mkdtemp(prefix="rfp-restore-"))
    elif not staging.is_absolute() or (staging.exists() and any(staging.iterdir())):
        raise ReleaseError("--staging must be an absolute, empty or new directory")
    if _inside(staging, settings.data_dir) or _inside(settings.data_dir, staging):
        raise ReleaseError("staging must be outside the live runtime directory")
    staging.mkdir(parents=True, exist_ok=True)
    checks: dict[str, dict] = {}

    def check(name: str, ok: bool, detail) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    db = staging / "rfp.sqlite3"
    shutil.copy2(src_dir / "rfp.sqlite3", db)
    check("database copy hash", _sha_file(db) == manifest["db_sha256"], manifest["db_sha256"][:16])
    with open_db(db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute("UPDATE budget_settings SET paid_enabled = 0 WHERE id = 1")  # staging never spends
    check("schema version", version == manifest["schema_version"], version)
    restored = ledger_summary(db)
    for key in ("spent_micro_usd", "pending_micro_usd", "unknown_micro_usd", "available_micro_usd",
                "prior_use_micro_usd", "adjustments", "attempts_by_state", "allowance_micro_usd", "cap_micro_usd",
                "requests", "last_reconciliation"):
        check(f"ledger {key}", restored[key] == manifest["ledger"][key],
              {"backup": manifest["ledger"][key], "restored": restored[key]})
    check("staging paid generation off", restored["paid_enabled"] is False,
          {"backup_paid_enabled": manifest["ledger"]["paid_enabled"]})
    # What a real recovery would then do: unresolved calls stay conservatively pending, never replayed.
    recovered_db = staging / "recovery-preview.sqlite3"
    shutil.copy2(db, recovered_db)
    recovered = budget.recover(recovered_db)
    after = ledger_summary(recovered_db)
    held = sum(v["reserved_micro_usd"] for s, v in manifest["ledger"]["attempts_by_state"].items()
               if s in ("dispatching", "unknown"))
    check("restart recovery keeps unknown reserves", after["unknown_micro_usd"] == held,
          {"recovered": recovered, "unknown_after_micro_usd": after["unknown_micro_usd"]})
    bad = []
    for rel, sha in manifest["copied"].items():
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_dir / "files" / rel, target)
        if _sha_file(target) != sha:
            bad.append(rel)
    check("copied datasets, sealed files, runs and releases", not bad, {"files": len(manifest["copied"]),
                                                                       "mismatched": bad})
    staged = Settings(database_backend="sqlite", source_dir=settings.source_dir, data_dir=staging,
                      hwp_converter=None, provider="fake")
    ref = manifest["referenced"]
    missing = [x["extraction_id"] for x in ref["extractions"]
               if x["sha256"] is None or not (settings.data_dir / x["artifact"]).exists()
               or _sha_file(settings.data_dir / x["artifact"]) != x["sha256"]]
    check("source extraction artifacts", not missing, {"extractions": len(ref["extractions"]), "mismatched": missing})
    index_ok, detail = True, {}
    if ref["active_index"]:
        try:
            idx = KeywordIndex.load(staged, ref["active_index"])  # the staged rows point at the immutable files
            detail = {"active_index": idx.version, "chunks": len(idx.chunks)}
            dense_version = (ref.get("active_run") or {}).get("dense_version")
            if dense_version:
                d = dense.DenseIndex.load(staged, dense_version, base=idx)
                detail["dense"] = {"version": d.version, "rows": d.matrix.shape[0]}
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            index_ok, detail = False, f"{type(exc).__name__}: {exc}"[:300]
    check("active index and row mapping", index_ok, detail)
    passed = all(c["ok"] for c in checks.values())
    report = {"restore_check_version": "restore-check-1", "checked_at": utcnow(), "backup": str(manifest_path),
              "staging": str(staging), "provider": "fake", "paid_enabled": False, "passed": passed, "checks": checks,
              "restored_ledger": restored, "after_restart_recovery": after}
    write_text_atomic(staging / "restore-check.json", json.dumps(report, ensure_ascii=False, indent=1))
    stamp = utcnow()[:19].replace(":", "")
    write_text_atomic(src_dir / f"restore-check-{stamp}.json", json.dumps(report, ensure_ascii=False, indent=1))
    summary = {k: report[k] for k in ("checked_at", "backup", "passed", "restored_ledger")}
    summary["failed_checks"] = [k for k, c in checks.items() if not c["ok"]]
    write_text_atomic(settings.data_dir / "releases" / "restore-checks" / f"{stamp}.json",
                      json.dumps(summary, ensure_ascii=False, indent=1))
    return report


# ---------------------------------------------------------------- release report

TARGETS = {"single_hit@20": 0.90, "multi_complete@20": 0.80, "claim_correctness": 0.90,
           "citation_precision": 0.95, "negative_handling": 0.90}
LATENCY_TARGETS_MS = {"retrieval_p95": 2000, "answer_p95": 15000}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def checks_dir(settings: Settings) -> Path:
    return settings.data_dir / "releases" / "checks"


def _answer_runs(settings: Settings) -> list[dict]:
    base = settings.data_dir / "runs"
    out = []
    for d in sorted(base.glob("A-*")) if base.exists() else []:
        config, scores = _load(d / "config.json"), _load(d / "scores.json")
        if config and scores:
            out.append({"run_id": d.name, "config": config, "scores": scores})
    return out


def _retrieval_runs(settings: Settings) -> list[dict]:
    base = settings.data_dir / "runs"
    out = []
    for d in sorted(base.iterdir()) if base.exists() else []:
        if d.name.startswith(("A-", "L-", "comparisons")) or not (d / "config.json").exists():
            continue
        try:
            out.append(evaluation.run_summary(settings, d.name))
        except (evaluation.EvaluationError, json.JSONDecodeError, KeyError):
            continue
    return out


def _latest_freeze(settings: Settings) -> dict | None:
    base = settings.data_dir / "releases"
    found = [(p.stat().st_mtime, p) for p in base.glob("F-*/freeze.json")] if base.exists() else []
    return _load(max(found)[1]) if found else None


def _rate(r: dict | None) -> float | None:
    return (r or {}).get("rate")


def decide_status(hard: list[tuple[str, bool | None, str]], quality: list[tuple[str, bool | None, str]],
                  evidence_label: str) -> tuple[str, list[str]]:
    """`blocked` when a hard check failed; `limited` when one is unverified, a quality target is missed or
    unmeasured, or the evidence is pilot-only; `ready` only when everything passed on sealed gold."""
    failed = [f"hard check failed: {name} ({ev})" for name, ok, ev in hard if ok is False]
    if failed:
        return "blocked", failed
    reasons = [f"hard check not verified: {name} ({ev})" for name, ok, ev in hard if ok is None]
    reasons += [f"quality target {'missed' if ok is False else 'not measured'}: {name} ({ev})"
                for name, ok, ev in quality if ok is not True]
    if evidence_label != "sealed gold":
        reasons.append(f"evidence is {evidence_label}, not a sealed evaluation of reviewed gold")
    return ("limited", reasons) if reasons else ("ready", [])


def write_release_report(settings: Settings, release_id: str | None = None) -> Path:
    """`releases/<release_id>/report.md` plus manifest, coverage, evaluation and budget JSON, from recorded evidence
    only. The release is the given or newest freeze; without a freeze it is a dated draft."""
    from . import sealed

    freeze = (sealed.load_freeze(settings, release_id) if release_id and release_id.startswith("F-")
              else _latest_freeze(settings) if release_id in (None, "latest") else None)
    rid = release_id if release_id not in (None, "latest") else (freeze["freeze_id"] if freeze else
                                                                  f"draft-{utcnow()[:10]}")
    out = settings.data_dir / "releases" / rid
    budget.ensure_budget_row(settings.db_path)
    with open_db(settings.db_path) as conn:
        active_run = get_app_setting(conn, "active_run")
        active_index = get_app_setting(conn, "active_index")
        envelopes = json.loads(conn.execute("SELECT envelopes_json FROM budget_settings WHERE id = 1").fetchone()[0])
        used = {p: budget._purpose_used(conn, p) for p in envelopes}
        spend = [dict(r) for r in conn.execute(
            "SELECT purpose, stage, state, COUNT(*) AS n, COALESCE(SUM(settled_micro_usd), 0) AS settled, "
            "COALESCE(SUM(reserved_micro_usd), 0) AS reserved FROM attempts GROUP BY purpose, stage, state")]
        adjustments = [dict(r) for r in conn.execute(
            "SELECT correction_key, amount_micro_usd, interval_start, interval_end, scope, created_at FROM adjustments "
            "ORDER BY created_at")]
        unresolved = conn.execute("SELECT COUNT(*) FROM attempts WHERE state IN ('reserved', 'dispatching', 'unknown')"
                                  ).fetchone()[0]
        history = json.loads(conn.execute("SELECT history_json FROM budget_settings WHERE id = 1").fetchone()[0])
    active = json.loads(active_run) if active_run else None
    snap = budget.snapshot(settings.db_path)
    manifest_rep = ingestion.manifest_report(settings)
    coverage = ingestion.review_coverage(settings)
    identity = ingestion.identity_report(settings)
    human = [c for c in coverage if c["review_status"] in ("sample_checked", "reviewed")]
    validation = {n: evaluation.validate_gold_v2(settings, n) if evaluation.dataset_path(settings, n).exists() else None
                  for n in ("dev", "test")}
    frozen = {n: evaluation.frozen_dataset(settings, n) for n in ("dev", "test")}
    retrieval_runs = [r for r in _retrieval_runs(settings) if r.get("eval_version") == evaluation.EVAL_VERSION]
    answer_runs = _answer_runs(settings)
    sealed_runs = sealed.sealed_summary(settings)
    latency = [_load(p) for p in sorted((settings.data_dir / "releases" / "latency").glob("*.json"))] \
        if (settings.data_dir / "releases" / "latency").exists() else []
    checks = {p.stem: _load(p) for p in sorted(checks_dir(settings).glob("*.json"))} \
        if checks_dir(settings).exists() else {}
    browser = _load(settings.data_dir / "releases" / "phase-3" / "browser-results.json")
    walkthrough = _load(out / "walkthrough-results.json")
    restore = [_load(p) for p in sorted((settings.data_dir / "releases" / "restore-checks").glob("*.json"))] \
        if (settings.data_dir / "releases" / "restore-checks").exists() else []
    lock = REPO_ROOT / "requirements-lock.txt"
    usd = lambda m: f"${(m or 0) / 1_000_000:,.6f}"  # noqa: E731

    # ---- evidence used for the decision, bound to the current candidate (review round 3, F6). Older results stay
    # on disk and in the evaluation JSON, but evidence recorded for other code, configuration or data is listed as
    # stale and never counts as a pass.
    current_code = evaluation.code_fingerprint()["source_sha256"]
    stale: list[str] = []
    first_sealed = next((s for s in sealed_runs if s["label"] == "sealed test" and s["status"] == "complete"), None)
    if first_sealed:
        try:
            problems = sealed.freeze_problems(settings, sealed.load_freeze(settings, first_sealed["freeze_id"]))
        except evaluation.EvaluationError as exc:
            problems = [str(exc)]
        if problems:
            stale.append(f"sealed run {first_sealed['run_id']}: {'; '.join(problems)}")
            first_sealed = None
    dev_answer = None
    for a in reversed(answer_runs):
        if not active or active["run_id"] not in a["scores"]["finalists"]:
            continue
        problems, _ = sealed._selection_problems(settings, active, a["run_id"], frozen["dev"] or {
            "dataset_sha256": (validation["dev"] or {}).get("dataset_sha256")})
        if a["config"].get("provenance", {}).get("code", {}).get("source_sha256") != current_code:
            problems.append("recorded for other package source")
        if problems:
            stale.append(f"development answer run {a['run_id']}: {'; '.join(problems)}")
            continue
        dev_answer = a
        break
    if first_sealed:
        label = "sealed gold" if (frozen["test"] or {}).get("label") == "gold" else "sealed pilot"
        ans = first_sealed["finalists"][next(iter(first_sealed["finalists"]))]
    elif dev_answer:
        label = "development pilot" if (frozen["dev"] or {}).get("label") != "gold" else "development gold"
        ans = dev_answer["scores"]["finalists"][active["run_id"]]
    else:
        label, ans = ("no answer evaluation for the current candidate" if stale else "no answer evaluation"), None
    served = (ans or {}).get("served_retrieval") or {}
    if not served and active:
        run = next((r for r in retrieval_runs if r["run_id"] == active["run_id"] and not r.get("blocking")), None)
        served = {"single_evidence": {"hit@20": run.get("hit@20")}, "multi_evidence": {
            "complete@20": run.get("complete@20")}} if run else {}
    lat = None
    for x in reversed(latency):
        if not x or x.get("provider") != "real":
            continue
        if (x.get("serving") or {}).get("run_id") != (active or {}).get("run_id") or \
                (x.get("code") or {}).get("source_sha256") != current_code:
            stale.append(f"latency sample {x.get('run_id')}: recorded for another serving run or package source")
            continue
        lat = x
        break
    all_check = checks.get("check-all")
    check_stale = bool(all_check) and (all_check.get("code") or {}).get("source_sha256") != current_code
    if check_stale:
        stale.append(f"saved check-all: recorded for package source "
                     f"{(all_check.get('code') or {}).get('source_sha256', '')[:12]}, current {current_code[:12]}")
    hard = [
        ("automated invariants (check --phase all --provider fake)",
         None if check_stale else (all_check or {}).get("ok"),
         ("stale: recorded for other package source; rerun check --phase all --provider fake --save" if check_stale
          else f"{(all_check or {}).get('tests_run')} tests, recorded {(all_check or {}).get('recorded_at')}")
         if all_check else "no saved run: check --phase all --provider fake --save"),
        ("no critical wrong deadline/amount/mandatory condition/institution in reviewed release cases",
         None if ans is None else False if ans["critical_wrong"] else None if ans.get("critical_unresolved") else True,
         "no answer evaluation" if ans is None else f"{len(ans['critical_wrong'])} observed wrong, "
         f"{len(ans.get('critical_unresolved') or [])} contested or awaiting blind review"),
        ("no selected-scope leakage", None if ans is None else ans["scope_leaks"] == 0 and not (
            served.get("wrong_scope_candidates") or 0), "no answer evaluation" if ans is None else
         f"claims/evidence outside scope {ans['scope_leaks']}, wrong-scope candidates "
         f"{served.get('wrong_scope_candidates', 0)}"),
        ("managed evidence links resolve", None if ans is None or not ans["link_validity"]["denominator"]
         else ans["link_validity"]["rate"] == 1.0, "no cited links" if ans is None else
         str(ans["link_validity"])),
        ("admission cap enforced and ledger not frozen", snap.spent_micro_usd + snap.pending_micro_usd
         <= snap.cap_micro_usd and not snap.frozen_reason, f"committed {usd(snap.spent_micro_usd + snap.pending_micro_usd)}"
         f" of cap {usd(snap.cap_micro_usd)}; frozen {snap.frozen_reason}"),
        ("no unresolved billing", True if unresolved == 0 else None, f"{unresolved} open or unknown attempts"),
    ]
    quality = [
        ("single-evidence hit@20 >= 0.90", None if _rate(served.get("single_evidence", {}).get("hit@20")) is None
         else _rate(served["single_evidence"]["hit@20"]) >= TARGETS["single_hit@20"],
         str(served.get("single_evidence", {}).get("hit@20"))),
        ("multi-evidence complete coverage@20 >= 0.80",
         None if _rate(served.get("multi_evidence", {}).get("complete@20")) is None
         else _rate(served["multi_evidence"]["complete@20"]) >= TARGETS["multi_complete@20"],
         str(served.get("multi_evidence", {}).get("complete@20"))),
        ("required-claim correctness >= 0.90", None if ans is None or _rate(ans["required_claim_correctness"]) is None
         else _rate(ans["required_claim_correctness"]) >= TARGETS["claim_correctness"],
         "n/a" if ans is None else str(ans["required_claim_correctness"])),
        ("citation precision >= 0.95 (unjudged links count as unsupported)",
         None if ans is None or _rate(ans["citation_precision_lower_bound"]) is None
         else _rate(ans["citation_precision_lower_bound"]) >= TARGETS["citation_precision"],
         "n/a" if ans is None else str(ans["citation_precision_lower_bound"])),
        ("negative/ambiguous handling >= 0.90", None if ans is None or _rate(ans["negative_handling"]) is None
         else _rate(ans["negative_handling"]) >= TARGETS["negative_handling"],
         "n/a" if ans is None else str(ans["negative_handling"])),
        ("full-answer p95 < 15 s at six users (real provider)", None if lat is None or lat["warm_ms"]["p95"] is None
         else lat["warm_ms"]["p95"] < LATENCY_TARGETS_MS["answer_p95"],
         "no real latency sample" if lat is None else f"warm p95 {lat['warm_ms']['p95']} ms, n={lat['warm_ms']['n']}"),
    ]
    status, reasons = decide_status(hard, quality, label)
    reasons += [f"stale evidence not counted: {x}" for x in stale]

    release_manifest = {
        "release_id": rid, "generated_at": utcnow(), "status": status, "reasons": reasons, "evidence_label": label,
        "code": evaluation.code_fingerprint(), "metric_code_sha256": evaluation.metric_code_sha256(),
        "settings_fingerprint": settings.fingerprint(), "model": settings.generation_model,
        "reasoning_effort": settings.generation_reasoning_effort,
        "max_output_tokens": settings.generation_max_output_tokens, "prompt_version": generation.PROMPT_VERSION,
        "rate_version": history[-1].get("rate_version") if history else None, "active_run": active,
        "active_index": active_index, "datasets": {n: {k: (frozen[n] or {}).get(k) for k in (
            "dataset_sha256", "rows", "label", "review_log_sha256", "current")} for n in ("dev", "test")},
        "requirements_lock_sha256": _sha_file(lock) if lock.exists() else None, "hardware": evaluation.hardware(),
        "stale_evidence": stale, "freeze": {k: (freeze or {}).get(k) for k in ("freeze_id", "frozen_at", "selected_run_id", "decided_by",
                                                       "post_test")} if freeze else None}
    coverage_rep = {"counts": manifest_rep["counts"], "parse_status": manifest_rep["parse_status"],
                    "review_status": manifest_rep["review_status"], "audit_differences": manifest_rep["audit_differences"],
                    "quarantined": [{k: q[k] for k in ("doc_id", "filename", "reason_code")}
                                    for q in manifest_rep["quarantined"]],
                    "human_checked_sources": len(human),
                    "provenance_conflicts": [{"filename": i["filename"],
                                              "fields": [c["field"] for c in i["provenance_conflicts"]],
                                              "resolutions": list(i["resolutions"])}
                                             for i in identity if i["provenance_conflicts"]]}
    evaluation_rep = {
        "validation": {n: None if v is None else {k: v[k] for k in ("ok", "rows", "label", "targets",
                                                                    "metadata_stratum", "answerability",
                                                                    "source_positions")}
                                                 | {"errors": len(v["errors"])} for n, v in validation.items()},
        "frozen": frozen, "retrieval_runs": [{k: r.get(k) for k in (
            "run_id", "label", "mode", "dataset_sha256", "population_size", "scored", "hit@20", "complete@20",
            "packed_complete", "ndcg@5", "mrr", "critical_failures", "wrong_scope", "latency_ms",
            "query_cost_micro_usd", "blocking")} for r in retrieval_runs],
        "answer_runs": [{"run_id": a["run_id"], "label": a["config"]["label"], "status": a["scores"]["status"],
                         "stop_reason": a["scores"].get("stop_reason"), "finalists": a["scores"]["finalists"],
                         "selection": a["scores"].get("selection")} for a in answer_runs],
        "sealed_runs": sealed_runs, "latency": latency, "checks": checks}
    budget_rep = {"snapshot": asdict(snap), "envelopes": {k: {"envelope": v, "used_incl_open": used[k]}
                                                          for k, v in envelopes.items()},
                  "attempts": spend, "adjustments": adjustments, "unresolved_attempts": unresolved,
                  "configuration_history": history}
    for name, data in (("manifest", release_manifest), ("coverage", coverage_rep), ("evaluation", evaluation_rep),
                       ("budget", budget_rep)):
        write_text_atomic(out / f"{name}.json", json.dumps(data, ensure_ascii=False, indent=1, default=str))

    L = [f"# Release report {rid}", "",
         f"Generated {release_manifest['generated_at']} from `{settings.data_dir.name}/` state. Every figure is read "
         "from recorded evidence; anything not run is listed as missing. This command never generates an answer.", "",
         f"## Decision: **{status}**", ""]
    L += [f"- {r}" for r in reasons] or ["- every hard check and quality target passed on the sealed evaluation"]
    code = release_manifest["code"]
    L += ["", "## Release manifest", "",
          f"- code: Git `{code.get('git_revision') or 'none recorded'}` (uncommitted changes: {code.get('git_dirty')}),"
          f" package source `{code['source_sha256'][:16]}`; metric code `{release_manifest['metric_code_sha256'][:16]}`",
          f"- model {settings.generation_model}, reasoning {settings.generation_reasoning_effort}, output cap "
          f"{settings.generation_max_output_tokens}, prompt {release_manifest['prompt_version']}, rates "
          f"{release_manifest['rate_version']}, settings `{release_manifest['settings_fingerprint']}`",
          f"- serving: {('run `' + active['run_id'] + '` (' + active['mode'] + ')') if active else 'keyword default (kiwi_bm25), no activated run'}, "
          f"keyword index `{active_index}`, dense `{(active or {}).get('dense_version')}`, reranker "
          f"{(active or {}).get('reranker')}; fallback kiwi_bm25",
          f"- dependencies: requirements-lock.txt `{(release_manifest['requirements_lock_sha256'] or '')[:16]}`; "
          f"hardware {release_manifest['hardware']}",
          f"- freeze: {release_manifest['freeze'] or 'none (draft release)'}", "",
          "## Coverage", "",
          f"- associations {coverage_rep['counts']['associations']} ({coverage_rep['counts']['hwp']} HWP, "
          f"{coverage_rep['counts']['pdf']} PDF), unique sources {coverage_rep['counts']['unique_sources']}; audit "
          f"differences {coverage_rep['audit_differences'] or 'none'}",
          f"- parse {coverage_rep['parse_status']}; review {coverage_rep['review_status']}; human-checked sources "
          f"{len(human)} (automatic verdicts are not counted as review)",
          f"- quarantined: {[q['filename'] for q in coverage_rep['quarantined']] or 'none'}",
          f"- byte-identical associations with conflicting metadata: {len(coverage_rep['provenance_conflicts'])}", "",
          "## Datasets", ""]
    for n in ("dev", "test"):
        v, f = validation[n], frozen[n]
        L.append(f"- `{n}`: " + ("missing" if v is None else
                                 f"{v['rows']} rows, valid {v['ok']} ({len(v['errors'])} errors), label **{v['label']}**, "
                                 f"targets met {sum(t['met'] for t in v['targets'].values())}/{len(v['targets'])}, "
                                 f"metadata stratum {v['metadata_stratum']}, positions {v['source_positions']}")
                 + (f"; frozen `{f['dataset_sha256'][:12]}` (current {f['current']})" if f else "; not frozen"))
    L += ["", "## Retrieval runs (development, retrieval only)", "",
          "| Run | Mode | Scored | hit@20 single | complete@20 multi | nDCG@5 | MRR | Critical | Wrong scope | p95 ms |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in retrieval_runs:
        L.append(f"| `{r['run_id']}` | {r['mode']} | {r['scored']} | {(r['hit@20'] or {}).get('rate')} "
                 f"({(r['hit@20'] or {}).get('numerator')}/{(r['hit@20'] or {}).get('denominator')}) | "
                 f"{(r['complete@20'] or {}).get('rate')} | {r['ndcg@5']} | {r['mrr']} | {r['critical_failures']} | "
                 f"{r['wrong_scope']} | {(r['latency_ms'] or {}).get('p95')} |")
    if not retrieval_runs:
        L.append("| — | no current-policy run recorded | | | | | | | | |")
    L += ["", "## Answer evaluation", ""]
    for a in answer_runs:
        L.append(f"- development run `{a['run_id']}` ({a['scores']['status']}): see `runs/{a['run_id']}/report.md`")
        for fid, v in a["scores"]["finalists"].items():
            L.append(f"  - `{fid}` ({v['mode']}) {v['completed']}/{v['of']}: claims "
                     f"{_rate(v['required_claim_correctness'])} (n={v['required_claim_correctness']['denominator']}), "
                     f"critical wrong {len(v['critical_wrong'])}, citation precision lower bound "
                     f"{_rate(v['citation_precision_lower_bound'])}, negatives {_rate(v['negative_handling'])} "
                     f"(n={v['negative_handling']['denominator']}), cost {usd(v['cost']['settled_micro_usd'])}")
    for s in sealed_runs:
        L.append(f"- sealed run `{s['run_id']}` ({s['label']}, {s['status']}) under freeze `{s['freeze_id']}`")
    if not answer_runs and not sealed_runs:
        L.append("- no answer run recorded: `plan-run --dataset dev --action answer-finalists`, then `run-answers`")
    L += ["", "## Latency", ""]
    L += [f"- {x['provider']} sample `{x['run_id']}`: n={x['n']}, failures {x['failures']}, warm p50/p95 "
          f"{x['warm_ms']['p50']}/{x['warm_ms']['p95']} ms (n={x['warm_ms']['n']}), cold first wave "
          f"{x['cold_wave_ms']} ms, {x['users']} users × {x['waves_run']} waves" for x in latency if x] \
        or ["- no latency sample recorded (`plan-run --action latency`, then `latency-run`)"]
    L += ["", "## Checks", ""]
    L += [f"- `{k}`: {'passed' if v.get('ok') else 'FAILED'} ({v.get('tests_run')} tests, {v.get('recorded_at')}, "
          f"code `{(v.get('code') or {}).get('source_sha256', '')[:12]}`)" for k, v in checks.items() if v] \
        or ["- no saved check results (`check --phase all --provider fake --save`)"]
    L += ["", "## Hard checks and quality targets", "", "| Check | Result | Evidence |", "| --- | --- | --- |"]
    L += [f"| {n} | {'pass' if ok else 'unverified' if ok is None else '**fail**'} | {ev} |" for n, ok, ev in hard]
    L += [f"| {n} | {'met' if ok else 'not measured' if ok is None else '**missed**'} | {ev} |" for n, ok, ev in quality]
    L += ["", "## Budget", "",
          f"- spent {usd(snap.spent_micro_usd)} of the {usd(snap.allowance_micro_usd)} allowance "
          f"({snap.spent_percent:.2f}%); pending {usd(snap.pending_micro_usd)} (unknown {usd(snap.unknown_micro_usd)}); "
          f"cap {usd(snap.cap_micro_usd)}; paid enabled {snap.paid_enabled}; last reconciliation "
          f"{snap.last_reconciliation or 'never'}; dates {snap.project_start}..{snap.project_end}",
          "- envelopes (used incl. open / envelope): " + ", ".join(
              f"{k} {usd(used[k])}/{usd(v)}" for k, v in envelopes.items()),
          f"- adjustments: {[(a['correction_key'], a['amount_micro_usd']) for a in adjustments] or 'none'}",
          "- tracked scope: calls through this application's gateway plus recorded adjustments; not provider credit", "",
          "## Mentor walkthrough", ""]
    if walkthrough or browser:
        w = walkthrough or browser
        L += [f"- {w.get('summary', '')}" + ("" if walkthrough else " (phase-3 browser record; repeat on this candidate)")]
        L += [f"  - {s.get('step')}: {s.get('outcome')}" for s in w.get("steps", [])]
    else:
        L.append("- not recorded for this candidate: follow docs/operations/runbook.md §10.6 and save "
                 f"`releases/{rid}/walkthrough-results.json`")
    L += ["", "## Restore drills", ""]
    L += [f"- {r['checked_at']}: {'passed' if r['passed'] else 'FAILED'} — backup `{Path(r['backup']).parent.name}`, "
          f"spent {usd(r['restored_ledger']['spent_micro_usd'])}, pending {usd(r['restored_ledger']['pending_micro_usd'])}"
          f", unknown {usd(r['restored_ledger']['unknown_micro_usd'])}, prior use "
          f"{usd(r['restored_ledger']['prior_use_micro_usd'])}" for r in restore if r] \
        or ["- no restore drill recorded (`backup`, then `restore-check`)"]
    L.append("")
    path = out / "report.md"
    write_text_atomic(path, "\n".join(L))
    return path
