"""Authenticated public functions, shared resource ownership and request orchestration.

Resources (Kiwi analyzer, loaded index, SDK transport, process-owner lock) are created once per process
by `Resources` and closed by `Resources.close()`. Sessions only supply the principal and the scope.
"""

from __future__ import annotations

import atexit
import dataclasses
import hashlib
import json
import re
import sqlite3
import threading
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path

from . import auth, budget, fidelity, generation, gold
from . import dense as dense_mod
from .auth import require_any
from .contracts import (AnswerRequest, AnswerResult, BudgetSnapshot, DocRef, EvidenceUnit, EvidenceView,
                        ManagedDownload, Principal, RequestView, RetrievalResult)
from .ingestion import (CODE_RE, QUARANTINE_TEXT, load_elements, nfc, printed_pdf_path, record_review,
                        resolutions_by_doc)
from .retrieval import DENSE_MODES, Analyzer, KeywordIndex, best_chunk_per_extraction, scope_rows
from .retrieval import retrieve as _retrieve
from .evaluation import EVAL_VERSION
from .settings import Settings, read_api_key
from .store import LockHeld, ProcessLock, dumps, init_schema, open_db, tx, utcnow


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
                 recover: bool = False) -> None:
        """`recover=True` claims exclusive ownership of the data directory (the serving application does; a real
        gateway always does) and resolves what a previous process left: unfinished requests become interrupted,
        dispatching attempts unknown, never-dispatched reservations released. Nothing is replayed."""
        self.settings = settings
        init_schema(settings.db_path)
        budget.ensure_budget_row(settings.db_path)
        self._lock: ProcessLock | None = None
        self.transport: generation.Transport | None = transport
        self.provider_note = ""
        self.recovered: dict = {}
        if recover:
            self._own(settings)
        if transport is None and settings.provider == "fake":
            self.transport = generation.FakeTransport(delay_seconds=settings.fake_delay_seconds)
        elif transport is None:
            key = read_api_key("OPENAI_API_KEY")
            if key:
                if self._lock is None:
                    self._own(settings)
                self.transport = generation.OpenAITransport(key, settings.request_timeout_seconds)
            else:
                self.provider_note = "OPENAI_API_KEY is not configured; paid generation is unavailable"
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
        self._closed = False
        closer = weakref.WeakMethod(self.close)  # atexit must not keep every Resources (and its index) alive
        atexit.register(lambda: (closer() or (lambda: None))())

    def _own(self, settings: Settings) -> None:
        try:
            self._lock = ProcessLock(settings.data_dir / "gateway.lock")
        except LockHeld:
            raise GatewayLockError(
                "another process already owns the paid gateway for this data directory") from None
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
        """The activated retrieval configuration (`activate-run`), or the keyword default before any selection."""
        from .store import get_app_setting

        with open_db(self.settings.db_path) as conn:
            run = get_app_setting(conn, "active_run")
            active = get_app_setting(conn, "active_index")
        if run:
            cfg = json.loads(run)
            if cfg["mode"] == "hybrid_rerank" and cfg.get("eval_version") != EVAL_VERSION:
                # Promoted under a superseded gate: keep hybrid retrieval, drop the reranker until a current
                # trial passes and is activated.
                cfg = {**cfg, "mode": "hybrid", "reranker": None, "stale_policy": cfg.get("eval_version")}
            return cfg
        return {"run_id": None, "mode": self.settings.retrieval_mode, "index_version": active,
                "dense_version": None, "reranker": None, "fallback_mode": "kiwi_bm25"}

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
        if runner is not None:
            runner.shutdown(self.settings.shutdown_wait_seconds)
        if self.transport is not None:
            self.transport.close()
        if self._lock is not None:
            self._lock.release()


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

    def try_admit(self) -> bool:
        return self._accepting and self._slots.acquire(blocking=False)

    def release(self) -> None:
        self._slots.release()

    def submit(self, request_id: str) -> None:
        with self._lock:
            if not self._accepting:
                raise ServiceError("service is shutting down")
            self._futures[request_id] = self._executor.submit(self._run, request_id)

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


def get_resources(settings: Settings) -> Resources:
    """The serving application's single owner of the data directory."""
    global _shared
    with _shared_lock:
        if _shared is None or _shared._closed:
            _shared = Resources(settings, recover=True)
        return _shared


def _authorize(res: Resources, principal: Principal | None, *capabilities: str) -> Principal:
    """Revalidates a session principal (expiry, revocation, current account capabilities) and checks one of the
    capabilities. Every public function calls this, including cached and read-only ones."""
    current = auth.refresh(res.settings, principal)
    return require_any(current, *capabilities)


# ---------------------------------------------------------------- scope and metadata


def _doc_rows(res: Resources, doc_ids: list[str] | None = None) -> list[dict]:
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
        d["meta"] = json.loads(d.pop("normalized_metadata_json"))
        d["quality"] = json.loads(d.pop("quality_json"))
        d["resolutions"] = resolutions.get(d["doc_id"], {})
        # Effective view: a written owner/reviewer resolution replaces the CSV value for filtering, ranking and
        # display; d["meta"] keeps the CSV values and d["resolutions"] the provenance.
        d["effective"] = {**d["meta"], **{f: r["value"] for f, r in d["resolutions"].items()}}
        d.pop("raw_metadata_json")
        out.append(d)
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


def search_projects(res: Resources, principal: Principal, filters: dict, query: str, limit: int = 20) -> list[dict]:
    """Free route: metadata filters plus keyword snippets. Never calls a paid model."""
    principal = _authorize(res, principal, "consultant", "verifier")
    docs = _doc_rows(res)
    inst = nfc(filters.get("institution") or "").strip()
    amount_min, amount_max = filters.get("amount_min"), filters.get("amount_max")
    close_from, close_to = filters.get("closing_from"), filters.get("closing_to")
    parsed_only = filters.get("parsed_only", False)
    items = []
    for d in docs:
        m = d["effective"]  # the competing CSV values stay visible in the result
        conflicts = {c["field"] for c in d["quality"].get("provenance_conflicts", [])} - set(d["resolutions"])
        if inst and inst not in (m["institution"] or ""):
            continue
        if amount_min is not None or amount_max is not None:  # unknown amounts never satisfy a range
            if m["amount_krw"] is None or "amount_krw" in conflicts:
                continue
            if amount_min is not None and m["amount_krw"] < amount_min:
                continue
            if amount_max is not None and m["amount_krw"] > amount_max:
                continue
        if close_from or close_to:
            if m["bid_close"] is None or "bid_close" in conflicts:
                continue
            day = m["bid_close"]["value"][:10]
            if (close_from and day < close_from) or (close_to and day > close_to):
                continue
        if parsed_only and d["parse_status"] != "parsed":
            continue
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
        "resolutions": d["resolutions"], "snippet": snip,
    } for _, _, d, snip in ranked[:limit]]


LIMIT_KEYS = ("evidence_max_units", "evidence_target_tokens", "evidence_max_tokens", "channel_top_k",
              "fused_top_k")


def retrieve(res: Resources, principal: Principal, question: str, scope: list[DocRef], *,
             request_id: str | None = None, allow_paid: bool = False, limits: dict | None = None,
             mode: str | None = None) -> RetrievalResult:
    """Serves the activated mode. A dense query vector comes from the cache, or through the gateway only when
    the caller is a paid request (`allow_paid`); an empty scope never pays for one. `limits` may only narrow the
    evidence/depth limits (comparison sides, verifier configurations); `mode` lets a verifier pick a free mode."""
    principal = _authorize(res, principal, "consultant", "verifier")
    docs = _resolve_scope(res, scope)
    idx = res.index()
    if idx is None:
        raise ServiceError("검색 색인이 아직 없습니다.")
    cfg = res.serving()
    if mode is not None and mode != cfg["mode"]:
        if mode not in ("whitespace_bm25", "kiwi_bm25") + tuple(DENSE_MODES):
            raise ServiceError("알 수 없는 검색 방식입니다.")
        cfg = {**cfg, "mode": mode}
    s = res.run_settings(cfg)
    if limits:
        unknown = set(limits) - set(LIMIT_KEYS)
        if unknown:
            raise ServiceError(f"unknown retrieval limits {sorted(unknown)}")
        narrowed = {k: max(1, min(int(v), getattr(s, k))) for k, v in limits.items()}
        narrowed["evidence_target_tokens"] = min(narrowed.get("evidence_target_tokens", s.evidence_target_tokens),
                                                 narrowed.get("evidence_max_tokens", s.evidence_max_tokens))
        s = s.with_(**narrowed)
    pairs = [(DocRef(d["doc_id"], d["active_source_hash"]), d["active_extraction_id"]) for d in docs]
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
                                                 member_id=principal.member_id, purpose="interactive",
                                                 allow_paid=allow_paid)
    result = _retrieve(s, idx, res.analyzer, question, pairs, mode=cfg["mode"], dense=dense,
                       query_vector=qvec, reranker=res.reranker() if cfg["mode"] == "hybrid_rerank" else None,
                       rerank_depth=(cfg.get("reranker") or {}).get("depth"))
    result.query_embedding = qinfo
    from .retrieval import index_compatibility

    outdated = index_compatibility(idx, res.analyzer)
    if outdated:  # still served (search must keep working) but never silently: rebuild and re-evaluate
        result.limitations.append(f"index_outdated:{outdated}")
    wanted = {"dense"} | ({"reranker"} if cfg["mode"] == "hybrid_rerank" else set())
    if cfg["mode"] in DENSE_MODES:
        result.limitations += [f"{stage}_unavailable" for stage in res.stage_errors if stage in wanted]
    return result


# ---------------------------------------------------------------- answers


def _config_snapshot(res: Resources, request: AnswerRequest) -> dict:
    idx = res.index()
    with open_db(res.settings.db_path) as conn:
        rate_version = conn.execute("SELECT rate_version FROM budget_settings WHERE id = 1").fetchone()[0]
    return {"config_id": request.config_id, "index_version": idx.version if idx else None,
            "serving": {k: v for k, v in res.serving().items() if k in ("run_id", "mode", "dense_version")},
            "prompt_version": generation.PROMPT_VERSION, "model": res.settings.generation_model,
            "reasoning_effort": res.settings.generation_reasoning_effort,
            "rate_version": rate_version, "max_output_tokens": res.settings.generation_max_output_tokens,
            "settings": res.settings.fingerprint()}


def _input_hash(request: AnswerRequest) -> str:
    data = {"q": nfc(request.question), "scope": [asdict(s) for s in request.scope], "mode": request.mode,
            "as_of": request.as_of, "config": request.config_id}
    return hashlib.sha256(dumps(data).encode()).hexdigest()


BILLING_PRECEDENCE = ("unknown", "pending", "reconciled", "settled", "released")
PAID_MODES = ("single", "compare")
FREE_MODES = ("metadata", "inventory")


class _Stop(Exception):
    """A checkpoint before a paid stage refused to continue (cancellation or an ended session)."""

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
    return result


def _evidence_map(evidence: list[EvidenceUnit]) -> dict[str, dict]:
    return {e.evidence_id: asdict(e) for e in evidence}


def prepare_answer(res: Resources, principal: Principal, question: str, scope: list[DocRef], as_of: str, *,
                   request_id: str | None = None, allow_paid: bool = False) -> dict:
    """Retrieval plus exact prompt counting and the maximum reservation estimate. Free unless `allow_paid`
    lets a dense mode pay for an uncached query embedding."""
    principal = _authorize(res, principal, "consultant", "verifier")
    docs = _resolve_scope(res, scope)
    retrieval = retrieve(res, principal, question, scope, request_id=request_id, allow_paid=allow_paid)
    return _priced(res, question, as_of, docs, retrieval, "single")


def _priced(res: Resources, question: str, as_of: str, docs: list[dict], retrieval: RetrievalResult,
            mode: str) -> dict:
    messages = generation.build_messages(question, as_of, [_doc_brief(d) for d in docs], retrieval.evidence,
                                         retrieval.limitations, mode=mode)
    rf = generation.answer_json_schema()
    tokens = generation.count_request_tokens(messages, rf, res.settings.framing_margin_tokens)
    try:  # an unknown rate leaves the estimate empty; admission then refuses with `unknown_rate`
        est = budget.estimate(res.settings.db_path, res.settings.generation_model, tokens,
                              res.settings.generation_max_output_tokens)
        qe = retrieval.query_embedding or {}
        if qe.get("cache") == "miss" and not qe.get("attempt_id"):  # a paid answer would embed the query first
            est += budget.estimate(res.settings.db_path, res.settings.embedding_model,
                                   dense_mod.count_embedding_tokens(question) + dense_mod.QUERY_MARGIN_TOKENS, 0)
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
    if request.mode != "inventory" and (not question or len(question) > s.question_max_characters):
        raise ServiceError(f"질문은 1~{s.question_max_characters}자여야 합니다.")
    if len(question) > s.question_max_characters:
        raise ServiceError(f"질문은 {s.question_max_characters}자를 넘을 수 없습니다.")
    refs = [(r.doc_id, r.source_hash) for r in request.scope]
    if len(set(refs)) != len(refs):
        raise ServiceError("같은 문서를 두 번 선택했습니다.")
    if request.mode == "compare" and len(refs) != 2:
        raise ServiceError("비교 질문은 문서를 정확히 두 개 선택해야 합니다.")
    if request.mode == "metadata" and not 1 <= len(refs) <= 2:
        raise ServiceError("기본 정보 조회는 문서를 한두 개 선택해야 합니다.")
    if not request.idempotency_key or len(request.idempotency_key) > 100 or len(request.generation_id) > 100:
        raise ServiceError("요청 식별자가 올바르지 않습니다.")
    return question


def _request_snapshot(principal: Principal, request: AnswerRequest, question: str) -> dict:
    return {"question": question, "scope": [asdict(r) for r in request.scope], "mode": request.mode,
            "as_of": request.as_of, "config_id": request.config_id, "generation_id": request.generation_id,
            "idempotency_key": request.idempotency_key, "capabilities": sorted(principal.capabilities)}


def _create(res: Resources, principal: Principal, request: AnswerRequest, question: str, status: str,
            before_insert=None) -> tuple[str, sqlite3.Row | None]:
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
            "scope_json, status, trace_json, created_at, updated_at, request_json, session_id, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, principal.member_id, request.idempotency_key, request.generation_id, input_hash,
             hashlib.sha256(dumps(config).encode()).hexdigest(), dumps([asdict(x) for x in request.scope]), status,
             dumps({"config": config}), utcnow(), utcnow(), dumps(_request_snapshot(principal, request, question)),
             principal.session_id, request.mode))
    return request_id, None


def answer(res: Resources, principal: Principal, request: AnswerRequest) -> AnswerResult:
    """Synchronous worker entry (CLI, tests): the same execution as a background request, on this thread."""
    principal = _authorize(res, principal, "consultant", "verifier")
    question = _validate_request(res, request)
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
    """Worker body: claims queued -> running exactly once, rebuilds the principal from the persisted snapshot,
    revalidates its session and executes. Never touches UI state."""
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        claimed = conn.execute("UPDATE requests SET status = 'running', updated_at = ? WHERE request_id = ? "
                               "AND status = 'queued' AND cancel_requested = 0", (utcnow(), request_id)).rowcount
        row = conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
    if not claimed:
        return
    snap = json.loads(row["request_json"])
    request = AnswerRequest(idempotency_key=snap["idempotency_key"], generation_id=snap["generation_id"],
                            question=snap["question"], scope=[DocRef(**r) for r in snap["scope"]],
                            mode=snap["mode"], as_of=snap["as_of"], config_id=snap["config_id"])
    principal = Principal(row["member_id"], frozenset(snap["capabilities"]), row["session_id"])
    _execute(res, principal, request_id, request, snap["question"])


def _checkpoint(res: Resources, request_id: str, principal: Principal) -> Principal:
    """Before every newly dispatched paid stage: a cancelled request or an ended session stops here. Already
    dispatched work still settles; this only prevents the next dispatch."""
    with open_db(res.settings.db_path) as conn:
        cancelled = conn.execute("SELECT cancel_requested FROM requests WHERE request_id = ?",
                                 (request_id,)).fetchone()[0]
    if cancelled:
        raise _Stop("cancelled", "cancelled", "요청이 취소되어 다음 유료 단계를 시작하지 않았습니다.")
    try:
        return _authorize(res, principal, "consultant", "verifier")
    except auth.AuthError:
        raise _Stop("auth_required", "failed",
                    "로그인이 만료되었거나 권한이 바뀌어 다음 유료 단계를 시작하지 않았습니다.") from None


def _execute(res: Resources, principal: Principal, request_id: str, request: AnswerRequest,
             question: str) -> AnswerResult:
    trace: dict = {"config": _config_snapshot(res, request), "question": question, "as_of": request.as_of,
                   "mode": request.mode}

    def done(status: str, summary: str, request_status: str = "completed", **kw) -> AnswerResult:
        result = AnswerResult(request_id, status, summary, generation_id=request.generation_id, mode=request.mode,
                              **kw)
        return _finish(res, request_id, result, trace, request_status)

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


def _paid_answer(res: Resources, principal: Principal, request_id: str, request: AnswerRequest, question: str,
                 trace: dict, done) -> AnswerResult:
    s = res.settings
    docs = _resolve_scope(res, request.scope)
    unavailable = [d for d in docs if d["parse_status"] != "parsed" or not _indexed(res, d["active_extraction_id"])]
    missing_unavailable = [{"doc_id": d["doc_id"], "field": "document", "reason": "ingestion_unavailable"}
                           for d in unavailable]
    if len(unavailable) == len(docs):
        reason = QUARANTINE_TEXT.get(unavailable[0]["reason_code"] or "",
                                     "이 문서는 아직 검색 색인에 포함되지 않았습니다.")
        return done("ingestion_unavailable", f"원문 전체를 확인할 수 없어 답변하지 않습니다. {reason}",
                    missing_fields=missing_unavailable)
    principal = _checkpoint(res, request_id, principal)  # the query embedding may be the first paid stage
    try:
        if request.mode == "compare":
            prep = _prepare_compare(res, principal, question, docs, request.as_of, request_id)
        else:
            prep = prepare_answer(res, principal, question, request.scope, request.as_of, request_id=request_id,
                                  allow_paid=True)
    except _Stop:
        raise
    except Exception as exc:  # noqa: BLE001
        return done("technical_error", "검색 또는 비용 추정에 실패했습니다.", request_status="failed",
                    error=f"{type(exc).__name__}: {exc}"[:300])
    retrieval: RetrievalResult = prep["retrieval"]
    trace["retrieval"] = asdict(retrieval)
    trace["input_tokens_estimate"] = prep["input_tokens"]
    coverage = prep.get("coverage") or [{"doc_id": d["doc_id"], "evidence": len(retrieval.evidence)} for d in docs]
    server_missing = missing_unavailable + [
        {"doc_id": c["doc_id"], "field": "answer", "reason": "not_found_in_context"}
        for c in coverage if c.get("evidence") == 0 and c["doc_id"] not in {d["doc_id"] for d in unavailable}]
    evidence_map = _evidence_map(retrieval.evidence)
    common = {"coverage": coverage, "limitations": retrieval.limitations}
    if not retrieval.evidence:
        return done("insufficient_evidence", "선택한 문서에서 질문과 관련된 근거를 찾지 못했습니다.",
                    missing_fields=server_missing, **common)
    principal = _checkpoint(res, request_id, principal)
    admission = budget.reserve(s.db_path, request_id=request_id, member_id=principal.member_id, stage="generation",
                               purpose="interactive", model=s.generation_model, input_tokens=prep["input_tokens"],
                               max_output_tokens=s.generation_max_output_tokens, count_method=generation.COUNT_METHOD)
    trace["admission"] = admission
    if not admission["admitted"]:
        return done("budget_blocked", "공유 사용 한도 또는 유료 호출 설정 때문에 답변 생성을 시작하지 않았습니다. "
                    "검색과 원문 열람은 계속 사용할 수 있습니다.", evidence=evidence_map, error=admission["reason"],
                    **common)
    attempt_id = admission["attempt_id"]
    try:
        _checkpoint(res, request_id, principal)
    except _Stop:
        budget.release(s.db_path, attempt_id, "stopped_before_dispatch")
        raise
    if res.transport is None:
        budget.release(s.db_path, attempt_id, "provider_unavailable")
        return done("technical_error", "유료 모델 연결이 설정되지 않았습니다.", request_status="failed",
                    evidence=evidence_map, error=res.provider_note, **common)
    budget.mark_dispatching(s.db_path, attempt_id)
    try:
        response = res.transport.chat(model=s.generation_model, messages=prep["messages"],
                                      response_format=prep["response_format"],
                                      max_completion_tokens=s.generation_max_output_tokens,
                                      reasoning_effort=s.generation_reasoning_effort)
    except generation.ProviderError as exc:
        if exc.pre_execution:
            budget.release(s.db_path, attempt_id, str(exc)[:300], confirmed_pre_execution=True)
        else:
            budget.mark_unknown(s.db_path, attempt_id, str(exc))
        return done("technical_error", "모델 호출에 실패했습니다. 자동으로 다시 시도하지 않습니다.",
                    request_status="failed", evidence=evidence_map, error=str(exc)[:300], **common)
    except Exception as exc:  # noqa: BLE001 - e.g. a transport closed by shutdown: execution may have happened
        budget.mark_unknown(s.db_path, attempt_id, f"{type(exc).__name__}: {exc}")
        return done("technical_error", "모델 호출 중 연결이 끊겼습니다. 비용은 확인 전까지 보류로 남습니다.",
                    request_status="failed", evidence=evidence_map, error=f"{type(exc).__name__}: {exc}"[:300],
                    **common)
    if response.usage is None:
        budget.mark_unknown(s.db_path, attempt_id, "provider returned no usage")
    else:
        trace["usage"] = response.usage
        try:
            trace["settlement"] = budget.settle(s.db_path, attempt_id, response.usage, response.response_id)
        except Exception as exc:  # noqa: BLE001 - e.g. a reused response ID or a locked ledger
            # The call was billed but could not be recorded: keep it conservatively pending for the recovery view.
            budget.mark_unknown(s.db_path, attempt_id, f"settlement_failed: {type(exc).__name__}: {exc}")
            return done("technical_error", "사용량을 기록하지 못했습니다. 비용은 확인 전까지 보류로 남습니다.",
                        request_status="failed", evidence=evidence_map,
                        error=f"settlement_failed: {type(exc).__name__}"[:300], **common)
    stored = {e.evidence_id: _stored_quote(res, e) for e in retrieval.evidence}
    represented = {c["doc_id"] for c in coverage if c.get("evidence")}
    try:
        payload = generation.validate_answer(response, retrieval.evidence, {d["doc_id"] for d in docs}, stored,
                                             required_doc_ids=represented if request.mode == "compare" else None)
    except generation.TechnicalError as exc:
        trace["raw_output"] = (response.content or "")[:4000]
        return done("technical_error", "모델 응답을 검증하지 못했습니다. 비용은 기록되었으며 자동 재시도는 하지 않습니다.",
                    request_status="failed", evidence=evidence_map, error=str(exc)[:300], **common)
    missing = [m.model_dump() for m in payload.missing_fields]
    seen = {(m["doc_id"], m["field"]) for m in missing}
    missing += [m for m in server_missing if (m["doc_id"], m["field"]) not in seen]
    return done(payload.status, payload.summary, claims=[c.model_dump() for c in payload.claims],
                missing_fields=missing, conflicts=[c.model_dump() for c in payload.conflicts],
                next_action=payload.next_action, evidence=evidence_map, **common)


# ---------------------------------------------------------------- balanced two-document comparison


def _prepare_compare(res: Resources, principal: Principal, question: str, docs: list[dict], as_of: str,
                     request_id: str | None, allow_paid: bool = True) -> dict:
    """One scoped subquery per selected document (the same question; no paid rewriting). Each starts with up to
    three evidence units and half the evidence target; unused room is redistributed only after both had a
    coverage attempt, and the global ceiling still holds. Each side reports evidence or its limitation."""
    s = res.run_settings()
    half_units = max(1, s.evidence_max_units // 2)
    target, ceiling = s.evidence_target_tokens, s.evidence_max_tokens
    first = {"evidence_max_units": half_units, "evidence_target_tokens": target // 2,
             "evidence_max_tokens": ceiling // 2}
    sides: dict[str, RetrievalResult | None] = {}
    for d in docs:
        if d["parse_status"] != "parsed" or not _indexed(res, d["active_extraction_id"]):
            sides[d["doc_id"]] = None
            continue
        sides[d["doc_id"]] = retrieve(res, principal, question, [DocRef(d["doc_id"], d["active_source_hash"])],
                                      request_id=request_id, allow_paid=allow_paid, limits=first)
    used = lambda: sum(r.evidence_tokens for r in sides.values() if r)  # noqa: E731
    units = lambda: sum(len(r.evidence) for r in sides.values() if r)  # noqa: E731
    for d in docs:  # second pass: a side that was cut by its half budget may use what the other left
        r = sides[d["doc_id"]]
        if r is None or not any(x["reason"] in ("token_budget", "unit_limit") for x in r.excluded):
            continue
        spare_tokens, spare_units = target - used(), s.evidence_max_units - units()
        if spare_tokens <= 0 and spare_units <= 0:
            continue
        others = used() - r.evidence_tokens
        wider = {"evidence_max_units": len(r.evidence) + max(0, spare_units),
                 "evidence_target_tokens": max(1, r.evidence_tokens + max(0, spare_tokens)),
                 "evidence_max_tokens": max(1, ceiling - others)}
        wider["evidence_target_tokens"] = min(wider["evidence_target_tokens"], wider["evidence_max_tokens"])
        again = retrieve(res, principal, question, [DocRef(d["doc_id"], d["active_source_hash"])],
                         request_id=request_id, allow_paid=allow_paid, limits=wider)
        if len(again.evidence) >= len(r.evidence) and used() - r.evidence_tokens + again.evidence_tokens <= ceiling:
            sides[d["doc_id"]] = again
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
    if total > ceiling:  # defensive: never exceed the global hard ceiling after expansion
        raise ServiceError("비교 근거가 전체 한도를 넘었습니다. 질문을 좁혀 주세요.")
    first_r = next((r for r in sides.values() if r), None)
    merged = RetrievalResult(
        mode=first_r.mode if first_r else res.serving()["mode"], scope=[DocRef(d["doc_id"], d["active_source_hash"])
                                                                        for d in docs],
        exact_matches=[c for c in candidates if c["channel"] == "exact"], candidates=candidates, evidence=evidence,
        excluded=excluded, limitations=["compare:balanced_per_document"] + limitations, evidence_tokens=total,
        timings_ms=timings, trace_id=str(uuid.uuid4()), index_version=first_r.index_version if first_r else None,
        ranking=ranking, fallback=first_r.fallback if first_r else None,
        dense_version=first_r.dense_version if first_r else None, query_embedding=query_embedding)
    prep = _priced(res, question, as_of, docs, merged, "compare")
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


def _view(conn, row: sqlite3.Row) -> RequestView:
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
        path = Path(conn.execute("SELECT original_path FROM sources WHERE source_hash = ?",
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
    except (sqlite3.Error, budget.BudgetError) as exc:
        raise ServiceError(f"ledger_unavailable: {type(exc).__name__}") from None


# ---------------------------------------------------------------- verifier runs, corrections and exports

VERIFIER_MODES = ("whitespace_bm25", "kiwi_bm25", "dense", "hybrid", "hybrid_rerank")


def verifier_config(res: Resources, mode: str | None = None, limits: dict | None = None) -> dict:
    """A versioned, immutable verifier configuration: the serving snapshot plus explicit overrides. Editing a
    control produces another config ID; an old run keeps its own snapshot."""
    idx = res.index()
    serving = res.serving()
    cfg = {"serving_run": serving.get("run_id"), "serving_mode": serving["mode"], "mode": mode or serving["mode"],
           "dense_version": serving.get("dense_version"), "index_version": idx.version if idx else None,
           "limits": {k: int(v) for k, v in sorted((limits or {}).items())},
           "prompt_version": generation.PROMPT_VERSION, "model": res.settings.generation_model}
    if cfg["mode"] not in VERIFIER_MODES:
        raise ServiceError("알 수 없는 검색 방식입니다.")
    cfg["config_id"] = "vc-" + hashlib.sha256(dumps(cfg).encode()).hexdigest()[:12]
    return cfg


def _embedding_estimate(res: Resources, question: str) -> int | None:
    try:
        return budget.estimate(res.settings.db_path, res.settings.embedding_model,
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
        prep = _prepare_compare(res, principal, question, docs, as_of, None, allow_paid=False)
    elif len(scope) == 1:
        r = retrieve(res, principal, question, scope, limits=limits or None, mode=mode)
        prep = _priced(res, question, as_of, docs, r, "single")
    else:
        raise ServiceError("문서를 한두 개 선택하세요.")
    r: RetrievalResult = prep["retrieval"]
    idx = res.index()
    qe = r.query_embedding or {}
    trace = {"retrieval": asdict(r), "query_tokens": res.analyzer.tokens(question),
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
    from .store import read_jsonl

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


_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)")
_PATH_RE = re.compile(r"([A-Za-z]:\\[^\s\"']+|/(?:home|root|Users|tmp|mnt|var)/[^\s\"']+)")
_REDACT_KEYS = {"session_id", "original_path", "artifact_path", "messages", "token", "token_sha256", "api_key"}


def redact(value):
    """Exports: drop credentials, sessions and unrestricted local paths; keep stable IDs, hashes and locations."""
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
                               list(record.get("covered_attempt_ids") or []))
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


def members_overview(res: Resources, principal: Principal) -> dict:
    principal = _authorize(res, principal, "budget_admin")
    with open_db(res.settings.db_path) as conn:
        active = {r[0]: r[1] for r in conn.execute(
            "SELECT member_id, COUNT(*) FROM sessions WHERE revoked_at IS NULL AND expires_at > ? GROUP BY member_id",
            (auth.clock().isoformat(),))}
    return {"mode": auth.mode(res.settings),
            "members": [{**m, "active_sessions": active.get(m["member_id"], 0)}
                        for m in auth.list_members(res.settings)]}


def revoke_member_sessions(res: Resources, principal: Principal, member_id: str, reason: str) -> int:
    principal = _authorize(res, principal, "budget_admin")
    _require_reason(reason)
    count = auth.revoke_sessions(res.settings, member_id, reason)
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        _audit(conn, principal.member_id, "revoke_sessions", member_id, reason, {"sessions": count})
    return count


def gold_queue(res: Resources, principal: Principal) -> dict:
    principal = _authorize(res, principal, "verifier")
    return {"pending": gold.queue(res.settings), "recent": gold.recent(res.settings)}


def gold_candidate(res: Resources, principal: Principal, candidate_id: str) -> dict:
    principal = _authorize(res, principal, "verifier")
    return gold.candidate(res.settings, candidate_id)


def gold_decide(res: Resources, principal: Principal, candidate_id: str, decision: str, expected_sha: str,
                categories: list[str] | None = None, note: str = "") -> dict:
    """The visitor's chosen name is recorded as the reviewer."""
    principal = _authorize(res, principal, "verifier")
    try:
        return gold.decide(res.settings, candidate_id, decision, principal.member_id, expected_sha, categories, note)
    except gold.GoldError as exc:
        raise ServiceError(str(exc)) from None


def fidelity_overview(res: Resources, principal: Principal) -> list[dict]:
    """Every HWP source with its automatic verdict for the active extraction (None until checked)."""
    principal = _authorize(res, principal, "verifier")
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute(
            "SELECT s.source_hash, s.review_status, MIN(d.filename) AS filename, f.metrics_json, f.findings_json "
            "FROM sources s JOIN documents d ON d.active_source_hash = s.source_hash "
            "LEFT JOIN fidelity_checks f ON f.extraction_id = s.active_extraction_id AND f.method = ? "
            "WHERE s.format = 'hwp' GROUP BY s.source_hash ORDER BY filename", (fidelity.FIDELITY_VERSION,)).fetchall()
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
