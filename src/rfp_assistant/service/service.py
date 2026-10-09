"""Authenticated public functions, shared resource ownership and request orchestration.

Resources (Kiwi analyzer, loaded index, SDK transport and its Langfuse tracing, process-owner lock) are created
once per process by `Resources` and closed by `Resources.close()`. Sessions only supply the principal and the scope.
"""

from __future__ import annotations

import atexit
import contextvars
import dataclasses
import functools
import hashlib
import json
import re
import secrets
import subprocess
import threading
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import date
from pathlib import Path

from . import auth, update
from ..gateway import budget, generation, tracing
from ..corpus import fidelity
from ..evaluation import gold
from ..storage import postgres
from ..retrieval import dense as dense_mod
from .auth import require_any
from ..contracts import (AnswerRequest, AnswerResult, BudgetSnapshot, DocRef, EvidenceUnit, EvidenceView,
                        ManagedDownload, Principal, RequestView, RetrievalResult, TERMINAL_STATUSES)
from ..corpus.ingestion import (CODE_RE, QUARANTINE_TEXT, extraction_review_status, load_elements, nfc, printed_pdf_path,
                        record_review, resolutions_by_doc)
from ..retrieval.retrieval import DENSE_MODES, Analyzer, KeywordIndex, best_chunk_per_extraction, corpus_scope, scope_rows
from ..retrieval.retrieval import retrieve as _retrieve
from ..evaluation.evaluation import EVAL_VERSION
from ..storage.postgres import Row
from ..settings import ALLOWED_EMBEDDING_MODELS, ALLOWED_GENERATION_MODELS, Settings, read_api_key
from ..storage.store import DATABASE_ERRORS, LockHeld, dumps, init_schema, open_db, tx, utcnow


KEY_SESSION = generation.KEY_SESSION  # the API binds the signed-in member's key session for each request


class ServiceError(RuntimeError):
    pass


class GatewayLockError(ServiceError):
    pass


_ANALYZER: Analyzer | None = None
_ANALYZER_LOCK = threading.Lock()


def shared_analyzer() -> Analyzer:
    """One Kiwi analyzer per process: its model is large and it is already shared by concurrent requests."""
    global _ANALYZER
    with _ANALYZER_LOCK:
        if _ANALYZER is None:
            _ANALYZER = Analyzer()
        return _ANALYZER


class Resources:
    """Process-wide owner. provider='fake' never builds a real SDK client, whatever keys the host has."""

    def __init__(self, settings: Settings, transport: generation.Transport | None = None,
                 recover: bool = False, dispatch: bool = True, tracer: tracing.Tracing | None = None) -> None:
        """`dispatch=False` is the ledger-only owner (budget administration beside the serving app): no provider
        client and no gateway ownership, so any paid stage through it is refused at admission."""
        from ..storage.store import database_lifecycle

        self._dispatch = dispatch
        self.tracing: tracing.Tracing | None = tracer
        self._database_lifecycle = database_lifecycle(settings.db_path)
        self._database_lifecycle.__enter__()
        try:
            self._initialize(settings, transport, recover)
        except BaseException:
            try:
                if getattr(self, "transport", None) is not None:
                    self.transport.close()
                if self.tracing is not None:
                    self.tracing.close()
            finally:
                self._release_database()
            raise

    def _release_database(self) -> None:
        """The gateway owner, then this process's hold on the database, even when releasing the owner fails."""
        try:
            self._release_owner()
        finally:
            self._database_lifecycle.__exit__(None, None, None)

    def _release_owner(self) -> None:
        """Owned or borrowed, the gateway owner counts its users and releases the lock with the last one."""
        for name in ("_lock", "_borrowed_owner"):
            owner = getattr(self, name, None)
            if owner is not None:
                setattr(self, name, None)
                owner.release()

    def _initialize(self, settings, transport, recover):
        """`recover=True` claims exclusive ownership of the data directory (the serving application does; a real
        gateway always does) and resolves what a previous process left: unfinished requests become interrupted,
        dispatching attempts unknown, never-dispatched reservations released. Nothing is replayed."""
        self.settings = settings
        self.paid_purpose = "interactive"  # ledger envelope of every paid stage this owner dispatches
        postgres.require_imported_database(settings.db_path)
        init_schema(settings.db_path)
        budget.ensure_budget_row(settings.db_path)
        self._lock: postgres.GatewayOwner | None = None  # owned here: released by close()
        self._borrowed_owner: postgres.GatewayOwner | None = None  # this process's owner, held by another Resources
        self.transport: generation.Transport | None = transport
        self.provider_note = ""
        self.key_sources: dict[str, dict] = {}  # key session -> who entered its key and when; never the key
        self.member_keys: dict[str, str] = {}  # signed-in member -> their current key session
        self.recovered: dict = {}
        if not self._dispatch:
            if recover or transport is not None:
                raise ValueError("a ledger-only Resources neither recovers nor dispatches")
            self.provider_note = "ledger-only owner: paid stages are not dispatched here"
        elif recover:
            self._own(settings)
        if self._dispatch and transport is None and settings.provider == "fake":
            self.transport = generation.FakeTransport(delay_seconds=settings.fake_delay_seconds)
        elif self._dispatch and transport is None:
            key = read_api_key("OPENAI_API_KEY")
            if key:
                self._openai_transport(key)
            else:
                self.provider_note = generation.NO_API_KEY
        if self.transport is not None and self._lock is None and self._borrowed_owner is None:
            self._own(settings)  # every PostgreSQL dispatch has a gateway owner
        self.analyzer = shared_analyzer()
        self._index: KeywordIndex | None = None
        self._dense: dense_mod.DenseIndex | None = None
        self._reranker = None
        self._reranker_key: tuple | None = None
        self._dense_failed: tuple | None = None
        self.stage_errors: dict[str, str] = {}
        self._index_lock = threading.Lock()
        self._runner: RequestRunner | None = None
        self._runner_lock = threading.Lock()
        self._jobs: list[threading.Thread] = []  # drafting, answer evaluation, judge runs: joined by close()
        # True while an accepted update may restart this process, so no new paid work starts (runbook 3.2). The API
        # sets it to its UpdateWatch.fenced; read under _runner_lock, which every paid admission holds.
        self.update_fence = lambda: False
        self.partials: dict[str, str] = {}  # request_id -> answer streamed so far, until the request finishes
        self._closed = False
        closer = weakref.WeakMethod(self.close)  # atexit must not keep every Resources (and its index) alive

        def stop() -> None:
            (closer() or (lambda: None))()

        # A controlled stop (uvicorn's SIGINT/SIGTERM handler ends the server loop, then the interpreter exits)
        # must close while the workers are still alive, so their next paid stage sees the stop. Ordinary atexit
        # runs only after `concurrent.futures` has joined every worker; threading's exit hooks run before that
        # join, newest first, so this one precedes the executor's own (registered at import).
        try:
            threading._register_atexit(stop)
        except (AttributeError, RuntimeError):  # no such hook, or the interpreter is already shutting down
            pass
        atexit.register(stop)

    def _openai_transport(self, key: str | None) -> None:
        if self._lock is None and self._borrowed_owner is None:
            self._own(self.settings)
        self.transport = generation.OpenAITransport(key, self.settings.request_timeout_seconds,
                                                    owner_check=(self._lock or self._borrowed_owner).check)
        if self.tracing is None:  # traced with the transport it observes; an injected transport is not
            self.tracing = tracing.Tracing.from_settings(self.settings)

    def set_api_key(self, session: str, key: str, member_id: str, model: str, billing_scope: str) -> None:
        """A member's key from the 설정 page, under a new key session that becomes that member's current one. It
        lives only in this process's memory: no file, row, log or trace holds it, so a restart needs it entered
        again. Other members never use it."""
        if not self._dispatch or self.settings.provider != "openai":
            raise ServiceError("이 서버는 OpenAI 제공자로 실행되지 않아 API 키를 쓰지 않습니다.")
        with self._runner_lock:  # close() takes it too: no transport appears after shutdown began
            if self._closed:
                raise ServiceError("서비스가 종료 중입니다.")
            if not isinstance(self.transport, generation.OpenAITransport):
                self._openai_transport(None)
            self.transport.set_key(session, key)
            self.key_sources[session] = {"set_by": member_id, "set_at": utcnow(), "model": model,
                                         "billing_scope": billing_scope}
            self.member_keys[member_id] = session

    def generation_model(self) -> str:
        """The answer model this request's member had chosen when the request began, else the configured one."""
        return generation.REQUEST_MODEL.get() or self.settings.generation_model

    def paid_refusal(self) -> str:
        """Why this context cannot dispatch a paid stage now, or "" when it can."""
        if self.transport is None:
            return self.provider_note
        has_key = getattr(getattr(self.transport, "inner", self.transport), "has_key", None)
        return generation.NO_API_KEY if has_key is not None and not has_key() else ""

    def _own(self, settings: Settings) -> None:
        shared = postgres.borrow_owner(settings.db_path)
        if shared is not None:  # another Resources of this process owns it: its work is live, so no recovery
            self._borrowed_owner = shared
            return
        try:
            self._lock = postgres.gateway_lock(settings.db_path, settings.data_dir)
        except LockHeld:
            raise GatewayLockError(
                "another process already owns the paid gateway for this database") from None
        # No older worker can still dispatch: recover its requests and attempts conservatively.
        self.recovered = {"requests": recover_requests(settings.db_path), **budget.recover(settings.db_path)}

    def runner(self) -> "RequestRunner":
        with self._runner_lock:
            if self._closed:
                raise ServiceError("서비스가 종료 중이어서 새 요청을 받지 않습니다. (유료 호출 없음)")
            if self._runner is None:
                self._runner = RequestRunner(self)
            return self._runner

    def serving(self) -> dict:
        return active_serving(self.settings)

    def run_settings(self, cfg: dict | None = None) -> Settings:
        """Settings the activated run was evaluated with: embedding model/dimensions, depths, RRF constant and
        evidence limits override the process configuration, so serving reproduces what was measured."""
        cfg = cfg or self.serving()
        changes = dict(cfg.get("limits") or {})
        if cfg.get("embedding"):
            changes.update(embedding_model=cfg["embedding"]["model"], embedding_dimensions=cfg["embedding"]["dims"])
        rr = cfg.get("reranker") or {}
        if rr.get("max_length"):
            changes["reranker_max_length"] = rr["max_length"]
        if rr.get("max_concurrency"):  # the bound the six-user latency gate was measured with
            changes["reranker_max_concurrency"] = rr["max_concurrency"]
        if rr:
            # A trial that recorded no precision was measured at the then-only fp32; never inherit the process value.
            changes["reranker_precision"] = rr.get("precision") or "fp32"
        known = set(Settings.__dataclass_fields__)
        return self.settings.with_(**{k: v for k, v in changes.items() if k in known})

    def index(self) -> KeywordIndex | None:
        with self._index_lock:
            active = self.serving()["index_version"]
            if active is None:
                return None
            if self._index is None or self._index.version != active:
                self._index = KeywordIndex.load(self.settings, active)
            return self._index

    def dense(self) -> dense_mod.DenseIndex | None:
        """Loaded and verified once per version. A mismatch leaves it unloaded so retrieval falls back to the
        keyword mode with the reason in its trace; no corpus rebuild happens during a question."""
        version = self.serving().get("dense_version")
        if not version:
            return None
        idx = self.index()
        with self._index_lock:
            if self._dense is not None and self._dense.version == version and idx is not None \
                    and self._dense.base_index_version == idx.version:
                return self._dense
            if self._dense_failed == (version, idx.version if idx else None):
                return None  # verified once and refused; a new activation or restart checks again
            try:
                self._dense = dense_mod.DenseIndex.load(self.settings, version, base=idx)
                self.stage_errors.pop("dense", None)
            except dense_mod.DenseError as exc:
                self._dense = None
                self._dense_failed = (version, idx.version if idx else None)
                self.stage_errors["dense"] = str(exc)[:300]
            return self._dense

    def retire_inactive(self, cfg: dict) -> None:
        """A serving transition (to another embedding, an API or keyword row, or away from reranking) retires the
        GPU models the activated configuration no longer uses, whichever process activated it; requests still
        using one keep their own reference until they finish."""
        from ..retrieval.models import EMBEDDINGS, _EMBEDDERS, free_gpu, unload_embedders

        model = (cfg.get("embedding") or {}).get("model") if cfg["mode"] in DENSE_MODES else None
        keep = model if model in EMBEDDINGS and EMBEDDINGS[model].backend == "local" else None
        if any(k != keep for k in list(_EMBEDDERS)):
            unload_embedders(keep)
        if cfg["mode"] != "hybrid_rerank" or not cfg.get("reranker"):
            with self._index_lock:
                if self._reranker is not None:
                    self._reranker, self._reranker_key = None, None
                    free_gpu()

    def reranker(self):
        cfg = self.serving().get("reranker")
        if not cfg:
            return None
        s = self.run_settings().with_(reranker_model=cfg.get("model") or self.settings.reranker_model,
                                      reranker_revision=cfg.get("revision") or "")
        # Everything the loaded model's inference depends on; depth alone needs no reload.
        key = (s.reranker_model, s.reranker_revision, s.reranker_max_length, s.reranker_max_concurrency,
               s.reranker_precision)
        with self._index_lock:
            if self._reranker_key != key:
                self._reranker_key = key
                # Retire the previous model before loading the next, so both never need the GPU at once; a request
                # still reranking with it keeps its own reference until it finishes.
                self._reranker = None
                from ..retrieval.models import free_gpu

                free_gpu()
                self._reranker, info = dense_mod.load_reranker(s)
                if self._reranker is None:
                    self.stage_errors["reranker"] = info.get("error", "unavailable")
                else:
                    self.stage_errors.pop("reranker", None)
            return self._reranker

    def close(self) -> None:
        """Controlled stop: reject new submissions, wait a bounded time for workers, persist what is unfinished,
        then close the owned client and release ownership."""
        with self._runner_lock:
            if self._closed:
                return
            self._closed = True
            runner = self._runner
            jobs = list(self._jobs)
        deadline = time.monotonic() + self.settings.shutdown_wait_seconds
        if runner is not None:
            runner.shutdown(self.settings.shutdown_wait_seconds)
        for thread in jobs:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in jobs):
            recover_requests(self.settings.db_path)
            budget.recover(self.settings.db_path)
        try:
            try:
                if self.transport is not None:
                    self.transport.close()
            finally:
                if self.tracing is not None:  # after the workers: flushes every trace they finished
                    self.tracing.close()
        finally:
            self._release_database()


class RequestRunner:
    """Process-owned bounded executor: at most `request_workers` threads and `request_admission` admitted
    unfinished requests. Workers get only request IDs; they never touch UI state."""

    def __init__(self, res: Resources) -> None:
        self.res = res
        s = res.settings
        self._executor = ThreadPoolExecutor(max_workers=s.request_workers, thread_name_prefix="rfp-request")
        self._slots = threading.BoundedSemaphore(s.request_admission)
        self._futures: dict[str, object] = {}
        self._lock = threading.Lock()
        self._accepting = True
        self.admitted = 0  # slots held, counted before the queued row exists (open_paid_work)

    def try_admit(self) -> bool:
        with self.res._runner_lock:  # one step with the update fence: an accepted update never races a new request
            _refuse_while_updating(self.res)
            if not (self._accepting and self._slots.acquire(blocking=False)):
                return False
            self.admitted += 1
            return True

    def release(self) -> None:
        with self.res._runner_lock:
            self.admitted -= 1
        self._slots.release()

    def submit(self, request_id: str) -> None:
        with self._lock:
            if not self._accepting:
                raise ServiceError("service is shutting down")
            self._futures[request_id] = self._executor.submit(contextvars.copy_context().run, self._run, request_id)

    def _run(self, request_id: str) -> None:
        try:
            run_queued(self.res, request_id)
        finally:
            self.release()
            with self._lock:
                self._futures.pop(request_id, None)

    def active(self) -> int:
        with self._lock:
            return len(self._futures)

    def shutdown(self, wait_seconds: float) -> dict:
        with self._lock:
            self._accepting = False
            futures = list(self._futures.values())
        done, pending = wait(futures, timeout=wait_seconds) if futures else (set(), set())
        self._executor.shutdown(wait=False, cancel_futures=True)
        with open_db(self.res.settings.db_path) as conn, tx(conn, immediate=True):
            # Unstarted work never dispatched; a running worker may still be inside a provider call: its attempt
            # stays dispatching (unknown after restart) and still settles if the call returns.
            queued = conn.execute("UPDATE requests SET status = 'interrupted', updated_at = ? WHERE status = 'queued' "
                                  "AND request_json IS NOT NULL", (utcnow(),)).rowcount
            running = conn.execute("UPDATE requests SET status = 'interrupted', updated_at = ? "
                                   "WHERE status = 'running' AND request_json IS NOT NULL", (utcnow(),)).rowcount \
                if pending else 0
        return {"finished": len(done), "unfinished": len(pending), "queued_interrupted": queued,
                "running_interrupted": running}


_shared: Resources | None = None
_shared_lock = threading.Lock()


def active_serving(settings: Settings) -> dict:
    """The activated retrieval configuration (`activate-run`), or the keyword default before any selection. Every
    reader of the activation goes through here (requests, latency samples), so none serves a run it would refuse."""
    from ..retrieval.retrieval import corpus_route_record
    from ..storage.store import get_app_setting

    with open_db(settings.db_path) as conn:
        run = get_app_setting(conn, "active_run")
        active = get_app_setting(conn, "active_index")
    default = {"run_id": None, "mode": settings.retrieval_mode, "index_version": active,
               "dense_version": None, "reranker": None, "fallback_mode": "kiwi_bm25"}
    if not run:
        return default
    cfg = json.loads(run)
    if cfg.get("run_id") and (cfg.get("limits") or {}).get("corpus_route") != corpus_route_record():
        # Routing changed since the run was measured: its gate evidence no longer describes retrieval, so serve the
        # unselected default (retrieve flags it) until a rerun is activated (run_errors refuses the old run).
        return {**default, "fallback_reason": "activated_corpus_route_requires_rerun", "stale_run_id": cfg["run_id"]}
    if cfg.get("embedding") and cfg["embedding"].get("model") not in ALLOWED_EMBEDDING_MODELS:
        # Serving follows the activated run's embedding model; only a model this code cannot run is refused.
        return {**cfg, "mode": "kiwi_bm25", "dense_version": None, "embedding": None,
                "reranker": None, "fallback_reason": "activated_embedding_model_unknown"}
    if cfg["mode"] == "hybrid_rerank" and cfg.get("eval_version") != EVAL_VERSION:
        # Promoted under a superseded gate: keep hybrid retrieval, drop the reranker until a current
        # trial passes and is activated.
        cfg = {**cfg, "mode": "hybrid", "reranker": None, "stale_policy": cfg.get("eval_version")}
    return cfg


def describe_serving(cfg: dict) -> str:
    """The one report wording for what requests serve (`active_serving`), naming a stored activation that is not."""
    if cfg.get("fallback_reason"):
        return (f"keyword default (kiwi_bm25); activated run `{cfg.get('stale_run_id') or cfg.get('run_id')}` "
                f"not served: {cfg['fallback_reason']}")
    if cfg.get("run_id"):
        return f"run `{cfg['run_id']}` ({cfg['mode']})"
    return "keyword default (kiwi_bm25), no activated run"


def get_resources(settings: Settings) -> Resources:
    """The serving application's single owner of the data directory."""
    global _shared
    with _shared_lock:
        if _shared is None or _shared._closed:
            _shared = Resources(settings, recover=True)
        return _shared


def _authorize(res: Resources, principal: Principal | None, *capabilities: str) -> Principal:
    """States which role a public function serves. Every signed-in member holds every capability; in-process
    callers (CLI, tests) may pass narrower principals."""
    return require_any(principal, *capabilities)


# ---------------------------------------------------------------- scope and metadata


def _doc_rows(res: Resources, doc_ids: list[str] | None = None) -> list[dict]:
    idx = res.index()
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute(
            "SELECT d.*, s.format, s.parse_status, s.review_status, s.reason_code, s.active_extraction_id "
            "FROM documents d JOIN sources s ON s.source_hash = d.active_source_hash ORDER BY d.csv_row_id").fetchall()
        resolutions = resolutions_by_doc(conn)
        out = []
        for r in rows:
            if doc_ids is not None and r["doc_id"] not in doc_ids:
                continue
            d = dict(r)
            # Serving reads an original through the extraction its activated index holds: a re-parse moves
            # `active_extraction_id` before the person activates the index built over the new extraction, and a
            # failed re-parse quarantines the source without moving it. Either way the source's statuses describe
            # the latest attempt, not the extraction the index serves, which then reports its own review.
            served = idx.source_extraction.get(d["active_source_hash"]) if idx else None
            if served is not None and (served != d["active_extraction_id"] or d["parse_status"] != "parsed"):
                d.update(active_extraction_id=served, parse_status="parsed", reason_code=None,
                         review_status=extraction_review_status(conn, served))
            out.append(d)
    for d in out:
        d["meta"] = json.loads(d.pop("normalized_metadata_json"))
        d["quality"] = json.loads(d.pop("quality_json"))
        d["resolutions"] = resolutions.get(d["doc_id"], {})
        # Effective view: a written owner/reviewer resolution replaces the CSV value for filtering, ranking and
        # display; d["meta"] keeps the CSV values and d["resolutions"] the provenance.
        d["effective"] = {**d["meta"], **{f: r["value"] for f, r in d["resolutions"].items()}}
        d.pop("raw_metadata_json")
    return out


def _resolve_scope(res: Resources, scope: list[DocRef]) -> list[dict]:
    if not scope:
        raise ServiceError("문서를 먼저 선택하세요.")
    docs = {d["doc_id"]: d for d in _doc_rows(res, [s.doc_id for s in scope])}
    out = []
    for ref in scope:
        d = docs.get(ref.doc_id)
        if d is None or d["active_source_hash"] != ref.source_hash:
            raise ServiceError("선택한 문서 버전이 현재 원문과 일치하지 않습니다. 다시 선택하세요.")
        out.append(d)
    return out


def _indexed(res: Resources, extraction_id: str | None) -> bool:
    idx = res.index()
    return bool(idx and extraction_id and extraction_id in idx.rows_by_extraction)


def _filtered(d: dict, filters: dict) -> list[str] | None:
    """The range filters a document's unknown or conflicting values left undecided, or None when the filters
    exclude it."""
    inst = nfc(filters.get("institution") or "").strip()
    amount_min, amount_max = filters.get("amount_min"), filters.get("amount_max")
    close_from, close_to = filters.get("closing_from"), filters.get("closing_to")
    m = d["effective"]  # the competing CSV values stay visible in the result
    conflicts = {c["field"] for c in d["quality"].get("provenance_conflicts", [])} - set(d["resolutions"])
    if inst and inst not in (m["institution"] or ""):
        return None
    undecided = []  # a range filter never treats an unknown or conflicting value as satisfied
    if amount_min is not None or amount_max is not None:
        if m["amount_krw"] is None or "amount_krw" in conflicts:
            undecided.append("amount_krw:" + ("conflict" if "amount_krw" in conflicts else "unknown"))
        elif (amount_min is not None and m["amount_krw"] < amount_min) or \
                (amount_max is not None and m["amount_krw"] > amount_max):
            return None
    if close_from or close_to:
        if m["bid_close"] is None or "bid_close" in conflicts:
            undecided.append("bid_close:" + ("conflict" if "bid_close" in conflicts else "unknown"))
        else:
            day = m["bid_close"]["value"][:10]
            if (close_from and day < close_from) or (close_to and day > close_to):
                return None
    if undecided and not filters.get("include_unknown", False):
        return None
    if filters.get("parsed_only", False) and d["parse_status"] != "parsed":
        return None
    return undecided


def search_projects(res: Resources, principal: Principal, filters: dict, query: str, limit: int = 20) -> list[dict]:
    """Free route: metadata filters plus keyword snippets. Never calls a paid model."""
    principal = _authorize(res, principal, "consultant", "verifier")
    items = []
    notes: dict[str, list[str]] = {}
    for d in _doc_rows(res):
        undecided = _filtered(d, filters)
        if undecided is not None:
            notes[d["doc_id"]] = undecided
            items.append(d)
    q = nfc(query or "").strip()
    snippets: dict[str, tuple[float, str]] = {}
    idx = res.index()
    if q and idx is not None:
        snippets = best_chunk_per_extraction(idx, res.analyzer, q)
    ranked = []
    for d in items:
        words = [w for w in re.split(r"\s+", q) if w]
        m = d["effective"]
        meta_hit = sum(w in (m["title"] or "") or w in (m["institution"] or "") for w in words)
        snip = snippets.get(d["active_extraction_id"] or "")
        if q and not meta_hit and not snip:
            continue
        ranked.append((meta_hit, snip[0] if snip else 0.0, d, snip[1] if snip else None))
    ranked.sort(key=lambda x: (-x[0], -x[1]))
    return [{
        "doc_id": d["doc_id"], "source_hash": d["active_source_hash"], "title": d["effective"]["title"],
        "institution": d["effective"]["institution"], "amount_krw": d["effective"]["amount_krw"],
        "published_at": d["effective"]["published_at"], "bid_close": d["effective"]["bid_close"],
        "csv_metadata": {f: d["meta"].get(f) for f in d["resolutions"]},
        "format": d["format"], "parse_status": d["parse_status"], "review_status": d["review_status"],
        "indexed": _indexed(res, d["active_extraction_id"]),
        "unavailable_reason": QUARANTINE_TEXT.get(d["reason_code"] or ""),
        "flags": d["quality"]["flags"], "conflicts": d["quality"].get("provenance_conflicts", []),
        "resolutions": d["resolutions"], "snippet": snip, "filter_undecided": notes.get(d["doc_id"], []),
        "notice": d["effective"].get("notice"),
    } for _, _, d, snip in ranked[:limit]]


LIMIT_KEYS = ("evidence_max_units", "evidence_target_tokens", "evidence_max_tokens", "channel_top_k",
              "fused_top_k")


def _narrow_limits(s: Settings, limits: dict | None) -> dict:
    """The limit values a narrowing override actually executes with: never above the serving value, at least 1,
    and the evidence target never above the evidence maximum."""
    if not limits:
        return {}
    unknown = set(limits) - set(LIMIT_KEYS)
    if unknown:
        raise ServiceError(f"unknown retrieval limits {sorted(unknown)}")
    narrowed = {k: max(1, min(int(v), getattr(s, k))) for k, v in limits.items()}
    target = min(narrowed.get("evidence_target_tokens", s.evidence_target_tokens),
                 narrowed.get("evidence_max_tokens", s.evidence_max_tokens))
    if target != s.evidence_target_tokens or "evidence_target_tokens" in narrowed:
        narrowed["evidence_target_tokens"] = target
    return narrowed


def retrieve(res: Resources, principal: Principal, question: str, scope: list[DocRef], *,
             request_id: str | None = None, allow_paid: bool = False, limits: dict | None = None,
             mode: str | None = None, all_documents: bool = False) -> RetrievalResult:
    """Serves the activated mode. A dense query vector comes from the cache, or through the gateway only when
    the caller is a paid request (`allow_paid`); an empty scope never pays for one. `limits` may only narrow the
    evidence/depth limits (comparison sides, verifier configurations); `mode` lets a verifier pick a free mode.
    `all_documents` searches every indexed original instead of `scope`."""
    principal = _authorize(res, principal, "consultant", "verifier")
    docs = [] if all_documents else _resolve_scope(res, scope)
    idx = res.index()
    if idx is None:
        raise ServiceError("검색 색인이 아직 없습니다.")
    cfg = res.serving()
    res.retire_inactive(cfg)
    if mode is not None and mode != cfg["mode"]:
        if mode not in ("whitespace_bm25", "kiwi_bm25") + tuple(DENSE_MODES):
            raise ServiceError("알 수 없는 검색 방식입니다.")
        cfg = {**cfg, "mode": mode}
    s = res.run_settings(cfg)
    if limits:
        s = s.with_(**_narrow_limits(s, limits))
    pairs = corpus_scope(res.settings, idx) if all_documents else \
        [(DocRef(d["doc_id"], d["active_source_hash"]), d["active_extraction_id"]) for d in docs]
    dense = qvec = qinfo = None
    if cfg["mode"] in DENSE_MODES:
        dense = res.dense()
        if dense is not None and (dense.model, dense.dims) != (s.embedding_model, s.embedding_dimensions):
            # Never pay for a query vector the matrix cannot score; serve the recorded keyword fallback.
            res.stage_errors["dense"] = (f"matrix {dense.model}/{dense.dims} does not match the run's embedding "
                                         f"{s.embedding_model}/{s.embedding_dimensions}")
            dense = None
        if dense is not None and scope_rows(idx, pairs):
            qvec, qinfo = dense_mod.query_vector(s, res.transport, question, request_id=request_id,
                                                 member_id=principal.member_id, purpose=res.paid_purpose,
                                                 allow_paid=allow_paid, guard=_dispatch_guard(res, request_id))
    with tracing.step("retrieve-evidence", "retriever", input={
            "question": question, "scope": [asdict(r) for r, _ in pairs], "mode": cfg["mode"]}) as found:
        result = _retrieve(s, idx, res.analyzer, question, pairs, mode=cfg["mode"], dense=dense,
                           query_vector=qvec, reranker=res.reranker() if cfg["mode"] == "hybrid_rerank" else None,
                           rerank_depth=(cfg.get("reranker") or {}).get("depth"),
                           rerank_protect=(cfg.get("reranker") or {}).get("protect") or 0)
        result.query_embedding = qinfo
        from ..retrieval.retrieval import index_compatibility

        outdated = index_compatibility(idx, res.analyzer)
        if outdated:  # still served (search must keep working) but never silently: rebuild and re-evaluate
            result.limitations.append(f"index_outdated:{outdated}")
        if cfg.get("fallback_reason") == "activated_corpus_route_requires_rerun":
            result.limitations.append("activated_run_stale:corpus_route")
        wanted = {"dense"} | ({"reranker"} if cfg["mode"] == "hybrid_rerank" else set())
        if cfg["mode"] in DENSE_MODES:
            result.limitations += [f"{stage}_unavailable" for stage in res.stage_errors if stage in wanted]
        found.update(lambda: {"output": _ranked_chunks(idx, result), "metadata": {  # chunk texts only for a trace
            "index_version": result.index_version, "fallback": result.fallback,
            "limitations": result.limitations, "timings_ms": result.timings_ms, "candidates": result.candidates}})
    return result


def _ranked_chunks(idx: KeywordIndex, result: RetrievalResult) -> list[dict]:
    """Pre-pack ranking with each chunk's best score per channel and its indexed text."""
    scores: dict[str, dict] = {}
    for c in result.candidates:
        scores.setdefault(c["chunk_id"], {})[c["channel"]] = c["score"]
    packed = {e.chunk_id for e in result.evidence}
    return [{"rank": r + 1, "chunk_id": cid, "scores": scores.get(cid, {}), "packed": cid in packed,
             "text": idx.chunks[idx.row_of[cid]].get("payload") if cid in idx.row_of else None}
            for r, cid in enumerate(result.ranking)]


# ---------------------------------------------------------------- answers


def _config_snapshot(res: Resources, request: AnswerRequest) -> dict:
    idx = res.index()
    with open_db(res.settings.db_path) as conn:
        rate_version = conn.execute("SELECT rate_version FROM budget_settings WHERE id = 1").fetchone()[0]
    return {"config_id": request.config_id, "verifier_run_id": request.verifier_run_id or None,
            "index_version": idx.version if idx else None,
            "serving": {k: v for k, v in res.serving().items() if k in ("run_id", "mode", "dense_version")},
            "prompt_version": generation.PROMPT_VERSION, "model": res.generation_model(),
            "reasoning_effort": res.settings.generation_reasoning_effort,
            "rate_version": rate_version, "max_output_tokens": res.settings.generation_max_output_tokens,
            "settings": res.settings.fingerprint()}


def _input_hash(request: AnswerRequest) -> str:
    data = {"q": nfc(request.question), "scope": [asdict(s) for s in request.scope], "mode": request.mode,
            "as_of": request.as_of, "config": request.config_id, "run": request.verifier_run_id}
    if request.previous_request_id:  # absent otherwise, so a stored single-turn key keeps its hash
        data["previous"] = request.previous_request_id
    return hashlib.sha256(dumps(data).encode()).hexdigest()


BILLING_PRECEDENCE = ("unknown", "pending", "reconciled", "settled", "released")
PAID_MODES = ("single", "compare", "corpus")
FREE_MODES = ("metadata", "inventory")


class _Stop(Exception):
    """A checkpoint before a paid stage refused to continue (the request was cancelled)."""

    def __init__(self, status: str, request_status: str, summary: str) -> None:
        super().__init__(summary)
        self.status, self.request_status, self.summary = status, request_status, summary


def _billing(conn, request_id: str) -> tuple[list[dict], str]:
    attempts = [dict(a) for a in conn.execute(
        "SELECT attempt_id, stage, state, reserved_micro_usd, settled_micro_usd FROM attempts WHERE request_id = ? "
        "ORDER BY created_at", (request_id,))]
    states = {"reserved": "pending", "dispatching": "pending"}
    overall = {states.get(a["state"], a["state"]) for a in attempts}
    return attempts, next((b for b in BILLING_PRECEDENCE if b in overall), "none")


def _finish(res: Resources, request_id: str, result: AnswerResult, trace: dict, status: str) -> AnswerResult:
    """The request's attempts and overall billing state come from the ledger, so every paid stage (query embedding
    and generation) is reported on every exit path. Unknown wins over settled; open reservations show as pending.
    A request already marked interrupted (controlled shutdown) or cancelled keeps that state; its outcome is still
    stored for the history, never as the current screen's answer."""
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        attempts, billing = _billing(conn, request_id)
        result.attempt_ids = [a["attempt_id"] for a in attempts]
        result.billing_state = billing
        trace["billing"] = attempts
        row = conn.execute("SELECT status, cancel_requested FROM requests WHERE request_id = ?",
                           (request_id,)).fetchone()
        if row["status"] == "running":
            status = "cancelled" if row["cancel_requested"] and status == "completed" else status
        else:
            status = row["status"]
        conn.execute("UPDATE requests SET status = ?, trace_json = ?, result_json = ?, updated_at = ? "
                     "WHERE request_id = ?", (status, dumps(trace), dumps(asdict(result)), utcnow(), request_id))
    res.partials.pop(request_id, None)  # the stored outcome replaces what was streamed
    return result


def _evidence_map(evidence: list[EvidenceUnit]) -> dict[str, dict]:
    return {e.evidence_id: asdict(e) for e in evidence}


def prepare_answer(res: Resources, principal: Principal, question: str, scope: list[DocRef], as_of: str, *,
                   request_id: str | None = None, allow_paid: bool = False, mode: str | None = None,
                   limits: dict | None = None, all_documents: bool = False, conversation: list[dict] | None = None,
                   carried: list[EvidenceUnit] = ()) -> dict:
    """Retrieval plus exact prompt counting and the maximum reservation estimate. Free unless `allow_paid`
    lets a dense mode pay for an uncached query embedding. `mode`/`limits` come from a frozen verifier
    configuration. With `all_documents` the prompt names only the documents whose passages were retrieved.
    A follow-up passes its `conversation` and the previous answer's `carried` evidence (`_with_carried`)."""
    principal = _authorize(res, principal, "consultant", "verifier")
    if all_documents:
        retrieval = retrieve(res, principal, question, [], request_id=request_id, allow_paid=allow_paid, mode=mode,
                             limits=limits, all_documents=True)
        retrieval = _with_carried(res, retrieval, carried)
        cited = [e.doc_id for e in retrieval.evidence]
        order = list(dict.fromkeys(cited))
        docs = sorted(_doc_rows(res, order), key=lambda d: order.index(d["doc_id"]))
        prep = _priced(res, question, as_of, docs, retrieval, "corpus", conversation=conversation)
        prep["coverage"] = [{"doc_id": d, "evidence": cited.count(d)} for d in order]
        return prep
    docs = _resolve_scope(res, scope)
    retrieval = retrieve(res, principal, question, scope, request_id=request_id, allow_paid=allow_paid, mode=mode,
                         limits=limits)
    retrieval = _with_carried(res, retrieval, carried, {d["doc_id"] for d in docs})
    return _priced(res, question, as_of, docs, retrieval, "single", conversation=conversation)


def _with_carried(res: Resources, retrieval: RetrievalResult, carried: list[EvidenceUnit],
                  doc_ids: set[str] | None = None) -> RetrievalResult:
    """A follow-up's evidence: the fresh retrieval first, then the previous answer's cited units it lacks, renumbered
    after it, so restating that answer can cite every fact again. A unit whose chunk left the serving index is
    dropped: its quote could no longer be checked."""
    have = {e.chunk_id for e in retrieval.evidence}
    extra = [e for e in carried if e.chunk_id not in have and (doc_ids is None or e.doc_id in doc_ids)
             and _stored_quote(res, e) is not None]
    if not extra:
        return retrieval
    n = len(retrieval.evidence)
    extra = [dataclasses.replace(e, evidence_id=f"E{n + i + 1}") for i, e in enumerate(extra)]
    return dataclasses.replace(retrieval, evidence=[*retrieval.evidence, *extra],
                               evidence_tokens=retrieval.evidence_tokens + sum(e.token_count for e in extra))


def _priced(res: Resources, question: str, as_of: str, docs: list[dict], retrieval: RetrievalResult,
            mode: str, price_query_embedding: bool = True, conversation: list[dict] | None = None) -> dict:
    with tracing.step("assemble-evidence", input={"question": question, "as_of": as_of, "mode": mode,
                                                  "doc_ids": [d["doc_id"] for d in docs]}) as packed:
        messages = generation.build_messages(question, as_of, [_doc_brief(d) for d in docs], retrieval.evidence,
                                             retrieval.limitations, mode=mode, conversation=conversation)
        rf = generation.answer_json_schema()
        tokens = generation.count_request_tokens(messages, rf, res.settings.framing_margin_tokens)
        packed.update(lambda: {"output": {
            "evidence": [{"evidence_id": e.evidence_id, "doc_id": e.doc_id, "chunk_id": e.chunk_id,
                          "location": e.location, "token_count": e.token_count, "text": e.quote}
                         for e in retrieval.evidence],
            "evidence_tokens": retrieval.evidence_tokens, "prompt_input_tokens": tokens,
            "limitations": retrieval.limitations, "excluded": retrieval.excluded}})
    try:  # an unknown rate leaves the estimate empty; admission then refuses with `unknown_rate`
        est = budget.estimate(res.settings.db_path, res.generation_model(), tokens,
                              res.settings.generation_max_output_tokens)
        qe = retrieval.query_embedding or {}
        if price_query_embedding and qe.get("cache") == "miss" and not qe.get("attempt_id"):
            # a paid answer that retrieves again would embed the query first
            query_est = _embedding_estimate(res, question)
            if query_est is None:
                raise budget.BudgetError("unknown_rate")
            est += query_est
    except budget.BudgetError:
        est = None
    return {"retrieval": retrieval, "messages": messages, "response_format": rf, "input_tokens": tokens,
            "estimate_micro_usd": est, "docs": docs}


def _doc_brief(d: dict) -> dict:
    return {"doc_id": d["doc_id"], "title": d["effective"]["title"], "csv_institution": d["meta"]["institution"],
            "resolved_metadata": {f: r["value"] for f, r in d["resolutions"].items()},
            "source_format": d["format"], "review_status": d["review_status"],
            "metadata_conflicts": [c["field"] for c in d["quality"].get("provenance_conflicts", [])]}


def _validate_request(res: Resources, request: AnswerRequest) -> str:
    s = res.settings
    question = (request.question or "").strip()
    if request.mode not in PAID_MODES + FREE_MODES:
        raise ServiceError("지원하지 않는 질문 방식입니다.")
    if request.mode in PAID_MODES and (not question or len(question) > s.question_max_characters):
        raise ServiceError(f"질문은 1~{s.question_max_characters}자여야 합니다.")
    if len(question) > s.question_max_characters:
        raise ServiceError(f"질문은 {s.question_max_characters}자를 넘을 수 없습니다.")
    refs = [(r.doc_id, r.source_hash) for r in request.scope]
    if len(set(refs)) != len(refs):
        raise ServiceError("같은 문서를 두 번 선택했습니다.")
    if request.mode == "corpus" and refs:
        raise ServiceError("전체 문서 질문은 문서를 선택하지 않습니다.")
    if request.mode != "corpus" and not 1 <= len(refs) <= 2:
        raise ServiceError("문서를 한두 개 선택하거나 전체 문서로 질문하세요.")
    if request.mode == "compare" and len(refs) != 2:
        raise ServiceError("비교 질문은 문서를 정확히 두 개 선택해야 합니다.")
    if request.mode == "metadata" and not 1 <= len(refs) <= 2:
        raise ServiceError("기본 정보 조회는 문서를 한두 개 선택해야 합니다.")
    if not request.idempotency_key or len(request.idempotency_key) > 100 or len(request.generation_id) > 100:
        raise ServiceError("요청 식별자가 올바르지 않습니다.")
    _frozen_run_for(res, request, question)  # unknown, mismatched or outdated: refused before anything is queued
    return question


CONVERSATION_TURNS = 6  # earlier turns a follow-up is rewritten from, newest kept
CONVERSATION_ANSWER_CHARACTERS = 1200  # of each earlier answer: its sentences, so a restatement can keep them all
# ponytail: the latest answer gets more room, so its value tables and requirement rows (about 60 at 90 characters)
# can be referred to ("세 번째 요구사항"); a longer inventory is cut, and older turns keep 1200 characters
LATEST_ANSWER_CHARACTERS = 6000
INVENTORY_ROW_CHARACTERS = 80
CARRIED_EVIDENCE = 8  # of the previous answer's cited units, supplied again to the follow-up's answer
FREE_TURNS = {"metadata": "기본 정보 조회", "inventory": "요구사항 목록 조회"}


def _cited_ids(result: dict) -> list[str]:
    return list(dict.fromkeys([*(result.get("summary_evidence_ids") or []),
                               *(i for c in result.get("claims") or [] for i in c["evidence_ids"]),
                               *(i for c in result.get("conflicts") or [] for a in c["alternatives"]
                                 for i in a["evidence_ids"])]))


def inventory_display_order(items: list[dict]) -> list[dict]:
    """Requirement rows as the 요구사항 table shows them (`inventoryRows` in answer-parts.tsx): grouped by the
    prefix of their source form (SFR, PER, ...; 기타 when none), groups in first-seen order, source order within a
    group. "세 번째 요구사항" means the third row there."""
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(re.split(r"[-_]", item.get("source_form") or "")[0] or "기타", []).append(item)
    return [item for group in groups.values() for item in group]


def _answer_text(result: dict) -> str:
    """An earlier answer as the screen showed it, for the conversation context: the summary and sentences, then the
    metadata values, conflicting values, requirement rows in their displayed order, and what was not found. A
    follow-up may refer to any of these ("방금 나온 금액", "세 번째 요구사항")."""
    # Labels follow the screen (coverage order, as docLabel), then any other document the answer refers to; a
    # conflict names its documents only inside its alternatives
    docs = list(dict.fromkeys(x["doc_id"] for x in [
        *(result.get("coverage") or []), *(result.get("claims") or []), *(result.get("facts") or []),
        *(a for c in result.get("conflicts") or [] for a in c.get("alternatives") or []),
        *(result.get("missing_fields") or [])] if x.get("doc_id")))
    side = (lambda d: f"문서 {docs.index(d) + 1} ") if len(docs) > 1 else (lambda d: "")
    claim_side = side if result.get("mode") == "compare" else (lambda d: "")  # as the screen groups them
    lines = [result.get("summary") or "", *(claim_side(c["doc_id"]) + c["text"] for c in result.get("claims") or [])]
    def shown(v) -> str:  # dates are stored as {precision, value}
        return "-" if v is None else str(v["value"] if isinstance(v, dict) and "value" in v else v)

    def others(alts) -> str:  # metadata conflicts: the competing CSV values by notice row
        return " / ".join(dict.fromkeys(shown(v) for v in (alts.values() if isinstance(alts, dict) else alts)))

    lines += [f"{side(f['doc_id'])}{f['field']}: {shown(f['value'])}"
              + ("" if f["state"] in ("known", "resolved") else f" ({f['state']})")
              + (f" 다른 값: {others(f['alternatives'])}" if f.get("alternatives") else "")
              for f in result.get("facts") or []]
    lines += [f"{c['field']}: " + " / ".join(f"{side(a['doc_id'])}{a['value']}" for a in c["alternatives"])
              for c in result.get("conflicts") or []]
    lines += [f"{n}. {i.get('source_form') or i['code']} {i.get('name') or ''} {' '.join(i['text'].split())}"
              .strip()[:INVENTORY_ROW_CHARACTERS]
              for n, i in enumerate(inventory_display_order((result.get("inventory") or {}).get("items") or []), 1)]
    lines += [f"{side(m['doc_id'])}{m['field']}: 확인되지 않음({m['reason']})" for m in result.get("missing_fields") or []]
    return "\n".join(x for x in lines if x).strip()


def _conversation(res: Resources, principal: Principal,
                  request: AnswerRequest) -> tuple[list[dict], list[EvidenceUnit]]:
    """The earlier turns of the conversation `request` continues, oldest first, as {question, answer}, and the
    evidence units the previous answer cited. Every earlier turn must be this member's, finished and asked of the
    same scope; otherwise the request is refused before it is queued. A rewritten turn contributes its standalone
    question, so references stay resolved."""
    if request.previous_request_id and request.verifier_run_id:
        raise ServiceError("검증 실행에서 만드는 답변은 대화를 이어 갈 수 없습니다.")
    scope = [asdict(r) for r in request.scope]
    turns, carried, previous = [], [], request.previous_request_id
    with open_db(res.settings.db_path) as conn:
        while previous and len(turns) < CONVERSATION_TURNS:
            row = conn.execute("SELECT member_id, status, scope_json, request_json, result_json FROM requests "
                               "WHERE request_id = ?", (previous,)).fetchone()
            if row is None or row["member_id"] != principal.member_id or not row["request_json"]:
                raise ServiceError("이어서 질문할 이전 대화를 찾을 수 없습니다.")
            if row["status"] not in TERMINAL_STATUSES:
                raise ServiceError("이전 질문의 답변이 끝난 뒤에 이어서 질문할 수 있습니다.")
            if json.loads(row["scope_json"]) != scope:
                raise ServiceError("대화 중에는 질문 범위를 바꿀 수 없습니다. 새 대화로 질문하세요.")
            snap, result = json.loads(row["request_json"]), json.loads(row["result_json"] or "{}")
            if not turns:
                units = result.get("evidence") or {}
                carried = [EvidenceUnit(**units[i]) for i in _cited_ids(result) if i in units][:CARRIED_EVIDENCE]
            limit = LATEST_ANSWER_CHARACTERS if not turns else CONVERSATION_ANSWER_CHARACTERS
            turns.append({"question": result.get("standalone_question") or snap.get("question")
                          or FREE_TURNS.get(snap.get("mode"), ""),
                          "answer": _answer_text(result)[:limit] or "(답변 없음)"})
            previous = snap.get("previous_request_id", "")
    return turns[::-1], carried


def _request_snapshot(principal: Principal, request: AnswerRequest, question: str) -> dict:
    return {"question": question, "scope": [asdict(r) for r in request.scope], "mode": request.mode,
            "as_of": request.as_of, "config_id": request.config_id, "verifier_run_id": request.verifier_run_id,
            "generation_id": request.generation_id, "previous_request_id": request.previous_request_id,
            "idempotency_key": request.idempotency_key, "capabilities": sorted(principal.capabilities)}


def _create(res: Resources, principal: Principal, request: AnswerRequest, question: str, status: str,
            before_insert=None) -> tuple[str, Row | None]:
    """Persists the input snapshot under its idempotency key in one short transaction. Returns (request_id, prior
    row) where a prior row means this key already exists with the same input."""
    input_hash = _input_hash(request)
    config = _config_snapshot(res, request)
    request_id = str(uuid.uuid4())
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        prior = conn.execute("SELECT * FROM requests WHERE member_id = ? AND idempotency_key = ?",
                             (principal.member_id, request.idempotency_key)).fetchone()
        if prior is not None:
            if prior["input_hash"] != input_hash:
                raise ServiceError("같은 요청 키가 다른 입력에 재사용되었습니다.")
            return prior["request_id"], prior
        if before_insert is not None:
            before_insert()
        conn.execute(
            "INSERT INTO requests(request_id, member_id, idempotency_key, generation_id, input_hash, config_hash, "
            "scope_json, status, trace_json, created_at, updated_at, request_json, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, principal.member_id, request.idempotency_key, request.generation_id, input_hash,
             hashlib.sha256(dumps(config).encode()).hexdigest(), dumps([asdict(x) for x in request.scope]), status,
             dumps({"config": config}), utcnow(), utcnow(), dumps(_request_snapshot(principal, request, question)),
             request.mode))
    return request_id, None


def answer(res: Resources, principal: Principal, request: AnswerRequest) -> AnswerResult:
    """Synchronous worker entry (CLI, tests): the same execution as a background request, on this thread."""
    principal = _authorize(res, principal, "consultant", "verifier")
    question = _validate_request(res, request)
    _conversation(res, principal, request)
    request_id, prior = _create(res, principal, request, question, "running")
    if prior is not None:
        if prior["result_json"]:
            return AnswerResult(**json.loads(prior["result_json"]))
        return AnswerResult(prior["request_id"], "in_progress", "이미 처리 중인 요청입니다.",
                            generation_id=request.generation_id, mode=request.mode)
    return _execute(res, principal, request_id, request, question)


def submit_answer(res: Resources, principal: Principal, request: AnswerRequest) -> str:
    """Persists the request, then runs it on the bounded background executor; free modes complete inline.
    The same key returns the same request across reruns; a full queue is rejected before any paid work."""
    principal = _authorize(res, principal, "consultant", "verifier")
    question = _validate_request(res, request)
    _conversation(res, principal, request)
    if request.mode in FREE_MODES:
        request_id, prior = _create(res, principal, request, question, "running")
        if prior is None:
            _execute(res, principal, request_id, request, question)
        return request_id
    runner = res.runner()
    admitted = []

    def admit() -> None:  # only for a new key: a duplicate never needs (or consumes) a slot
        if not runner.try_admit():
            raise ServiceError("요청이 많아 지금은 접수할 수 없습니다. 잠시 후 다시 시도하세요. (유료 호출 없음)")
        admitted.append(True)

    request_id, prior = _create(res, principal, request, question, "queued", before_insert=admit)
    if prior is not None:
        return request_id
    try:
        runner.submit(request_id)
    except Exception as exc:  # noqa: BLE001 - nothing was reserved: mark the request failed and free the slot
        runner.release()
        _fail_unscheduled(res, request_id, request, f"{type(exc).__name__}: {exc}"[:300])
        raise ServiceError("요청을 실행 대기열에 넣지 못했습니다. (유료 호출 없음)") from None
    return request_id


def _fail_unscheduled(res: Resources, request_id: str, request: AnswerRequest, error: str) -> None:
    result = AnswerResult(request_id, "technical_error", "요청을 실행하지 못했습니다.", error=error,
                          generation_id=request.generation_id, mode=request.mode)
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("UPDATE requests SET status = 'failed', result_json = ?, updated_at = ? WHERE request_id = ? "
                     "AND status = 'queued'", (dumps(asdict(result)), utcnow(), request_id))


def run_queued(res: Resources, request_id: str) -> None:
    """Worker body: claims queued -> running exactly once, rebuilds the principal from the persisted snapshot
    and executes. Never touches UI state."""
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        claimed = conn.execute("UPDATE requests SET status = 'running', updated_at = ? WHERE request_id = ? "
                               "AND status = 'queued' AND cancel_requested = 0", (utcnow(), request_id)).rowcount
        row = conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
    if not claimed:
        return
    snap = json.loads(row["request_json"])
    request = AnswerRequest(idempotency_key=snap["idempotency_key"], generation_id=snap["generation_id"],
                            question=snap["question"], scope=[DocRef(**r) for r in snap["scope"]],
                            mode=snap["mode"], as_of=snap["as_of"], config_id=snap["config_id"],
                            verifier_run_id=snap.get("verifier_run_id", ""),
                            previous_request_id=snap.get("previous_request_id", ""))
    principal = Principal(row["member_id"], frozenset(snap["capabilities"]))
    _execute(res, principal, request_id, request, snap["question"])


STOPS = {"cancelled": "요청이 취소되어 다음 유료 단계를 시작하지 않았습니다.",
         "interrupted": "서비스 종료로 요청이 중단되어 다음 유료 단계를 시작하지 않았습니다. (유료 호출 없음)"}


def _stop_reason(res: Resources, conn, request_id: str) -> str | None:
    """Why this request may not start a new paid stage: cancelled, or interrupted (marked by a controlled
    shutdown, no longer running, or the owning resources are closing). None when it may."""
    row = conn.execute("SELECT status, cancel_requested FROM requests WHERE request_id = ?",
                       (request_id,)).fetchone()
    if row is None:
        return None  # not a persisted request (e.g. a standalone retrieval): nothing to stop
    if row["cancel_requested"] or row["status"] == "cancelled":
        return "cancelled"
    if res._closed or row["status"] != "running":
        return "interrupted"
    return None


def _dispatch_guard(res: Resources, request_id: str | None):
    """The check `budget.mark_dispatching` runs in the dispatch transaction itself."""
    return None if request_id is None else (lambda conn: _stop_reason(res, conn, request_id))


def _checkpoint(res: Resources, request_id: str, principal: Principal) -> Principal:
    """Before every new paid stage: a cancelled or interrupted request (or a closing service) stops here.
    Already dispatched work still settles; this only prevents the next dispatch. The dispatch itself repeats
    the check atomically (`_dispatch_guard`)."""
    with open_db(res.settings.db_path) as conn:
        reason = _stop_reason(res, conn, request_id)
    if reason:
        raise _Stop(reason, reason, STOPS[reason])
    return principal


def _execute(res: Resources, principal: Principal, request_id: str, request: AnswerRequest,
             question: str) -> AnswerResult:
    """One Langfuse trace per executed request (none when tracing is off); scores come from existing checks."""
    scope = [asdict(s) for s in request.scope]
    with tracing.run(res.tracing, "answer-question", seed=request_id, user_id=principal.member_id,
                     input={"question": question, "scope": scope, "mode": request.mode, "as_of": request.as_of},
                     metadata={"request_id": request_id, "member_id": principal.member_id, "as_of": request.as_of,
                               "mode": request.mode, "scope": dumps(scope)[:200],
                               "previous_request_id": request.previous_request_id},
                     tags=["ask", request.mode]) as root:
        result = _execute_traced(res, principal, request_id, request, question, root)
    return result


def _execute_traced(res: Resources, principal: Principal, request_id: str, request: AnswerRequest, question: str,
                    root: tracing.Step) -> AnswerResult:
    trace: dict = {"config": _config_snapshot(res, request), "question": question, "as_of": request.as_of,
                   "mode": request.mode}

    def done(status: str, summary: str, request_status: str = "completed", **kw) -> AnswerResult:
        result = AnswerResult(request_id, status, summary, generation_id=request.generation_id, mode=request.mode,
                              **kw)
        result = _finish(res, request_id, result, trace, request_status)
        _trace_outcome(root, result, trace)
        return result

    try:
        if request.mode == "metadata":
            return _metadata_answer(res, request, done)
        if request.mode == "inventory":
            return _inventory_answer(res, request, done)
        return _paid_answer(res, principal, request_id, request, question, trace, done)
    except _Stop as stop:
        return done(stop.status, stop.summary, request_status=stop.request_status)
    except ServiceError as exc:
        return done("clarification_required", str(exc), request_status="failed")
    except Exception as exc:  # noqa: BLE001 - any failure ends the request instead of leaving it running
        return done("technical_error", "요청을 처리하지 못했습니다.", request_status="failed",
                    error=f"{type(exc).__name__}: {exc}"[:300])


CITATION_ERRORS = ("unknown_evidence_id", "evidence_scope_mismatch", "evidence_quote_mismatch",
                   "claim_without_evidence", "conflict_without_evidence", "summary_without_evidence")


def _trace_outcome(root: tracing.Step, result: AnswerResult, trace: dict) -> None:
    """The answer as the trace output, and the free deterministic scores known for every exit path."""
    root.update(lambda: {"output": {k: getattr(result, k) for k in (
        "status", "standalone_question", "summary", "summary_evidence_ids", "claims", "missing_fields", "conflicts",
        "next_action", "billing_state", "error")},
        "level": "ERROR" if result.status == "technical_error" else "DEFAULT"})
    root.score_trace("insufficient_evidence", 1.0 if result.status == "insufficient_evidence" else 0.0, "BOOLEAN")
    if "retrieval" in trace:
        root.score_trace("evidence_tokens", lambda: float(trace["retrieval"]["evidence_tokens"]), "NUMERIC")


def _paid_answer(res: Resources, principal: Principal, request_id: str, request: AnswerRequest, question: str,
                 trace: dict, done) -> AnswerResult:
    docs = [] if request.mode == "corpus" else _resolve_scope(res, request.scope)
    unavailable = [d for d in docs if d["parse_status"] != "parsed" or not _indexed(res, d["active_extraction_id"])]
    missing_unavailable = [{"doc_id": d["doc_id"], "field": "document", "reason": "ingestion_unavailable"}
                           for d in unavailable]
    if docs and len(unavailable) == len(docs):
        reason = QUARANTINE_TEXT.get(unavailable[0]["reason_code"] or "",
                                     "이 문서는 아직 검색 색인에 포함되지 않았습니다.")
        return done("ingestion_unavailable", f"원문 전체를 확인할 수 없어 답변하지 않습니다. {reason}",
                    missing_fields=missing_unavailable)
    # A frozen verifier run is generated from exactly the evidence it shows: no new retrieval, so no query
    # embedding is paid and a cache miss cannot switch it to another mode (refused when serving changed since).
    frozen = _frozen_run_for(res, request, question)
    principal = _checkpoint(res, request_id, principal)  # the rewrite or the query embedding may be the first paid stage
    history, carried = ([], []) if frozen is not None else _conversation(res, principal, request)
    if history:  # a follow-up: retrieval and the answer use the standalone question rewritten from the conversation
        question = _rewrite(res, principal, request_id, question, history, docs, trace, done)
        if isinstance(question, AnswerResult):
            return question
        done = functools.partial(done, standalone_question=question)
        principal = _checkpoint(res, request_id, principal)
    try:
        prep, docs = _prepare_paid(res, principal, request_id, request, question, docs, frozen, history, carried)
    except _Stop:
        raise
    except Exception as exc:  # noqa: BLE001
        return done("technical_error", "검색 또는 비용 추정에 실패했습니다.", request_status="failed",
                    error=f"{type(exc).__name__}: {exc}"[:300])
    if _above_consented(prep, frozen):
        return done("clarification_required", "검증 실행 이후 프롬프트가 바뀌어 동의한 최대 비용을 넘을 수 있습니다. "
                    "새 검증 실행을 만든 뒤 다시 생성하세요. (유료 호출 없음)", request_status="failed")
    retrieval: RetrievalResult = prep["retrieval"]
    _trace_retrieval(trace, prep, carried)
    coverage = prep.get("coverage") or [{"doc_id": d["doc_id"], "evidence": len(retrieval.evidence)} for d in docs]
    server_missing = missing_unavailable + _not_found(coverage, unavailable)
    evidence_map = _evidence_map(retrieval.evidence)
    common = {"coverage": coverage, "limitations": retrieval.limitations}
    if not retrieval.evidence:
        where = "전체 문서" if request.mode == "corpus" else "선택한 문서"
        return done("insufficient_evidence", f"{where}에서 질문과 관련된 근거를 찾지 못했습니다.",
                    missing_fields=server_missing, **common)
    principal = _checkpoint(res, request_id, principal)
    response = _generate(res, principal, request_id, prep, trace, frozen,
                         fail=functools.partial(done, evidence=evidence_map, **common))
    if isinstance(response, AnswerResult):
        return response
    represented = {c["doc_id"] for c in coverage if c.get("evidence")}
    try:
        payload = _validated(res, response, retrieval, docs, represented if request.mode == "compare" else None)
    except generation.TechnicalError as exc:
        trace["raw_output"] = (response.content or "")[:4000]
        return done("technical_error", "모델 응답을 검증하지 못했습니다. 비용은 기록되었으며 자동 재시도는 하지 않습니다.",
                    request_status="failed", evidence=evidence_map, error=str(exc)[:300], **common)
    return done(payload.status, payload.summary, claims=[c.model_dump() for c in payload.claims],
                missing_fields=_merged_missing(payload, server_missing),
                conflicts=[c.model_dump() for c in payload.conflicts], next_action=payload.next_action,
                evidence=evidence_map, summary_evidence_ids=payload.summary_evidence_ids, **common)


def _prepare_paid(res: Resources, principal: Principal, request_id: str, request: AnswerRequest, question: str,
                  docs: list[dict], frozen: dict | None, history: list[dict], carried: list) -> tuple[dict, list[dict]]:
    """The prompt and its estimate for the request's mode; a corpus question returns the documents it searched."""
    if frozen is not None:
        return _frozen_prep(res, question, request, docs, frozen), docs
    if request.mode == "compare":
        return _prepare_compare(res, principal, question, docs, request.as_of, request_id,
                                conversation=history, carried=carried), docs
    if request.mode == "corpus":
        prep = prepare_answer(res, principal, question, [], request.as_of, request_id=request_id,
                              allow_paid=True, all_documents=True, conversation=history, carried=carried)
        return prep, prep["docs"]
    return prepare_answer(res, principal, question, request.scope, request.as_of, request_id=request_id,
                          allow_paid=True, conversation=history, carried=carried), docs


def _above_consented(prep: dict, frozen: dict | None) -> bool:
    """A frozen run's prompt may now cost more than the estimate consented to, e.g. a metadata resolution recorded
    since lengthened it: the consented maximum no longer holds."""
    return frozen is not None and (prep["estimate_micro_usd"] is None or frozen["estimate_micro_usd"] is None
                                   or prep["estimate_micro_usd"] > frozen["estimate_micro_usd"])


def _not_found(coverage: list[dict], unavailable: list[dict]) -> list[dict]:
    """Each available document the retrieval found no evidence in."""
    return [{"doc_id": c["doc_id"], "field": "answer", "reason": "not_found_in_context"}
            for c in coverage if c.get("evidence") == 0 and c["doc_id"] not in {d["doc_id"] for d in unavailable}]


def _trace_retrieval(trace: dict, prep: dict, carried: list) -> None:
    retrieval: RetrievalResult = prep["retrieval"]
    trace["retrieval"] = asdict(retrieval)
    if carried:
        chunks = {e.chunk_id for e in carried}
        trace["carried_evidence_ids"] = [e.evidence_id for e in retrieval.evidence if e.chunk_id in chunks]
    trace["input_tokens_estimate"] = prep["input_tokens"]


def _generate(res: Resources, principal: Principal, request_id: str, prep: dict, trace: dict, frozen: dict | None,
              fail):
    """The answer call, streamed into `res.partials`; a frozen run may not reserve above its consented estimate."""

    def streamed(content: str) -> None:  # read by `answer_progress` until `_finish` stores the validated outcome
        res.partials[request_id] = content

    return _metered_chat(
        res, principal, request_id, stage="generation", step="generate-answer",
        prompt_version=generation.PROMPT_VERSION, messages=prep["messages"],
        response_format=prep["response_format"], input_tokens=prep["input_tokens"],
        max_output_tokens=res.settings.generation_max_output_tokens, record=trace, fail=fail,
        ceiling=frozen["estimate_micro_usd"] if frozen is not None else None, on_delta=streamed)


def _validated(res: Resources, response, retrieval: RetrievalResult, docs: list[dict], required_doc_ids):
    """The answer checked against the evidence it may cite, traced as the guardrail step; raises TechnicalError."""
    stored = {e.evidence_id: _stored_quote(res, e) for e in retrieval.evidence}
    with tracing.step("validate-answer", "guardrail", input={
            "allowed_evidence_ids": [e.evidence_id for e in retrieval.evidence],
            "allowed_doc_ids": sorted(d["doc_id"] for d in docs)}) as check:
        try:
            payload = generation.validate_answer(response, retrieval.evidence, {d["doc_id"] for d in docs},
                                                 stored, required_doc_ids=required_doc_ids)
        except generation.TechnicalError as exc:
            check.update(output={"passed": False, "error": str(exc)[:300]}, level="ERROR",
                         status_message=str(exc)[:300])
            if str(exc).startswith(CITATION_ERRORS):
                check.score_trace("citation_valid", 0.0, "BOOLEAN")
            raise
        check.update(lambda: {"output": {"passed": True, "status": payload.status, "cited_evidence_ids": sorted(
            {i for c in payload.claims for i in c.evidence_ids} | set(payload.summary_evidence_ids))}})
        check.score_trace("citation_valid", 1.0, "BOOLEAN")
    return payload


def _merged_missing(payload, server_missing: list[dict]) -> list[dict]:
    """The model's missing fields, then the server's for any document and field the model did not name."""
    missing = [m.model_dump() for m in payload.missing_fields]
    seen = {(m["doc_id"], m["field"]) for m in missing}
    return missing + [m for m in server_missing if (m["doc_id"], m["field"]) not in seen]


REWRITE_MAX_OUTPUT_TOKENS = 1500  # reasoning included; the output is one question


def _rewrite(res: Resources, principal: Principal, request_id: str, question: str, history: list[dict],
             docs: list[dict], trace: dict, done) -> str | AnswerResult:
    """The follow-up as one standalone question, from the conversation, through the gateway. Returns the question,
    or the request's outcome when the call was refused or its output unusable (billed, never retried)."""
    messages = generation.build_rewrite_messages(history, question, [d["effective"]["title"] for d in docs])
    rf = generation.rewrite_json_schema()
    record = trace["rewrite"] = {"asked": question, "conversation_turns": len(history),
                                 "prompt_version": generation.REWRITE_PROMPT_VERSION}
    response = _metered_chat(
        res, principal, request_id, stage="query_rewrite", step="rewrite-question",
        prompt_version=generation.REWRITE_PROMPT_VERSION, messages=messages, response_format=rf,
        input_tokens=generation.count_request_tokens(messages, rf, res.settings.framing_margin_tokens),
        max_output_tokens=REWRITE_MAX_OUTPUT_TOKENS, record=record, fail=done)
    if isinstance(response, AnswerResult):
        return response
    try:
        record["query"] = generation.validate_rewrite(response, res.settings.question_max_characters)
    except generation.TechnicalError as exc:
        record["raw_output"] = (response.content or "")[:2000]
        return done("technical_error", "이어진 질문을 검색용 질문으로 바꾸지 못했습니다. 비용은 기록되었으며 자동 재시도는 "
                    "하지 않습니다.", request_status="failed", error=str(exc)[:300])
    return record["query"]


def _metered_chat(res: Resources, principal: Principal, request_id: str, *, stage: str, step: str,
                  prompt_version: str, messages: list[dict], response_format: dict, input_tokens: int,
                  max_output_tokens: int, record: dict, fail, ceiling: int | None = None, on_delta=None):
    """One paid chat call of a request through the gateway: reserve its maximum, mark it dispatching under the
    request's stop check, call, then settle, release (confirmed pre-execution) or keep it unknown. Returns the
    provider response, or `fail(...)`'s outcome when the call was refused or failed; never retries. `record`
    receives the admission, usage and settlement."""
    s = res.settings
    model = res.generation_model()
    admission = budget.reserve(s.db_path, request_id=request_id, member_id=principal.member_id, stage=stage,
                               purpose=res.paid_purpose, model=model, input_tokens=input_tokens,
                               max_output_tokens=max_output_tokens, count_method=generation.COUNT_METHOD,
                               ceiling_micro_usd=ceiling)
    record["admission"] = admission
    if admission["reason"] == "above_consented_maximum":
        return fail("clarification_required", "요금 설정이 바뀌어 예약 금액이 검증 실행에서 동의한 최대 비용을 넘습니다. "
                    "새 검증 실행을 만든 뒤 다시 생성하세요. (유료 호출 없음)", request_status="failed",
                    error=admission["reason"])
    if not admission["admitted"]:
        return fail("budget_blocked", "공유 사용 한도 또는 유료 호출 설정 때문에 답변 생성을 시작하지 않았습니다. "
                    "검색과 원문 열람은 계속 사용할 수 있습니다.", error=admission["reason"])
    attempt_id = admission["attempt_id"]
    if refusal := res.paid_refusal():
        budget.release(s.db_path, attempt_id, "provider_unavailable")
        return fail("technical_error", "유료 모델 연결이 설정되지 않았습니다.", request_status="failed",
                    error=refusal)
    try:  # the stop check and the dispatching marker are one transaction: no shutdown can slip between them
        budget.mark_dispatching(s.db_path, attempt_id, _dispatch_guard(res, request_id))
    except budget.DispatchRefused as refused:
        reason = str(refused)
        if reason in STOPS:
            raise _Stop(reason, reason, STOPS[reason]) from None
        # the ledger refused (unresolved unknown billing, maintenance, lost ownership): released, nothing sent
        return fail("budget_blocked", "결제 기록 점검 중이어서 유료 호출을 시작하지 않았습니다. 검색과 원문 열람은 계속 "
                    "사용할 수 있습니다.", error=reason[:300])
    return _call_and_settle(res, model, attempt_id, step=step, prompt_version=prompt_version, messages=messages,
                            response_format=response_format, max_output_tokens=max_output_tokens, record=record,
                            fail=fail, on_delta=on_delta)


def _call_and_settle(res: Resources, model: str, attempt_id: str, *, step: str, prompt_version: str,
                     messages: list[dict], response_format: dict, max_output_tokens: int, record: dict, fail,
                     on_delta):
    """The dispatched call, traced: settle its usage, release it when it provably never ran, else keep it unknown."""
    s = res.settings
    with tracing.step(step, "generation", model=model, input=messages,
                      model_parameters={"reasoning_effort": s.generation_reasoning_effort,
                                        "max_completion_tokens": max_output_tokens},
                      metadata={"attempt_id": attempt_id, "prompt_version": prompt_version}) as gen:
        try:
            response = res.transport.chat(model=model, messages=messages,
                                          response_format=response_format, max_completion_tokens=max_output_tokens,
                                          reasoning_effort=s.generation_reasoning_effort, on_delta=on_delta)
        except generation.ProviderError as exc:
            gen.update(level="ERROR", status_message=str(exc)[:300])
            if exc.pre_execution:
                budget.release(s.db_path, attempt_id, str(exc)[:300], confirmed_pre_execution=True)
            else:
                budget.mark_unknown(s.db_path, attempt_id, str(exc))
            return fail("technical_error", "모델 호출에 실패했습니다. 자동으로 다시 시도하지 않습니다.",
                        request_status="failed", error=str(exc)[:300])
        except Exception as exc:  # noqa: BLE001 - e.g. a transport closed by shutdown: execution may have happened
            gen.update(level="ERROR", status_message=type(exc).__name__)
            budget.mark_unknown(s.db_path, attempt_id, f"{type(exc).__name__}: {exc}")
            return fail("technical_error", "모델 호출 중 연결이 끊겼습니다. 비용은 확인 전까지 보류로 남습니다.",
                        request_status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
        gen.update(lambda: {"output": tracing.readable(response.content), "metadata": {
            "response_id": response.response_id, "finish_reason": response.finish_reason,
            "refusal": response.refusal, "reasoning_tokens": (response.usage or {}).get("reasoning_tokens")}})
        if response.usage is None:
            budget.mark_unknown(s.db_path, attempt_id, "provider returned no usage")
        else:
            record["usage"] = response.usage
            try:
                record["settlement"] = budget.settle(s.db_path, attempt_id, response.usage, response.response_id)
            except Exception as exc:  # noqa: BLE001 - e.g. a reused response ID or a locked ledger
                # The call was billed but could not be recorded: keep it conservatively pending for the recovery view.
                budget.mark_unknown(s.db_path, attempt_id, f"settlement_failed: {type(exc).__name__}: {exc}")
                return fail("technical_error", "사용량을 기록하지 못했습니다. 비용은 확인 전까지 보류로 남습니다.",
                            request_status="failed", error=f"settlement_failed: {type(exc).__name__}"[:300])
            gen.update(lambda: tracing.usage_and_cost(response.usage, record["settlement"]))
    return response


# ---------------------------------------------------------------- balanced two-document comparison


def _prepare_compare(res: Resources, principal: Principal, question: str, docs: list[dict], as_of: str,
                     request_id: str | None, allow_paid: bool = True, mode: str | None = None,
                     limits: dict | None = None, price_query_embedding: bool = True,
                     conversation: list[dict] | None = None, carried: list[EvidenceUnit] = ()) -> dict:
    """One scoped subquery per selected document (the same question; no paid rewriting), each with the limits a
    single-document question gets, exactly as `evaluation._execute` measures a comparison row. Halving them per side
    served less than the retrieval gate measured (refresh50-ad-migration-design packed 4 of 13 groups while the gate
    recorded it complete). Each side reports evidence or its limitation. `mode` and narrowing `limits` (a verifier
    configuration) apply to both sides."""
    sides: dict[str, RetrievalResult | None] = {}
    for d in docs:
        if d["parse_status"] != "parsed" or not _indexed(res, d["active_extraction_id"]):
            sides[d["doc_id"]] = None
            continue
        sides[d["doc_id"]] = retrieve(res, principal, question, [DocRef(d["doc_id"], d["active_source_hash"])],
                                      request_id=request_id, allow_paid=allow_paid, limits=limits, mode=mode)
    evidence: list[EvidenceUnit] = []
    coverage, limitations, candidates, excluded, ranking = [], [], [], [], []
    timings: dict[str, float] = {}
    query_embedding = None
    for d in docs:
        r = sides[d["doc_id"]]
        if r is None:
            coverage.append({"doc_id": d["doc_id"], "evidence": 0, "limitation": "ingestion_unavailable"})
            continue
        for e in r.evidence:
            evidence.append(dataclasses.replace(e, evidence_id=f"E{len(evidence) + 1}"))
        coverage.append({"doc_id": d["doc_id"], "evidence": len(r.evidence), "tokens": r.evidence_tokens,
                         "limitation": None if r.evidence else "not_found_in_context"})
        limitations += [f"{d['doc_id'][:8]}:{x}" for x in r.limitations if not x.startswith(("idf:", "expansion:"))]
        candidates += [{**c, "doc_id": d["doc_id"]} for c in r.candidates]
        excluded += [{**x, "doc_id": d["doc_id"]} for x in r.excluded]
        ranking += r.ranking
        for k, v in r.timings_ms.items():
            timings[k] = round(timings.get(k, 0) + v, 1)
        query_embedding = query_embedding or r.query_embedding
    total = sum(e.token_count for e in evidence)
    first_r = next((r for r in sides.values() if r), None)
    merged = RetrievalResult(
        mode=first_r.mode if first_r else (mode or res.serving()["mode"]), scope=[DocRef(d["doc_id"], d["active_source_hash"])
                                                                        for d in docs],
        exact_matches=[c for c in candidates if c["channel"] == "exact"], candidates=candidates, evidence=evidence,
        excluded=excluded, limitations=["compare:balanced_per_document"] + limitations, evidence_tokens=total,
        timings_ms=timings, trace_id=str(uuid.uuid4()), index_version=first_r.index_version if first_r else None,
        ranking=ranking, fallback=first_r.fallback if first_r else None,
        dense_version=first_r.dense_version if first_r else None, query_embedding=query_embedding)
    merged = _with_carried(res, merged, carried, {d["doc_id"] for d in docs})
    for c in coverage:  # carried units count for their side
        c["evidence"] = sum(e.doc_id == c["doc_id"] for e in merged.evidence)
    prep = _priced(res, question, as_of, docs, merged, "compare", price_query_embedding, conversation=conversation)
    prep["coverage"] = coverage
    return prep


# ---------------------------------------------------------------- free deterministic modes

METADATA_FIELDS = ("title", "institution", "notice", "revision", "amount_krw", "published_at", "bid_start",
                   "bid_close")


def metadata_facts(d: dict) -> list[dict]:
    """Typed CSV values with provenance. A recorded resolution replaces a conflicting value (with its evidence);
    an unresolved conflict and an unknown value keep their state instead of a guess."""
    conflicts = {c["field"]: c["values"] for c in d["quality"].get("provenance_conflicts", [])}
    facts = []
    for f in METADATA_FIELDS:
        value, res_ = d["meta"].get(f), d["resolutions"].get(f)
        if res_:
            facts.append({"doc_id": d["doc_id"], "field": f, "value": res_["value"], "state": "resolved",
                          "provenance": "resolution", "evidence": res_["evidence"], "csv_value": value})
        elif f in conflicts:
            facts.append({"doc_id": d["doc_id"], "field": f, "value": value, "state": "conflict",
                          "provenance": "csv", "alternatives": conflicts[f]})
        else:
            state = "unknown" if value is None else ("zero_review" if f == "amount_krw" and value == 0 else "known")
            facts.append({"doc_id": d["doc_id"], "field": f, "value": value, "state": state, "provenance": "csv"})
    return facts


def _metadata_answer(res: Resources, request: AnswerRequest, done) -> AnswerResult:
    docs = _resolve_scope(res, request.scope)
    facts = [f for d in docs for f in metadata_facts(d)]
    missing = [{"doc_id": f["doc_id"], "field": f["field"], "reason": "unknown_metadata"}
               for f in facts if f["state"] == "unknown"]
    conflicts = [f for f in facts if f["state"] == "conflict"]
    status = "conflicting_evidence" if conflicts else "answered"
    return done(status, "CSV 기본 정보입니다(유료 호출 없음). 원문과 다를 수 있으니 중요한 값은 원문으로 확인하세요.",
                facts=facts, missing_fields=missing,
                coverage=[{"doc_id": d["doc_id"], "source": "csv_metadata"} for d in docs])


def _inventory_answer(res: Resources, request: AnswerRequest, done) -> AnswerResult:
    if len(request.scope) != 1:
        raise ServiceError("요구사항 목록은 문서를 하나 선택해야 합니다.")
    (doc,) = _resolve_scope(res, request.scope)
    idx = res.index()
    if doc["parse_status"] != "parsed" or idx is None or doc["active_extraction_id"] not in idx.rows_by_extraction:
        return done("ingestion_unavailable", "원문을 수집하지 못해 요구사항 목록을 만들 수 없습니다.",
                    missing_fields=[{"doc_id": doc["doc_id"], "field": "requirements",
                                     "reason": "ingestion_unavailable"}])
    inv = requirement_inventory(res, doc)
    evidence = {}
    for i, item in enumerate(inv["items"], 1):
        eid = f"R{i}"
        item["evidence_id"] = eid
        evidence[eid] = {"evidence_id": eid, "doc_id": doc["doc_id"], "source_hash": doc["active_source_hash"],
                         "extraction_id": doc["active_extraction_id"], "chunk_id": "",
                         "element_ids": [item["element_id"]], "quote": item["text"], "location": item["location"],
                         "token_count": 0}
    summary = (f"구조화된 요구사항 {inv['counts']['codes']}개 코드(상세 {inv['counts']['detail']} · 요약 "
               f"{inv['counts']['summary']})를 원문 순서로 나열했습니다. {inv['completeness_text']}")
    return done("answered" if inv["items"] else "insufficient_evidence", summary, inventory=inv, evidence=evidence,
                coverage=[{"doc_id": doc["doc_id"], "source": "requirement_inventory"}])


def requirement_inventory(res: Resources, doc: dict) -> dict:
    """Every structured requirement row the index recorded for this source revision, in source order. The
    declaration says what the list can and cannot promise; it never comes from top-k passages."""
    idx = res.index()
    x = doc["active_extraction_id"]
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute("SELECT requirement_key, source_form, kind, element_id, name FROM requirements "
                            "WHERE index_version = ? AND extraction_id = ?", (idx.version, x)).fetchall()
    items = []
    for r in rows:
        el = idx.elements.get((x, r["element_id"]))
        items.append({"code": r["requirement_key"], "source_form": r["source_form"], "kind": r["kind"],
                      "name": r["name"], "element_id": r["element_id"],
                      "order": el["source_order"] if el else 10 ** 9,
                      "text": (el["raw_text"] if el else "")[:600], "location": el["location"] if el else {}})
    items.sort(key=lambda i: (i["order"], i["code"]))
    codes = {i["code"] for i in items}
    detail_codes = {i["code"] for i in items if i["kind"] == "detail"}
    repeated = sorted(c for c in codes if sum(1 for i in items if i["code"] == c and i["kind"] == "detail") > 1)
    reviewed = doc["review_status"] in ("reviewed", "sample_checked")
    text = ("원문 대조가 기록된 추출본 기준입니다." if reviewed else "원문 대조 전 추출본입니다: 표·번호 누락 가능성이 있습니다.")
    text += " 요구사항 고유번호가 있는 표 행만 포함하며, 번호 없는 서술형 요구사항은 포함되지 않습니다."
    if codes - detail_codes:
        text += f" 상세 표 없이 요약에만 있는 코드 {len(codes - detail_codes)}개가 있습니다."
    return {"items": items, "counts": {"codes": len(codes), "detail": len(detail_codes),
                                       "summary": len(codes - detail_codes), "rows": len(items)},
            "summary_only": sorted(codes - detail_codes), "repeated_detail_codes": repeated,
            "review_status": doc["review_status"], "index_version": idx.version, "extraction_id": x,
            "complete_for": "coded_requirement_rows", "completeness_text": text}


# ---------------------------------------------------------------- request status, listing and cancellation


def _view(conn, row: Row) -> RequestView:
    attempts, billing = _billing(conn, row["request_id"])
    snap = json.loads(row["request_json"]) if row["request_json"] else {}
    open_states = ("reserved", "dispatching", "unknown")
    result = AnswerResult(**json.loads(row["result_json"])) if row["result_json"] else None
    return RequestView(
        request_id=row["request_id"], member_id=row["member_id"], status=row["status"],
        mode=row["mode"] or snap.get("mode") or "single", generation_id=row["generation_id"] or "",
        question=snap.get("question", ""), as_of=snap.get("as_of", ""), scope=json.loads(row["scope_json"]),
        input_hash=row["input_hash"], created_at=row["created_at"], updated_at=row["updated_at"],
        cancel_requested=bool(row["cancel_requested"]), billing_state=billing,
        reserved_micro_usd=sum(a["reserved_micro_usd"] for a in attempts if a["state"] in open_states),
        settled_micro_usd=sum(a["settled_micro_usd"] or 0 for a in attempts if a["state"] == "settled"),
        attempts=attempts, result=result)


def request_status(res: Resources, principal: Principal, request_id: str) -> RequestView:
    """Read-only; safe for polling. Owner or verifier only."""
    principal = _authorize(res, principal, "consultant", "verifier")
    with open_db(res.settings.db_path) as conn:
        row = conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is None or (row["member_id"] != principal.member_id and not principal.can("verifier")):
            raise auth.AuthError("이 요청을 볼 권한이 없습니다.")
        return _view(conn, row)


def list_requests(res: Resources, principal: Principal, limit: int = 20, all_members: bool = False) -> list[RequestView]:
    principal = _authorize(res, principal, "consultant", "verifier")
    if all_members:
        principal = _authorize(res, principal, "verifier")
    sql = "SELECT * FROM requests WHERE request_json IS NOT NULL"
    args: tuple = ()
    if not all_members:
        sql += " AND member_id = ?"
        args = (principal.member_id,)
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", (*args, max(1, min(limit, 200)))).fetchall()
        return [_view(conn, r) for r in rows]


def cancel_request(res: Resources, principal: Principal, request_id: str) -> str:
    """Queued: conclusively cancelled before any dispatch. Running: no further paid stage starts and the result is
    not attached; an already dispatched call keeps its reservation until its usage or evidence arrives."""
    principal = _authorize(res, principal, "consultant", "verifier")
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        row = conn.execute("SELECT member_id, status, request_json FROM requests WHERE request_id = ?",
                           (request_id,)).fetchone()
        if row is None or row["member_id"] != principal.member_id:
            raise auth.AuthError("이 요청을 취소할 권한이 없습니다.")
        if row["status"] == "queued":
            snap = json.loads(row["request_json"] or "{}")
            result = AnswerResult(request_id, "cancelled", "실행 전에 취소되었습니다. 유료 호출은 없습니다.",
                                  generation_id=snap.get("generation_id", ""), mode=snap.get("mode", "single"))
            conn.execute("UPDATE requests SET status = 'cancelled', cancel_requested = 1, result_json = ?, "
                         "updated_at = ? WHERE request_id = ?", (dumps(asdict(result)), utcnow(), request_id))
            return "cancelled"
        if row["status"] == "running":
            conn.execute("UPDATE requests SET cancel_requested = 1, updated_at = ? WHERE request_id = ?",
                         (utcnow(), request_id))
            return "cancel_requested"
        return row["status"]


def recover_requests(db: Path) -> dict:
    """Restart recovery under exclusive ownership: nothing is replayed. Queued requests never dispatched; running
    ones may have, and their dispatching attempts become unknown through `budget.recover`."""
    note = dumps({"status": "interrupted", "summary": "서버가 다시 시작되어 요청이 중단되었습니다. 자동으로 다시 실행하지 "
                                                      "않습니다."})
    with open_db(db) as conn, tx(conn, immediate=True):
        queued = conn.execute("UPDATE requests SET status = 'interrupted', trace_json = COALESCE(trace_json, ?), "
                              "updated_at = ? WHERE status = 'queued'", (note, utcnow())).rowcount
        running = conn.execute("UPDATE requests SET status = 'interrupted', updated_at = ? WHERE status = 'running'",
                               (utcnow(),)).rowcount
    return {"queued": queued, "running": running}


def _stored_quote(res: Resources, e: EvidenceUnit) -> str | None:
    idx = res.index()
    for c in idx.chunks if idx else []:
        if c["chunk_id"] == e.chunk_id and c["extraction_id"] == e.extraction_id:
            return c["body"]
    return None


# ---------------------------------------------------------------- evidence and originals

_HEX64 = re.compile(r"[0-9a-f]{64}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SOURCE_WARNINGS = {"unreviewed": "source_unreviewed", "auto_flagged": "source_auto_flagged",
                   "needs_recovery": "source_needs_recovery"}


def get_request(res: Resources, principal: Principal, request_id: str) -> dict:
    principal = _authorize(res, principal, "consultant", "verifier")
    with open_db(res.settings.db_path) as conn:
        row = conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
    if row is None or (row["member_id"] != principal.member_id and not principal.can("verifier")):
        raise auth.AuthError("이 요청을 볼 권한이 없습니다.")
    return dict(row)


def _ordered_elements(res: Resources, extraction_id: str) -> list[dict]:
    """The extraction revision's elements in source order: from the loaded index, or from the preserved
    extraction artifact when a newer index no longer carries this revision (issued citations keep resolving)."""
    idx = res.index()
    if idx is not None and extraction_id in idx.rows_by_extraction:
        found = [e for (x, _), e in idx.elements.items() if x == extraction_id]
    else:
        try:
            found = load_elements(res.settings, extraction_id)
        except Exception:  # noqa: BLE001 - a missing artifact leaves the stored quote as the only context
            found = []
    return sorted(found, key=lambda e: e["source_order"])


def open_evidence(res: Resources, principal: Principal, request_id: str, evidence_id: str) -> EvidenceView:
    """Server-generated citation from the request's persisted evidence map: exact quote, neighbouring elements,
    location and the source's review state. The model never supplies a URL, path or page."""
    row = get_request(res, principal, request_id)
    result = json.loads(row["result_json"] or "{}")
    ev = (result.get("evidence") or {}).get(evidence_id)
    if ev is None:
        raise ServiceError("요청에 없는 근거 ID입니다.")
    (doc,) = _doc_rows(res, [ev["doc_id"]])
    ordered = _ordered_elements(res, ev["extraction_id"])
    orders = {e["element_id"]: i for i, e in enumerate(ordered)}
    hit = [orders[i] for i in ev["element_ids"] if i in orders]
    context = []
    if hit:
        lo, hi = min(hit), max(hit)
        context = [{"element_id": e["element_id"], "text": e["raw_text"], "cited": e["element_id"] in ev["element_ids"],
                    "location": e["location"]} for e in ordered[max(0, lo - 1):hi + 2]]
    warnings = []
    if doc["active_source_hash"] != ev["source_hash"]:
        warnings.append("source_version_changed")
    if doc["active_extraction_id"] != ev["extraction_id"]:
        warnings.append("extraction_revised")
    if doc["review_status"] in SOURCE_WARNINGS:
        warnings.append(SOURCE_WARNINGS[doc["review_status"]])
    if (ev.get("location") or {}).get("format") == "image_ocr":
        warnings.append("ocr_text")
    return EvidenceView(evidence_id, ev["doc_id"], doc["effective"]["title"], ev["quote"], context, ev["location"],
                        doc["format"], doc["active_source_hash"] == ev["source_hash"], ev["source_hash"],
                        doc["review_status"], warnings)


def original_download(res: Resources, principal: Principal, doc_id: str, source_hash: str) -> ManagedDownload:
    """Resolves only managed `(doc_id, source_hash)` pairs of the current source version; client filenames and
    paths are never accepted, and a recorded path outside the managed originals directory is refused."""
    principal = _authorize(res, principal, "consultant", "verifier")
    if not _UUID.fullmatch(doc_id or "") or not _HEX64.fullmatch(source_hash or ""):
        raise ServiceError("원문 식별자가 올바르지 않습니다.")
    (doc,) = _resolve_scope(res, [DocRef(doc_id, source_hash)])
    with open_db(res.settings.db_path) as conn:
        path = postgres.host_path(conn.execute("SELECT original_path FROM sources WHERE source_hash = ?",
                                               (source_hash,)).fetchone()[0])
    resolved = path.resolve()
    if resolved.parent != res.settings.files_dir.resolve() or not resolved.is_file():
        raise ServiceError("관리되지 않는 원문 경로입니다.")
    data = resolved.read_bytes()
    if hashlib.sha256(data).hexdigest() != source_hash:
        raise ServiceError("원문 파일이 기록된 버전과 다릅니다. 관리자에게 알려 주세요.")
    mime = "application/pdf" if doc["format"] == "pdf" else "application/x-hwp"
    return ManagedDownload(doc["filename"], data, mime)


def budget_snapshot(res: Resources, principal: Principal) -> BudgetSnapshot:
    """Read-only ledger view for polling. A failed read raises: callers must not treat stale numbers as live."""
    principal = _authorize(res, principal, "consultant", "verifier", "budget_admin")
    try:
        return budget.snapshot(res.settings.db_path)
    except (*DATABASE_ERRORS, budget.BudgetError) as exc:
        raise ServiceError(f"ledger_unavailable: {type(exc).__name__}") from None


# ---------------------------------------------------------------- verifier runs, corrections and exports

def set_budget_limit(res: Resources, principal: Principal, cap_micro_usd: int, reason: str) -> BudgetSnapshot:
    """The shared Settings control uses the existing budget-admin capability and visitor attribution."""
    principal = _authorize(res, principal, "budget_admin")
    try:
        budget.set_limit(res.settings.db_path, principal.member_id, cap_micro_usd, reason)
    except (*DATABASE_ERRORS, budget.BudgetError) as exc:
        raise ServiceError(str(exc) if isinstance(exc, budget.BudgetError) else "ledger_unavailable") from None
    return budget_snapshot(res, principal)


def api_key_status(res: Resources, principal: Principal) -> dict:
    """Whether the signed-in member's paid calls have a key, and when they entered it. The key never leaves the
    transport; another member's key is never reported here."""
    _authorize(res, principal, "consultant", "verifier", "budget_admin")
    source = res.key_sources.get(generation.KEY_SESSION.get())
    if source is None and res.transport is not None and not res.paid_refusal():
        source = {"set_by": "server environment", "set_at": None}
    return {"configured": source is not None, "set_by": (source or {}).get("set_by"),
            "set_at": (source or {}).get("set_at"),
            "model": (source or {}).get("model") or res.settings.generation_model,
            "models": list(ALLOWED_GENERATION_MODELS)}


def bind_request(res: Resources | None, member_id: str | None):
    """Fixes, for one HTTP request and all work it starts, the signed-in member's key, answer model and billing
    scope as they are now. Returns the reset to call when the request ends."""
    session = res.member_keys.get(member_id) if res is not None and member_id else None
    source = (res.key_sources.get(session) if session else None) or {}
    bound = [(generation.KEY_SESSION, session), (generation.REQUEST_MODEL, source.get("model")),
             (budget.BILLING_SCOPE, source.get("billing_scope"))]
    tokens = [(var, var.set(value)) for var, value in bound]

    def reset() -> None:
        for var, token in reversed(tokens):
            var.reset(token)
    return reset


def _selectable(res: Resources, model: str, actor: str) -> None:
    if model not in ALLOWED_GENERATION_MODELS:
        raise ServiceError("선택할 수 없는 모델입니다.")
    try:
        budget.ensure_generation_rate(res.settings.db_path, model, actor)
    except (*DATABASE_ERRORS, budget.BudgetError) as exc:
        raise ServiceError(str(exc) if isinstance(exc, budget.BudgetError) else "ledger_unavailable") from None


def set_generation_model(res: Resources, principal: Principal, model: str) -> dict:
    """The member's answer model for requests they start from now on, checked against the key they entered (a
    free metadata read). Requests already submitted keep the model they began with."""
    principal = _authorize(res, principal, "budget_admin")
    session = generation.KEY_SESSION.get()
    if session not in res.key_sources:
        raise ServiceError(generation.NO_API_KEY)
    if problem := res.transport.check_model(model):
        raise ServiceError(problem)
    _selectable(res, model, principal.member_id)
    res.key_sources[session] = {**res.key_sources[session], "model": model}
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        _audit(conn, principal.member_id, "set_generation_model", "openai", "", {"model": model})
    return api_key_status(res, principal)


def set_api_key(res: Resources, principal: Principal, api_key: str, model: str) -> dict:
    """Checks the key against the chosen model (a free metadata read), then makes it the member's, in every browser
    they sign in from. Returns the member's key status. The audit row records who set a key, never its value."""
    principal = _authorize(res, principal, "budget_admin")
    key = api_key.strip()
    if not key or len(key) > 500 or not key.isascii() or any(c.isspace() for c in key):
        raise ServiceError("API 키 형식이 아닙니다.")
    if model not in ALLOWED_GENERATION_MODELS:
        raise ServiceError("선택할 수 없는 모델입니다.")
    problem, billing_scope = generation.check_api_key(key, model, res.settings.request_timeout_seconds)
    if problem:
        raise ServiceError(problem)
    _selectable(res, model, principal.member_id)
    # Always a new session: requests already running keep the session, key, model and project they began with.
    session = secrets.token_urlsafe(32)
    res.set_api_key(session, key, principal.member_id, model, billing_scope)
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        _audit(conn, principal.member_id, "set_api_key", "openai", "", {"model": model, "billing_scope": billing_scope})
    token = generation.KEY_SESSION.set(session)
    try:
        return api_key_status(res, principal)
    finally:
        generation.KEY_SESSION.reset(token)

VERIFIER_MODES = ("whitespace_bm25", "kiwi_bm25", "dense", "hybrid", "hybrid_rerank")


def verifier_config(res: Resources, mode: str | None = None, limits: dict | None = None) -> dict:
    """A versioned, immutable verifier configuration: the serving snapshot plus explicit overrides. Editing a
    control produces another config ID; an old run keeps its own snapshot. `limits` records the values the run
    executes with (an override is clamped to the serving limits), `effective_limits` every limit in force."""
    idx = res.index()
    serving = res.serving()
    s = res.run_settings(serving)
    narrowed = _narrow_limits(s, limits)
    cfg = {"serving_run": serving.get("run_id"), "serving_mode": serving["mode"], "mode": mode or serving["mode"],
           "dense_version": serving.get("dense_version"), "index_version": idx.version if idx else None,
           "limits": {k: narrowed[k] for k in sorted(narrowed)},
           "effective_limits": {k: narrowed.get(k, getattr(s, k)) for k in LIMIT_KEYS},
           "prompt_version": generation.PROMPT_VERSION, "model": res.generation_model()}
    if cfg["mode"] not in VERIFIER_MODES:
        raise ServiceError("알 수 없는 검색 방식입니다.")
    cfg["config_id"] = "vc-" + hashlib.sha256(dumps(cfg).encode()).hexdigest()[:12]
    return cfg


def _stored_verifier_config(res: Resources, config_id: str) -> dict | None:
    with open_db(res.settings.db_path) as conn:
        row = conn.execute("SELECT config_json FROM verifier_runs WHERE config_id = ? LIMIT 1", (config_id,)).fetchone()
    return json.loads(row["config_json"]) if row else None


def resolve_verifier_config(res: Resources, config_id: str) -> dict | None:
    """The frozen verifier configuration a paid request runs with (None for the serving default). It must still
    describe the current serving state: recomputing it now must give the same ID, otherwise the index, serving
    run, prompt or model changed since the run was frozen and its estimate no longer applies."""
    if not config_id or config_id == "default":
        return None
    cfg = _stored_verifier_config(res, config_id)
    if cfg is None:
        raise ServiceError("알 수 없는 검증 설정입니다. (유료 호출 없음)")
    if verifier_config(res, cfg["mode"], cfg["limits"])["config_id"] != config_id:
        raise ServiceError("검증 실행 이후 서비스 구성(색인·검색 실행·프롬프트·모델)이 바뀌었습니다. 새 검증 실행을 "
                           "만든 뒤 그 추정 비용으로 생성하세요. (유료 호출 없음)")
    return cfg


def _frozen_run_for(res: Resources, request: AnswerRequest, question: str) -> dict | None:
    """The frozen verifier run a paid request generates from, checked against the request: same question, scope,
    answer mode and as-of date, and a configuration that still describes the current serving state."""
    if not request.verifier_run_id:
        if request.config_id != "default":
            raise ServiceError("검증 설정은 고정된 검증 실행으로만 적용됩니다. (유료 호출 없음)")
        return None
    if request.mode not in PAID_MODES:
        raise ServiceError("검증 실행은 답변 생성 요청에만 적용됩니다.")
    with open_db(res.settings.db_path) as conn:
        row = conn.execute("SELECT * FROM verifier_runs WHERE run_id = ?", (request.verifier_run_id,)).fetchone()
    if row is None:
        raise ServiceError("없는 검증 실행입니다. (유료 호출 없음)")
    cfg, trace = json.loads(row["config_json"]), json.loads(row["trace_json"])
    scope = [(s["doc_id"], s["source_hash"]) for s in json.loads(row["scope_json"])]
    if (request.config_id not in ("default", cfg["config_id"]) or row["question"] != question
            or scope != [(r.doc_id, r.source_hash) for r in request.scope]
            or request.mode != ("compare" if len(scope) == 2 else "single")
            or request.as_of != trace.get("as_of", request.as_of)):
        raise ServiceError("요청이 검증 실행의 질문·범위·기준일과 다릅니다. (유료 호출 없음)")
    resolve_verifier_config(res, cfg["config_id"])
    return {"config": cfg, **trace}


def _frozen_prep(res: Resources, question: str, request: AnswerRequest, docs: list[dict], frozen: dict) -> dict:
    """Prices the frozen run's own evidence (same prompt, same count) instead of retrieving again."""
    r = frozen["retrieval"]
    retrieval = RetrievalResult(**{**r, "scope": [DocRef(**s) for s in r["scope"]],
                                   "evidence": [EvidenceUnit(**e) for e in r["evidence"]]})
    prep = _priced(res, question, request.as_of, docs, retrieval, request.mode, price_query_embedding=False)
    if frozen.get("coverage"):
        prep["coverage"] = frozen["coverage"]
    return prep


def _embedding_estimate(res: Resources, question: str) -> int | None:
    """The shared-allowance price of the activated model's query embedding: a local model is free and Gemini is
    charged to its own cap, so neither adds to the OpenAI estimate."""
    model = res.run_settings().embedding_model
    if dense_mod.embedding_backend(res.run_settings()) != "openai":
        return 0
    try:
        return budget.estimate(res.settings.db_path, model,
                               dense_mod.count_embedding_tokens(question) + dense_mod.QUERY_MARGIN_TOKENS, 0)
    except budget.BudgetError:
        return None


def verifier_trace(res: Resources, principal: Principal, question: str, scope: list[DocRef], as_of: str,
                   mode: str | None = None, limits: dict | None = None) -> dict:
    """Retrieval-only verification run, frozen under a run ID with its configuration snapshot, the exact final
    token count and the paid estimate. Free: a dense mode uses only a cached query vector and shows the cost of
    the miss instead of paying for it."""
    principal = _authorize(res, principal, "verifier")
    question = (question or "").strip()
    if not question or len(question) > res.settings.question_max_characters:
        raise ServiceError(f"질문은 1~{res.settings.question_max_characters}자여야 합니다.")
    cfg = verifier_config(res, mode, limits)
    docs = _resolve_scope(res, scope)
    if len(scope) == 2:
        prep = _prepare_compare(res, principal, question, docs, as_of, None, allow_paid=False, mode=cfg["mode"],
                                limits=cfg["limits"], price_query_embedding=False)
    elif len(scope) == 1:
        r = retrieve(res, principal, question, scope, limits=cfg["limits"] or None, mode=cfg["mode"])
        prep = _priced(res, question, as_of, docs, r, "single", price_query_embedding=False)
    else:
        raise ServiceError("문서를 한두 개 선택하세요.")
    r: RetrievalResult = prep["retrieval"]
    idx = res.index()
    qe = r.query_embedding or {}
    trace = {"retrieval": asdict(r), "as_of": as_of, "query_tokens": res.analyzer.tokens(question),
             "codes": list(dict.fromkeys(CODE_RE.findall(nfc(question)))), "input_tokens": prep["input_tokens"],
             "estimate_micro_usd": prep["estimate_micro_usd"],
             "query_embedding_estimate_micro_usd": _embedding_estimate(res, question) if qe.get("cache") == "miss"
             else 0,
             "coverage": prep.get("coverage"), "index_version": idx.version if idx else None,
             "review_scope": idx.review_scope if idx else None, "serving": res.serving(),
             "stage_errors": dict(res.stage_errors),
             "docs": [{k: d[k] for k in ("doc_id", "filename", "parse_status", "review_status", "reason_code",
                                         "active_source_hash", "active_extraction_id")} for d in docs]}
    run_id = "vr-" + uuid.uuid4().hex[:12]
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("INSERT INTO verifier_runs(run_id, member_id, config_id, config_json, question, scope_json, "
                     "trace_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (run_id, principal.member_id, cfg["config_id"], dumps(cfg), question,
                      dumps([asdict(x) for x in scope]), dumps(trace), utcnow()))
    return {"run_id": run_id, "config": cfg, **trace}


def verifier_runs(res: Resources, principal: Principal, limit: int = 30) -> list[dict]:
    principal = _authorize(res, principal, "verifier")
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute("SELECT run_id, member_id, config_id, question, scope_json, created_at FROM verifier_runs "
                            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "scope": json.loads(r["scope_json"])} for r in rows]


def verifier_run(res: Resources, principal: Principal, run_id: str) -> dict:
    principal = _authorize(res, principal, "verifier")
    with open_db(res.settings.db_path) as conn:
        row = conn.execute("SELECT * FROM verifier_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise ServiceError("없는 검증 실행입니다.")
    return {"run_id": row["run_id"], "member_id": row["member_id"], "created_at": row["created_at"],
            "question": row["question"], "scope": json.loads(row["scope_json"]),
            "config": json.loads(row["config_json"]), **json.loads(row["trace_json"])}


def compare_verifier_runs(res: Resources, principal: Principal, run_a: str, run_b: str) -> dict:
    """Controlled comparison of two frozen runs: what changed in configuration, ranking and packed evidence."""
    a, b = verifier_run(res, principal, run_a), verifier_run(res, principal, run_b)

    def evidence_keys(run: dict) -> list[str]:
        return [f"{e['extraction_id'][:8]}:{','.join(e['element_ids'])}" for e in run["retrieval"]["evidence"]]

    ea, eb = evidence_keys(a), evidence_keys(b)
    changed = {k: (a["config"].get(k), b["config"].get(k)) for k in set(a["config"]) | set(b["config"])
               if k != "config_id" and a["config"].get(k) != b["config"].get(k)}
    return {"runs": [run_a, run_b], "same_question": a["question"] == b["question"],
            "same_scope": a["scope"] == b["scope"], "config_changes": changed,
            "evidence_only_a": [k for k in ea if k not in eb], "evidence_only_b": [k for k in eb if k not in ea],
            "evidence_common": [k for k in ea if k in eb],
            "top5_a": a["retrieval"]["ranking"][:5], "top5_b": b["retrieval"]["ranking"][:5],
            "evidence_tokens": [a["retrieval"]["evidence_tokens"], b["retrieval"]["evidence_tokens"]],
            "input_tokens": [a["input_tokens"], b["input_tokens"]],
            "total_ms": [a["retrieval"]["timings_ms"].get("total"), b["retrieval"]["timings_ms"].get("total")],
            "limitations": [a["retrieval"]["limitations"], b["retrieval"]["limitations"]]}


SEALED_DATASETS = ("test",)


def dataset_rows(res: Resources, principal: Principal, dataset: str) -> list[dict]:
    """Reviewed development rows for verifier reproduction. Sealed test questions and labels are never served
    here; the phase-4 `sealed_evaluator` procedure reads them under its freeze."""
    principal = _authorize(res, principal, "verifier")
    if dataset in SEALED_DATASETS or not re.fullmatch(r"[a-z0-9-]{1,40}", dataset or ""):
        raise auth.AuthError("봉인된 평가 데이터는 검증 화면에서 열 수 없습니다.")
    path = res.settings.data_dir / "datasets" / f"{dataset}.jsonl"
    if not path.exists():
        return []
    if dataset == "dev":
        from ..evaluation.evaluation import load_eval_rows

        rows, _, _ = load_eval_rows(res.settings, dataset)
        return rows
    from ..storage.store import read_jsonl

    return [r for r in read_jsonl(path) if r.get("reviewed_by")]


def sealed_status(res: Resources, principal: Principal) -> dict:
    """Existence and size of the sealed test set only; no question or label leaves this function."""
    principal = _authorize(res, principal, "sealed_evaluator")
    test = res.settings.data_dir / "sealed" / "test.jsonl"
    manifest = res.settings.data_dir / "sealed" / "test-manifest.json"
    rows = sum(1 for line in test.read_text(encoding="utf-8").splitlines() if line.strip()) if test.exists() else 0
    return {"rows": rows, "manifest": manifest.exists()}


def record_correction(res: Resources, principal: Principal, *, reason: str, evidence: dict, quote: str,
                      proposal: dict, dataset: str | None = None, row_id: str | None = None,
                      run_id: str | None = None, request_id: str | None = None) -> str:
    """Append-only verifier correction with reviewer, time, reason, quoted original evidence and the affected
    row/run/request. It never edits a dataset or an old run; sealed labels need the sealed evaluator."""
    principal = _authorize(res, principal, "verifier")
    if dataset in SEALED_DATASETS:
        principal = _authorize(res, principal, "sealed_evaluator")
    if not (reason or "").strip() or not (quote or "").strip():
        raise ServiceError("수정 기록에는 사유와 원문 인용이 필요합니다.")
    if not (dataset and row_id) and not run_id and not request_id:
        raise ServiceError("수정 대상(데이터셋 행, 검증 실행 또는 요청)을 지정하세요.")
    (doc,) = _doc_rows(res, [evidence.get("doc_id", "")]) or [None]
    if doc is None or evidence.get("source_hash") != doc["active_source_hash"]:
        raise ServiceError("근거 문서 또는 원문 버전이 현재 원문과 일치하지 않습니다.")
    element = next((e for e in _ordered_elements(res, evidence.get("extraction_id") or "")
                    if e["element_id"] == evidence.get("element_id")), None)
    if element is None or nfc(quote.strip()) not in nfc(element["raw_text"]):
        raise ServiceError("인용문이 지정한 원문 요소에 없습니다. 원문 그대로 인용하세요.")
    if run_id:
        verifier_run(res, principal, run_id)
    if request_id:
        get_request(res, principal, request_id)
    correction_id = str(uuid.uuid4())
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("INSERT INTO corrections(correction_id, reviewer, dataset, row_id, run_id, request_id, reason, "
                     "quote, evidence_json, proposal_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (correction_id, principal.member_id, dataset, row_id, run_id, request_id, reason.strip(),
                      quote.strip(), dumps(evidence), dumps(proposal), utcnow()))
    return correction_id


def record_run_correction(res: Resources, principal: Principal, run_id: str, evidence_id: str, element_id: str,
                          reason: str, quote: str, proposal: dict) -> str:
    """`record_correction` against one packed evidence unit of a frozen run, named by its IDs."""
    run = verifier_run(res, principal, run_id)
    ev = next((e for e in run["retrieval"]["evidence"] if e["evidence_id"] == evidence_id), None)
    if ev is None or element_id not in ev["element_ids"]:
        raise ServiceError("검증 실행에 없는 근거 또는 원문 요소입니다.")
    return record_correction(res, principal, reason=reason, quote=quote, proposal=proposal, run_id=run_id,
                             evidence={"doc_id": ev["doc_id"], "source_hash": ev["source_hash"],
                                       "extraction_id": ev["extraction_id"], "element_id": element_id})


def list_corrections(res: Resources, principal: Principal) -> list[dict]:
    principal = _authorize(res, principal, "verifier")
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute("SELECT * FROM corrections ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        if r["dataset"] in SEALED_DATASETS and not principal.can("sealed_evaluator"):
            continue
        out.append({**{k: r[k] for k in r.keys() if not k.endswith("_json")},
                    "evidence": json.loads(r["evidence_json"]), "proposal": json.loads(r["proposal_json"])})
    return out


def verification_history(res: Resources, principal: Principal, limit: int = 100) -> list[dict]:
    """Read saved review actions directly; proposals remain explicitly unapplied. Never expose sealed labels."""
    _authorize(res, principal, "verifier")
    limit = max(1, min(limit, 200))
    with open_db(res.settings.db_path) as conn:
        sources = conn.execute(
            "SELECT r.*, (SELECT MIN(d.filename) FROM documents d WHERE d.active_source_hash = r.source_hash) "
            "AS filename FROM reviews r ORDER BY r.created_at DESC, r.review_id DESC LIMIT ?", (limit,)).fetchall()
        placeholders = ",".join("?" for _ in SEALED_DATASETS)
        decisions = conn.execute(
            "SELECT r.*, c.row_json, c.dataset FROM gold_reviews r JOIN gold_candidates c "
            f"ON c.candidate_id = r.candidate_id WHERE c.dataset NOT IN ({placeholders}) "
            "ORDER BY r.created_at DESC, r.review_id DESC LIMIT ?", (*SEALED_DATASETS, limit)).fetchall()
        legacy = conn.execute(
            f"SELECT c.* FROM gold_candidates c WHERE c.dataset NOT IN ({placeholders}) "
            "AND c.status != 'pending' AND c.decided_at IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM gold_reviews r WHERE r.candidate_id = c.candidate_id AND r.kind = 'decision') "
            "ORDER BY c.decided_at DESC, c.candidate_id DESC LIMIT ?", (*SEALED_DATASETS, limit)).fetchall()
        proposals = conn.execute(
            f"SELECT * FROM corrections WHERE dataset IS NULL OR dataset NOT IN ({placeholders}) "
            "ORDER BY created_at DESC, correction_id DESC LIMIT ?", (*SEALED_DATASETS, limit)).fetchall()
    events = []
    for r in sources:
        findings = json.loads(r["findings_json"])
        events.append({"event_id": r["review_id"], "kind": "source", "action": r["status"],
                       "target_id": r["source_hash"], "target": r["filename"] or r["source_hash"][:12],
                       "reviewer": r["reviewer"], "created_at": r["created_at"],
                       "note": findings.get("note", ""), "quote": "",
                       "locations": json.loads(r["locations_json"])})
    for r in decisions:
        row = json.loads(r["row_json"])
        events.append({"event_id": r["review_id"], "kind": "gold", "action": f'{r["kind"]}:{r["decision"]}',
                       "target_id": r["candidate_id"], "target": row.get("question") or r["candidate_id"],
                       "reviewer": r["reviewer"], "created_at": r["created_at"],
                       "note": r["note"], "quote": "", "locations": []})
    for r in legacy:
        row = json.loads(r["row_json"])
        rejection = json.loads(r["reject_json"] or "{}")
        note = rejection.get("note") or ", ".join(gold.REJECT_CATEGORIES.get(c, c)
                                                 for c in rejection.get("categories", []))
        events.append({"event_id": f'legacy:{r["candidate_id"]}', "kind": "gold",
                       "action": "decision:approve" if r["status"] == "approved" else "decision:reject",
                       "target_id": r["candidate_id"], "target": row.get("question") or r["candidate_id"],
                       "reviewer": r["decided_by"] or "", "created_at": r["decided_at"],
                       "note": note, "quote": "", "locations": []})
    for r in proposals:
        events.append({"event_id": r["correction_id"], "kind": "proposal", "action": "unapplied",
                       "target_id": r["run_id"] or r["request_id"] or r["row_id"],
                       "target": r["run_id"] or r["request_id"] or r["row_id"],
                       "reviewer": r["reviewer"], "created_at": r["created_at"],
                       "note": r["reason"], "quote": r["quote"], "locations": []})
    return sorted(events, key=lambda e: (e["created_at"], e["event_id"]), reverse=True)[:limit]


_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)")
_PATH_RE = re.compile(r"([A-Za-z]:\\[^\s\"']+|/(?:home|root|Users|tmp|mnt|var)/[^\s\"']+)")
_REDACT_KEYS = {"original_path", "artifact_path", "messages", "api_key"}


def redact(value):
    """Exports: drop credentials and unrestricted local paths; keep stable IDs, hashes and locations."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if k not in _REDACT_KEYS}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _PATH_RE.sub("[local-path]", _SECRET_RE.sub("[redacted]", value))
    return value


def export_request(res: Resources, principal: Principal, request_id: str) -> dict:
    """A saved trace: input snapshot, configuration, ranks, evidence with stable locations/hashes, outcome and
    billing. Consultants export their own requests; verifiers may export any."""
    row = get_request(res, principal, request_id)
    with open_db(res.settings.db_path) as conn:
        attempts = [dict(a) for a in conn.execute(
            "SELECT attempt_id, stage, purpose, model, state, reserved_micro_usd, settled_micro_usd, raw_usage_json, "
            "price_json, created_at, dispatched_at, finished_at FROM attempts WHERE request_id = ?", (request_id,))]
    snap = json.loads(row["request_json"] or "{}")
    snap.pop("idempotency_key", None)
    return redact({"export_version": "request-export-1", "exported_at": utcnow(), "request_id": request_id,
                   "member_id": row["member_id"], "status": row["status"], "input": snap,
                   "input_hash": row["input_hash"], "config_hash": row["config_hash"],
                   "trace": json.loads(row["trace_json"] or "{}"), "result": json.loads(row["result_json"] or "{}"),
                   "attempts": attempts})


def export_verifier_run(res: Resources, principal: Principal, run_id: str) -> dict:
    run = verifier_run(res, principal, run_id)
    return redact({"export_version": "verifier-run-export-1", "exported_at": utcnow(), **run})


# ---------------------------------------------------------------- budget administration


def _audit(conn, actor: str, action: str, target: str, reason: str, details: dict) -> str:
    event_id = str(uuid.uuid4())
    conn.execute("INSERT INTO audit_events(event_id, actor, action, target, reason, details_json, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (event_id, actor, action, target, reason, dumps(details), utcnow()))
    return event_id


def _require_reason(reason: str, evidence: str | None = None) -> None:
    if not (reason or "").strip() or (evidence is not None and not evidence.strip()):
        raise ServiceError("관리 작업에는 사유와 근거가 필요합니다.")


def unresolved_attempts(res: Resources, principal: Principal) -> list[dict]:
    """Recovery view: every attempt whose billing is not settled or released, with its reservation, stage,
    member, dispatch time and recorded error."""
    principal = _authorize(res, principal, "budget_admin")
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute(
            "SELECT attempt_id, request_id, member_id, stage, purpose, model, state, reserved_micro_usd, "
            "estimated_input_tokens, max_output_tokens, created_at, dispatched_at, error_json FROM attempts "
            "WHERE state IN ('reserved', 'dispatching', 'unknown', 'reconciled') ORDER BY created_at").fetchall()
    return [{**dict(r), "error": json.loads(r["error_json"] or "null")} for r in rows]


def settle_from_evidence(res: Resources, principal: Principal, attempt_id: str, usage: dict, response_id: str | None,
                         evidence: str, reason: str) -> dict:
    """Owner settles an unknown (or reconciled) attempt from dated provider usage evidence: exactly once."""
    principal = _authorize(res, principal, "budget_admin")
    _require_reason(reason, evidence)
    clean = {k: int(usage.get(k, 0)) for k in ("prompt_tokens", "completion_tokens", "cached_tokens",
                                                 "cache_write_tokens")}
    if any(v < 0 for v in clean.values()):
        raise ServiceError("토큰 수는 음수일 수 없습니다.")
    with open_db(res.settings.db_path) as conn:
        row = conn.execute("SELECT state FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    if row is None or row["state"] not in ("unknown", "reconciled"):
        raise ServiceError("미확정(unknown) 또는 대조된 시도만 증빙으로 정산할 수 있습니다.")
    settlement = budget.settle(res.settings.db_path, attempt_id, {**clean, "source": "owner_evidence"},
                               response_id or None)
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        _audit(conn, principal.member_id, "settle_from_evidence", attempt_id, reason,
               {"usage": clean, "evidence": evidence, "settlement": settlement})
    return settlement


def reconcile(res: Resources, principal: Principal, record: dict) -> BudgetSnapshot:
    """Owner import of a dated provider interval total (see `budget.reconcile`). Importing the same record twice
    changes nothing; a different total under the same ID is refused."""
    principal = _authorize(res, principal, "budget_admin")
    required = ("reconciliation_id", "interval_start", "interval_end", "provider_total_micro_usd", "scope",
                "evidence")
    missing = [k for k in required if record.get(k) in (None, "")]
    if missing:
        raise ServiceError(f"대조 기록에 필요한 값이 없습니다: {', '.join(missing)}")
    applied = budget.reconcile(res.settings.db_path, principal.member_id, str(record["reconciliation_id"]),
                               record["interval_start"], record["interval_end"],
                               int(record["provider_total_micro_usd"]), record["scope"], record["evidence"],
                               list(record.get("covered_attempt_ids") or []), record.get("unscoped_attempts"))
    if applied:
        with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
            _audit(conn, principal.member_id, "reconcile", str(record["reconciliation_id"]),
                   record.get("reason") or "provider reconciliation", {k: record.get(k) for k in required})
    return budget.snapshot(res.settings.db_path)


def add_external_adjustment(res: Resources, principal: Principal, correction_key: str, amount_micro: int,
                            evidence: str, reason: str) -> bool:
    """Spend outside the gateway (or an owner correction), idempotent by key, with evidence and an audit event."""
    principal = _authorize(res, principal, "budget_admin")
    _require_reason(reason, evidence)
    if not re.fullmatch(r"[A-Za-z0-9:_\-.]{3,80}", correction_key or ""):
        raise ServiceError("조정 키는 영문·숫자·:_-. 3~80자여야 합니다.")
    applied = budget.add_adjustment(res.settings.db_path, principal.member_id, f"external:{correction_key}",
                                    int(amount_micro), evidence, reason)
    if applied:
        with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
            _audit(conn, principal.member_id, "external_adjustment", correction_key, reason,
                   {"amount_micro_usd": int(amount_micro), "evidence": evidence})
    return applied


def set_paid_enabled(res: Resources, principal: Principal, enabled: bool, reason: str) -> None:
    """Pauses or resumes paid stages; resuming also clears a cost-overrun freeze after inspection."""
    principal = _authorize(res, principal, "budget_admin")
    _require_reason(reason)
    budget.set_paid_enabled(res.settings.db_path, principal.member_id, enabled, reason)
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        _audit(conn, principal.member_id, "set_paid_enabled", "budget", reason, {"enabled": enabled})


def audit_events(res: Resources, principal: Principal, limit: int = 50) -> list[dict]:
    principal = _authorize(res, principal, "budget_admin")
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [{**{k: r[k] for k in r.keys() if k != "details_json"}, "details": json.loads(r["details_json"])}
            for r in rows]


def gold_queue(res: Resources, principal: Principal) -> dict:
    principal = _authorize(res, principal, "verifier")
    return {"pending": gold.queue(res.settings), "recent": gold.recent(res.settings)}


def gold_candidate(res: Resources, principal: Principal, candidate_id: str) -> dict:
    """The candidate, its source context, and who asked for the drafting run it came from (if any)."""
    principal = _authorize(res, principal, "verifier")
    try:
        c = gold.candidate(res.settings, candidate_id)
    except gold.GoldError as exc:
        raise ServiceError(str(exc)) from None
    return {**c, "requested_by": _drafting_meta(res.settings, c["batch_id"]).get("requested_by")}


def gold_decide(res: Resources, principal: Principal, candidate_id: str, decision: str, expected_sha: str,
                categories: list[str] | None = None, note: str = "", *, original_inspected: bool = False,
                disputed: bool = False) -> dict:
    """The visitor's chosen name is recorded as the reviewer, and every decision carries a note. Whoever started a
    drafting run cannot approve its candidates, any more than the drafting model can. Sealed test candidates are
    refused here: only the owner's CLI reviews them."""
    principal = _authorize(res, principal, "verifier")
    if not (note or "").strip():
        raise ServiceError("승인·거절 모두 메모를 적어야 합니다.")
    if decision == "approve":
        with open_db(res.settings.db_path) as conn:
            row = conn.execute("SELECT batch_id FROM gold_candidates WHERE candidate_id = ?",
                               (candidate_id,)).fetchone()
        requester = _drafting_meta(res.settings, row["batch_id"]).get("requested_by") if row else None
        if requester and requester == principal.member_id:
            raise ServiceError("초안 생성을 요청한 사람은 그 초안을 승인할 수 없습니다. 다른 검토자가 승인하세요.")
    try:
        return gold.decide(res.settings, candidate_id, decision, principal.member_id, expected_sha, categories, note,
                           original_inspected=original_inspected, disputed=disputed)
    except gold.GoldError as exc:
        raise ServiceError(str(exc)) from None


def gold_second_review(res: Resources, principal: Principal, candidate_id: str, agreed: bool, note: str) -> dict:
    principal = _authorize(res, principal, "verifier")
    try:
        return gold.second_review(res.settings, candidate_id, principal.member_id, agreed, note)
    except gold.GoldError as exc:
        raise ServiceError(str(exc)) from None


def gold_awaiting_second_review(res: Resources, principal: Principal) -> list[dict]:
    """Approved development gold rows marked disputed that still lack an independent second review."""
    principal = _authorize(res, principal, "verifier")
    from ..storage.store import read_jsonl

    path = res.settings.data_dir / "datasets" / "dev.jsonl"
    rows = read_jsonl(path) if path.exists() else []
    return [r for r in rows if (r.get("review") or {}).get("disputed") and not (r["review"].get("second_review"))]


# ---------------------------------------------------------------- phase 4: development answer evaluation

_EVAL_JOBS: dict[str, threading.Thread] = {}
_JUDGE_JOBS: dict[str, threading.Thread] = {}  # judge comparison parts; never alongside an answer evaluation
_EVAL_LOCK = threading.Lock()


def evaluation_overview(res: Resources, principal: Principal) -> dict:
    """What the verifier may see of phase 4: development validation and freeze state, the sealed set's size and
    freeze state only, development answer runs and their scores, and the latest release decision."""
    principal = _authorize(res, principal, "verifier")
    from ..evaluation import evaluation, release

    s = res.settings
    dev = evaluation.dataset_path(s, "dev")
    validation = json.loads(dev.with_suffix(".validation.json").read_text(encoding="utf-8")) \
        if dev.with_suffix(".validation.json").exists() else None
    runs = []
    for d in sorted((s.data_dir / "runs").glob("A-*")):  # nothing when the directory does not exist
        try:
            config = json.loads((d / "config.json").read_text(encoding="utf-8"))
            rows_file = d / "rows.jsonl"
            done = sum(1 for line in rows_file.read_text(encoding="utf-8").splitlines()
                       if line.strip() and json.loads(line).get("status") == "done") if rows_file.exists() else 0
            runs.append({"run_id": d.name, "config": config,
                         "progress": {"done": done, "total": (config.get("rows") or 0) * len(config["finalists"])},
                         "scores": json.loads((d / "scores.json").read_text(encoding="utf-8"))
                         if (d / "scores.json").exists() else None,
                         "running": d.name in _EVAL_JOBS and _EVAL_JOBS[d.name].is_alive()})
        except (OSError, json.JSONDecodeError):
            continue
    test = evaluation.frozen_dataset(s, "test")
    releases = []
    for path in (s.data_dir / "releases").glob("*/manifest.json") if (s.data_dir / "releases").exists() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "status" in data:  # release-report manifests; phase-2 handoff manifests have no decision
            releases.append((path.stat().st_mtime, data))
    return {"dev_validation": validation, "dev_frozen": evaluation.frozen_dataset(s, "dev"),
            "test": {"rows": sealed_rows_count(s), "frozen": bool(test), "current": bool(test and test["current"])},
            "answer_runs": sorted(runs, key=lambda r: r["config"].get("created_at") or ""),  # IDs are hashes: by time
            "release": max(releases, key=lambda x: x[0])[1] if releases else None, "targets": {
                "rates": release.TARGETS, "latency_ms": release.LATENCY_TARGETS_MS}}


def sealed_rows_count(settings: Settings) -> int:
    test = settings.data_dir / "sealed" / "test.jsonl"
    return sum(1 for line in test.read_text(encoding="utf-8").splitlines() if line.strip()) if test.exists() else 0


def plan_answer_evaluation(res: Resources, principal: Principal, run_ids: list[str] | None = None) -> dict:
    """Free: prices every development answer the finalists would still generate. Nothing is sent."""
    principal = _authorize(res, principal, "verifier")
    from . import answers

    try:
        return answers.plan_run(res.settings, "answer-finalists", "dev", run_ids or None)
    except answers.AnswerEvalError as exc:
        raise ServiceError(str(exc)) from None


def start_answer_evaluation(res: Resources, principal: Principal, estimate_id: str) -> str:
    """Runs a planned development answer evaluation on this process's gateway in one background thread, with the
    estimate the verifier consented to. One evaluation at a time; the service's stop interrupts it between rows and
    before any new paid stage; rerunning resumes."""
    principal = _authorize(res, principal, "verifier")
    from . import answers

    try:
        est = answers.load_estimate(res.settings, estimate_id)
        if est["action"] != "answer-finalists":
            raise ServiceError("검증 화면에서는 개발 질문 평가만 실행할 수 있습니다. 봉인 평가는 소유자 CLI로 실행합니다.")
    except answers.AnswerEvalError as exc:
        raise ServiceError(str(exc)) from None
    with _EVAL_LOCK:
        _refuse_closed_or_busy(res)
        try:  # published before the start returns, so the overview lists the run on its very next read
            begun = answers.begin_answers(res.settings, estimate_id, principal.member_id)
        except answers.AnswerEvalError as exc:
            raise ServiceError(str(exc)) from None
        run_id = begun["run_id"]

        def job() -> None:
            try:
                answers.run_answers(res.settings, res, estimate_id, principal.member_id, begun=begun)
            except Exception as exc:  # noqa: BLE001 - recorded for the overview; rows already finished stay
                from ..storage.store import write_text_atomic

                write_text_atomic(answers.run_dir(res.settings, run_id) / "last-error.txt",
                                  f"{type(exc).__name__}: {exc}"[:500])

        _start_job(res, _EVAL_JOBS, run_id, threading.Thread(target=contextvars.copy_context().run, args=(job,), name=f"rfp-eval-{run_id}", daemon=True))
    return run_id


def _refuse_closed_or_busy(res: Resources) -> None:
    if res._closed or res.paid_refusal():
        raise ServiceError(res.paid_refusal() or "서비스가 종료 중입니다.")
    _refuse_while_updating(res)  # before a run is published; _start_job checks again under the lock
    if any(t.is_alive() for t in [*_EVAL_JOBS.values(), *_JUDGE_JOBS.values()]):
        raise ServiceError("다른 평가가 실행 중입니다. 끝난 뒤 다시 시도하세요.")


def _start_job(res: Resources, jobs: dict[str, threading.Thread], run_id: str, thread: threading.Thread) -> None:
    """Registers and starts a paid background job; called under `_EVAL_LOCK` after its run was published."""
    with res._runner_lock:  # close() must see and join every thread that uses its transport
        if res._closed or res.paid_refusal():  # the run stays listed as partial; rerunning resumes it
            raise ServiceError(res.paid_refusal() or "서비스가 종료 중입니다.")
        _refuse_while_updating(res)
        jobs[run_id] = thread
        res._jobs.append(thread)
        thread.start()


# ---------------------------------------------------------------- judge comparison (judges.py)

def _judge_running() -> set[str]:
    return {run_id for run_id, t in _JUDGE_JOBS.items() if t.is_alive()}


def _judge_call(fn, *args):
    from . import answers
    from ..evaluation import judges

    try:
        return fn(*args)
    except (judges.JudgeError, answers.AnswerEvalError, budget.BudgetError) as exc:
        raise ServiceError(str(exc)) from None


def judge_overview(res: Resources, principal: Principal) -> dict:
    """The judge reference, its split, the declared replacement rule and every judge run with its progress."""
    principal = _authorize(res, principal, "verifier")
    from ..evaluation import judges

    return _judge_call(judges.overview, res.settings, _judge_running())


def plan_judges(res: Resources, principal: Principal, part: str) -> dict:
    """Free: prices every translation and Luna judge call a part still needs, and counts the Jev calls."""
    principal = _authorize(res, principal, "verifier")
    from ..evaluation import judges

    return _judge_call(judges.plan, res.settings, part)


def start_judges(res: Resources, principal: Principal, estimate_id: str) -> str:
    """Runs a planned judge comparison part on this process's gateway in one background thread, never above the
    estimate the verifier consented to. One evaluation or comparison at a time; rerunning resumes."""
    principal = _authorize(res, principal, "verifier")
    from ..evaluation import judges

    with _EVAL_LOCK:
        _refuse_closed_or_busy(res)
        # published before the start returns, so the overview lists the run on its very next read
        begun = _judge_call(judges.begin, res.settings, estimate_id, principal.member_id)
        run_id = begun["run_id"]

        def job() -> None:
            try:
                judges.run(res.settings, res.transport, estimate_id, principal.member_id,
                           closing=lambda: res._closed, begun=begun)
            except Exception as exc:  # noqa: BLE001 - recorded for the overview; finished judgements stay
                from ..storage.store import write_text_atomic

                write_text_atomic(judges.run_dir(res.settings, run_id) / "last-error.txt",
                                  f"{type(exc).__name__}: {exc}"[:500])

        _start_job(res, _JUDGE_JOBS, run_id, threading.Thread(target=contextvars.copy_context().run, args=(job,), name=f"rfp-judge-{run_id}", daemon=True))
    return run_id


_MAINTENANCE_JOB: list[threading.Thread] = []  # at most one maintenance sequence per process


def maintenance_status(res: Resources, principal: Principal) -> dict:
    """The running or last maintenance run. A run recorded as running with no live thread was cut off by a stop."""
    principal = _authorize(res, principal, "verifier")
    from ..storage import maintenance

    running = any(t.is_alive() for t in _MAINTENANCE_JOB)
    state = maintenance.last(res.settings)
    if state and state["status"] == "running" and not running:
        state = {**state, "status": "interrupted", "reason": state.get("reason") or "the process stopped mid-run"}
    return {"running": running, "run": state, "backup_root": str(maintenance.backup_root(res.settings))}


def start_maintenance(res: Resources, principal: Principal) -> str:
    """Starts the maintenance sequence on this process's gateway in one background thread; the run is published
    before the start returns. A paid embedding stops it at its estimate; nothing is activated."""
    principal = _authorize(res, principal, "verifier")
    from ..storage import maintenance

    with _EVAL_LOCK:
        if res._closed:
            raise ServiceError("서비스가 종료 중입니다.")
        _refuse_while_updating(res)
        if any(t.is_alive() for t in _MAINTENANCE_JOB):
            raise ServiceError("유지보수가 이미 실행 중입니다. 끝난 뒤 다시 시도하세요.")
        state = maintenance.start_state(principal.member_id)
        maintenance._write(res.settings, state)

        def job() -> None:
            maintenance.run(res.settings, res.analyzer, res.transport, principal.member_id,
                            closing=lambda: res._closed, state=state)

        thread = threading.Thread(target=contextvars.copy_context().run, args=(job,), name=f"rfp-maintenance-{state['run_id']}", daemon=True)
        with res._runner_lock:  # close() joins it like every other job that may use the transport
            if res._closed:
                raise ServiceError("서비스가 종료 중입니다.")
            _refuse_while_updating(res)
            _MAINTENANCE_JOB[:] = [thread]
            res._jobs.append(thread)
            thread.start()
    return state["run_id"]


def judge_results(res: Resources, principal: Principal, run_id: str) -> dict:
    principal = _authorize(res, principal, "verifier")
    from ..evaluation import judges

    return _judge_call(judges.results, res.settings, run_id)


EXPERIMENT_FACTS = ("licence", "revision", "dims", "context", "backend", "size_bytes", "peak_vram_mb", "precision",
                    "max_length", "layer", "batch", "cold_load_seconds", "chunks", "duplicated_tokens", "dense_version")


def _failed(q: dict) -> bool:
    return (not q["complete"] or q["critical"] or q.get("hit5") == 0
            or (q.get("ndcg") is not None and q["ndcg"] < 1))


def experiments(res: Resources, principal: Principal) -> dict:
    """Every comparison table (`compare --matrix`), each row's column values, which row serves, and the gold
    counts. Per-question outcomes stay out of this listing (`experiment_questions`)."""
    principal = _authorize(res, principal, "verifier")
    from ..evaluation import compare

    active = active_serving(res.settings)
    tables = []
    for t in compare.load_tables(res.settings):
        rows = []
        for i, r in enumerate(t["rows"]):
            questions = r.get("questions") or []
            rows.append({"index": i, "name": r["name"], "axes": r.get("axes") or {}, "status": r["status"],
                         "reason": r.get("reason"), "run_id": r.get("run_id"), "label": r.get("label"),
                         "values": {c["key"]: compare.value(r, c["key"]) for c in t["columns"]},
                         "facts": {k: r[k] for k in EXPERIMENT_FACTS if r.get(k) is not None},
                         "estimate": r.get("estimate"), "failures": sum(map(_failed, questions)),
                         "active": bool(r.get("run_id")) and r.get("run_id") == active.get("run_id")})
        tables.append({"matrix": t["matrix"], "title": t["title"], "created_at": t["created_at"],
                       "columns": t["columns"], "fixed": t.get("fixed") or {}, "populations": t["populations"],
                       "needs_evidence_review": t.get("needs_evidence_review") or [], "rows": rows,
                       **{k: t.get(k) for k in ("answer_run_id", "model", "conclusion")}})
    golden = compare.compare_dir(res.settings) / "golden-counts.json"
    reranker = active.get("reranker") or {}
    serving_detail = {"mode": active.get("mode"), "embedding": (active.get("embedding") or {}).get("model"),
                      "reranker": reranker.get("model"), "protect": reranker.get("protect") or 0,
                      "fallback": active.get("fallback_reason")}
    return {"tables": tables, "active_run_id": active.get("run_id"), "serving": describe_serving(active),
            "serving_detail": serving_detail, "golden_counts": json.loads(golden.read_text(encoding="utf-8")) if golden.exists() else None}


def experiment_questions(res: Resources, principal: Principal, matrix: str, index: int) -> list[dict]:
    """The questions one row failed: incomplete support, a critical failure, a missed top 5 or nDCG@5 below 1."""
    principal = _authorize(res, principal, "verifier")
    from ..evaluation import compare

    table = next((t for t in compare.load_tables(res.settings) if t["matrix"] == matrix), None)
    if table is None or not 0 <= index < len(table["rows"]):
        raise ServiceError("비교표나 행을 찾을 수 없습니다.")
    return [q for q in table["rows"][index].get("questions") or [] if _failed(q)]


def activate_experiment(res: Resources, principal: Principal, run_id: str, decided_by: str, note: str) -> dict:
    """The person's pick from a comparison table, through the same validation as `activate-run`."""
    principal = _authorize(res, principal, "verifier")
    from ..evaluation import evaluation

    if not decided_by.strip():
        raise ServiceError("활성화하려면 고른 사람의 이름이 필요합니다.")
    try:
        config, _ = evaluation.load_run(res.settings, run_id)
        decision = {"run_id": run_id, "mode": config["mode"], "decided_by": decided_by.strip(),
                    "rationale": note.strip(), "source": "실험 비교"}
        active = evaluation.activate_decision(res.settings, run_id, decision, f"verify:{principal.member_id}")
    except evaluation.EvaluationError as exc:
        raise ServiceError(f"활성화할 수 없습니다: {exc}") from None
    return {"run_id": active["run_id"], "mode": active["mode"], "activated_at": active["activated_at"]}


def judge_disagreements(res: Resources, principal: Principal, run_id: str) -> list[dict]:
    principal = _authorize(res, principal, "verifier")
    from ..evaluation import judges

    return _judge_call(judges.disagreements, res.settings, run_id)


def fidelity_overview(res: Resources, principal: Principal) -> list[dict]:
    """Every HWP source with its automatic verdict for the active extraction (None until checked)."""
    principal = _authorize(res, principal, "verifier")
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute(
            "SELECT s.source_hash, s.review_status, MIN(d.filename) AS filename, f.metrics_json, f.findings_json "
            "FROM sources s JOIN documents d ON d.active_source_hash = s.source_hash "
            "LEFT JOIN fidelity_checks f ON f.extraction_id = s.active_extraction_id AND f.method = ? "
            # at most one check per (extraction, method), so grouping by its columns is exact; PostgreSQL requires it
            "WHERE s.format = 'hwp' GROUP BY s.source_hash, s.review_status, f.metrics_json, f.findings_json "
            "ORDER BY filename", (fidelity.FIDELITY_VERSION,)).fetchall()
    return [{"source_hash": r["source_hash"], "filename": r["filename"], "review_status": r["review_status"],
             "metrics": json.loads(r["metrics_json"]) if r["metrics_json"] else None,
             "findings": json.loads(r["findings_json"]) if r["findings_json"] else []} for r in rows]


def rendered_page_png(res: Resources, principal: Principal, source_hash: str, page: int) -> bytes:
    """One page of the Hancom-printed PDF, so a person can look at exactly the reported place."""
    principal = _authorize(res, principal, "verifier")
    import pymupdf

    pdf = printed_pdf_path(res.settings, source_hash)
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash) or not pdf.exists():
        raise ServiceError("인쇄본이 없습니다. 먼저 자동 원문 대조를 실행하세요.")
    with pymupdf.open(pdf) as doc:
        if not 1 <= page <= doc.page_count:
            raise ServiceError("쪽 번호가 범위를 벗어났습니다.")
        return doc[page - 1].get_pixmap(dpi=110).tobytes("png")


def confirm_fidelity(res: Resources, principal: Principal, source_hash: str, note: str) -> str:
    """The person looked at every reported place and found the extraction faithful for answering."""
    principal = _authorize(res, principal, "verifier")
    row = fidelity.latest(res.settings, source_hash)
    if row is None:
        raise ServiceError("자동 원문 대조 결과가 없습니다.")
    findings = json.loads(row["findings_json"])
    locations = [{k: f.get(k) for k in ("side", "page", "element_id", "cell")} for f in findings]
    return record_review(res.settings, source_hash, principal.member_id, "sample_checked",
                         locations or [{"method": row["method"], "findings": 0}],
                         {"method": row["method"], "findings_confirmed": len(findings), "note": note})


def ingestion_overview(res: Resources, principal: Principal) -> list[dict]:
    principal = _authorize(res, principal, "verifier")
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute(
            "SELECT d.doc_id, d.filename, s.format, s.parse_status, s.review_status, s.reason_code, s.warnings_json "
            "FROM documents d JOIN sources s ON s.source_hash = d.active_source_hash ORDER BY d.csv_row_id").fetchall()
    return [{**dict(r), "warnings": [w.get("code") for w in json.loads(r["warnings_json"])]} for r in rows]


# ---------------------------------------------------------------- the screens' entry points
# api.py imports nothing from this package except this module and each route calls one function: every action
# and every decision that is not rendering lives here, so web/ stays presentation only (tests/test_api.py).

AuthError = auth.AuthError
REJECT_CATEGORIES = gold.REJECT_CATEGORIES
SHELL_ERRORS = (ServiceError, auth.AuthError)  # what a screen catches and shows instead of a traceback


def app_resources() -> Resources:
    from ..settings import load_settings

    return get_resources(load_settings())


def visitor(name: str | None) -> Principal:
    return auth.visitor(name)


Login, LoginError = auth.Login, auth.LoginError  # the API's sign-in (JupyterHub OAuth) and its refusals


@functools.lru_cache(maxsize=1)
def build_head() -> str:
    """The commit this server process loaded (read once), or `unknown` outside a checkout."""
    from ..settings import REPO_ROOT

    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    head = out.stdout.strip()
    return head if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", head) else "unknown"


# ---------------------------------------------------------------- updating to GitHub main (update.py, runbook 3.2)

UpdateWatch = update.UpdateWatch
UPDATING = "서버 업데이트가 진행 중이어서 새 유료 작업을 받지 않습니다. 업데이트가 끝난 뒤 다시 시도하세요. (유료 호출 없음)"


def _refuse_while_updating(res: Resources) -> None:
    if res.update_fence():
        raise ServiceError(UPDATING)


def open_paid_work(res: Resources) -> dict:
    """Whether a restart now would cut off paid work: an open reservation or attempt in the ledger, a queued or
    running request, or a live background job (drafting, evaluation, judge run, maintenance).

    Read in the order work leaves them: threads and request slots, then request rows, then the ledger last. Work
    writes its ledger rows before its row finishes and its thread or slot ends, so whatever ended before its count
    has already left its open attempts for the ledger read; reading the ledger first could miss both."""
    with res._runner_lock:
        jobs = sum(1 for t in res._jobs if t.is_alive())
        admitted = res._runner.admitted if res._runner is not None else 0  # counted before its queued row exists
    try:
        with open_db(res.settings.db_path) as conn:
            active = conn.execute("SELECT COUNT(*) FROM requests WHERE status IN ('queued', 'running')").fetchone()[0]
        snap = budget.snapshot(res.settings.db_path)
    except (*DATABASE_ERRORS, budget.BudgetError) as exc:
        raise ServiceError(f"ledger_unavailable: {type(exc).__name__}") from None
    active = max(active, admitted)
    reasons = []
    if active:
        reasons.append(f"질문 {active}건 실행 중")
    if jobs:
        reasons.append(f"백그라운드 작업 {jobs}건 실행 중")
    if snap.unknown_micro_usd:
        reasons.append("결과를 모르는 유료 호출이 정산을 기다리는 중")
    elif snap.open_attempts or snap.pending_micro_usd > 0:
        reasons.append(f"유료 호출 {snap.open_attempts}건 진행 중")
    return {"open": bool(reasons), "pending_micro_usd": snap.pending_micro_usd, "open_attempts": snap.open_attempts,
            "active_requests": active, "background_jobs": jobs,
            "reason": ("진행 중인 유료 작업이 끝나면 업데이트할 수 있습니다: " + ", ".join(reasons)) if reasons else None}


def update_status(res: Resources, principal: Principal, watch: update.UpdateWatch) -> dict:
    """What the header's update banner shows. Every signed-in member may read and request it."""
    _authorize(res, principal, "consultant", "verifier", "budget_admin")
    check = watch.check
    return {"configured": watch.configured, "running_commit": watch.running, "latest_commit": check.latest,
            "ahead_by": check.ahead_by, "commits": check.commits,
            "available": watch.configured and check.available, "checked_at": check.checked_at, "note": check.note,
            "paid_work": open_paid_work(res), "in_progress": watch.in_progress(), "last_result": watch.last_result()}


def request_update(res: Resources, principal: Principal, watch: update.UpdateWatch) -> dict:
    """Asks the root updater to move this server to the latest main. It takes no branch or commit: the updater
    fetches origin/main itself. Refused while paid work is open, so a restart never cuts one off."""
    principal = _authorize(res, principal, "consultant", "verifier", "budget_admin")
    if not watch.configured:
        raise ServiceError("이 서버에는 업데이트 장치가 설치되어 있지 않습니다.")
    # The fence goes up before paid work is counted, under the lock every paid admission holds: work admitted
    # before it is counted and refuses the update, work after it is refused. The marker then keeps it up until the
    # updater ends without a restart; a restart starts a new process, fenced while its result still says running.
    with res._runner_lock:
        if watch.fenced():
            raise ServiceError("업데이트가 이미 진행 중입니다.")
        watch.accepting = True
    try:
        work = open_paid_work(res)
        if work["open"]:
            raise ServiceError(work["reason"])
        watch.write_request(principal.member_id)
    finally:
        watch.accepting = False
    with open_db(res.settings.db_path) as conn, tx(conn):
        _audit(conn, principal.member_id, "request_update", "main", "header update button",
               {"running_commit": watch.running, "latest_commit": watch.check.latest})
    return update_status(res, principal, watch)


def target_key(scope: list[tuple[str, str]], question: str, mode: str, as_of: str, previous: str = "") -> str:
    """Identity of what a screen currently asks: selected (doc_id, source_hash) pairs, question, mode, date, and the
    conversation turn it follows."""
    data = {"scope": [list(x) for x in scope], "q": " ".join((question or "").split()), "mode": mode, "as_of": as_of,
            "previous": previous}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def may_attach(owned: dict | None, current_target: str, view) -> bool:
    """A request's outcome may render as the current answer only while the screen still asks exactly what it
    asked: same request, generation and target, and not cancelled. Otherwise it stays history."""
    return bool(owned and view is not None and owned.get("target") == current_target
                and owned.get("request_id") == view.request_id and owned.get("generation_id") == view.generation_id
                and not view.cancel_requested and view.status != "cancelled")


def visible_warnings(warnings: list[str]) -> list[str]:
    """Only the highest cap level (exhaustion replaces them all), then the other warnings."""
    caps = [w for w in warnings if w.startswith("cap_")]
    top = ["cap_exhausted"] if "cap_exhausted" in caps else caps[-1:]
    return top + [w for w in warnings if not w.startswith("cap_")]


def abandon_request(res: Resources, principal: Principal, request_id: str) -> None:
    """The screen no longer owns this request: cancel it only while it is still queued (never dispatched)."""
    if request_status(res, principal, request_id).status == "queued":
        cancel_request(res, principal, request_id)


def find_documents(res: Resources, principal: Principal, query: str = "", institution: str = "",
                   amount_min: int | None = None, amount_max: int | None = None, closing_from: str | None = None,
                   closing_to: str | None = None, limit: int = 100) -> list[dict]:
    """질문하기's document search. A document whose amount or closing date cannot be judged stays listed with its
    `filter_undecided` notes, and askable documents come first; the relevance order holds within each group."""
    items = search_projects(res, principal, {
        "institution": institution, "amount_min": amount_min, "amount_max": amount_max,
        "closing_from": closing_from, "closing_to": closing_to, "include_unknown": True}, query, limit=limit)
    return sorted(items, key=lambda i: not i["indexed"])


def ask(res: Resources, principal: Principal, scope: list[tuple[str, str]], question: str, mode: str,
        as_of: str, previous_request_id: str = "") -> dict:
    """One question from 질문하기 under a fresh generation ID, continuing the conversation of `previous_request_id`
    when given; returns the ownership record the screen keeps."""
    generation_id = str(uuid.uuid4())
    request_id = submit_answer(res, principal, AnswerRequest(
        idempotency_key=generation_id, generation_id=generation_id, question=question or "",
        scope=[DocRef(*x) for x in scope], mode=mode, as_of=as_of, previous_request_id=previous_request_id))
    return {"request_id": request_id, "generation_id": generation_id,
            "target": target_key(scope, question, mode, as_of, previous_request_id)}


def answer_progress(res: Resources, principal: Principal, request_id: str, generation_id: str) -> dict:
    """Read-only, for a streaming screen: whether the request finished, and the unvalidated answer written so far
    (None before generation starts, once it finished, or when this generation no longer owns it). The validated
    outcome comes only from `request_status`."""
    view = request_status(res, principal, request_id)
    content = res.partials.get(request_id)
    live = view.status == "running" and not view.cancel_requested and view.generation_id == generation_id
    return {"finished": view.status not in ("queued", "running"),
            "partial": generation.partial_answer(content) if live and content else None}


def trace_documents(res: Resources, principal: Principal) -> list[dict]:
    """Documents a verifier can trace: parsed and in the serving index."""
    return [i for i in search_projects(res, principal, {"parsed_only": True}, "", limit=100) if i["indexed"]]


def trace_questions(res: Resources, principal: Principal) -> list[dict]:
    """Reviewed development questions (gold, then the pilot set) to reproduce on the trace tab. Never sealed."""
    return dataset_rows(res, principal, "dev") + dataset_rows(res, principal, "dev-pilot")


def trace_modes(res: Resources) -> list[str]:
    """Retrieval modes a trace can run, the serving one first."""
    return list(dict.fromkeys([res.serving()["mode"], "kiwi_bm25", "whitespace_bm25", "dense", "hybrid",
                               "hybrid_rerank"]))


def run_trace(res: Resources, principal: Principal, question: str, scope: list[tuple[str, str]], as_of: str,
              mode: str) -> dict:
    """A frozen retrieval-only run at the settings' evidence limits; the serving mode is recorded as the default."""
    return verifier_trace(res, principal, question, [DocRef(*x) for x in scope], as_of,
                          mode=None if mode == res.serving()["mode"] else mode)


def generate_from_run(res: Resources, principal: Principal, run: dict) -> str:
    """The verifier's paid answer from a frozen run: its evidence, as-of date and configuration, one idempotent
    request per run however often the button is pressed."""
    key = f"vgen-{run['run_id']}"
    scope = [DocRef(s["doc_id"], s["source_hash"]) for s in run["scope"]]
    return submit_answer(res, principal, AnswerRequest(
        idempotency_key=key, generation_id=key, question=run["question"], scope=scope,
        mode="compare" if len(scope) == 2 else "single", as_of=run.get("as_of") or date.today().isoformat(),
        config_id=run["config"]["config_id"], verifier_run_id=run["run_id"]))


def generate_from_run_id(res: Resources, principal: Principal, run_id: str) -> str:
    """`generate_from_run` for a screen that holds only the run's ID."""
    return generate_from_run(res, principal, verifier_run(res, principal, run_id))


def run_generation_block(res: Resources, principal: Principal, run_id: str) -> str | None:
    """Why a paid answer cannot be generated from this frozen run now (None when it can): the model has no price,
    or the serving configuration changed since the run was frozen, so its estimate no longer applies."""
    run = verifier_run(res, principal, run_id)
    if run.get("estimate_micro_usd") is None:
        return "현재 요금표에 없는 모델이라 비용을 추정할 수 없어 유료 생성을 막습니다."
    try:
        resolve_verifier_config(res, run["config"]["config_id"])
    except ServiceError as exc:
        return str(exc)
    return None


def candidate_spans(c: dict) -> list[dict]:
    """Each evidence alternative of a gold candidate as its element text cut into segments, the cited span marked,
    so a draft can be read next to its original. The recorded offsets win; a quote found nowhere marks nothing."""
    offsets = {(g.get("group_id"), alt.get("element_id")): alt.get("offsets")
               for g in c["row"].get("evidence_groups") or [] for alt in g.get("alternatives") or []}
    spans = []
    for i, ev in enumerate(c["context"].get("evidence") or [], 1):
        text, quote = ev.get("text") or "", ev.get("quote") or ""
        off = offsets.get((ev.get("group_id"), ev.get("element_id")))
        if off and isinstance(off, list) and len(off) == 2 and text[off[0]:off[1]] == quote:
            start = off[0]
        else:
            start = text.find(quote) if quote else -1
        parts = [(text[:start], False), (quote, True), (text[start + len(quote):], False)] if start >= 0 \
            else [(text, False)]
        spans.append({"label": ev.get("group_id") or f"근거 {i}", "doc_id": ev.get("doc_id"),
                      "location": ev.get("location"), "quote": quote, "cited_found": start >= 0,
                      "missing": not text, "segments": [{"text": t, "cited": c} for t, c in parts if t]})
    return spans


# ---------------------------------------------------------------- dataset generation (development split only)
# A drafting run lives in <data>/drafts/<run_id>/: request.json (who asked, the plan, the consented maximum) and
# output/, written by drafting's generation loop. Its valid candidates enter review as gold batch <run_id>.

_DRAFT_JOBS: dict[str, threading.Thread] = {}
_DRAFT_LOCK = threading.Lock()
DRAFT_RUN_RE = re.compile(r"draft-\d{8}-[0-9a-f]{6}")


def _drafts_dir(settings: Settings) -> Path:
    return settings.data_dir / "drafts"


def candidate_review(res: Resources, principal: Principal, candidate_id: str) -> dict:
    """A pending candidate as the review screen reads it: the draft and its labels, who drafted it and who asked
    for the drafting (that person cannot approve), each scoped document with its review state and the PDF pages the
    draft points at, the cited spans inside their original element text, and the CSV metadata it relies on."""
    c = gold_candidate(res, principal, candidate_id)
    row, ctx = c["row"], c["context"]
    scope = row["scope"] if isinstance(row.get("scope"), list) else [
        {"doc_id": row.get("doc_id"), "source_hash": row.get("source_hash")}]
    documents = []
    for doc, sc in zip(ctx.get("documents") or [ctx.get("document")], scope):
        pages = [{"group_id": loc.get("group_id"), "pdf_pages": loc["pdf_pages"]}
                 for loc in row.get("review_locations") or []
                 if isinstance(loc, dict) and (loc.get("doc_id"), loc.get("source_hash")) == (sc.get("doc_id"),
                                                                                              sc.get("source_hash"))
                 and isinstance(loc.get("pdf_pages"), list) and loc["pdf_pages"]
                 and all(type(p) is int and p > 0 for p in loc["pdf_pages"])]
        documents.append({**(doc or {}), "found": doc is not None, "doc_id": sc.get("doc_id"),
                          "source_hash": sc.get("source_hash"), "pdf_pages": pages})
    meta = ctx.get("metadata") or {}
    metadata = [{"field": k, "value": v} for k, v in meta.items()] if not c["gold"] else [
        {"field": f"{d[:8]} {f}", "value": v} for d, fields in meta.items() for f, v in fields.items()]
    metadata += [{"field": f"충돌: {x.get('field')}", "value": x} for x in ctx.get("metadata_conflicts") or []]
    answer = row.get("expected_answer")
    return {"candidate_id": candidate_id, "dataset": c["dataset"], "gold": c["gold"], "row_sha256": c["row_sha256"],
            "drafted_by": c["drafted_by"], "requested_by": c["requested_by"],
            "type": row.get("question_type") or row.get("type"), "question": row.get("question") or "",
            "expected_answer": answer if isinstance(answer, str) and answer.strip() else None,
            "difficulty_reason": row.get("difficulty_reason"), "answerability": row.get("answerability"),
            "expected_status": row.get("expected_status"), "as_of_date": row.get("as_of_date"),
            "required_claims": row.get("required_claims") or [],
            "negative_validation": row.get("negative_validation") or {}, "current_errors": c["current_errors"],
            "documents": documents, "spans": candidate_spans(c), "metadata": metadata}


def _drafting_meta(settings: Settings, run_id: str | None) -> dict:
    if not run_id or not DRAFT_RUN_RE.fullmatch(run_id):
        return {}
    try:
        return json.loads((_drafts_dir(settings) / run_id / "request.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def draft_documents(res: Resources, principal: Principal) -> list[dict]:
    """Documents drafting may use: current, parsed and assigned to a development family. A sealed-family document
    never appears, so no sealed source reaches development generation through this page."""
    principal = _authorize(res, principal, "verifier")
    from ..evaluation import evaluation

    out = []
    with open_db(res.settings.db_path) as conn:
        checker = evaluation.GoldChecker(res.settings, conn)
        for doc_id, doc in checker.docs.items():
            fam = checker.fam_of_doc.get(doc_id)
            if not fam or fam[1]["split"] != "dev" or doc["parse_status"] != "parsed":
                continue
            ctx = gold._document_context(conn, doc_id) or {}
            out.append({"doc_id": doc_id, "source_hash": doc["active_source_hash"],
                        "extraction_id": doc["active_extraction_id"], "title": ctx.get("title"),
                        "institution": ctx.get("institution"), "filename": ctx.get("filename")})
    return sorted(out, key=lambda d: (d["title"] or "", d["doc_id"]))


def draft_elements(res: Resources, principal: Principal, doc_id: str, contains: str = "") -> list[dict]:
    """The source elements of one development document in reading order, optionally only those containing a
    phrase (whitespace-insensitive), to pick drafting sources from."""
    doc = next((d for d in draft_documents(res, principal) if d["doc_id"] == doc_id), None)
    if doc is None:
        raise ServiceError("개발용 문서가 아닙니다. 봉인 평가 문서와 원문 미수집 문서는 초안에 쓸 수 없습니다.")
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute("SELECT element_id, kind, raw_text, location_json FROM elements WHERE extraction_id = ? "
                            "ORDER BY source_order", (doc["extraction_id"],)).fetchall()
    needle = "".join(nfc(contains or "").split())
    return [{"element_id": r["element_id"], "kind": r["kind"], "text": r["raw_text"],
             "location": json.loads(r["location_json"])} for r in rows
            if not needle or needle in "".join(nfc(r["raw_text"] or "").split())]


def draft_slot(question_type: str, intent: str, doc_ids: list[str], sources: list[dict]) -> dict:
    """One question slot as a screen describes it, with a fresh question ID. Identities are resolved at planning."""
    from .drafting import TYPES

    if question_type not in TYPES:
        raise ServiceError("질문 유형을 고르세요.")
    if not (intent or "").strip():
        raise ServiceError("무엇을 묻는 질문인지(의도)를 적으세요.")
    if not 1 <= len(doc_ids) <= 2 or len(set(doc_ids)) != len(doc_ids):
        raise ServiceError("서로 다른 문서를 한두 개 고르세요.")
    if not sources or {s["doc_id"] for s in sources} != set(doc_ids):
        raise ServiceError("고른 문서마다 원문 구절을 하나 이상 고르세요.")
    return {"question_id": f"ui-{uuid.uuid4().hex[:10]}", "question_type": question_type, "intent": intent.strip(),
            "doc_ids": list(doc_ids),
            "sources": [{"doc_id": s["doc_id"], "element_id": s["element_id"]} for s in sources]}


def _draft_plan(res: Resources, principal: Principal, slots: list[dict]) -> dict:
    if not 1 <= len(slots) <= 50:
        raise ServiceError("질문 자리를 1~50개 만드세요.")
    docs = {d["doc_id"]: d for d in draft_documents(res, principal)}
    today = date.today().isoformat()
    plan = []
    for s in slots:
        scope = [docs.get(d) for d in s["doc_ids"]]
        if None in scope:
            raise ServiceError(f"{s['question_id']}: 개발용 문서가 아니거나 원문이 바뀌었습니다.")
        plan.append({"question_id": s["question_id"], "revision": 1, "question_type": s["question_type"],
                     "intent": s["intent"], "as_of_date": today, "sources": s["sources"],
                     "scope": [{k: d[k] for k in ("doc_id", "source_hash", "extraction_id")} for d in scope]})
    return {"slots": plan}


def plan_drafting(res: Resources, principal: Principal, slots: list[dict]) -> dict:
    """Free: the maximum a drafting run of these slots can cost (five slots per call, full output allowance),
    against the remaining `gold_eval` envelope and the cap. Nothing is sent."""
    principal = _authorize(res, principal, "verifier")
    from . import answers, drafting

    s = res.settings
    plan = _draft_plan(res, principal, slots)
    try:
        learned, resolved = drafting.lessons(s), drafting.source_slots(s, plan, "dev")
        fmt, total, calls = drafting.response_format(), 0, 0
        for start in range(0, len(resolved), 5):
            subset = resolved[start:start + 5]
            tokens = generation.count_request_tokens(drafting.messages(subset, learned), fmt, s.framing_margin_tokens)
            total += budget.estimate(s.db_path, drafting.MODEL, tokens, min(drafting.MAX_OUTPUT, 1200 * len(subset)))
            calls += 1
    except (gold.GoldError, budget.BudgetError) as exc:
        raise ServiceError(str(exc)) from None
    ledger = answers._ledger(s)
    room = min(ledger["envelope_remaining_micro_usd"], ledger["available_micro_usd"])
    return {"plan": plan, "calls": calls, "max_micro_usd": total, "paid_enabled": ledger["paid_enabled"],
            "envelope_remaining_micro_usd": ledger["envelope_remaining_micro_usd"],
            "available_micro_usd": ledger["available_micro_usd"],
            "fits": ledger["paid_enabled"] and total <= room}


def start_drafting(res: Resources, principal: Principal, slots: list[dict], consented_max_micro: int) -> str:
    """Runs gpt-6-luna drafting of these slots on this process's gateway in one background thread, never above
    the maximum the person consented to. Every call is reserved and settled in the shared ledger (`gold_eval`).
    The run only prepares pending candidates; nothing is approved here."""
    principal = _authorize(res, principal, "verifier")
    from . import drafting
    from ..storage.store import write_text_atomic

    est = plan_drafting(res, principal, slots)
    if est["max_micro_usd"] > consented_max_micro:
        raise ServiceError("최대 비용이 동의한 금액보다 커졌습니다. 다시 추정하세요.")
    if not est["fits"]:
        raise ServiceError("유료 호출이 꺼져 있거나 최대 비용이 평가 예산 또는 운영 한도를 넘습니다.")
    with _DRAFT_LOCK, res._runner_lock:
        if res._closed or res.paid_refusal():
            raise ServiceError(res.paid_refusal() or "서비스가 종료 중입니다.")
        _refuse_while_updating(res)
        if any(t.is_alive() for t in _DRAFT_JOBS.values()):
            raise ServiceError("다른 초안 생성이 실행 중입니다. 끝난 뒤 다시 시도하세요.")
        run_id = f"draft-{date.today():%Y%m%d}-{uuid.uuid4().hex[:6]}"
        run_dir = _drafts_dir(res.settings) / run_id
        run_dir.mkdir(parents=True)
        write_text_atomic(run_dir / "request.json", dumps({
            "run_id": run_id, "requested_by": principal.member_id, "created_at": utcnow(),
            "max_micro_usd": consented_max_micro, "plan": est["plan"]}))

        def job() -> None:
            try:  # this process already owns the gateway lock that drafting.generate would take
                drafting._generate(res.settings, est["plan"], run_dir / "output", consented_max_micro,
                                   res.transport, "dev", guard=lambda conn: "interrupted" if res._closed else None,
                                   tracer=res.tracing)
            except Exception as exc:  # noqa: BLE001 - recorded for the run list; settled calls stay settled
                name = "interrupted.txt" if res._closed else "error.txt"
                write_text_atomic(run_dir / name, f"{type(exc).__name__}: {exc}"[:500])

        thread = threading.Thread(target=contextvars.copy_context().run, args=(job,), name=f"rfp-{run_id}", daemon=True)
        _DRAFT_JOBS[run_id] = thread
        res._jobs.append(thread)
        thread.start()
    return run_id


def drafting_runs(res: Resources, principal: Principal) -> list[dict]:
    """Every drafting run, newest first: who asked, its state, valid and invalid drafts, and whether its valid
    drafts were sent to review."""
    principal = _authorize(res, principal, "verifier")
    from ..storage.store import read_jsonl

    base = _drafts_dir(res.settings)
    with open_db(res.settings.db_path) as conn:
        submitted = {r[0] for r in conn.execute("SELECT DISTINCT batch_id FROM gold_candidates")}
    runs = []
    for d in sorted(base.iterdir(), reverse=True) if base.exists() else []:
        if not DRAFT_RUN_RE.fullmatch(d.name):
            continue
        meta, out = _drafting_meta(res.settings, d.name), d / "output"
        try:
            receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8")) \
                if (out / "receipt.json").exists() else None
            rows = read_jsonl(out / "candidates.jsonl") if (out / "candidates.jsonl").exists() else []
            invalid = json.loads((out / "invalid.json").read_text(encoding="utf-8")) \
                if (out / "invalid.json").exists() else []
            interrupted = (d / "interrupted.txt").exists()
            error_path = d / ("interrupted.txt" if interrupted else "error.txt")
            error = error_path.read_text(encoding="utf-8") if error_path.exists() else None
        except (OSError, json.JSONDecodeError):
            continue
        running = d.name in _DRAFT_JOBS and _DRAFT_JOBS[d.name].is_alive()
        runs.append({"run_id": d.name, "requested_by": meta.get("requested_by"), "created_at": meta.get("created_at"),
                     "max_micro_usd": meta.get("max_micro_usd"),
                     "slots": len((meta.get("plan") or {}).get("slots") or []),
                     "status": "running" if running else "interrupted" if interrupted else "completed" if receipt else "failed" if error
                     else "interrupted",
                     "receipt": receipt, "rows": rows, "invalid": invalid, "error": error,
                     "submitted": d.name in submitted})
    return runs


def submit_drafts(res: Resources, principal: Principal, run_id: str) -> dict:
    """Sends a finished run's valid drafts to the development review queue as one batch drafted by the model."""
    principal = _authorize(res, principal, "verifier")
    from . import drafting
    from ..evaluation import evaluation

    run = next((r for r in drafting_runs(res, principal) if r["run_id"] == run_id), None)
    if run is None or run["status"] != "completed" or not run["rows"]:
        raise ServiceError("검토 대기열에 올릴 유효한 초안이 없습니다.")
    if run["submitted"]:
        raise ServiceError("이미 검토 대기열에 올린 초안입니다.")
    evaluation.assign_families(res.settings)
    try:
        return gold.submit(res.settings, _drafts_dir(res.settings) / run_id / "output" / "candidates.jsonl", run_id,
                           "dev", drafting.DRAFTER)
    except gold.GoldError as exc:
        raise ServiceError(str(exc)) from None
