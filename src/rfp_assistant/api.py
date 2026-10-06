"""HTTP API for the web app in `web/`: one route per `service` function, and nothing else from the package
(tests/test_api.py checks the imports).

There is no login. The `X-Member` header carries the visitor's typed name, percent-encoded UTF-8, for
attribution only. Paid work runs on the service's in-process executors, so serve this with exactly one worker:
`uvicorn rfp_assistant.api:app --workers 1`.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import service

WEB = Path(__file__).resolve().parents[2] / "web" / "out"


def create_app(resources=None) -> FastAPI:
    """`resources` is for tests; the served app opens the data directory's single owner and closes it on stop."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = resources is None
        app.state.res = service.app_resources() if owned else resources
        try:
            yield
        finally:
            if owned:
                app.state.res.close()

    app = FastAPI(title="RFP assistant", version="1", lifespan=lifespan)
    app.add_middleware(_KeySession)

    @app.exception_handler(service.ServiceError)
    async def _service_error(_: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    for error in service.SHELL_ERRORS:
        if error is not service.ServiceError:
            app.add_exception_handler(error, _forbidden)
    _routes(app)
    _verify_routes(app)
    _dataset_routes(app)  # after /api/gold/recent and friends, which /api/gold/{candidate_id} would shadow
    if WEB.is_dir():  # the built screens (`cd web && npm run build`); mounted last, so /api/* stays the API's
        app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app


KEY_COOKIE = "bidmate_key_session"


class _KeySession:
    """Paid work in a request pays with the key its browser entered on 설정 (the session cookie), or none."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        session = Request(scope).cookies.get(KEY_COOKIE) if scope["type"] == "http" else None
        token = service.KEY_SESSION.set(session)
        try:
            await self.app(scope, receive, send)
        finally:
            service.KEY_SESSION.reset(token)


async def _forbidden(_: Request, exc: Exception):
    return JSONResponse({"detail": str(exc)}, status_code=403)


def _res(request: Request):
    return request.app.state.res


def _member(x_member: Annotated[str, Header()] = "owner"):
    return service.visitor(unquote(x_member))


Res = Annotated[object, Depends(_res)]
Member = Annotated[object, Depends(_member)]


# ---------------------------------------------------------------- shapes


class Info(BaseModel):
    build: str
    today: str
    question_max_characters: int


class Budget(BaseModel):
    snapshot: service.BudgetSnapshot
    warnings: list[str] = Field(description="Only the highest cap level, then the other warnings")


class BudgetLimitIn(BaseModel):
    cap_micro_usd: int = Field(strict=True, gt=0, le=9_000_000_000_000_000)
    reason: str = Field(min_length=1, max_length=500)


class ApiKeyIn(BaseModel):
    api_key: str  # no length constraint here: a validation error would echo the value back; service checks it


class ApiKeyStatus(BaseModel):
    configured: bool
    set_by: str | None
    set_at: str | None


class Dated(BaseModel):
    value: str
    precision: str


class Document(BaseModel):
    doc_id: str
    source_hash: str
    title: str | None
    institution: str | None
    amount_krw: int | None
    published_at: Dated | None
    bid_close: Dated | None
    notice: str | None
    format: str
    parse_status: str
    review_status: str
    indexed: bool
    unavailable_reason: str | None
    conflicts: list[dict]
    resolutions: dict[str, dict]
    filter_undecided: list[str]
    snippet: str | None


class ScopeRef(BaseModel):
    doc_id: str
    source_hash: str


class AskIn(BaseModel):
    scope: list[ScopeRef] = Field(max_length=2)  # empty only for corpus (all documents)
    question: str = ""
    mode: Literal["single", "compare", "corpus", "metadata", "inventory"]
    previous_request_id: str = Field("", max_length=100, description="The conversation's previous turn, if any")


class Owned(BaseModel):
    """What the screen keeps for the one request it owns."""
    request_id: str
    generation_id: str
    target: str


# The service keeps answers as dataclasses holding plain dicts; these read-only mirrors give the screens typed
# fields. They describe what the service already returns and never change it.

class _Read(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Claim(_Read):
    text: str
    kind: Literal["source_fact", "inference"]
    doc_id: str
    evidence_ids: list[str]


class MissingField(_Read):
    doc_id: str
    field: str
    reason: str


class ConflictAlternative(_Read):
    doc_id: str
    value: Any
    evidence_ids: list[str] = []


class Conflict(_Read):
    field: str
    alternatives: list[ConflictAlternative]


class Fact(_Read):
    doc_id: str
    field: str
    value: Any
    state: Literal["known", "unknown", "conflict", "zero_review", "resolved"]
    provenance: Literal["csv", "resolution"]
    evidence: Any = None


class InventoryItem(_Read):
    code: str
    source_form: str
    kind: Literal["detail", "summary"]
    name: str | None
    evidence_id: str | None = None
    location: dict = {}


class Inventory(_Read):
    items: list[InventoryItem]
    counts: dict[str, int]
    summary_only: list[str]
    repeated_detail_codes: list[str]
    completeness_text: str


class Coverage(_Read):
    doc_id: str
    evidence: int | None = Field(None, description="Evidence units found; absent for basic information")
    source: str | None = Field(None, description="csv_metadata for basic information")
    limitation: str | None = None


class Answer(_Read):
    request_id: str
    status: str = Field(description="Domain status, or technical_error / budget_blocked / ingestion_unavailable")
    summary: str
    summary_evidence_ids: list[str] = []
    standalone_question: str = Field("", description="A follow-up as rewritten from the conversation for retrieval")
    claims: list[Claim]
    missing_fields: list[MissingField]
    conflicts: list[Conflict]
    next_action: str | None
    evidence: dict[str, dict]
    billing_state: str
    error: str | None
    generation_id: str
    mode: str
    facts: list[Fact]
    inventory: Inventory | None
    coverage: list[Coverage]
    limitations: list[str]


class RequestView(_Read):
    request_id: str
    member_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
    mode: str
    generation_id: str
    question: str
    as_of: str
    scope: list[dict]
    created_at: str
    updated_at: str
    cancel_requested: bool
    billing_state: str
    reserved_micro_usd: int
    settled_micro_usd: int
    result: Answer | None


class EvidenceContext(_Read):
    element_id: str
    text: str
    cited: bool
    location: dict


class Evidence(_Read):
    evidence_id: str
    doc_id: str
    title: str | None
    quote: str
    context: list[EvidenceContext]
    location: dict
    source_format: str
    download_available: bool
    source_hash: str
    review_status: str
    warnings: list[str]


class RequestOut(BaseModel):
    view: RequestView
    attachable: bool = Field(description="May render as the owner's current answer (service.may_attach)")


class Cancelled(BaseModel):
    status: str


def _scope(refs: list[ScopeRef]) -> list[tuple[str, str]]:
    return [(r.doc_id, r.source_hash) for r in refs]


# ---------------------------------------------------------------- routes


def _routes(app: FastAPI) -> None:
    @app.get("/api/info", response_model=Info)
    def info(res: Res):
        return Info(build=service.build_head(), today=date.today().isoformat(),
                    question_max_characters=res.settings.question_max_characters)

    @app.get("/api/budget", response_model=Budget)
    def budget(res: Res, member: Member):
        snap = service.budget_snapshot(res, member)
        return Budget(snapshot=snap, warnings=service.visible_warnings(snap.warnings))

    @app.put("/api/budget/limit", response_model=Budget)
    def budget_limit(body: BudgetLimitIn, res: Res, member: Member):
        snap = service.set_budget_limit(res, member, body.cap_micro_usd, body.reason)
        return Budget(snapshot=snap, warnings=service.visible_warnings(snap.warnings))

    @app.get("/api/settings/api-key", response_model=ApiKeyStatus)
    def api_key_status(res: Res, member: Member):
        return service.api_key_status(res, member)

    @app.put("/api/settings/api-key", response_model=ApiKeyStatus)
    def api_key(body: ApiKeyIn, res: Res, member: Member, response: Response):
        session, status = service.set_api_key(res, member, body.api_key)
        # Not the key: an unguessable handle to it, sent only back to this server and unreadable by scripts.
        response.set_cookie(KEY_COOKIE, session, httponly=True, samesite="strict", path="/")
        return status

    @app.get("/api/documents", response_model=list[Document])
    def documents(res: Res, member: Member, query: str = "", institution: str = "", amount_min: int | None = None,
                  amount_max: int | None = None, closing_from: date | None = None, closing_to: date | None = None):
        return service.find_documents(res, member, query, institution, amount_min, amount_max,
                                      closing_from.isoformat() if closing_from else None,
                                      closing_to.isoformat() if closing_to else None)

    @app.post("/api/ask", response_model=Owned)
    def ask(body: AskIn, res: Res, member: Member):
        """Answers are as of today (DESIGN.md: the 기준일 control was removed)."""
        return service.ask(res, member, _scope(body.scope), body.question, body.mode, date.today().isoformat(),
                           body.previous_request_id)

    @app.get("/api/requests", response_model=list[RequestView])
    def requests(res: Res, member: Member, limit: int = 20, all_members: bool = False):
        return service.list_requests(res, member, limit, all_members)

    @app.get("/api/requests/{request_id}", response_model=RequestOut)
    def request(request_id: str, res: Res, member: Member, generation_id: str = "", target: str = ""):
        """Read-only; safe to poll. Pass the owned generation and target to learn whether it may attach."""
        view = service.request_status(res, member, request_id)
        owned = {"request_id": request_id, "generation_id": generation_id, "target": target}
        return RequestOut(view=view, attachable=bool(target) and service.may_attach(owned, target, view))

    @app.get("/api/requests/{request_id}/stream")
    def stream(request_id: str, res: Res, member: Member, generation_id: str = ""):
        """Server-sent events: `data:` carries the unvalidated answer written so far whenever it grows, `data: null`
        withdraws it once cancellation or a lost generation revokes it, then one `done` event when the request
        finished. Read-only; the validated outcome still comes from the request."""
        service.answer_progress(res, member, request_id, generation_id)  # refuse before the stream opens

        def events():
            last = None
            while True:
                progress = service.answer_progress(res, member, request_id, generation_id)
                if progress["partial"] != last and (progress["partial"] is not None or not progress["finished"]):
                    last = progress["partial"]  # a finished request keeps its last text until the outcome replaces it
                    yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
                if progress["finished"]:
                    yield "event: done\ndata: {}\n\n"
                    return
                time.sleep(0.15)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/requests/{request_id}/cancel", response_model=Cancelled)
    def cancel(request_id: str, res: Res, member: Member):
        return Cancelled(status=service.cancel_request(res, member, request_id))

    @app.post("/api/requests/{request_id}/abandon", status_code=204)
    def abandon(request_id: str, res: Res, member: Member):
        service.abandon_request(res, member, request_id)

    @app.get("/api/requests/{request_id}/export")
    def export_request(request_id: str, res: Res, member: Member) -> dict:
        return service.export_request(res, member, request_id)

    @app.get("/api/requests/{request_id}/evidence/{evidence_id}", response_model=Evidence)
    def evidence(request_id: str, evidence_id: str, res: Res, member: Member):
        return service.open_evidence(res, member, request_id, evidence_id)

    @app.get("/api/originals/{doc_id}/{source_hash}", response_class=Response)
    def original(doc_id: str, source_hash: str, res: Res, member: Member):
        dl = service.original_download(res, member, doc_id, source_hash)
        return Response(dl.data, media_type=dl.mime,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(dl.filename)}"})


# ---------------------------------------------------------------- 검증: shapes


class Rate(_Read):
    """A rate with its denominator; an empty denominator is 'not applicable', never 100 %."""
    numerator: int
    denominator: int
    rate: float | None


class Cost(_Read):
    settled_micro_usd: int | None = None


class Latency(_Read):
    p95: float | None = None


class FinalistScores(_Read):
    mode: str | None = None
    completed: int | None = None
    of: int | None = None
    required_claim_correctness: Rate | None = None
    critical_wrong: list = []
    citation_precision_lower_bound: Rate | None = None
    links_unjudged: int = 0
    negative_handling: Rate | None = None
    cost: Cost | None = None
    latency_ms: Latency | None = None


class RunScores(_Read):
    status: str | None = None
    stop_reason: str | None = None
    finalists: dict[str, FinalistScores] = {}


class RunConfig(_Read):
    config_id: str | None = None
    label: str | None = None
    mode: str | None = None


class AnswerRun(_Read):
    run_id: str
    config: RunConfig
    progress: dict[str, int]
    scores: RunScores | None
    running: bool


class Target(_Read):
    rows: int
    target: int
    met: bool


class Validation(_Read):
    ok: bool
    rows: int
    label: str | None = None
    errors: list[str] = []
    validated_at: str | None = None
    targets: dict[str, Target] = {}


class SealedState(_Read):
    """The sealed test set's size and freeze state only; its questions and labels never leave the service."""
    rows: int
    frozen: bool
    current: bool


class Release(_Read):
    release_id: str
    status: str
    reasons: list[str] = []


class EvaluationOverview(_Read):
    dev_validation: Validation | None
    dev_frozen: dict[str, Any] | None
    test: SealedState
    answer_runs: list[AnswerRun]
    release: Release | None


class GoldClaim(_Read):
    claim_id: str | None = None
    match: dict[str, Any] = {}
    qualifiers: list[list[str]] = []
    critical_kind: str | None = None
    support_groups: list[str] = []


class GoldQuote(_Read):
    quote: str | None = None


class GoldGroup(_Read):
    group_id: str | None = None
    doc_id: str | None = None
    alternatives: list[GoldQuote] = []


class SecondReview(_Read):
    candidate_id: str
    question: str
    required_claims: list[GoldClaim] = []
    evidence_groups: list[GoldGroup] = []


class VerifyOverview(BaseModel):
    evaluation: EvaluationOverview
    awaiting_second_review: list[SecondReview]
    build: str


class TraceQuestion(_Read):
    question_id: str | None = None
    id: str | None = None
    question: str = ""
    scope: list[dict] = []
    doc_ids: list[str] = []
    doc_id: str | None = None
    as_of_date: str | None = None
    as_of: str | None = None


class TraceSources(BaseModel):
    documents: list[Document]
    questions: list[TraceQuestion]
    modes: list[str]


class TraceIn(BaseModel):
    question: str = Field(min_length=1)
    scope: list[ScopeRef] = Field(min_length=1, max_length=2)
    mode: str
    as_of: date | None = None


class TraceEvidence(_Read):
    evidence_id: str
    doc_id: str
    source_hash: str
    extraction_id: str
    chunk_id: str
    element_ids: list[str]
    quote: str
    location: dict
    token_count: int


class Candidate(_Read):
    chunk_id: str
    channel: str
    rank: int
    score: float | None
    doc_id: str | None = None


class Excluded(_Read):
    chunk_id: str
    reason: str
    doc_id: str | None = None


class TraceConfig(_Read):
    config_id: str
    mode: str
    serving_mode: str
    model: str
    prompt_version: str
    index_version: str | None = None
    effective_limits: dict[str, int] = {}


class Retrieval(_Read):
    mode: str
    candidates: list[Candidate]
    evidence: list[TraceEvidence]
    excluded: list[Excluded]
    limitations: list[str]
    evidence_tokens: int
    timings_ms: dict[str, float]
    ranking: list[str] = []
    fallback: str | None = None
    query_embedding: dict[str, Any] | None = None


class TraceDoc(_Read):
    doc_id: str
    filename: str | None = None
    parse_status: str
    review_status: str
    reason_code: str | None = None


class TraceRun(_Read):
    run_id: str
    member_id: str
    created_at: str
    question: str
    scope: list[dict]
    config: TraceConfig
    as_of: str | None = None
    retrieval: Retrieval
    query_tokens: list[str]
    codes: list[str]
    input_tokens: int
    estimate_micro_usd: int | None
    query_embedding_estimate_micro_usd: int | None = None
    coverage: list[Coverage] | None = None
    index_version: str | None
    review_scope: str | None = None
    docs: list[TraceDoc]
    generation_block: str | None = Field(None, description="Why a paid answer cannot be generated from this run now")


class TraceSummary(_Read):
    run_id: str
    member_id: str
    config_id: str
    question: str
    created_at: str


class Comparison(_Read):
    same_question: bool
    same_scope: bool
    config_changes: dict[str, list[Any]]
    evidence_only_a: list[str]
    evidence_only_b: list[str]
    evidence_common: list[str]
    top5_a: list[str]
    top5_b: list[str]
    evidence_tokens: list[int]
    input_tokens: list[int]
    total_ms: list[float | None]


class Estimate(_Read):
    estimate_id: str
    finalists: list[str]
    rows_remaining: int
    max_micro_usd: int
    envelope_remaining_micro_usd: int | None = None
    expires_at: str
    fits: bool


class EstimateIn(BaseModel):
    estimate_id: str


class Started(BaseModel):
    run_id: str


class RequestRef(BaseModel):
    request_id: str


class MaintenanceStep(_Read):
    name: Literal["backup", "restore_check", "ingest", "fidelity", "keyword", "embedding", "regression", "report"]
    status: Literal["pending", "running", "done", "reused", "skipped", "failed", "needs_approval"]
    started_at: str | None = None
    finished_at: str | None = None
    detail: dict[str, Any] = {}
    reason: str | None = None


class MaintenanceRun(_Read):
    run_id: str
    actor: str
    started_at: str
    finished_at: str | None = None
    status: Literal["running", "complete", "unverified", "failed", "needs_approval", "interrupted"]
    stopped_at: str | None = None
    reason: str | None = None
    steps: list[MaintenanceStep]
    serving: dict[str, Any] | None = None
    provider_calls: int | None = None
    reused: bool | None = None


class MaintenanceStatus(_Read):
    running: bool
    run: MaintenanceRun | None
    backup_root: str


class JudgeReference(_Read):
    run_id: str
    items: int
    reference_sha256: str
    labels: dict[str, int]


class JudgeSplit(_Read):
    seed: int
    calibration: int
    held_out: int
    raw_sample: int
    split_sha256: str


class ArmProgress(_Read):
    done: int
    total: int


class JudgeRun(_Read):
    run_id: str
    part: Literal["calibration", "held_out", "judge_set"]
    created_at: str
    running: bool
    status: str
    stop_reason: str | None = None
    error: str | None = None
    progress: dict[str, ArmProgress]
    translated_batches: int
    thresholds_fitted: bool


class JudgeSetCounts(_Read):
    version: str
    items: int
    positives: int
    negatives: int
    by_type: dict[str, int]
    by_kind: dict[str, int]
    set_sha256: str


class JudgeOverview(_Read):
    reference: JudgeReference | None
    split: JudgeSplit | None
    judge_set: JudgeSetCounts | None = None
    rule: dict[str, Any] | None = None
    runs: list[JudgeRun]


class JudgePlanIn(BaseModel):
    part: Literal["calibration", "held_out", "judge_set"]


class PaidPlan(_Read):
    attempts: int
    max_micro_usd: int
    segments: int | None = None


class JevPlan(_Read):
    calls: int
    priced: bool
    note: str


class JudgeEstimate(_Read):
    estimate_id: str
    part: Literal["calibration", "held_out", "judge_set"]
    run_id: str
    items: int
    raw_sample: int
    model: str
    jev_model: str
    translation: PaidPlan
    judge: PaidPlan
    jev: JevPlan
    max_micro_usd: int
    paid_enabled: bool
    envelope_remaining_micro_usd: int | None = None
    available_micro_usd: int
    fits: bool
    blocked: str | None = None
    expires_at: str


class AgreementRate(_Read):
    numerator: int
    denominator: int
    rate: float | None = None
    wilson95: list[float] | None = None


class Usd(_Read):
    settled_micro_usd: int | None = None
    priced: bool = True
    calls: int
    source: str


class JudgeLatency(_Read):
    p50: float | None = None
    p95: float | None = None
    n: int


class ArmMetrics(_Read):
    items: int
    judged: int
    coverage: float | None = None
    agreement: AgreementRate
    kappa: float | None = None
    false_accepts: int
    reference_negatives: int
    code_settled: int
    abstentions: dict[str, int]
    confusion: dict[str, dict[str, dict[str, int]]]
    usd: Usd
    bridge_usd: Usd | None = None
    latency_ms: JudgeLatency


class RuleCheck(_Read):
    condition: str
    passed: bool | None = None
    detail: str


class ReplacementVerdict(_Read):
    verdict: Literal["replaceable", "not_replaceable", "inconclusive"]
    checks: list[RuleCheck]
    deciding: list[RuleCheck]
    rule: dict[str, Any]


class MutationRate(_Read):
    items: int
    judged: int
    passed: int
    code_settled: int
    rate: float | None = None
    wilson95: list[float] | None = None
    measure: str


class JudgeResults(_Read):
    run_id: str
    part: str
    status: str
    stop_reason: str | None = None
    progress: dict[str, ArmProgress]
    arms: dict[str, ArmMetrics] = {}
    verdict: ReplacementVerdict | None = None
    thresholds_sha256: str | None = None
    config_hashes: dict[str, str] = {}
    mutations: dict[str, dict[str, MutationRate]] = {}
    judge_set_sha256: str | None = None


class ArmView(_Read):
    label: str | None = None
    source: str
    abstain: str | None = None
    probability: float | None = None
    reason: str | None = None


class Disagreement(_Read):
    blind_id: str
    kind: Literal["link", "answer_claim", "claim"]
    reference: str
    korean: dict[str, Any]
    english: dict[str, Any] | None = None
    untranslatable: str | None = None
    arms: dict[str, ArmView | None]
    mutation: dict[str, Any] | None = None


class Finding(_Read):
    side: str
    page: int | None = None
    unmatched_chars: int = 0
    digits: list[str] = []
    stretch: str | None = None
    stretches: list[str] = []
    text: str | None = None


class FidelityMetrics(_Read):
    pages: int
    extracted_chars: int
    rendered_chars: int
    extraction_unmatched: int
    rendering_unmatched: int
    image_pages: list[int] = []


class FidelitySource(_Read):
    source_hash: str
    filename: str | None
    review_status: str
    metrics: FidelityMetrics | None
    findings: list[Finding]


class NoteIn(BaseModel):
    note: str = Field("", max_length=300)


class Recorded(BaseModel):
    id: str


class IngestionRow(_Read):
    doc_id: str
    filename: str | None
    format: str
    parse_status: str
    review_status: str
    reason_code: str | None
    warnings: list[str | None]


class Correction(_Read):
    created_at: str
    reviewer: str
    reason: str
    quote: str
    run_id: str | None = None
    request_id: str | None = None
    evidence: dict[str, Any] = {}
    proposal: dict[str, Any] = {}


class CorrectionIn(BaseModel):
    run_id: str
    evidence_id: str
    element_id: str
    reason: str
    quote: str
    proposal: dict[str, Any] = {}


class VerificationEvent(_Read):
    event_id: str
    kind: str
    action: str
    target_id: str
    target: str
    reviewer: str
    created_at: str
    note: str
    quote: str
    locations: list[Any]


class Decided(_Read):
    candidate_id: str
    status: str
    decided_by: str | None = None
    decided_at: str | None = None
    categories: list[str] = []


class SecondReviewIn(BaseModel):
    agreed: bool
    note: str


class ExperimentColumn(_Read):
    key: str
    label: str
    better: Literal["high", "low"] | None


class ExperimentRow(_Read):
    index: int
    name: str
    axes: dict[str, Any]
    status: str
    reason: str | None
    run_id: str | None
    label: str | None
    values: dict[str, float | int | str | None]
    facts: dict[str, Any]
    estimate: dict[str, Any] | None
    failures: int
    active: bool


class ExperimentTable(_Read):
    matrix: str
    title: str
    created_at: str
    columns: list[ExperimentColumn]
    fixed: dict[str, Any]
    populations: dict[str, str]
    needs_evidence_review: list[str] = []
    rows: list[ExperimentRow]


class ServingDetail(_Read):
    mode: str | None
    embedding: str | None
    reranker: str | None
    protect: int
    fallback: str | None


class Experiments(_Read):
    tables: list[ExperimentTable]
    active_run_id: str | None
    serving: str
    serving_detail: ServingDetail
    golden_counts: dict[str, Any] | None


class ExperimentQuestion(_Read):
    population: str
    id: str
    question: str | None
    type: str | None
    ndcg: float | None
    complete: bool
    hit5: int | None
    qualifier_loss: int
    missing: list[str]
    critical: bool
    fallback: str | None


class ActivateIn(BaseModel):
    run_id: str
    decided_by: str
    note: str = ""


class Activated(_Read):
    run_id: str
    mode: str
    activated_at: str


# ---------------------------------------------------------------- 검증: routes


def _verify_routes(app: FastAPI) -> None:
    @app.get("/api/verify/overview", response_model=VerifyOverview)
    def overview(res: Res, member: Member):
        return VerifyOverview(evaluation=service.evaluation_overview(res, member),
                              awaiting_second_review=service.gold_awaiting_second_review(res, member),
                              build=service.build_head())

    @app.get("/api/verify/trace-sources", response_model=TraceSources)
    def trace_sources(res: Res, member: Member):
        return TraceSources(documents=service.trace_documents(res, member),
                            questions=service.trace_questions(res, member), modes=service.trace_modes(res))

    @app.post("/api/verify/traces", response_model=TraceRun)
    def run_trace(body: TraceIn, res: Res, member: Member):
        run = service.run_trace(res, member, body.question, _scope(body.scope),
                                (body.as_of or date.today()).isoformat(), body.mode)
        return trace(run["run_id"], res, member)

    @app.get("/api/verify/traces", response_model=list[TraceSummary])
    def traces(res: Res, member: Member):
        return service.verifier_runs(res, member)

    @app.get("/api/verify/traces/{run_id}", response_model=TraceRun)
    def trace(run_id: str, res: Res, member: Member):
        return {**service.verifier_run(res, member, run_id),
                "generation_block": service.run_generation_block(res, member, run_id)}

    @app.get("/api/verify/traces/{run_id}/export")
    def export_trace(run_id: str, res: Res, member: Member) -> dict:
        return service.export_verifier_run(res, member, run_id)

    @app.post("/api/verify/traces/{run_id}/generate", response_model=RequestRef)
    def generate(run_id: str, res: Res, member: Member):
        """Paid: one idempotent request per run, however often it is pressed."""
        return RequestRef(request_id=service.generate_from_run_id(res, member, run_id))

    @app.get("/api/verify/compare", response_model=Comparison)
    def compare(a: str, b: str, res: Res, member: Member):
        return service.compare_verifier_runs(res, member, a, b)

    @app.post("/api/verify/evaluation/plan", response_model=Estimate)
    def plan_evaluation(res: Res, member: Member):
        """Free: prices the development answers still to generate. Nothing is sent."""
        return service.plan_answer_evaluation(res, member)

    @app.post("/api/verify/evaluation/start", response_model=Started)
    def start_evaluation(body: EstimateIn, res: Res, member: Member):
        return Started(run_id=service.start_answer_evaluation(res, member, body.estimate_id))

    @app.get("/api/verify/judges/progress", response_model=JudgeOverview)
    def judge_progress(res: Res, member: Member):
        return service.judge_overview(res, member)

    @app.post("/api/verify/judges/plan", response_model=JudgeEstimate)
    def plan_judges(body: JudgePlanIn, res: Res, member: Member):
        """Free: prices the translations and Luna judge calls a part still needs, and counts its Jev calls."""
        return service.plan_judges(res, member, body.part)

    @app.post("/api/verify/judges/start", response_model=Started)
    def start_judges(body: EstimateIn, res: Res, member: Member):
        """Paid: runs the planned part through the gateway (`judge_eval`), never above the estimate."""
        return Started(run_id=service.start_judges(res, member, body.estimate_id))

    @app.get("/api/verify/judges/results/{run_id}", response_model=JudgeResults)
    def judge_results(run_id: str, res: Res, member: Member):
        return service.judge_results(res, member, run_id)

    @app.get("/api/verify/judges/disagreements/{run_id}", response_model=list[Disagreement])
    def judge_disagreements(run_id: str, res: Res, member: Member):
        return service.judge_disagreements(res, member, run_id)

    @app.get("/api/verify/maintenance", response_model=MaintenanceStatus)
    def maintenance_status(res: Res, member: Member):
        """The maintenance sequence's progress, or the last run's report."""
        return service.maintenance_status(res, member)

    @app.post("/api/verify/maintenance/start", response_model=Started)
    def start_maintenance(res: Res, member: Member):
        """Runs backup, restore check, ingest, fidelity, keyword, embedding and regression once, in order; a paid
        embedding stops at its estimate. Activates nothing."""
        return Started(run_id=service.start_maintenance(res, member))

    @app.get("/api/verify/experiments", response_model=Experiments)
    def experiments(res: Res, member: Member):
        """Free: every comparison table the runner wrote, and which row serves."""
        return service.experiments(res, member)

    @app.get("/api/verify/experiments/{matrix}/{index}/questions", response_model=list[ExperimentQuestion])
    def experiment_questions(matrix: str, index: int, res: Res, member: Member):
        return service.experiment_questions(res, member, matrix, index)

    @app.post("/api/verify/experiments/activate", response_model=Activated)
    def activate_experiment(body: ActivateIn, res: Res, member: Member):
        """Switches what serves to the picked row's run (`activate-run`), recorded with the person's name."""
        return service.activate_experiment(res, member, body.run_id, body.decided_by, body.note)

    @app.get("/api/verify/fidelity", response_model=list[FidelitySource])
    def fidelity(res: Res, member: Member):
        return service.fidelity_overview(res, member)

    @app.get("/api/verify/fidelity/{source_hash}/pages/{page}", response_class=Response)
    def fidelity_page(source_hash: str, page: int, res: Res, member: Member):
        return Response(service.rendered_page_png(res, member, source_hash, page), media_type="image/png")

    @app.post("/api/verify/fidelity/{source_hash}/confirm", response_model=Recorded)
    def confirm_fidelity(source_hash: str, body: NoteIn, res: Res, member: Member):
        return Recorded(id=service.confirm_fidelity(res, member, source_hash, body.note))

    @app.get("/api/verify/ingestion", response_model=list[IngestionRow])
    def ingestion(res: Res, member: Member):
        return service.ingestion_overview(res, member)

    @app.get("/api/verify/corrections", response_model=list[Correction])
    def corrections(res: Res, member: Member):
        return service.list_corrections(res, member)

    @app.get("/api/verify/history", response_model=list[VerificationEvent])
    def history(res: Res, member: Member, limit: int = Query(100, ge=1, le=200)):
        return service.verification_history(res, member, limit)

    @app.post("/api/verify/corrections", response_model=Recorded)
    def add_correction(body: CorrectionIn, res: Res, member: Member):
        return Recorded(id=service.record_run_correction(
            res, member, body.run_id, body.evidence_id, body.element_id, body.reason, body.quote, body.proposal))

    @app.get("/api/gold/reject-categories")
    def reject_categories() -> dict[str, str]:
        return service.REJECT_CATEGORIES

    @app.get("/api/gold/recent", response_model=list[Decided])
    def gold_recent(res: Res, member: Member):
        return service.gold_queue(res, member)["recent"]

    @app.post("/api/gold/{candidate_id}/second-review")
    def second_review(candidate_id: str, body: SecondReviewIn, res: Res, member: Member) -> dict:
        return service.gold_second_review(res, member, candidate_id, body.agreed, body.note)


# ---------------------------------------------------------------- 데이터셋 만들기: shapes


class DraftDocument(_Read):
    doc_id: str
    source_hash: str
    title: str | None
    institution: str | None
    filename: str | None


class DraftElement(_Read):
    element_id: str
    kind: str
    text: str | None
    location: dict


class SourceRef(BaseModel):
    doc_id: str
    element_id: str


class SlotIn(BaseModel):
    question_type: str
    intent: str
    doc_ids: list[str]
    sources: list[SourceRef]


class Slot(SlotIn):
    question_id: str


class SlotsIn(BaseModel):
    slots: list[Slot]


class StartIn(SlotsIn):
    consented_max_micro_usd: int


class DraftEstimate(_Read):
    calls: int
    max_micro_usd: int
    paid_enabled: bool
    envelope_remaining_micro_usd: int | None
    available_micro_usd: int | None
    fits: bool


class Receipt(_Read):
    rows: int
    invalid: int
    settled_micro_usd: int


class DraftRow(_Read):
    question_id: str
    question_type: str | None = None
    question: str = ""
    required_claims: list = []


class InvalidDraft(_Read):
    question_id: str
    reason: str


class DraftRun(_Read):
    run_id: str
    requested_by: str | None
    created_at: str | None
    max_micro_usd: int | None
    slots: int
    status: Literal["running", "completed", "failed", "interrupted"]
    receipt: Receipt | None
    rows: list[DraftRow]
    invalid: list[InvalidDraft]
    error: str | None
    submitted: bool


class Pending(_Read):
    candidate_id: str
    dataset: str
    batch_id: str
    type: str | None
    question: str | None


class PdfPages(_Read):
    group_id: str | None
    pdf_pages: list[int]


class CandidateDocument(_Read):
    found: bool
    doc_id: str | None
    source_hash: str | None
    title: str | None = None
    institution: str | None = None
    filename: str | None = None
    format: str | None = None
    review_status: str | None = None
    unavailable_reason: str | None = None
    pdf_pages: list[PdfPages]


class Segment(_Read):
    text: str
    cited: bool


class Span(_Read):
    label: str
    location: dict | None
    quote: str
    cited_found: bool
    missing: bool
    segments: list[Segment] = Field(description="The element text in order; `cited` marks the quoted span")


class MetadataRow(_Read):
    field: str
    value: Any


class CandidateReview(_Read):
    candidate_id: str
    dataset: str
    gold: bool
    row_sha256: str
    drafted_by: str | None
    requested_by: str | None = Field(description="Who started the drafting run; refused as approver")
    type: str | None
    question: str
    expected_answer: str | None
    difficulty_reason: str | None
    answerability: str | None
    expected_status: str | None
    as_of_date: str | None
    required_claims: list[GoldClaim]
    negative_validation: dict[str, Any]
    current_errors: list[str]
    documents: list[CandidateDocument]
    spans: list[Span]
    metadata: list[MetadataRow]


class DecideIn(BaseModel):
    decision: Literal["approve", "reject"]
    expected_sha: str
    note: str
    categories: list[str] = []
    original_inspected: bool = False
    disputed: bool = False


# ---------------------------------------------------------------- 데이터셋 만들기: routes


def _dataset_routes(app: FastAPI) -> None:
    @app.get("/api/drafting/documents", response_model=list[DraftDocument])
    def draft_documents(res: Res, member: Member):
        """Development-family documents only: no sealed source reaches drafting."""
        return service.draft_documents(res, member)

    @app.get("/api/drafting/documents/{doc_id}/elements", response_model=list[DraftElement])
    def draft_elements(doc_id: str, res: Res, member: Member, contains: str = ""):
        return service.draft_elements(res, member, doc_id, contains)

    @app.post("/api/drafting/slots", response_model=Slot)
    def draft_slot(body: SlotIn):
        return service.draft_slot(body.question_type, body.intent, body.doc_ids, [s.model_dump() for s in body.sources])

    @app.post("/api/drafting/plan", response_model=DraftEstimate)
    def plan_drafting(body: SlotsIn, res: Res, member: Member):
        """Free: the maximum cost of drafting these slots. Nothing is sent."""
        return service.plan_drafting(res, member, [s.model_dump() for s in body.slots])

    @app.post("/api/drafting/start", response_model=Started)
    def start_drafting(body: StartIn, res: Res, member: Member):
        """Paid: gpt-6-luna drafting through the budget gateway, never above the consented maximum."""
        return Started(run_id=service.start_drafting(res, member, [s.model_dump() for s in body.slots],
                                                     body.consented_max_micro_usd))

    @app.get("/api/drafting/runs", response_model=list[DraftRun])
    def drafting_runs(res: Res, member: Member):
        return service.drafting_runs(res, member)

    @app.post("/api/drafting/runs/{run_id}/submit")
    def submit_drafts(run_id: str, res: Res, member: Member) -> dict:
        return service.submit_drafts(res, member, run_id)

    @app.get("/api/gold/pending", response_model=list[Pending])
    def gold_pending(res: Res, member: Member):
        """Development candidates only; sealed ones are reviewed through the owner's CLI."""
        return service.gold_queue(res, member)["pending"]

    @app.get("/api/gold/{candidate_id}", response_model=CandidateReview)
    def gold_candidate(candidate_id: str, res: Res, member: Member):
        return service.candidate_review(res, member, candidate_id)

    @app.post("/api/gold/{candidate_id}/decide", response_model=Decided)
    def gold_decide(candidate_id: str, body: DecideIn, res: Res, member: Member):
        """A note is required for both outcomes, and whoever started the drafting run cannot approve it."""
        return service.gold_decide(res, member, candidate_id, body.decision, body.expected_sha,
                                   body.categories if body.decision == "reject" else None, body.note,
                                   original_inspected=body.original_inspected, disputed=body.disputed)


app = create_app()
