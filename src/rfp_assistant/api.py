"""HTTP API for the web app in `web/`: one route per `service` function, and nothing else from the package
(tests/test_api.py checks the imports).

There is no login. The `X-Member` header carries the visitor's typed name, percent-encoded UTF-8, for
attribution only. Paid work runs on the service's in-process executors, so serve this with exactly one worker:
`uvicorn rfp_assistant.api:app --workers 1`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import service


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

    @app.exception_handler(service.ServiceError)
    async def _service_error(_: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    for error in service.SHELL_ERRORS:
        if error is not service.ServiceError:
            app.add_exception_handler(error, _forbidden)
    _routes(app)
    return app


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
    scope: list[ScopeRef] = Field(min_length=1, max_length=2)
    question: str = ""
    mode: Literal["single", "compare", "metadata", "inventory"]


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

    @app.get("/api/documents", response_model=list[Document])
    def documents(res: Res, member: Member, query: str = "", institution: str = "", amount_min: int | None = None,
                  amount_max: int | None = None, closing_from: date | None = None, closing_to: date | None = None):
        return service.find_documents(res, member, query, institution, amount_min, amount_max,
                                      closing_from.isoformat() if closing_from else None,
                                      closing_to.isoformat() if closing_to else None)

    @app.post("/api/ask", response_model=Owned)
    def ask(body: AskIn, res: Res, member: Member):
        """Answers are as of today (DESIGN.md: the 기준일 control was removed)."""
        return service.ask(res, member, _scope(body.scope), body.question, body.mode, date.today().isoformat())

    @app.get("/api/requests", response_model=list[RequestView])
    def requests(res: Res, member: Member, limit: int = 20, all_members: bool = False):
        return service.list_requests(res, member, limit, all_members)

    @app.get("/api/requests/{request_id}", response_model=RequestOut)
    def request(request_id: str, res: Res, member: Member, generation_id: str = "", target: str = ""):
        """Read-only; safe to poll. Pass the owned generation and target to learn whether it may attach."""
        view = service.request_status(res, member, request_id)
        owned = {"request_id": request_id, "generation_id": generation_id, "target": target}
        return RequestOut(view=view, attachable=bool(target) and service.may_attach(owned, target, view))

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


app = create_app()
