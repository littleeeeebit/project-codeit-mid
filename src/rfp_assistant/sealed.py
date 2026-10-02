"""The release freeze and the single sealed evaluation.

`freeze_release` is the privileged action that opens the sealed test set: the owner, from the CLI (there is no
login; `sealed_evaluator` is the job's declared role), records the release-candidate configuration, code and
metric fingerprints, the frozen dev/test manifests and the development selection evidence. The sealed answer run
then executes once under that freeze (`answers.run_answers` with a `sealed` estimate). A completed untouched result
is preserved; any later run on the same test set is labeled a post-test regression and needs a stated reason.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import auth, evaluation, generation
from .contracts import Principal
from .evaluation import EvaluationError
from .settings import Settings
from .store import dumps, get_app_setting, open_db, utcnow, write_text_atomic


class SealedError(EvaluationError):
    pass


def freeze_path(settings: Settings, freeze_id: str) -> Path:
    if not freeze_id.startswith("F-") or not freeze_id[2:].isalnum():
        raise SealedError("invalid freeze id")
    return settings.data_dir / "releases" / freeze_id / "freeze.json"


def load_freeze(settings: Settings, freeze_id: str) -> dict:
    path = freeze_path(settings, freeze_id)
    if not path.exists():
        raise SealedError(f"unknown freeze {freeze_id}; run freeze-release first")
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot(settings: Settings) -> dict:
    """Everything a sealed result depends on, as it is right now."""
    with open_db(settings.db_path) as conn:
        active = get_app_setting(conn, "active_run")
        rate_version = conn.execute("SELECT rate_version FROM budget_settings WHERE id = 1").fetchone()[0]
    dev, test = evaluation.frozen_dataset(settings, "dev"), evaluation.frozen_dataset(settings, "test")
    return {"active_run": json.loads(active) if active else None, "rate_version": rate_version,
            "code": evaluation.code_fingerprint(), "metric_code_sha256": evaluation.metric_code_sha256(),
            "prompt_version": generation.PROMPT_VERSION, "model": settings.generation_model,
            "reasoning_effort": settings.generation_reasoning_effort,
            "max_output_tokens": settings.generation_max_output_tokens, "settings": settings.fingerprint(),
            "dev": dev, "test": test}


def sealed_exposures(settings: Settings, test_sha: str) -> list[dict]:
    """Every sealed run that ever started on this test set, finished or not: its run directory (config written at
    start) and the durable `sealed_run_started` audit event. Exposure is per test-set identity, not per freeze."""
    found: dict[str, dict] = {}
    base = settings.data_dir / "sealed" / "runs"
    for d in sorted(base.iterdir()) if base.exists() else []:
        try:
            config = json.loads((d / "config.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if config.get("dataset_sha256") == test_sha:
            scores_path = d / "scores.json"
            status = json.loads(scores_path.read_text(encoding="utf-8")).get("status") if scores_path.exists() \
                else "started"
            found[d.name] = {"run_id": d.name, "label": config.get("label"), "freeze_id": config.get("freeze_id"),
                             "status": status}
    with open_db(settings.db_path) as conn:
        events = conn.execute("SELECT target, details_json FROM audit_events WHERE action = 'sealed_run_started'"
                              ).fetchall()
    for e in events:
        details = json.loads(e["details_json"])
        if details.get("test_sha256") == test_sha and e["target"] not in found:
            found[e["target"]] = {"run_id": e["target"], "freeze_id": details.get("freeze_id"), "status": "started",
                                  "label": "post-test regression" if details.get("post_test_regression")
                                  else "sealed test"}
    return list(found.values())


def _selection_problems(settings: Settings, active: dict, answer_run_id: str, dev: dict | None) -> tuple[list, dict | None]:
    """The development answer run must have evaluated this very candidate: the activated serving configuration, the
    current prompt, model, reasoning and output cap, and the frozen development set."""
    from . import answers

    problems: list[str] = []
    d = answers.run_dir(settings, answer_run_id)
    try:
        scores = json.loads((d / "scores.json").read_text(encoding="utf-8"))
        config = json.loads((d / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"development answer run {answer_run_id} not found"], None
    run_id = (active or {}).get("run_id")
    if config.get("action") != "answer-finalists" or scores.get("status") != "complete":
        return [f"{answer_run_id} is not a complete development answer run"], None
    used = next((f for f in config.get("finalists") or [] if f.get("run_id") == run_id), None)
    if used is None or run_id not in scores.get("finalists", {}):
        return [f"{answer_run_id} did not answer with {run_id}"], None
    if active and answers._finalist_identity(used) != answers._finalist_identity(active):
        problems.append(f"{answer_run_id} answered with another configuration of {run_id} than the activated one")
    for key, now in (("prompt_version", generation.PROMPT_VERSION), ("model", settings.generation_model),
                     ("reasoning_effort", settings.generation_reasoning_effort),
                     ("max_output_tokens", settings.generation_max_output_tokens)):
        if config.get(key) != now:
            problems.append(f"{answer_run_id} used {key} {config.get(key)!r}, the candidate uses {now!r}")
    if dev is None or config.get("dataset_sha256") != dev.get("dataset_sha256"):
        problems.append(f"{answer_run_id} evaluated another development set than the frozen one")
    selection = {"answer_run_id": answer_run_id, "finalists": {
        k: {m: v[m] for m in ("mode", "required_claim_correctness", "critical_wrong", "negative_handling",
                              "citation_precision_lower_bound", "latency_ms", "cost")}
        for k, v in scores["finalists"].items()}, "comparison": scores.get("selection")}
    return problems, selection


def freeze_release(settings: Settings, principal: Principal, run_id: str, answer_run_id: str, decided_by: str,
                   rationale: str) -> dict:
    """Refuses unless the freeze is complete: the selected retrieval run is the activated, servable one; the dev and
    test sets are validated, frozen and unchanged since; a development answer run compared that run; and the owner
    states who decided and why. Writes `releases/<freeze_id>/freeze.json` and an audit event."""
    principal = auth.require(principal, "sealed_evaluator")
    problems = []
    if not decided_by.strip() or not rationale.strip():
        problems.append("the selection needs decided_by and a rationale")
    snap = _snapshot(settings)
    active = snap["active_run"]
    if not active or active.get("run_id") != run_id:
        problems.append(f"{run_id} is not the activated retrieval run; activate the selected candidate first")
    try:
        blocking = evaluation.run_errors(settings, run_id)
        if blocking:
            problems.append(f"{run_id} cannot serve: {'; '.join(blocking)}")
    except EvaluationError as exc:
        problems.append(str(exc))
    for name in ("dev", "test"):
        m = snap[name]
        if m is None:
            problems.append(f"{name} is not frozen; run freeze-dataset --dataset {name}")
        elif not m["current"]:
            problems.append(f"{name} changed since it was frozen ({', '.join(m['changed'])}); validate and freeze it "
                            "again")
    selection_problems, selection = _selection_problems(settings, active, answer_run_id, snap["dev"])
    problems += selection_problems
    if problems:
        raise SealedError("freeze refused: " + "; ".join(problems))
    previous = sealed_exposures(settings, snap["test"]["dataset_sha256"])
    body = {"selected_run_id": run_id, "serving": {k: v for k, v in active.items()
                                                   if k not in ("activated_at",)},
            "dev_manifest": {k: snap["dev"][k] for k in ("dataset_sha256", "rows", "label", "review_log_sha256",
                                                         "families_sha256")},
            "test_manifest": {k: snap["test"][k] for k in ("dataset_sha256", "rows", "label", "review_log_sha256",
                                                           "families_sha256")},
            "code": snap["code"], "metric_code_sha256": snap["metric_code_sha256"],
            "prompt_version": snap["prompt_version"], "model": snap["model"],
            "reasoning_effort": snap["reasoning_effort"], "max_output_tokens": snap["max_output_tokens"],
            "rate_version": snap["rate_version"], "settings": snap["settings"], "selection": selection,
            "decided_by": decided_by.strip(), "rationale": rationale.strip(),
            "post_test": bool(previous), "earlier_sealed_runs": previous}
    freeze_id = "F-" + hashlib.sha256(dumps(body).encode()).hexdigest()[:10]
    freeze = {"freeze_id": freeze_id, "frozen_by": principal.member_id, "frozen_at": utcnow(), **body}
    write_text_atomic(freeze_path(settings, freeze_id), json.dumps(freeze, ensure_ascii=False, indent=1))
    evaluation.record_audit(settings, principal.member_id, "freeze_release", freeze_id, rationale.strip(),
                            {"selected_run_id": run_id, "test_sha256": body["test_manifest"]["dataset_sha256"],
                             "post_test": body["post_test"]})
    return freeze


def freeze_problems(settings: Settings, freeze: dict) -> list[str]:
    """Why the current state no longer matches a freeze. Empty when the sealed run may (still) execute."""
    snap = _snapshot(settings)
    problems = []
    active = snap["active_run"] or {}
    if active.get("run_id") != freeze["selected_run_id"]:
        problems.append("the activated retrieval run is not the frozen one")
    if snap["code"]["source_sha256"] != freeze["code"]["source_sha256"]:
        problems.append("the package source changed since the freeze")
    for key in ("metric_code_sha256", "prompt_version", "model", "reasoning_effort", "max_output_tokens",
                "rate_version", "settings"):
        if snap[key] != freeze[key]:
            problems.append(f"{key} changed since the freeze")
    for name in ("dev", "test"):
        m, frozen = snap[name], freeze[f"{name}_manifest"]
        if m is None:
            problems.append(f"the {name} set has no frozen manifest")
        elif not m["current"]:
            problems.append(f"the {name} set changed since it was frozen ({', '.join(m['changed'])})")
        elif any(m.get(k) != frozen.get(k) for k in ("dataset_sha256", "review_log_sha256", "families_sha256")):
            problems.append(f"the {name} set was frozen again since the release freeze; freeze the release again")
    return problems


def sealed_run_id(settings: Settings, base_id: str, post_test: bool) -> str:
    return f"{base_id}-post" if post_test else base_id


def begin(settings: Settings, est: dict, actor: str, reason: str | None) -> None:
    """Admits a sealed answer run: the first untouched run on a test set, its own resume, or (with a reason) one
    labeled post-test regression. Records the start once as an audit event."""
    from . import answers

    freeze = load_freeze(settings, est["freeze_id"])
    problems = freeze_problems(settings, freeze)
    if problems:
        raise SealedError("the release freeze no longer holds: " + "; ".join(problems))
    run_id = est["run_id"]
    d = answers.run_dir(settings, run_id)
    scores_path = d / "scores.json"
    if scores_path.exists() and json.loads(scores_path.read_text(encoding="utf-8")).get("status") == "complete":
        raise SealedError(f"sealed run {run_id} is already complete; its result stands. A further run on this test set "
                          "is a post-test regression: plan it with --post-test-regression and run it with --reason")
    test_sha = freeze["test_manifest"]["dataset_sha256"]
    first = [r for r in sealed_exposures(settings, test_sha) if r["label"] == "sealed test" and r["run_id"] != run_id]
    if est.get("post_test_regression"):
        if not first and not freeze.get("post_test"):
            raise SealedError("this test set has not been exposed yet; run it without the post-test flag")
        if not (reason or "").strip():
            raise SealedError("a post-test regression run needs --reason")
    elif first or freeze.get("post_test"):
        exposed = first[0] if first else {"run_id": "an earlier run", "status": "started"}
        raise SealedError(f"this test set was already exposed by sealed run {exposed['run_id']} "
                          f"({exposed['status']}); only that run may resume, unchanged, under its own freeze. Any "
                          "other run on this test set is a post-test regression (plan it with "
                          "--post-test-regression and a reason), and a new reliability claim needs a fresh "
                          "independently sealed set")
    if not (d / "config.json").exists():
        evaluation.record_audit(settings, actor, "sealed_run_started", run_id,
                                reason or "sealed evaluation of the frozen release candidate",
                                {"freeze_id": freeze["freeze_id"], "estimate_id": est["estimate_id"],
                                 "test_sha256": test_sha,
                                 "max_micro_usd": est["max_micro_usd"],
                                 "post_test_regression": bool(est.get("post_test_regression"))})


def sealed_summary(settings: Settings) -> list[dict]:
    """Sealed runs with their aggregate scores only (no question text), for the release report."""
    base = settings.data_dir / "sealed" / "runs"
    out = []
    for d in sorted(base.iterdir()) if base.exists() else []:
        try:
            config = json.loads((d / "config.json").read_text(encoding="utf-8"))
            scores = json.loads((d / "scores.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"run_id": d.name, "label": config.get("label"), "freeze_id": config.get("freeze_id"),
                    "status": scores.get("status"), "stop_reason": scores.get("stop_reason"),
                    "finalists": scores.get("finalists"), "scored_at": scores.get("scored_at")})
    return out
