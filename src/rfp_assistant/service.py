"""Authenticated public functions, shared resource ownership and request orchestration.

Resources (Kiwi analyzer, loaded index, SDK transport, process-owner lock) are created once per process
by `Resources` and closed by `Resources.close()`. Sessions only supply the principal and the scope.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import re
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from . import auth, budget, fidelity, generation, gold
from .auth import require, require_any
from .contracts import (AnswerRequest, AnswerResult, BudgetSnapshot, DocRef, EvidenceUnit, EvidenceView,
                        ManagedDownload, Principal, RetrievalResult)
from .ingestion import CODE_RE, QUARANTINE_TEXT, nfc, printed_pdf_path, record_review
from .retrieval import Analyzer, KeywordIndex, RetrievalError, best_chunk_per_extraction
from .retrieval import retrieve as _retrieve
from .settings import Settings, read_api_key
from .store import LockHeld, ProcessLock, dumps, init_schema, open_db, tx, utcnow


class ServiceError(RuntimeError):
    pass


class GatewayLockError(ServiceError):
    pass


class Resources:
    """Process-wide owner. provider='fake' never builds a real SDK client, whatever keys the host has."""

    def __init__(self, settings: Settings, transport: generation.Transport | None = None) -> None:
        self.settings = settings
        init_schema(settings.db_path)
        budget.ensure_budget_row(settings.db_path)
        self._lock: ProcessLock | None = None
        self.transport: generation.Transport | None = transport
        self.provider_note = ""
        if transport is None and settings.provider == "fake":
            self.transport = generation.FakeTransport()
        elif transport is None:
            key = read_api_key("OPENAI_API_KEY")
            if key:
                try:
                    self._lock = ProcessLock(settings.data_dir / "gateway.lock")
                except LockHeld:
                    raise GatewayLockError(
                        "another process already owns the paid gateway for this data directory") from None
                self.recovered = budget.recover(settings.db_path)  # no older worker can still dispatch
                self.transport = generation.OpenAITransport(key, settings.request_timeout_seconds)
            else:
                self.provider_note = "OPENAI_API_KEY is not configured; paid generation is unavailable"
        self.analyzer = Analyzer()
        self._index: KeywordIndex | None = None
        self._index_lock = threading.Lock()
        self._closed = False
        atexit.register(self.close)

    def index(self) -> KeywordIndex | None:
        with self._index_lock:
            from .store import get_app_setting

            with open_db(self.settings.db_path) as conn:
                active = get_app_setting(conn, "active_index")
            if active is None:
                return None
            if self._index is None or self._index.version != active:
                self._index = KeywordIndex.load(self.settings, active)
            return self._index

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.transport is not None:
            self.transport.close()
        if self._lock is not None:
            self._lock.release()


_shared: Resources | None = None
_shared_lock = threading.Lock()


def get_resources(settings: Settings) -> Resources:
    global _shared
    with _shared_lock:
        if _shared is None or _shared._closed:
            _shared = Resources(settings)
        return _shared


# ---------------------------------------------------------------- scope and metadata


def _doc_rows(res: Resources, doc_ids: list[str] | None = None) -> list[dict]:
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute(
            "SELECT d.*, s.format, s.parse_status, s.review_status, s.reason_code, s.active_extraction_id "
            "FROM documents d JOIN sources s ON s.source_hash = d.active_source_hash ORDER BY d.csv_row_id").fetchall()
    out = []
    for r in rows:
        if doc_ids is not None and r["doc_id"] not in doc_ids:
            continue
        d = dict(r)
        d["meta"] = json.loads(d.pop("normalized_metadata_json"))
        d["quality"] = json.loads(d.pop("quality_json"))
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
    require_any(principal, "consultant", "verifier")
    docs = _doc_rows(res)
    inst = nfc(filters.get("institution") or "").strip()
    amount_min, amount_max = filters.get("amount_min"), filters.get("amount_max")
    close_from, close_to = filters.get("closing_from"), filters.get("closing_to")
    parsed_only = filters.get("parsed_only", False)
    items = []
    for d in docs:
        m = d["meta"]
        conflicts = {c["field"] for c in d["quality"].get("provenance_conflicts", [])}
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
        meta_hit = sum(w in d["meta"]["title"] or w in (d["meta"]["institution"] or "") for w in words)
        snip = snippets.get(d["active_extraction_id"] or "")
        if q and not meta_hit and not snip:
            continue
        ranked.append((meta_hit, snip[0] if snip else 0.0, d, snip[1] if snip else None))
    ranked.sort(key=lambda x: (-x[0], -x[1]))
    return [{
        "doc_id": d["doc_id"], "source_hash": d["active_source_hash"], "title": d["meta"]["title"],
        "institution": d["meta"]["institution"], "amount_krw": d["meta"]["amount_krw"],
        "published_at": d["meta"]["published_at"], "bid_close": d["meta"]["bid_close"],
        "format": d["format"], "parse_status": d["parse_status"], "review_status": d["review_status"],
        "indexed": _indexed(res, d["active_extraction_id"]),
        "unavailable_reason": QUARANTINE_TEXT.get(d["reason_code"] or ""),
        "flags": d["quality"]["flags"], "conflicts": d["quality"].get("provenance_conflicts", []),
        "snippet": snip,
    } for _, _, d, snip in ranked[:limit]]


def retrieve(res: Resources, principal: Principal, question: str, scope: list[DocRef]) -> RetrievalResult:
    require_any(principal, "consultant", "verifier")
    docs = _resolve_scope(res, scope)
    idx = res.index()
    if idx is None:
        raise ServiceError("검색 색인이 아직 없습니다.")
    return _retrieve(res.settings, idx, res.analyzer, question,
                     [(DocRef(d["doc_id"], d["active_source_hash"]), d["active_extraction_id"]) for d in docs])


# ---------------------------------------------------------------- answers


def _config_snapshot(res: Resources, request: AnswerRequest) -> dict:
    idx = res.index()
    with open_db(res.settings.db_path) as conn:
        rate_version = conn.execute("SELECT rate_version FROM budget_settings WHERE id = 1").fetchone()[0]
    return {"config_id": request.config_id, "index_version": idx.version if idx else None,
            "prompt_version": generation.PROMPT_VERSION, "model": res.settings.generation_model,
            "reasoning_effort": res.settings.generation_reasoning_effort,
            "rate_version": rate_version, "max_output_tokens": res.settings.generation_max_output_tokens,
            "settings": res.settings.fingerprint()}


def _input_hash(request: AnswerRequest) -> str:
    data = {"q": nfc(request.question), "scope": [asdict(s) for s in request.scope], "mode": request.mode,
            "as_of": request.as_of, "config": request.config_id}
    return hashlib.sha256(dumps(data).encode()).hexdigest()


def _finish(res: Resources, request_id: str, result: AnswerResult, trace: dict, status: str) -> AnswerResult:
    with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("UPDATE requests SET status = ?, trace_json = ?, result_json = ?, updated_at = ? "
                     "WHERE request_id = ?", (status, dumps(trace), dumps(asdict(result)), utcnow(), request_id))
    return result


def _evidence_map(evidence: list[EvidenceUnit]) -> dict[str, dict]:
    return {e.evidence_id: asdict(e) for e in evidence}


def prepare_answer(res: Resources, principal: Principal, question: str, scope: list[DocRef], as_of: str) -> dict:
    """Retrieval plus exact prompt counting and the maximum reservation estimate. Free."""
    require_any(principal, "consultant", "verifier")
    docs = _resolve_scope(res, scope)
    retrieval = retrieve(res, principal, question, scope)
    messages = generation.build_messages(question, as_of, [_doc_brief(d) for d in docs], retrieval.evidence,
                                         retrieval.limitations)
    rf = generation.answer_json_schema()
    tokens = generation.count_request_tokens(messages, rf, res.settings.framing_margin_tokens)
    est = budget.estimate(res.settings.db_path, res.settings.generation_model, tokens,
                          res.settings.generation_max_output_tokens)
    return {"retrieval": retrieval, "messages": messages, "response_format": rf, "input_tokens": tokens,
            "estimate_micro_usd": est, "docs": docs}


def _doc_brief(d: dict) -> dict:
    return {"doc_id": d["doc_id"], "title": d["meta"]["title"], "csv_institution": d["meta"]["institution"],
            "source_format": d["format"], "review_status": d["review_status"],
            "metadata_conflicts": [c["field"] for c in d["quality"].get("provenance_conflicts", [])]}


def answer(res: Resources, principal: Principal, request: AnswerRequest) -> AnswerResult:
    require_any(principal, "consultant", "verifier")
    s = res.settings
    question = (request.question or "").strip()
    if not question or len(question) > s.question_max_characters:
        raise ServiceError(f"질문은 1~{s.question_max_characters}자여야 합니다.")
    if request.mode != "single":
        raise ServiceError("1단계에서는 선택한 단일 문서 질문만 지원합니다.")
    input_hash = _input_hash(request)
    config = _config_snapshot(res, request)
    request_id = str(uuid.uuid4())
    with open_db(s.db_path) as conn, tx(conn, immediate=True):
        prior = conn.execute("SELECT * FROM requests WHERE member_id = ? AND idempotency_key = ?",
                             (principal.member_id, request.idempotency_key)).fetchone()
        if prior is not None:
            if prior["input_hash"] != input_hash:
                raise ServiceError("같은 요청 키가 다른 입력에 재사용되었습니다.")
            if prior["result_json"]:
                return AnswerResult(**json.loads(prior["result_json"]))
            return AnswerResult(prior["request_id"], "in_progress", "이미 처리 중인 요청입니다.",
                                generation_id=request.generation_id)
        conn.execute(
            "INSERT INTO requests(request_id, member_id, idempotency_key, generation_id, input_hash, config_hash, "
            "scope_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (request_id, principal.member_id, request.idempotency_key, request.generation_id, input_hash,
             hashlib.sha256(dumps(config).encode()).hexdigest(), dumps([asdict(x) for x in request.scope]),
             utcnow(), utcnow()))
    trace: dict = {"config": config, "question": question, "as_of": request.as_of}

    def done(status: str, summary: str, request_status: str = "completed", **kw) -> AnswerResult:
        result = AnswerResult(request_id, status, summary, generation_id=request.generation_id, **kw)
        return _finish(res, request_id, result, trace, request_status)

    if len(request.scope) != 1:
        return done("clarification_required", "질문할 문서를 하나 선택하세요.",
                    missing_fields=[{"doc_id": "", "field": "document", "reason": "scope_ambiguous"}])
    try:
        (doc,) = _resolve_scope(res, request.scope)
    except ServiceError as exc:
        return done("clarification_required", str(exc), request_status="failed")
    if doc["parse_status"] != "parsed" or not _indexed(res, doc["active_extraction_id"]):
        reason = QUARANTINE_TEXT.get(doc["reason_code"] or "", "이 문서는 아직 검색 색인에 포함되지 않았습니다.")
        return done("ingestion_unavailable", f"원문 전체를 확인할 수 없어 답변하지 않습니다. {reason}",
                    missing_fields=[{"doc_id": doc["doc_id"], "field": "document",
                                     "reason": "ingestion_unavailable"}])
    try:
        prep = prepare_answer(res, principal, question, request.scope, request.as_of)
    except (RetrievalError, budget.BudgetError) as exc:
        return done("technical_error", "검색 또는 비용 추정에 실패했습니다.", request_status="failed", error=str(exc))
    retrieval: RetrievalResult = prep["retrieval"]
    trace["retrieval"] = asdict(retrieval)
    trace["input_tokens_estimate"] = prep["input_tokens"]
    evidence_map = _evidence_map(retrieval.evidence)
    if not retrieval.evidence:
        return done("insufficient_evidence", "선택한 문서에서 질문과 관련된 근거를 찾지 못했습니다.",
                    missing_fields=[{"doc_id": doc["doc_id"], "field": "answer", "reason": "not_found_in_context"}])

    admission = budget.reserve(s.db_path, request_id=request_id, member_id=principal.member_id, stage="generation",
                               purpose="interactive", model=s.generation_model, input_tokens=prep["input_tokens"],
                               max_output_tokens=s.generation_max_output_tokens, count_method=generation.COUNT_METHOD)
    trace["admission"] = admission
    if not admission["admitted"]:
        return done("budget_blocked", "공유 사용 한도 또는 유료 호출 설정 때문에 답변 생성을 시작하지 않았습니다. "
                    "검색과 원문 열람은 계속 사용할 수 있습니다.", evidence=evidence_map, error=admission["reason"])
    attempt_id = admission["attempt_id"]
    if res.transport is None:
        budget.release(s.db_path, attempt_id, "provider_unavailable")
        return done("technical_error", "유료 모델 연결이 설정되지 않았습니다.", request_status="failed",
                    evidence=evidence_map, attempt_ids=[attempt_id], billing_state="released",
                    error=res.provider_note)
    budget.mark_dispatching(s.db_path, attempt_id)
    try:
        response = res.transport.chat(model=s.generation_model, messages=prep["messages"],
                                      response_format=prep["response_format"],
                                      max_completion_tokens=s.generation_max_output_tokens,
                                      reasoning_effort=s.generation_reasoning_effort)
    except generation.ProviderError as exc:
        if exc.pre_execution:
            budget.release(s.db_path, attempt_id, str(exc)[:300], confirmed_pre_execution=True)
            billing = "released"
        else:
            budget.mark_unknown(s.db_path, attempt_id, str(exc))
            billing = "unknown"
        return done("technical_error", "모델 호출에 실패했습니다. 자동으로 다시 시도하지 않습니다.",
                    request_status="failed", evidence=evidence_map, attempt_ids=[attempt_id], billing_state=billing,
                    error=str(exc)[:300])
    if response.usage is None:
        budget.mark_unknown(s.db_path, attempt_id, "provider returned no usage")
        billing = "unknown"
    else:
        settlement = budget.settle(s.db_path, attempt_id, response.usage, response.response_id)
        trace["settlement"] = settlement
        trace["usage"] = response.usage
        billing = "settled"
    stored = {e.evidence_id: _stored_quote(res, e) for e in retrieval.evidence}
    try:
        payload = generation.validate_answer(response, retrieval.evidence, {doc["doc_id"]}, stored)
    except generation.TechnicalError as exc:
        trace["raw_output"] = (response.content or "")[:4000]
        return done("technical_error", "모델 응답을 검증하지 못했습니다. 비용은 기록되었으며 자동 재시도는 하지 않습니다.",
                    request_status="failed", evidence=evidence_map, attempt_ids=[attempt_id], billing_state=billing,
                    error=str(exc)[:300])
    return done(payload.status, payload.summary, claims=[c.model_dump() for c in payload.claims],
                missing_fields=[m.model_dump() for m in payload.missing_fields],
                conflicts=[c.model_dump() for c in payload.conflicts], next_action=payload.next_action,
                evidence=evidence_map, attempt_ids=[attempt_id], billing_state=billing)


def _stored_quote(res: Resources, e: EvidenceUnit) -> str | None:
    idx = res.index()
    for c in idx.chunks if idx else []:
        if c["chunk_id"] == e.chunk_id and c["extraction_id"] == e.extraction_id:
            return c["body"]
    return None


# ---------------------------------------------------------------- evidence and originals


def get_request(res: Resources, principal: Principal, request_id: str) -> dict:
    require_any(principal, "consultant", "verifier")
    with open_db(res.settings.db_path) as conn:
        row = conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
    if row is None or (row["member_id"] != principal.member_id and not principal.can("verifier")):
        raise auth.AuthError("이 요청을 볼 권한이 없습니다.")
    return dict(row)


def open_evidence(res: Resources, principal: Principal, request_id: str, evidence_id: str) -> EvidenceView:
    row = get_request(res, principal, request_id)
    result = json.loads(row["result_json"] or "{}")
    ev = (result.get("evidence") or {}).get(evidence_id)
    if ev is None:
        raise ServiceError("요청에 없는 근거 ID입니다.")
    (doc,) = _doc_rows(res, [ev["doc_id"]])
    idx = res.index()
    context = []
    if idx is not None:
        ordered = sorted((e for (x, _), e in idx.elements.items() if x == ev["extraction_id"]),
                         key=lambda e: e["source_order"])
        orders = {e["element_id"]: e["source_order"] for e in ordered}
        hit = [orders[i] for i in ev["element_ids"] if i in orders]
        if hit:
            lo, hi = min(hit), max(hit)
            context = [{"element_id": e["element_id"], "text": e["raw_text"], "cited": e["element_id"] in
                        ev["element_ids"], "location": e["location"]}
                       for e in ordered[max(0, lo - 1):hi + 2]]
    return EvidenceView(evidence_id, ev["doc_id"], doc["meta"]["title"], ev["quote"], context, ev["location"],
                        doc["format"], True)


def original_download(res: Resources, principal: Principal, doc_id: str, source_hash: str) -> ManagedDownload:
    """Resolves only managed IDs; client paths are never accepted."""
    require_any(principal, "consultant", "verifier")
    (doc,) = _resolve_scope(res, [DocRef(doc_id, source_hash)])
    with open_db(res.settings.db_path) as conn:
        path = Path(conn.execute("SELECT original_path FROM sources WHERE source_hash = ?",
                                 (source_hash,)).fetchone()[0])
    if path.resolve().parent != res.settings.files_dir.resolve():
        raise ServiceError("관리되지 않는 원문 경로입니다.")
    mime = "application/pdf" if doc["format"] == "pdf" else "application/x-hwp"
    return ManagedDownload(doc["filename"], path.read_bytes(), mime)


def budget_snapshot(res: Resources, principal: Principal) -> BudgetSnapshot:
    require_any(principal, "consultant", "verifier", "budget_admin")
    return budget.snapshot(res.settings.db_path)


def verifier_trace(res: Resources, principal: Principal, question: str, scope: list[DocRef], as_of: str) -> dict:
    """Retrieval-only verification run with the exact final token budget and the paid estimate. Free."""
    require(principal, "verifier")
    prep = prepare_answer(res, principal, question, scope, as_of)
    r: RetrievalResult = prep["retrieval"]
    idx = res.index()
    codes = list(dict.fromkeys(CODE_RE.findall(nfc(question))))
    return {"retrieval": asdict(r), "query_tokens": res.analyzer.tokens(question), "codes": codes,
            "input_tokens": prep["input_tokens"], "estimate_micro_usd": prep["estimate_micro_usd"],
            "index_version": idx.version if idx else None, "review_scope": idx.review_scope if idx else None,
            "docs": [{k: d[k] for k in ("doc_id", "filename", "parse_status", "review_status", "reason_code")}
                     for d in prep["docs"]]}


def gold_queue(res: Resources, principal: Principal) -> dict:
    require(principal, "verifier")
    return {"pending": gold.queue(res.settings), "recent": gold.recent(res.settings)}


def gold_candidate(res: Resources, principal: Principal, candidate_id: str) -> dict:
    require(principal, "verifier")
    return gold.candidate(res.settings, candidate_id)


def gold_decide(res: Resources, principal: Principal, candidate_id: str, decision: str, expected_sha: str,
                categories: list[str] | None = None, note: str = "") -> dict:
    """The visitor's chosen name is recorded as the reviewer."""
    require(principal, "verifier")
    try:
        return gold.decide(res.settings, candidate_id, decision, principal.member_id, expected_sha, categories, note)
    except gold.GoldError as exc:
        raise ServiceError(str(exc)) from None


def fidelity_overview(res: Resources, principal: Principal) -> list[dict]:
    """Every HWP source with its automatic verdict for the active extraction (None until checked)."""
    require(principal, "verifier")
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
    require(principal, "verifier")
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
    require(principal, "verifier")
    row = fidelity.latest(res.settings, source_hash)
    if row is None:
        raise ServiceError("자동 원문 대조 결과가 없습니다.")
    findings = json.loads(row["findings_json"])
    locations = [{k: f.get(k) for k in ("side", "page", "element_id", "cell")} for f in findings]
    return record_review(res.settings, source_hash, principal.member_id, "sample_checked",
                         locations or [{"method": row["method"], "findings": 0}],
                         {"method": row["method"], "findings_confirmed": len(findings), "note": note})


def ingestion_overview(res: Resources, principal: Principal) -> list[dict]:
    require(principal, "verifier")
    with open_db(res.settings.db_path) as conn:
        rows = conn.execute(
            "SELECT d.doc_id, d.filename, s.format, s.parse_status, s.review_status, s.reason_code, s.warnings_json "
            "FROM documents d JOIN sources s ON s.source_hash = d.active_source_hash ORDER BY d.csv_row_id").fetchall()
    return [{**dict(r), "warnings": [w.get("code") for w in json.loads(r["warnings_json"])]} for r in rows]
