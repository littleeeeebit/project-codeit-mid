"""Phase 4 answer evaluation: estimated, metered answer runs for at most two development finalists, the single
sealed run, deterministic scoring with a blind human review overlay, and a bounded latency sample.

Every answer goes through the service's own answer path (the same prompt, validation, idempotent request record,
gateway and ledger as a consultant's question) with the retrieval configuration of a recorded run pinned in place
of the activated one, and is charged to the `gold_eval` envelope. A run is resumable: completed rows are kept, a row
whose paid attempt has unknown billing is never replayed, and only rows with conclusively known billing run again.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import budget, evaluation, generation, service
from .contracts import AnswerRequest, DocRef, Principal
from .evaluation import EvaluationError
from .settings import Settings
from .store import dumps, open_db, read_jsonl, tx, utcnow, write_jsonl_atomic, write_text_atomic

ANSWER_EVAL_VERSION = "answer-eval-1"
EVAL_MEMBER = "evaluation-job"  # fixed: resuming under another typed name must find the same idempotency keys
MAX_FINALISTS = 2
ESTIMATE_TTL_HOURS = 24
ACTIONS = ("answer-finalists", "sealed", "latency")
TECHNICAL = ("technical_error", "budget_blocked", "ingestion_unavailable", "cancelled", "interrupted")
REFUSALS = ("insufficient_evidence", "clarification_required")
CLAIM_VERDICTS = ("correct", "wrong_value", "contested", "incomplete_qualifier", "missing", "needs_review")
REVIEW_VERDICTS = {"claim": ("correct", "wrong_value", "incomplete_qualifier", "missing"),
                   "link": ("supporting", "unsupported"), "answer_claim": ("supported", "unsupported")}


class AnswerEvalError(EvaluationError):
    pass


def _principal() -> Principal:
    return Principal(EVAL_MEMBER, frozenset({"consultant", "verifier"}))


class _Borrowed:
    """The owner's transport, lent to a pinned evaluation owner: closing the borrower never closes the client."""

    def __init__(self, inner) -> None:
        self.inner = inner

    def chat(self, **kw):
        return self.inner.chat(**kw)

    def embed(self, **kw):
        return self.inner.embed(**kw)

    def close(self) -> None:
        pass


class PinnedResources(service.Resources):
    """Serves a recorded retrieval run's configuration instead of the activated one and charges `gold_eval`. It
    never takes the gateway lock itself: the maintenance owner that lends its transport holds it."""

    def __init__(self, settings: Settings, transport, serving: dict, owner: service.Resources | None = None) -> None:
        self._owner = owner  # its controlled stop is this borrower's stop: no new paid stage after it
        self._own_closed = False
        # Estimate only (no transport) is ledger-only: it never borrows or takes the gateway owner.
        super().__init__(settings, transport=None if transport is None else _Borrowed(transport),
                         dispatch=transport is not None)
        if transport is None:
            self.provider_note = "no provider transport: estimate only"
        self.paid_purpose = "gold_eval"
        self._pinned = dict(serving)

    def serving(self) -> dict:
        return dict(self._pinned)

    @property
    def _closed(self) -> bool:
        return self._own_closed or bool(self._owner is not None and self._owner._closed)

    @_closed.setter
    def _closed(self, value: bool) -> None:
        self._own_closed = value


# ---------------------------------------------------------------- run identity and storage


def run_dir(settings: Settings, run_id: str) -> Path:
    if not run_id or not all(c.isalnum() or c == "-" for c in run_id):
        raise AnswerEvalError("invalid answer run id")
    base = settings.data_dir / "sealed" / "runs" if run_id.startswith("S-") else settings.data_dir / "runs"
    return base / run_id


def _finalist_identity(cfg: dict) -> dict:
    return {k: cfg.get(k) for k in ("run_id", "mode", "index_version", "dense_version", "reranker", "embedding",
                                    "limits", "eval_version")}


def _identity(settings: Settings, action: str, dataset: str, dataset_sha: str, population: str,
              finalists: list[dict], extra: dict | None = None) -> str:
    key = dumps({"v": ANSWER_EVAL_VERSION, "action": action, "dataset": dataset, "dataset_sha256": dataset_sha,
                 "source_sha256": evaluation.code_fingerprint()["source_sha256"],
                 "population": population, "finalists": [_finalist_identity(f) for f in finalists],
                 "prompt": generation.PROMPT_VERSION, "model": settings.generation_model,
                 "reasoning": settings.generation_reasoning_effort,
                 "max_output": settings.generation_max_output_tokens, **(extra or {})})
    prefix = {"answer-finalists": "A", "sealed": "S", "latency": "L"}[action]
    return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def load_progress(settings: Settings, run_id: str) -> dict[tuple[str, str], dict]:
    path = run_dir(settings, run_id) / "rows.jsonl"
    return {(r["finalist"], r["question_id"]): r for r in read_jsonl(path)} if path.exists() else {}


def _save_progress(settings: Settings, run_id: str, progress: dict) -> None:
    write_jsonl_atomic(run_dir(settings, run_id) / "rows.jsonl",
                       sorted(progress.values(), key=lambda r: (r["finalist"], r["question_id"])))


# ---------------------------------------------------------------- finalists


def _retrieval_finalists(settings: Settings, dataset: str, population: str, run_ids: list[str] | None) -> list[dict]:
    from .store import get_app_setting

    if not run_ids:
        with open_db(settings.db_path) as conn:
            active = get_app_setting(conn, "active_run")
        if not active:
            raise AnswerEvalError("no activated retrieval run: pass --runs with at most two development finalists")
        a = json.loads(active)
        run_ids = [a["run_id"]] + ([a["finalist_run_id"]] if a.get("finalist_run_id") else [])
    run_ids = list(dict.fromkeys(run_ids))
    if not 1 <= len(run_ids) <= MAX_FINALISTS:
        raise AnswerEvalError(f"answer evaluation takes one or two retrieval finalists, not {len(run_ids)}")
    out = []
    for rid in run_ids:
        config, scores = evaluation.load_run(settings, rid)
        if config.get("dataset") != dataset or config.get("population_sha256") != population:
            raise AnswerEvalError(f"{rid} scored another dataset or population; rerun evaluate-retrieval on {dataset}")
        blocking = evaluation.run_errors(settings, rid)
        if blocking:
            raise AnswerEvalError(f"{rid} cannot serve: {'; '.join(blocking)}")
        out.append(evaluation.serving_config(rid, config))
    ceilings = {json.dumps((f.get("limits") or {}).get("evidence_max_tokens")) for f in out}
    if len(ceilings) > 1:
        raise AnswerEvalError("finalists differ in their evidence ceiling; a retrieval-to-answer comparison holds the "
                              "model, prompt, output cap and evidence ceiling fixed")
    return out


# ---------------------------------------------------------------- pricing


def _refs(row: dict) -> list[DocRef]:
    return [DocRef(s["doc_id"], s["source_hash"]) for s in row.get("scope") or []]


def price_row(pinned: PinnedResources, row: dict) -> dict:
    """The maximum this row's answer may reserve under the pinned configuration, computed exactly as the answer
    path will (retrieval and prompt counting are free). A dense query vector that is not cached is priced, and the
    generation bound then assumes the evidence ceiling, because the evidence it retrieves after paying may differ."""
    if row.get("mode") == "metadata":
        return {"max_micro_usd": 0, "input_tokens": 0, "note": "free metadata mode"}
    p = _principal()
    try:
        docs = service._resolve_scope(pinned, _refs(row))
        if all(d["parse_status"] != "parsed" or not service._indexed(pinned, d["active_extraction_id"]) for d in docs):
            return {"max_micro_usd": 0, "input_tokens": 0, "note": "ingestion_unavailable: answered without a call"}
        if row.get("mode") == "compare":
            prep = service._prepare_compare(pinned, p, row["question"], docs, row["as_of_date"], None,
                                            allow_paid=False)
        else:
            prep = service.prepare_answer(pinned, p, row["question"], _refs(row), row["as_of_date"], allow_paid=False)
    except service.ServiceError as exc:
        return {"max_micro_usd": 0, "input_tokens": 0, "note": f"refused before any call: {exc}"}
    r = prep["retrieval"]
    if not r.evidence:
        return {"max_micro_usd": 0, "input_tokens": prep["input_tokens"], "note": "no evidence: answered without a call"}
    if prep["estimate_micro_usd"] is None:
        raise AnswerEvalError(f"no configured rate for {pinned.settings.generation_model}; record rates first")
    qe = r.query_embedding or {}
    tokens = prep["input_tokens"]
    if qe.get("cache") == "miss":
        s = pinned.run_settings()
        tokens += max(0, s.evidence_max_tokens - r.evidence_tokens)
        bound = budget.estimate(pinned.settings.db_path, pinned.settings.generation_model, tokens,
                                pinned.settings.generation_max_output_tokens)
        bound += prep["estimate_micro_usd"] - budget.estimate(
            pinned.settings.db_path, pinned.settings.generation_model, prep["input_tokens"],
            pinned.settings.generation_max_output_tokens)  # the query-embedding part of the service's estimate
        return {"max_micro_usd": bound, "input_tokens": tokens, "query_embedding": "uncached (paid)",
                "note": "uncached query vector: generation bounded by the evidence ceiling"}
    return {"max_micro_usd": prep["estimate_micro_usd"], "input_tokens": tokens}


def _ledger(settings: Settings, purpose: str = "gold_eval") -> dict:
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM budget_settings WHERE id = 1").fetchone()
        totals = budget._totals(conn)
        used = budget._purpose_used(conn, purpose)
    envelopes = json.loads(row["envelopes_json"])
    return {"rate_version": row["rate_version"], "paid_enabled": bool(row["paid_enabled"]),
            "envelope_micro_usd": envelopes.get(purpose), "envelope_remaining_micro_usd": envelopes.get(purpose, 0) - used,
            "available_micro_usd": row["cap_micro_usd"] - totals["spent"] - totals["pending"]}


# ---------------------------------------------------------------- plans and estimates


def _plan_inputs(settings: Settings, action: str, dataset: str | None, run_ids: list[str] | None,
                 freeze_id: str | None) -> dict:
    if action == "answer-finalists":
        if dataset not in ("dev",):
            raise AnswerEvalError("answer finalists are compared on the reviewed development split (`dev`)")
        rows, skipped, sha = evaluation.load_eval_rows(settings, dataset)
        population = evaluation.population_identity(rows, skipped)
        finalists = _retrieval_finalists(settings, dataset, population, run_ids)
        extra = {}
    elif action == "sealed":
        from . import sealed

        freeze = sealed.load_freeze(settings, freeze_id or "")
        problems = sealed.freeze_problems(settings, freeze)
        if problems:
            raise AnswerEvalError("the release freeze no longer holds: " + "; ".join(problems))
        dataset = "test"
        rows, skipped, sha = evaluation.load_eval_rows(settings, dataset, sealed=True)
        population = evaluation.population_identity(rows, skipped)
        finalists = [freeze["serving"]]
        extra = {"freeze_id": freeze["freeze_id"]}
    else:
        raise AnswerEvalError(f"plan-run actions: {ACTIONS}")
    if not rows:
        raise AnswerEvalError(f"no independently reviewed {dataset} rows to evaluate")
    run_id = _identity(settings, action, dataset, sha, population, finalists, extra)
    return {"action": action, "dataset": dataset, "dataset_sha256": sha, "population_sha256": population,
            "rows": rows, "skipped": skipped, "finalists": finalists, "run_id": run_id, "freeze_id": freeze_id}


def plan_run(settings: Settings, action: str, dataset: str | None = None, run_ids: list[str] | None = None,
             freeze_id: str | None = None, *, store: bool = True, post_test: bool = False) -> dict:
    """Estimates every attempt a run could still make (generation, uncached query embeddings; no paid judge is
    planned) before anything is dispatched. The stored estimate binds the configuration, prices and per-row token
    counts; any change requires a new plan. Rows a run already completed are not priced again."""
    if action == "latency":
        return plan_latency(settings, store=store)
    inputs = _plan_inputs(settings, action, dataset, run_ids, freeze_id)
    run_id = inputs["run_id"]
    if action == "sealed":
        from . import sealed

        run_id = sealed.sealed_run_id(settings, run_id, post_test)
    done = {k for k, r in load_progress(settings, run_id).items() if r["status"] == "done"}
    per_row, total, remaining = [], 0, 0
    for f in inputs["finalists"]:
        pinned = PinnedResources(settings, None, f)
        for row in inputs["rows"]:
            price = price_row(pinned, row)
            finished = (f["run_id"], row["question_id"]) in done
            per_row.append({"finalist": f["run_id"], "question_id": row["question_id"], "done": finished, **price})
            total += price["max_micro_usd"]
            remaining += 0 if finished else 1
    to_spend = sum(p["max_micro_usd"] for p in per_row if not p["done"])
    ledger = _ledger(settings)
    fingerprint = hashlib.sha256(dumps({"run_id": run_id, "rate_version": ledger["rate_version"],
                                        "rows": [[p["finalist"], p["question_id"], p["input_tokens"],
                                                  p["max_micro_usd"]] for p in per_row]}).encode()).hexdigest()
    now = datetime.now(timezone.utc)
    estimate = {
        "estimate_id": uuid.uuid4().hex[:12], "action": action, "dataset": inputs["dataset"], "run_id": run_id,
        "freeze_id": freeze_id, "post_test_regression": post_test,
        "finalists": [f["run_id"] for f in inputs["finalists"]], "rows": len(inputs["rows"]),
        "skipped": inputs["skipped"], "attempts_planned": sum(1 for p in per_row if not p["done"]
                                                             and p["max_micro_usd"]),
        "rows_remaining": remaining, "max_micro_usd": to_spend, "max_micro_usd_all_rows": total,
        "judge": {"attempts": 0, "max_micro_usd": 0, "note": "no paid judge is planned; review is human and blind"},
        "model": settings.generation_model, "prompt_version": generation.PROMPT_VERSION,
        "max_output_tokens": settings.generation_max_output_tokens, "purpose": "gold_eval", **ledger,
        "fits": to_spend <= min(ledger["envelope_remaining_micro_usd"], ledger["available_micro_usd"]),
        "fingerprint": fingerprint, "per_row": per_row, "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=ESTIMATE_TTL_HOURS)).isoformat(),
        "invalidated_by": "a change to the finalists, dataset or population, index, prompt, model, output cap, rates "
                          "or any row's token count; expiry",
    }
    if store:
        _store_estimate(settings, estimate)
    return estimate


def _store_estimate(settings: Settings, estimate: dict) -> None:
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("INSERT INTO eval_estimates(estimate_id, action, fingerprint, estimate_json, created_at, "
                     "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (estimate["estimate_id"], estimate["action"], estimate["fingerprint"], dumps(estimate),
                      estimate["created_at"], estimate["expires_at"]))


def load_estimate(settings: Settings, estimate_id: str) -> dict:
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT estimate_json FROM eval_estimates WHERE estimate_id = ?", (estimate_id,)).fetchone()
    if row is None:
        raise AnswerEvalError(f"unknown estimate {estimate_id}; run plan-run first")
    est = json.loads(row[0])
    if datetime.fromisoformat(est["expires_at"]) < datetime.now(timezone.utc):
        raise AnswerEvalError("the estimate expired; run plan-run again")
    return est


def recheck(settings: Settings, est: dict) -> dict:
    """Plans again without storing and refuses a changed configuration or price."""
    if est["action"] == "latency":
        again = plan_latency(settings, est["waves"], est["users"], store=False)
    else:
        again = plan_run(settings, est["action"], est["dataset"], est["finalists"] if est["action"] != "sealed"
                         else None, est.get("freeze_id"), store=False, post_test=est.get("post_test_regression", False))
    if again["fingerprint"] != est["fingerprint"]:
        raise AnswerEvalError("the configuration, prices or token counts changed since the estimate; run plan-run "
                              "again and review the new maximum")
    if not again["fits"]:
        raise AnswerEvalError(f"the remaining maximum {again['max_micro_usd']} micro-USD no longer fits the gold_eval "
                              f"envelope or the cap; nothing was dispatched")
    return again


# ---------------------------------------------------------------- execution


def _attempt_states(settings: Settings, request_id: str) -> list[str]:
    with open_db(settings.db_path) as conn:
        return [r[0] for r in conn.execute("SELECT state FROM attempts WHERE request_id = ?", (request_id,))]


def _existing(settings: Settings, key: str):
    with open_db(settings.db_path) as conn:
        return conn.execute("SELECT * FROM requests WHERE member_id = ? AND idempotency_key = ?",
                            (EVAL_MEMBER, key)).fetchone()


def _conclusive(states: list[str]) -> bool:
    """No attempt whose provider outcome is unknown or still in flight."""
    return not any(s in ("dispatching", "unknown", "reserved") for s in states)


def begin_answers(settings: Settings, estimate_id: str, actor: str, post_test_reason: str | None = None) -> dict:
    """Free and synchronous: checks the estimate and inputs, then publishes the run (its `config.json`) so the
    overview lists it from the moment a start returns, before any paid call."""
    est = load_estimate(settings, estimate_id)
    if est["action"] not in ("answer-finalists", "sealed"):
        raise AnswerEvalError("run-answers executes answer-finalists or sealed estimates; latency uses latency-run")
    recheck(settings, est)
    inputs = _plan_inputs(settings, est["action"], est["dataset"],
                          est["finalists"] if est["action"] != "sealed" else None, est.get("freeze_id"))
    run_id = est["run_id"]
    if est["action"] == "sealed":
        from . import sealed

        sealed.begin(settings, est, actor, post_test_reason)
    d = run_dir(settings, run_id)
    config_path = d / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {
        "run_id": run_id, "eval_version": ANSWER_EVAL_VERSION, "action": est["action"], "dataset": est["dataset"],
        "dataset_sha256": inputs["dataset_sha256"], "population_sha256": inputs["population_sha256"],
        "finalists": inputs["finalists"], "prompt_version": generation.PROMPT_VERSION,
        "model": settings.generation_model, "reasoning_effort": settings.generation_reasoning_effort,
        "max_output_tokens": settings.generation_max_output_tokens, "freeze_id": est.get("freeze_id"),
        "label": "post-test regression" if est.get("post_test_regression") else (
            "sealed test" if est["action"] == "sealed" else "development finalists"),
        "skipped": inputs["skipped"], "rows": len(inputs["rows"]), "created_at": utcnow(), "estimates": [],
        "provenance": {"code": evaluation.code_fingerprint(), "hardware": evaluation.hardware(),
                       "metric_code_sha256": evaluation.metric_code_sha256()}}
    config["estimates"].append({"estimate_id": estimate_id, "max_micro_usd": est["max_micro_usd"], "actor": actor,
                                "started_at": utcnow()})
    write_text_atomic(config_path, json.dumps(config, ensure_ascii=False, indent=1))
    return {"estimate_id": estimate_id, "est": est, "inputs": inputs, "run_id": run_id}


def run_answers(settings: Settings, owner: service.Resources, estimate_id: str, actor: str,
                post_test_reason: str | None = None, begun: dict | None = None) -> dict:
    """Executes a planned answer run (development finalists or the sealed run) with the owner's gateway. Stops on
    the first budget refusal or unknown billing; rerunning the same command (after reconciliation, or with a new
    estimate) resumes only unfinished rows. `begun` is this estimate's `begin_answers`, when the caller already
    published the run."""
    if begun is None or begun["estimate_id"] != estimate_id:
        begun = begin_answers(settings, estimate_id, actor, post_test_reason)
    inputs, run_id = begun["inputs"], begun["run_id"]
    progress = load_progress(settings, run_id)
    stop_reason = None
    for f in inputs["finalists"]:
        pinned = PinnedResources(settings, owner.transport, f, owner)
        for row in inputs["rows"]:
            if owner._closed and not stop_reason:
                stop_reason = "interrupted: the service is stopping; rerun to resume"
            if stop_reason:
                break
            key_base = (f["run_id"], row["question_id"])
            record = progress.get(key_base)
            if record and record["status"] == "done":
                continue
            outcome = _answer_row(settings, pinned, run_id, f["run_id"], row, record)
            progress[key_base] = outcome
            _save_progress(settings, run_id, progress)
            if outcome["status"] == "blocked":
                stop_reason = f"budget_blocked: {outcome.get('reason')}"
            elif outcome["status"] == "unknown_billing" and (record or {}).get("status") != "unknown_billing":
                # a new unknown outcome stops spending; a known one is skipped until it is reconciled
                stop_reason = "unknown_billing: reconcile the attempt before resuming (it is never replayed)"
        if stop_reason:
            break
    return finalize(settings, run_id, stop_reason)


def _answer_row(settings: Settings, pinned: PinnedResources, run_id: str, finalist: str, row: dict,
                record: dict | None) -> dict:
    """One row, idempotently. Each attempt has its own request key: an earlier key that ended without a billed
    call (interrupted before dispatch, refused by the budget, rejected by the provider before execution) is
    followed by a new attempt; a key whose call may have been billed is never sent again; a finished key returns
    its stored answer without any call."""
    attempt_no = (record or {}).get("attempt_no", 1)
    history = list((record or {}).get("history") or [])
    while True:
        key = f"{run_id}:{finalist}:{row['question_id']}:{attempt_no}"
        prior = _existing(settings, key)
        if prior is None:
            break
        states = _attempt_states(settings, prior["request_id"])
        if not _conclusive(states):
            return {"finalist": finalist, "question_id": row["question_id"], "status": "unknown_billing",
                    "attempt_no": attempt_no, "request_id": prior["request_id"], "attempt_states": states,
                    "history": history}
        stored = json.loads(prior["result_json"]) if prior["result_json"] else None
        unbilled = "settled" not in states and "reconciled" not in states
        # retry: interrupted or refused without a billed call, or a call whose unknown billing has since been
        # settled or reconciled (its answer was lost); never a finished answer
        retry = stored is None or stored["status"] == "budget_blocked" or (
            stored["status"] == "technical_error" and "released" in states and unbilled) or (
            (record or {}).get("status") == "unknown_billing" and record.get("attempt_no") == attempt_no)
        if not retry:
            break  # the service returns the stored answer; nothing is sent
        history.append({"attempt_no": attempt_no, "request_id": prior["request_id"],
                        "outcome": stored["status"] if stored else prior["status"], "attempt_states": states})
        attempt_no += 1
    t0 = time.perf_counter()
    result = service.answer(pinned, _principal(), AnswerRequest(
        idempotency_key=key, generation_id=key, question=row["question"], scope=_refs(row), mode=row["mode"],
        as_of=row["as_of_date"]))
    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    request = _existing(settings, key)
    states = _attempt_states(settings, request["request_id"])
    with open_db(settings.db_path) as conn:
        cost = conn.execute("SELECT COALESCE(SUM(settled_micro_usd), 0) FROM attempts WHERE request_id = ? AND "
                            "state = 'settled'", (request["request_id"],)).fetchone()[0]
    base = {"finalist": finalist, "question_id": row["question_id"], "attempt_no": attempt_no,
            "request_id": request["request_id"], "attempt_ids": result.attempt_ids, "attempt_states": states,
            "billing_state": result.billing_state, "settled_micro_usd": cost, "latency_ms": elapsed,
            "answered_at": utcnow(), "history": history}
    if result.status == "budget_blocked":
        return {**base, "status": "blocked", "reason": result.error}
    if not _conclusive(states):
        return {**base, "status": "unknown_billing", "outcome": result.status}
    if result.status == "technical_error" and "released" in states and "settled" not in states:
        return {**base, "status": "technical", "outcome": result.status, "error": result.error}
    return {**base, "status": "done", "outcome": result.status,
            "answer": {"summary": result.summary, "claims": result.claims, "missing_fields": result.missing_fields,
                       "conflicts": result.conflicts, "next_action": result.next_action,
                       "facts": result.facts, "error": result.error},
            "evidence": {eid: {k: ev.get(k) for k in ("doc_id", "source_hash", "extraction_id", "chunk_id",
                                                      "element_ids", "quote")}
                         for eid, ev in result.evidence.items()},
            "link_validity": _link_validity(pinned, request["request_id"], asdict(result), row)}


def _link_validity(pinned: PinnedResources, request_id: str, result: dict, row: dict) -> dict[str, bool]:
    """Each cited evidence ID opens on the server and resolves to the scoped original and its pinned extraction.
    A resolving link says nothing about whether it supports the claim."""
    hashes = {s["source_hash"] for s in row.get("scope") or []}
    out = {}
    for claim in result.get("claims") or []:
        for eid in claim.get("evidence_ids") or []:
            if eid in out:
                continue
            try:
                view = service.open_evidence(pinned, _principal(), request_id, eid)
                out[eid] = bool(view.context) and view.source_hash in hashes and view.download_available
            except Exception:  # noqa: BLE001 - an unresolvable link is invalid, whatever the cause
                out[eid] = False
    return out


# ---------------------------------------------------------------- scoring


def doc_text(answer: dict, doc_id: str | None, single_document: bool) -> str:
    """What the answer states about one document: its claims and conflict alternatives attributed to that
    document. The unattributed summary counts only when a single document is in scope, so a comparison can never
    credit one side with the other side's value."""
    parts = [c.get("text", "") for c in answer.get("claims") or [] if c.get("doc_id") == doc_id]
    parts += [a.get("value", "") for c in answer.get("conflicts") or [] for a in c.get("alternatives") or []
              if a.get("doc_id") == doc_id]
    if single_document:
        parts.insert(0, answer.get("summary") or "")
    return " ".join(p for p in parts if p)


def claim_verdict(claim: dict, answer: dict, doc_id: str | None, single_document: bool = True) -> str:
    return evaluation.typed_verdict(claim, doc_text(answer, doc_id, single_document))


def verbatim_support(claim_text: str, cited_text: str) -> bool:
    """The whole generated assertion appears word for word (ignoring whitespace) in the cited evidence text. Only
    then is support demonstrable without judgement; a paraphrase, an added assertion, a negation or a qualifier the
    source does not state all go to the blind reviewer."""
    claim, cited = evaluation.norm_text(claim_text), evaluation.norm_text(cited_text)
    return len(claim) >= 4 and claim in cited


def score_record(row: dict, record: dict, index, reviews: dict[str, dict]) -> dict:
    """Deterministic verdicts where typed labels allow it, overlaid by the latest blind human review per item."""
    outcome, answer = record.get("outcome"), record.get("answer") or {}
    groups = evaluation.row_groups(row)
    doc_of_group = {g["group_id"]: g["doc_id"] for g in groups}
    prefix = f"{record['finalist']}|{row['question_id']}"
    out = {"question_id": row["question_id"], "finalist": record["finalist"], "type": row["question_type"],
           "answerability": row["answerability"], "expected_status": row["expected_status"], "outcome": outcome,
           "technical": outcome in TECHNICAL, "status_ok": outcome in evaluation.EXPECTED_STATUS[row["answerability"]]
           and (outcome != "conflicting_evidence" or bool((answer.get("next_action") or "").strip())),
           "claims": [], "links": [], "answer_claims": [], "settled_micro_usd": record.get("settled_micro_usd", 0),
           "latency_ms": record.get("latency_ms"), "attempt_no": record.get("attempt_no", 1)}
    scope_docs = {s["doc_id"] for s in row.get("scope") or []}
    out["scope_leaks"] = sum(1 for c in answer.get("claims") or [] if c.get("doc_id") not in scope_docs) + sum(
        1 for ev in (record.get("evidence") or {}).values() if ev.get("doc_id") not in scope_docs)
    if row.get("mode") == "metadata":
        states = {(f["doc_id"], f["field"]): f["state"] for f in answer.get("facts") or []}
        wanted = row.get("expected_states") or {}
        doc = row["scope"][0]["doc_id"]
        out["metadata_correct"] = bool(states) and all(states.get((doc, f)) == s for f, s in wanted.items())
        return out
    answered = outcome == "answered"
    single = len(scope_docs) == 1
    for c in row.get("required_claims") or []:
        doc_id = doc_of_group.get((c.get("support_groups") or [None])[0])
        verdict = claim_verdict(c, answer, doc_id, single) if answered else "missing"
        item = f"{prefix}|claim|{c['claim_id']}"
        reviewed = reviews.get(item)
        out["claims"].append({"claim_id": c["claim_id"], "item": item, "verdict": (reviewed or {}).get("verdict", verdict),
                              "deterministic": verdict, "reviewed_by": (reviewed or {}).get("reviewer"),
                              "critical_kind": c.get("critical_kind")})
    chunks = {c["chunk_id"]: c for c in index.chunks} if index is not None else {}
    validity = record.get("link_validity") or {}
    for i, ac in enumerate(answer.get("claims") or []):
        supports = []
        for eid in ac.get("evidence_ids") or []:
            ev = (record.get("evidence") or {}).get(eid) or {}
            chunk = chunks.get(ev.get("chunk_id"))
            best = max((evaluation.group_grade(chunk, g, index.elements) for g in groups
                        if chunk is not None and g["doc_id"] == ev.get("doc_id")), default=0)
            # A gold span in the cited chunk is retrieval relevance, not support for whatever the claim says
            # (review rounds 1-2): only a claim quoted verbatim from its citation is supported without a person.
            support = "supporting" if ac.get("doc_id") == ev.get("doc_id") and verbatim_support(
                ac.get("text", ""), ev.get("quote") or "") else "unjudged"
            item = f"{prefix}|link|{i}|{eid}"
            reviewed = reviews.get(item)
            support = (reviewed or {}).get("verdict", support)
            supports.append(support)
            out["links"].append({"claim_index": i, "evidence_id": eid, "item": item, "support": support,
                                 "valid": bool(validity.get(eid)), "grade": best})
        item = f"{prefix}|answer_claim|{i}"
        reviewed = reviews.get(item)
        supported = True if supports and "supporting" in supports else None
        if reviewed:
            supported = reviewed["verdict"] == "supported"
        elif supports and all(s == "unsupported" for s in supports):
            supported = False
        out["answer_claims"].append({"index": i, "item": item, "kind": ac.get("kind"), "supported": supported,
                                     "doc_id": ac.get("doc_id")})
    return out


def served_retrieval(settings: Settings, index, row: dict, record: dict) -> dict | None:
    """Retrieval metrics of what the answer request actually retrieved and packed (its stored trace), graded on the
    same source spans as the retrieval-only runs."""
    if index is None or not evaluation.is_passage_row(row) or not record.get("request_id"):
        return None
    with open_db(settings.db_path) as conn:
        trace = conn.execute("SELECT trace_json FROM requests WHERE request_id = ?",
                             (record["request_id"],)).fetchone()
    r = json.loads(trace[0] or "{}").get("retrieval") if trace else None
    if not r:
        return None
    chunk = lambda cid: index.chunks[index.row_of[cid]] if cid in index.row_of else None  # noqa: E731
    ranking = [c for c in map(chunk, r.get("ranking") or []) if c]
    packed = [c for c in (chunk(e["chunk_id"]) for e in r.get("evidence") or []) if c]
    scope, groups = evaluation.row_scope(row), evaluation.row_groups(row)
    if len(scope) == 1:
        metrics = evaluation.score_row(row, ranking, packed, index.elements, groups)
    else:
        metrics = evaluation.combine_sides([evaluation.score_row(
            row, [c for c in ranking if c["extraction_id"] == x], [c for c in packed if c["extraction_id"] == x],
            index.elements, [g for g in groups if g["doc_id"] == ref.doc_id]) for ref, x in scope])
    allowed = {x for _, x in scope}
    return {"id": row["question_id"], "type": row["question_type"], "critical": evaluation.row_critical(row),
            "families": evaluation.row_families(row), "metrics": metrics,
            "code_check": evaluation.code_check(row, packed) if len(scope) == 1 else None,
            "wrong_scope": sum(c["extraction_id"] not in allowed for c in ranking + packed),
            "timings_ms": r.get("timings_ms"), "fallback": r.get("fallback")}


def _wilson_rate(k: int, n: int) -> dict:
    return {"numerator": k, "denominator": n, "rate": round(k / n, 4) if n else None,
            "wilson95": evaluation.wilson(k, n)}


def aggregate_answers(scored: list[dict]) -> dict:
    """Per-finalist answer metrics with their denominators. An empty denominator is not applicable, never 100%."""
    passage = [s for s in scored if "metadata_correct" not in s]
    answerable = [s for s in passage if s["answerability"] == "answerable"]
    claims = [c for s in answerable for c in s["claims"]]
    complete_rows = [s for s in answerable if s["claims"] and all(c["verdict"] == "correct" for c in s["claims"])]
    links = [link for s in passage for link in s["links"]]
    supporting = sum(link["support"] == "supporting" for link in links)
    unsupported = sum(link["support"] == "unsupported" for link in links)
    material = [a for s in passage for a in s["answer_claims"]]
    negatives = [s for s in passage if s["answerability"] != "answerable"]
    metadata = [s for s in scored if "metadata_correct" in s]
    critical = [{"question_id": s["question_id"], "claim_id": c["claim_id"], "kind": c["critical_kind"]}
                for s in answerable for c in s["claims"] if c["critical_kind"] and c["verdict"] == "wrong_value"]
    critical_open = [{"question_id": s["question_id"], "claim_id": c["claim_id"], "kind": c["critical_kind"],
                      "verdict": c["verdict"]}
                     for s in answerable for c in s["claims"]
                     if c["critical_kind"] and c["verdict"] in ("contested", "needs_review")]
    from .dense import percentile

    latencies = [s["latency_ms"] for s in scored if s.get("latency_ms") is not None]
    by_type: dict[str, dict] = {}
    for t in sorted({s["type"] for s in answerable}):
        cs = [c for s in answerable if s["type"] == t for c in s["claims"]]
        by_type[t] = _wilson_rate(sum(c["verdict"] == "correct" for c in cs), len(cs))
    return {
        "rows": len(scored), "answerable_rows": len(answerable),
        "required_claim_correctness": _wilson_rate(sum(c["verdict"] == "correct" for c in claims), len(claims)),
        "question_completeness": _wilson_rate(len(complete_rows), len(answerable)),
        "claims_needing_review": sum(c["verdict"] == "needs_review" for c in claims),
        "claim_verdicts": {v: sum(c["verdict"] == v for c in claims) for v in CLAIM_VERDICTS},
        "critical_wrong": critical,
        "critical_unresolved": critical_open,
        "citation_precision_judged": _wilson_rate(supporting, supporting + unsupported),
        "citation_precision_lower_bound": _wilson_rate(supporting, len(links)),
        "links_unjudged": sum(link["support"] not in ("supporting", "unsupported") for link in links),
        "citation_coverage": _wilson_rate(sum(a["supported"] is True for a in material), len(material)),
        "unsupported_claim_rate": _wilson_rate(sum(a["supported"] is False for a in material), len(material)),
        "answer_claims_unjudged": sum(a["supported"] is None for a in material),
        "link_validity": _wilson_rate(sum(link["valid"] for link in links), len(links)),
        "negative_handling": _wilson_rate(sum(s["status_ok"] for s in negatives), len(negatives)),
        "false_answers": [s["question_id"] for s in negatives if s["outcome"] == "answered"],
        "unnecessary_refusals": _wilson_rate(sum(s["outcome"] in REFUSALS for s in answerable), len(answerable)),
        "metadata_correct": _wilson_rate(sum(s["metadata_correct"] for s in metadata), len(metadata)),
        "technical_outcomes": {o: sum(s["outcome"] == o for s in scored) for o in TECHNICAL
                               if any(s["outcome"] == o for s in scored)},
        "scope_leaks": sum(s["scope_leaks"] for s in scored),
        "by_type": by_type,
        "cost": {"settled_micro_usd": sum(s["settled_micro_usd"] for s in scored),
                 "per_question_micro_usd": round(sum(s["settled_micro_usd"] for s in scored) / len(scored), 1)
                 if scored else None, "retried_rows": sum(s["attempt_no"] > 1 for s in scored)},
        "latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95), "n": len(latencies),
                       "condition": "sequential, single process, one call at a time"},
    }


def load_reviews(settings: Settings, run_id: str) -> dict[str, dict]:
    path = run_dir(settings, run_id) / "review.jsonl"
    latest: dict[str, dict] = {}
    for r in read_jsonl(path) if path.exists() else []:
        latest[r["item"]] = r
    return latest


def ledger_cost(settings: Settings, run_id: str, finalist: str) -> dict:
    """Every paid attempt this finalist's rows made in this run, read from the shared ledger through all of their
    request keys (`<run>:<finalist>:<question>:<attempt>`), retries and unfinished rows included. Settled cost
    counts once; open and unknown reservations are pending; reconciled attempts are paid through the provider
    reconciliation adjustment, not here. Nothing is charged or settled by reading."""
    prefix = f"{run_id}:{finalist}:"
    with open_db(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT r.idempotency_key, a.state, a.reserved_micro_usd, a.settled_micro_usd FROM attempts a "
            "JOIN requests r ON r.request_id = a.request_id WHERE r.member_id = ? AND substr(r.idempotency_key, 1, ?) "
            "= ?", (EVAL_MEMBER, len(prefix), prefix)).fetchall()
    final_keys = {}
    for r in rows:  # the highest attempt number per question is that row's current request
        qid, _, n = r["idempotency_key"][len(prefix):].rpartition(":")
        final_keys[qid] = max(final_keys.get(qid, 0), int(n) if n.isdigit() else 0)
    retry = [r for r in rows if r["idempotency_key"][len(prefix):].rpartition(":")[2] !=
             str(final_keys[r["idempotency_key"][len(prefix):].rpartition(":")[0]])]
    settled = lambda xs: sum(r["settled_micro_usd"] or 0 for r in xs if r["state"] == "settled")  # noqa: E731
    return {"settled_micro_usd": settled(rows), "settled_in_earlier_attempts_micro_usd": settled(retry),
            "pending_micro_usd": sum(r["reserved_micro_usd"] for r in rows
                                     if r["state"] in ("reserved", "dispatching")),
            "unknown_micro_usd": sum(r["reserved_micro_usd"] for r in rows if r["state"] == "unknown"),
            "reconciled_attempts": sum(r["state"] == "reconciled" for r in rows),
            "attempts": len(rows), "retried_rows": sum(1 for n in final_keys.values() if n > 1),
            "source": "shared ledger, all request keys of the run"}


def finalize(settings: Settings, run_id: str, stop_reason: str | None = None) -> dict:
    """Scores the recorded rows (free; no generation) and writes scores.json and report.md."""
    from .retrieval import KeywordIndex

    d = run_dir(settings, run_id)
    config = json.loads((d / "config.json").read_text(encoding="utf-8"))
    progress = load_progress(settings, run_id)
    reviews = load_reviews(settings, run_id)
    rows, _, _ = evaluation.load_eval_rows(settings, config["dataset"], sealed=config["dataset"] == "test")
    by_id = {r["question_id"]: r for r in rows}
    finalists, per_finalist, all_scored = {}, {}, []
    for f in config["finalists"]:
        index = None
        try:
            index = KeywordIndex.load(settings, f["index_version"])
        except Exception:  # noqa: BLE001 - citation grading then stays unjudged
            pass
        done = [(by_id[qid], r) for (fin, qid), r in sorted(progress.items())
                if fin == f["run_id"] and r["status"] == "done" and qid in by_id]
        scored = [score_record(row, r, index, reviews) for row, r in done]
        all_scored += scored
        served = [x for x in (served_retrieval(settings, index, row, r) for row, r in done) if x]
        pending = [qid for (fin, qid), r in progress.items() if fin == f["run_id"] and r["status"] != "done"]
        missing = [q for q in by_id if (f["run_id"], q) not in progress]
        finalists[f["run_id"]] = f
        per_finalist[f["run_id"]] = {"mode": f["mode"], "completed": len(scored), "of": len(by_id),
                                     "not_finished": sorted(pending + missing), **aggregate_answers(scored),
                                     "served_retrieval": evaluation.aggregate(served, []) if served else None}
        cost = ledger_cost(settings, run_id, f["run_id"])
        cost["per_question_micro_usd"] = round(cost["settled_micro_usd"] / len(scored), 1) if scored else None
        per_finalist[f["run_id"]]["cost"] = cost
    complete = all(v["completed"] == v["of"] for v in per_finalist.values())
    scores = {"status": "complete" if complete else "partial", "stop_reason": stop_reason, "run_id": run_id,
              "label": config["label"], "finalists": per_finalist, "reviews": len(reviews),
              "selection": compare_finalists(per_finalist) if len(per_finalist) > 1 else None,
              "scored_at": utcnow()}
    write_text_atomic(d / "scores.json", json.dumps(scores, ensure_ascii=False, indent=1))
    write_jsonl_atomic(d / "scored.jsonl", all_scored)
    write_text_atomic(d / "report.md", answer_report_md(config, scores))
    return {"run_id": run_id, "status": scores["status"], "stop_reason": stop_reason,
            "finalists": {k: {"completed": v["completed"], "of": v["of"],
                              "claim_correctness": v["required_claim_correctness"]["rate"],
                              "critical_wrong": len(v["critical_wrong"]),
                              "settled_micro_usd": v["cost"]["settled_micro_usd"]} for k, v in per_finalist.items()}}


def compare_finalists(per: dict) -> dict:
    """Benefit, critical regressions, latency and cost of each finalist against the other (development only)."""
    (a, x), (b, y) = list(per.items())[:2]
    gain = lambda m: None if x[m]["rate"] is None or y[m]["rate"] is None else round(y[m]["rate"] - x[m]["rate"], 4)  # noqa: E731
    return {"baseline": a, "candidate": b, "claim_correctness_gain": gain("required_claim_correctness"),
            "negative_handling_gain": gain("negative_handling"),
            "new_critical_wrong": [c for c in y["critical_wrong"] if c not in x["critical_wrong"]],
            "p95_ms": [x["latency_ms"]["p95"], y["latency_ms"]["p95"]],
            "settled_micro_usd": [x["cost"]["settled_micro_usd"], y["cost"]["settled_micro_usd"]],
            "note": "development evidence for the owner's selection; small denominators: read the intervals"}


def _fmt(r: dict) -> str:
    if not r or not r.get("denominator"):
        return "n/a (0 eligible)"
    return f"{r['rate']} ({r['numerator']}/{r['denominator']}, Wilson {r['wilson95']})"


def answer_report_md(config: dict, scores: dict) -> str:
    usd = lambda m: f"${m / 1_000_000:.6f}"  # noqa: E731
    lines = [f"# Answer run {config['run_id']} ({config['label']})", "",
             f"- dataset `{config['dataset']}` (`{config['dataset_sha256'][:12]}`), population "
             f"`{config['population_sha256'][:12]}`, status **{scores['status']}**"
             + (f" ({scores['stop_reason']})" if scores.get("stop_reason") else ""),
             f"- model {config['model']} (reasoning {config['reasoning_effort']}), prompt {config['prompt_version']}, "
             f"output cap {config['max_output_tokens']}; code `{(config['provenance']['code'].get('git_revision') or 'no git revision')[:12]}`"
             f" dirty={config['provenance']['code'].get('git_dirty')}",
             f"- blind reviews applied: {scores['reviews']}", ""]
    for rid, v in scores["finalists"].items():
        lines += [f"## Finalist `{rid}` ({v['mode']}): {v['completed']}/{v['of']} rows", "",
                  "| Metric | Value |", "| --- | --- |",
                  f"| required-claim correctness | {_fmt(v['required_claim_correctness'])} |",
                  f"| question-level completeness | {_fmt(v['question_completeness'])} |",
                  f"| claims needing review | {v['claims_needing_review']} |",
                  f"| critical wrong values | {v['critical_wrong'] or 'none observed'} |",
                  f"| critical claims contested or awaiting review | {v['critical_unresolved'] or 'none'} |",
                  f"| citation precision (judged links) | {_fmt(v['citation_precision_judged'])} |",
                  f"| citation precision (lower bound, unjudged = unsupported) | "
                  f"{_fmt(v['citation_precision_lower_bound'])} |",
                  f"| unjudged links | {v['links_unjudged']} |",
                  f"| citation coverage of material claims | {_fmt(v['citation_coverage'])} |",
                  f"| unsupported claim rate (reviewed) | {_fmt(v['unsupported_claim_rate'])} |",
                  f"| link validity | {_fmt(v['link_validity'])} |",
                  f"| negative/ambiguous handling | {_fmt(v['negative_handling'])} |",
                  f"| false answers on negatives | {v['false_answers'] or 'none'} |",
                  f"| unnecessary refusals | {_fmt(v['unnecessary_refusals'])} |",
                  f"| metadata stratum | {_fmt(v['metadata_correct'])} |",
                  f"| technical outcomes | {v['technical_outcomes'] or 'none'} |",
                  f"| scope leaks | {v['scope_leaks']} |",
                  f"| settled cost (ledger, every attempt) | {usd(v['cost']['settled_micro_usd'])}; earlier attempts "
                  f"{usd(v['cost'].get('settled_in_earlier_attempts_micro_usd', 0))}, pending "
                  f"{usd(v['cost'].get('pending_micro_usd', 0))}, unknown {usd(v['cost'].get('unknown_micro_usd', 0))}, "
                  f"reconciled attempts {v['cost'].get('reconciled_attempts', 0)}, retried rows "
                  f"{v['cost']['retried_rows']} |",
                  f"| latency p50/p95 ms | {v['latency_ms']['p50']}/{v['latency_ms']['p95']} (n={v['latency_ms']['n']}, "
                  f"{v['latency_ms']['condition']}) |", ""]
        if v["not_finished"]:
            lines += [f"Not finished: {', '.join(v['not_finished'])}", ""]
    if scores.get("selection"):
        lines += ["## Finalist comparison", "", "```json", json.dumps(scores["selection"], ensure_ascii=False,
                                                                     indent=1), "```", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- blind review


def export_review_sheet(settings: Settings, run_id: str, out: Path | None = None) -> dict:
    """Items a person must judge (text claims, unjudged links and answer claims, and any claim to double-check),
    shuffled and keyed by blind IDs: the sheet names neither the finalist nor its retrieval mode."""
    import random

    d = run_dir(settings, run_id)
    config = json.loads((d / "config.json").read_text(encoding="utf-8"))
    scored = read_jsonl(d / "scored.jsonl") if (d / "scored.jsonl").exists() else []
    progress = load_progress(settings, run_id)
    rows, _, _ = evaluation.load_eval_rows(settings, config["dataset"], sealed=config["dataset"] == "test")
    by_id = {r["question_id"]: r for r in rows}
    items, key = [], {}
    for s in scored:
        row, rec = by_id.get(s["question_id"]), progress.get((s["finalist"], s["question_id"])) or {}
        if row is None:
            continue
        answer = rec.get("answer") or {}
        base = {"question": row["question"], "answer_summary": answer.get("summary"),
                "answer_claims": [c.get("text") for c in answer.get("claims") or []],
                "gold_quotes": [a["quote"] for g in row.get("evidence_groups") or [] for a in g["alternatives"]]}
        for c in s["claims"]:
            if c["verdict"] in ("needs_review", "wrong_value", "contested") or c["critical_kind"]:
                spec = next(x for x in row["required_claims"] if x["claim_id"] == c["claim_id"])
                items.append(("claim", c["item"], {**base, "expected": spec["match"], "qualifiers": spec["qualifiers"],
                                                   "deterministic": c["deterministic"]}))
        for link in s["links"]:
            if link["support"] not in ("supporting", "unsupported"):
                ev = (rec.get("evidence") or {}).get(link["evidence_id"]) or {}
                items.append(("link", link["item"], {**base, "claim": base["answer_claims"][link["claim_index"]],
                                                     "cited_quote": ev.get("quote")}))
        for a in s["answer_claims"]:
            if a["supported"] is None:
                items.append(("answer_claim", a["item"], {**base, "claim": base["answer_claims"][a["index"]]}))
    random.Random(run_id).shuffle(items)
    sheet = []
    for kind, item, body in items:
        blind = hashlib.sha256(f"{run_id}|{item}".encode()).hexdigest()[:12]
        key[blind] = item
        sheet.append({"blind_id": blind, "kind": kind, "allowed": list(REVIEW_VERDICTS[kind]), **body,
                      "verdict": None, "note": ""})
    write_text_atomic(d / "review-key.json", json.dumps(key, ensure_ascii=False, indent=1))
    target = out or d / "review-sheet.jsonl"
    if out is not None and not out.is_absolute():
        raise AnswerEvalError("--out must be an absolute path")
    write_jsonl_atomic(target, sheet)
    return {"run_id": run_id, "items": len(sheet), "sheet": str(target),
            "note": "blind: no finalist or mode is named; keep the sheet with the sealed files for a test run"}


def import_reviews(settings: Settings, run_id: str, path: Path, reviewer: str) -> dict:
    """Appends completed blind verdicts; the latest review of an item wins at scoring. Re-scores the run."""
    if not reviewer.strip():
        raise AnswerEvalError("--reviewer is required")
    d = run_dir(settings, run_id)
    key = json.loads((d / "review-key.json").read_text(encoding="utf-8"))
    records, errors = [], []
    for i, r in enumerate(read_jsonl(path), 1):
        if r.get("verdict") in (None, ""):
            continue
        item = key.get(r.get("blind_id"))
        if item is None:
            errors.append(f"line {i}: unknown blind_id")
            continue
        kind = item.split("|")[2]
        if r["verdict"] not in REVIEW_VERDICTS[kind]:
            errors.append(f"line {i}: verdict must be one of {REVIEW_VERDICTS[kind]}")
            continue
        records.append({"item": item, "verdict": r["verdict"], "note": r.get("note", ""), "reviewer": reviewer,
                        "reviewed_at": utcnow()})
    if errors:
        raise AnswerEvalError("\n".join(errors))
    existing = read_jsonl(d / "review.jsonl") if (d / "review.jsonl").exists() else []
    write_jsonl_atomic(d / "review.jsonl", existing + records)
    summary = finalize(settings, run_id, json.loads((d / "scores.json").read_text(encoding="utf-8")).get("stop_reason")
                       if (d / "scores.json").exists() else None)
    return {"imported": len(records), **summary}


# ---------------------------------------------------------------- latency sample


def _latency_questions(settings: Settings) -> list[dict]:
    rows, _, _ = evaluation.load_eval_rows(settings, "dev")
    return [r for r in rows if r["answerability"] == "answerable" and r.get("mode") == "single"]


def plan_latency(settings: Settings, waves: int = 5, users: int = 6, *, store: bool = True) -> dict:
    """Bounded real latency sample: `waves` waves of `users` concurrent answers on the activated configuration
    (development questions), priced before anything is sent."""
    if not 1 <= waves <= 20 or not 1 <= users <= settings.request_workers:
        raise AnswerEvalError(f"latency samples take 1..20 waves of 1..{settings.request_workers} members")
    questions = _latency_questions(settings)
    if not questions:
        raise AnswerEvalError("no reviewed single-document development question to time")
    serving = service.active_serving(settings)  # what requests serve now, a stale activation included
    pinned =PinnedResources(settings, None, serving)
    picks = [questions[i % len(questions)] for i in range(waves * users)]
    prices = [price_row(pinned, q) for q in picks]
    ledger = _ledger(settings)
    total = sum(p["max_micro_usd"] for p in prices)
    run_id = "L-" + hashlib.sha256(dumps({"serving": _finalist_identity(serving), "waves": waves, "users": users,
                                          "q": [q["question_id"] for q in picks]}).encode()).hexdigest()[:12]
    now = datetime.now(timezone.utc)
    est = {"estimate_id": uuid.uuid4().hex[:12], "action": "latency", "run_id": run_id, "waves": waves,
           "users": users, "questions": [q["question_id"] for q in picks], "max_micro_usd": total,
           "serving": serving, "purpose": "gold_eval", **ledger,
           "fits": total <= min(ledger["envelope_remaining_micro_usd"], ledger["available_micro_usd"]),
           "fingerprint": hashlib.sha256(dumps({"run": run_id, "rates": ledger["rate_version"],
                                                "p": [p["max_micro_usd"] for p in prices]}).encode()).hexdigest(),
           "created_at": now.isoformat(), "expires_at": (now + timedelta(hours=ESTIMATE_TTL_HOURS)).isoformat()}
    if store:
        _store_estimate(settings, est)
    return est


def latency_run(settings: Settings, owner: service.Resources, estimate_id: str, actor: str) -> dict:
    """Runs the planned waves, each wave's members answering concurrently as the executor's workers would, and records n,
    failures, per-wave timing and the first (cold) wave separately. A preliminary sample, not an SLA."""
    import threading

    est = load_estimate(settings, estimate_id)
    if est["action"] != "latency":
        raise AnswerEvalError("latency-run takes a latency estimate")
    path = settings.data_dir / "releases" / "latency" / f"{est['run_id']}-{estimate_id}.json"
    if path.exists():  # its request keys would return stored answers: timings of nothing
        raise AnswerEvalError("this latency estimate was already used; plan a new sample")
    recheck(settings, est)
    pinned = PinnedResources(settings, owner.transport, est["serving"], owner)
    by_id = {q["question_id"]: q for q in _latency_questions(settings)}
    waves = []
    for w in range(est["waves"]):
        batch = est["questions"][w * est["users"]:(w + 1) * est["users"]]
        timings: list[dict] = []
        lock = threading.Lock()

        def one(i: int, qid: str) -> None:
            q = by_id[qid]
            key = f"{est['run_id']}:{estimate_id}:{w}:{i}"
            t0 = time.perf_counter()
            r = service.answer(pinned, Principal(f"{EVAL_MEMBER}-{i + 1}", frozenset({"consultant"})), AnswerRequest(
                key, key, q["question"], _refs(q), mode="single", as_of=q["as_of_date"]))
            with lock:
                timings.append({"question_id": qid, "ms": round((time.perf_counter() - t0) * 1000, 1),
                                "status": r.status, "billing": r.billing_state})

        threads = [threading.Thread(target=one, args=(i, qid)) for i, qid in enumerate(batch)]
        start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        waves.append({"wave": w + 1, "wall_ms": round((time.perf_counter() - start) * 1000, 1), "requests": timings})
        if any(t["status"] == "budget_blocked" or t["billing"] == "unknown" for t in timings):
            break
    from .dense import percentile

    ok = [t["ms"] for wv in waves for t in wv["requests"] if t["status"] not in TECHNICAL]
    warm = [t["ms"] for wv in waves[1:] for t in wv["requests"] if t["status"] not in TECHNICAL]
    result = {"run_id": est["run_id"], "estimate_id": estimate_id, "actor": actor,
              "provider": "fake" if isinstance(getattr(owner.transport, "inner", owner.transport),
                                                 generation.FakeTransport) else "real",
              "users": est["users"], "waves_planned": est["waves"], "waves_run": len(waves),
              "n": sum(len(wv["requests"]) for wv in waves),
              "failures": sum(t["status"] in TECHNICAL for wv in waves for t in wv["requests"]),
              "all_ms": {"p50": percentile(ok, 0.5), "p95": percentile(ok, 0.95), "n": len(ok)},
              "warm_ms": {"p50": percentile(warm, 0.5), "p95": percentile(warm, 0.95), "n": len(warm),
                          "note": "waves after the first"},
              "cold_wave_ms": waves[0]["wall_ms"] if waves else None, "waves": waves,
              "serving": {k: est["serving"].get(k) for k in ("run_id", "mode", "index_version")},
              "hardware": evaluation.hardware(), "code": evaluation.code_fingerprint(), "recorded_at": utcnow(),
              "note": "preliminary latency sample of full answers under concurrent members; not an SLA"}
    write_text_atomic(path, json.dumps(result, ensure_ascii=False, indent=1))
    return {**result, "path": str(path)}
