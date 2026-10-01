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
