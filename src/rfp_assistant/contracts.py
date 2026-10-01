"""Request/result records and the strict generated answer shape."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict

CAPABILITIES = ("consultant", "verifier", "budget_admin", "sealed_evaluator")


@dataclass(frozen=True)
class Principal:
    member_id: str
    capabilities: frozenset[str]
    session_id: str | None = None

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class DocRef:
    doc_id: str
    source_hash: str


@dataclass
class EvidenceUnit:
    evidence_id: str  # request-local E1, E2, ...
    doc_id: str
    source_hash: str
    extraction_id: str
    chunk_id: str
    element_ids: list[str]
    quote: str
    location: dict
    token_count: int


@dataclass
class RetrievalResult:
    mode: str
    scope: list[DocRef]
    exact_matches: list[dict]
    candidates: list[dict]  # chunk_id, doc_id, rank, score, channel
    evidence: list[EvidenceUnit]
    excluded: list[dict]
    limitations: list[str]
    evidence_tokens: int
    timings_ms: dict
    trace_id: str
    index_version: str | None
    ranking: list[str] = field(default_factory=list)  # pre-pack order (exact first), kept apart from packed evidence
    fallback: str | None = None  # "<asked mode>-><served mode>:<reason>" when a stage was unavailable
    dense_version: str | None = None
    query_embedding: dict | None = None  # cache hit or paid attempt for the query vector


@dataclass
class AnswerRequest:
    idempotency_key: str
    generation_id: str
    question: str
    scope: list[DocRef]
    mode: Literal["single", "compare", "metadata", "inventory"] = "single"
    as_of: str = ""
    config_id: str = "default"
    verifier_run_id: str = ""  # generate from this frozen verifier run's evidence (its config_id is then implied)


@dataclass
class AnswerResult:
    request_id: str
    status: str  # domain status, or 'technical_error' / 'budget_blocked' / 'ingestion_unavailable'
    summary: str
    claims: list[dict] = field(default_factory=list)
    missing_fields: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    next_action: str | None = None
    evidence: dict[str, dict] = field(default_factory=dict)
    attempt_ids: list[str] = field(default_factory=list)
    billing_state: str = "none"
    error: str | None = None
    generation_id: str = ""
    mode: str = "single"
    facts: list[dict] = field(default_factory=list)  # metadata mode: typed values with provenance and state
    inventory: dict | None = None  # inventory mode: structured requirements and the completeness declaration
    coverage: list[dict] = field(default_factory=list)  # per selected document: evidence count or limitation
    limitations: list[str] = field(default_factory=list)


REQUEST_STATUSES = ("queued", "running", "completed", "failed", "cancelled", "interrupted")
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted")


@dataclass
class RequestView:
    """One persisted request as its owner (or a verifier) may see it. `status` is the execution state;
    the domain outcome and billing state live in `result` / `billing_state`."""
    request_id: str
    member_id: str
    status: str
    mode: str
    generation_id: str
    question: str
    as_of: str
    scope: list[dict]
    input_hash: str
    created_at: str
    updated_at: str
    cancel_requested: bool
    billing_state: str
    reserved_micro_usd: int  # open reservations still held by this request
    settled_micro_usd: int
    attempts: list[dict]
    result: AnswerResult | None


@dataclass
class BudgetSnapshot:
    allowance_micro_usd: int
    cap_micro_usd: int
    spent_micro_usd: int
    pending_micro_usd: int
    available_micro_usd: int
    spent_percent: float
    committed_percent: float
    paid_enabled: bool
    frozen_reason: str | None
    tokens: dict
    per_member: dict
    open_attempts: int
    ledger_revision: str
    tracking_scope: str
    last_reconciliation: str | None
    project_start: str | None = None
    project_end: str | None = None
    cap_percent: float = 0.0  # (spent + pending) / operational cap: warnings use this, not the $20 percentage
    warnings: list[str] = field(default_factory=list)
    pacing: dict | None = None
    read_at: str = ""
    unknown_micro_usd: int = 0


@dataclass
class EvidenceView:
    evidence_id: str
    doc_id: str
    title: str
    quote: str
    context: list[dict]
    location: dict
    source_format: str
    download_available: bool
    source_hash: str = ""
    review_status: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class ManagedDownload:
    filename: str
    data: bytes
    mime: str


# --- Strict generated answer shape (field names English, user-facing strings Korean) ---


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Claim(_Strict):
    text: str
    kind: Literal["source_fact", "inference"]
    doc_id: str
    evidence_ids: list[str]


class MissingField(_Strict):
    doc_id: str
    field: str
    reason: Literal[
        "unknown_metadata", "not_found_in_context", "ingestion_unavailable", "source_absence_verified", "scope_ambiguous"
    ]


class ConflictAlternative(_Strict):
    doc_id: str
    value: str
    evidence_ids: list[str]


class Conflict(_Strict):
    field: str
    alternatives: list[ConflictAlternative]


class AnswerPayload(_Strict):
    status: Literal["answered", "insufficient_evidence", "clarification_required", "conflicting_evidence"]
    summary: str
    claims: list[Claim]
    missing_fields: list[MissingField]
    conflicts: list[Conflict]
    next_action: str | None
