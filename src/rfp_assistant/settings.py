"""Absolute paths, validated limits, model/rate configuration and configuration fingerprint."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from pathlib import Path

from .retrieval.models import EMBEDDINGS, RERANKERS
from .storage.postgres import Target

REPO_ROOT = Path(__file__).resolve().parents[2]

# USD per one million tokens, standard tier, short context (<= 272K input tokens), from
# https://developers.openai.com/api/docs/pricing checked 2026-09-30. Cache writes bill 1.25x input on
# GPT-5.6 and later. configure-budget snapshots the rates it was given.
RATE_VERSION = "openai-standard-gpt-6-luna+text-embedding-3-large-2026-10-03"
LARGE_RATE_VERSION = "openai-standard-text-embedding-3-large-2026-10-03"
LARGE_RATE_CHECKED_AT = "2026-10-03T09:31:30Z"
DEFAULT_RATES: dict[str, dict[str, str]] = {
    "gpt-6-luna": {"input": "0.10", "cached_input": "0.01", "cache_write": "0.125", "output": "0.50"},
    "text-embedding-3-small": {"input": "0.02"},
    "text-embedding-3-large": {"input": "0.13"},
    "gpt-5-mini": {"input": "0.25", "cached_input": "0.025", "output": "2.00"},
    "gpt-5-nano": {"input": "0.05", "cached_input": "0.005", "output": "0.40"},
}
# Answer models: the team's shared key reaches only gpt-5-mini and gpt-5-nano, so each browser picks one on 설정.
# gpt-6-luna keeps its rate above for dataset drafting, the judges and AI review (their own model constants).
ALLOWED_GENERATION_MODELS = ("gpt-5-mini", "gpt-5-nano")
# Where the gpt-5-mini and gpt-5-nano rates above come from; a ledger that predates them gets them on first selection.
SELECTABLE_RATE_SOURCE = {"source": "https://developers.openai.com/api/docs/pricing", "tier": "standard",
                          "checked_at": "2026-10-06"}
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Every compared embedding model (models.EMBEDDINGS). What serves is the activated run's model, never this setting:
# the process value only names the model a maintenance build embeds with.
ALLOWED_EMBEDDING_MODELS = tuple(EMBEDDINGS)
# Embedding endpoint limits as documented in the pinned SDK (openai 3.22.1, embedding_create_params.py):
# 8192 tokens per input, at most 2048 inputs per array, 300,000 tokens summed across one request.
EMBEDDING_MAX_TOKENS_PER_INPUT = 8192
EMBEDDING_MAX_INPUTS_PER_REQUEST = 2048
EMBEDDING_MAX_TOKENS_PER_REQUEST = 300_000
EMBEDDING_MAX_DIMENSIONS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}
EMBEDDING_TOKENIZER = {"text-embedding-3-small": "cl100k_base", "text-embedding-3-large": "cl100k_base"}


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    source_dir: Path
    data_dir: Path
    hwp_converter: Path | None
    provider: str = "openai"  # "openai" or "fake"; fake refuses to build a real SDK client
    generation_model: str = "gpt-5-mini"
    generation_reasoning_effort: str = "low"
    embedding_model: str = "text-embedding-3-large"
    evidence_target_tokens: int = 3000
    evidence_max_tokens: int = 5000  # keeps every prompt far below the 272K long-context price tier
    evidence_max_units: int = 6
    generation_max_output_tokens: int = 4000  # includes reasoning tokens; the released candidate's cap
    question_max_characters: int = 2000
    retrieval_mode: str = "kiwi_bm25"  # default until `activate-run` records a measured selection
    reranker_enabled: bool = False
    embedding_dimensions: int = 1536  # owner-selected reduction; activation still requires quality acceptance
    embedding_batch_inputs: int = 256
    embedding_batch_tokens: int = 100_000
    embedding_estimate_ttl_hours: int = 24
    channel_top_k: int = 20  # BM25 top 20 and dense top 20
    fused_top_k: int = 20
    rrf_k: int = 60
    # Hybrid fusion: "rrf" (weighted reciprocal rank fusion) or "keyword_first" (BM25 order, then dense-only rows by
    # fused score). Serving takes both from the activated run; change them only through a measured comparison.
    fusion: str = "rrf"
    dense_weight: float = 1.0  # RRF weight of the dense channel; the keyword channel weighs 1
    keyword_head: int = 20  # keyword_first: leading BM25 rows kept in BM25 order; the rest fuse by weighted RRF
    dense_search: str = "exact"  # "hnsw" only through a run whose recall against exact search was measured
    hnsw_ef_search: int = 100
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_revision: str = ""  # commit hash from the model card; the trial refuses to load an unpinned model
    reranker_max_length: int = 512
    reranker_max_concurrency: int = 1
    reranker_precision: str = "fp32"  # "fp16"/"bf16" halve weights on a CUDA device; part of the trial/serving identity
    request_timeout_seconds: float = 60.0
    converter_timeout_seconds: float = 300.0
    framing_margin_tokens: int = 200
    # Phase 3 operation: the bounded background executor and a fake-provider delay that makes races observable.
    request_workers: int = 6
    request_admission: int = 12  # admitted unfinished (queued + running) requests across the process
    shutdown_wait_seconds: float = 20.0
    fake_delay_seconds: float = 0.0
    jev_model: str = "jev-1.13.0"  # the TypeSafe judge compared with the Luna judge (judges.py)
    extra: dict = field(default_factory=dict)
    database_dsn_env: str = "RFP_DATABASE_DSN"
    database_pool_max: int = 8
    database_timeout_seconds: float = 5

    @property
    def csv_path(self) -> Path:
        return self.source_dir / "data_list.csv"

    @property
    def files_dir(self) -> Path:
        return self.source_dir / "files"

    @property
    def db_path(self) -> Target:
        return Target(self.database_dsn_env, self.database_pool_max, self.database_timeout_seconds)

    def fingerprint(self) -> str:
        data = asdict(self)
        data = {k: str(v) if isinstance(v, Path) else v for k, v in data.items()}
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    def with_(self, **changes) -> "Settings":
        return replace(self, **changes)


def _abs_path(name: str, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise SettingsError(f"{name} must be an absolute path")
    return path


def default_converter() -> Path | None:
    exe = Path(sys.executable).parent / "Scripts" / "hwp5proc.exe"
    if exe.exists():
        return exe
    found = shutil.which("hwp5proc")
    return Path(found).resolve() if found else None


def load_settings(**overrides) -> Settings:
    """Resolve from the repository location and RFP_* environment variables, never the working directory."""
    config: dict = {}
    if os.environ.get("RFP_CONFIG_FILE"):
        cfg_path = _abs_path("RFP_CONFIG_FILE", os.environ["RFP_CONFIG_FILE"])
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    source = os.environ.get("RFP_SOURCE_DIR")
    data = os.environ.get("RFP_DATA_DIR")
    conv = os.environ.get("RFP_HWP_CONVERTER")
    values = dict(
        source_dir=_abs_path("RFP_SOURCE_DIR", source) if source else REPO_ROOT / "원본 데이터",
        data_dir=_abs_path("RFP_DATA_DIR", data) if data else REPO_ROOT / ".runtime",
        hwp_converter=_abs_path("RFP_HWP_CONVERTER", conv) if conv else default_converter(),
    )
    known = {f for f in Settings.__dataclass_fields__}
    unknown = set(config) - known
    if unknown:
        raise SettingsError(f"unknown configuration keys: {sorted(unknown)}")
    values.update(config)
    values.update(overrides)
    settings = Settings(**values)
    validate(settings)
    return settings


def validate(s: Settings) -> None:
    if not 1 <= s.database_pool_max <= 16 or not 1 <= s.database_timeout_seconds <= 30:
        raise SettingsError("database pool maximum must be 1..16 and timeout 1..30 seconds")
    if not os.environ.get(s.database_dsn_env):
        raise SettingsError(f"{s.database_dsn_env} is required: the application runs only on its validated "
                            "PostgreSQL database and has no fallback")
    if s.provider not in ("openai", "fake"):
        raise SettingsError("provider must be 'openai' or 'fake'")
    if s.generation_model not in ALLOWED_GENERATION_MODELS or s.generation_model not in DEFAULT_RATES:
        raise SettingsError(f"generation_model {s.generation_model!r} is not an answer model; allowed: "
                            f"{', '.join(ALLOWED_GENERATION_MODELS)}")
    if s.embedding_model not in ALLOWED_EMBEDDING_MODELS:
        raise SettingsError(f"embedding model {s.embedding_model!r} is not a compared model")
    if EMBEDDINGS[s.embedding_model].backend != "openai" and s.embedding_dimensions != EMBEDDINGS[s.embedding_model].dims:
        raise SettingsError(f"{s.embedding_model} produces {EMBEDDINGS[s.embedding_model].dims} dimensions")
    if s.reranker_model not in RERANKERS:
        raise SettingsError(f"reranker {s.reranker_model!r} is not a compared model")
    if not 0 < s.evidence_target_tokens <= s.evidence_max_tokens:
        raise SettingsError("evidence target must be positive and not exceed the evidence maximum")
    if s.generation_reasoning_effort not in REASONING_EFFORTS:
        raise SettingsError(f"generation_reasoning_effort must be one of {REASONING_EFFORTS}")
    if not 0 < s.generation_max_output_tokens <= 8000:
        raise SettingsError("generation_max_output_tokens must be within 1..8000")
    if s.reranker_enabled:
        raise SettingsError("the reranker is enabled only through `activate-run` after its measured gate")
    if s.retrieval_mode not in ("kiwi_bm25",):
        raise SettingsError("retrieval_mode is the keyword default; other modes are activated with `activate-run`")
    if not 1 <= s.embedding_dimensions <= EMBEDDING_MAX_DIMENSIONS.get(s.embedding_model,
                                                                        EMBEDDINGS[s.embedding_model].dims):
        raise SettingsError("embedding_dimensions exceeds the model's native size")
    if not 1 <= s.embedding_batch_inputs <= EMBEDDING_MAX_INPUTS_PER_REQUEST:
        raise SettingsError(f"embedding_batch_inputs must be within 1..{EMBEDDING_MAX_INPUTS_PER_REQUEST}")
    if not 1 <= s.embedding_batch_tokens <= EMBEDDING_MAX_TOKENS_PER_REQUEST:
        raise SettingsError(f"embedding_batch_tokens must be within 1..{EMBEDDING_MAX_TOKENS_PER_REQUEST}")
    if s.reranker_max_concurrency != 1:
        raise SettingsError("reranker_max_concurrency must be 1 until per-worker tokenizer isolation exists")
    if s.reranker_precision not in ("fp32", "fp16", "bf16"):
        raise SettingsError("reranker_precision must be 'fp32', 'fp16' or 'bf16'")
    if s.fusion not in ("rrf", "keyword_first") or not 0 < s.dense_weight <= 2 or not 0 <= s.keyword_head <= 100:
        raise SettingsError("fusion must be 'rrf' or 'keyword_first', dense_weight within (0, 2], keyword_head 0..100")
    if s.dense_search not in ("exact", "hnsw") or not 1 <= s.hnsw_ef_search <= 1000:
        raise SettingsError("dense_search must be 'exact' or 'hnsw' and hnsw_ef_search within 1..1000")
    if s.rrf_k < 1 or s.channel_top_k < 1 or s.fused_top_k < 1 or s.reranker_max_concurrency < 1:
        raise SettingsError("rrf_k, top-k depths and reranker concurrency must be positive")
    if not 1 <= s.request_workers <= 6 or not s.request_workers <= s.request_admission <= 48:
        raise SettingsError("request_workers must be 1..6 and request_admission within workers..48")
    if s.fake_delay_seconds and s.provider != "fake":
        raise SettingsError("fake_delay_seconds applies only to provider 'fake'")
    if not 0 <= s.fake_delay_seconds <= 120 or not 0 <= s.shutdown_wait_seconds <= 300:
        raise SettingsError("fake_delay_seconds must be within 0..120 and shutdown_wait_seconds within 0..300")
    s.data_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(s.data_dir, os.W_OK):
        raise SettingsError("runtime directory is not writable")


def rate_card(model: str) -> dict[str, Decimal]:
    try:
        return {k: Decimal(v) for k, v in DEFAULT_RATES[model].items()}
    except KeyError:
        raise SettingsError(f"no verified rate for model {model!r}") from None


TRACING_ENV = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")


def tracing_credentials() -> tuple[str, str, str] | None:
    """Langfuse host and project keys, read like the API key. Any one missing disables tracing."""
    values = tuple(read_api_key(name) for name in TRACING_ENV)
    return values if all(values) else None  # type: ignore[return-value]


def read_api_key(name: str) -> str | None:
    """Process environment, then the repository's gitignored .env. Never logged."""
    key = os.environ.get(name)
    if key:
        return key
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            found, sep, value = line.strip().partition("=")
            if sep and found.strip() == name and value.strip().strip("'\""):
                return value.strip().strip("'\"")
    return None
