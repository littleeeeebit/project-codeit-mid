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

REPO_ROOT = Path(__file__).resolve().parents[2]

# USD per one million tokens, standard tier, short context (<= 272K input tokens), from
# https://developers.openai.com/api/docs/pricing checked 2026-09-30. Cache writes bill 1.25x input on
# GPT-5.6 and later. configure-budget snapshots the rates it was given.
RATE_VERSION = "openai-standard-gpt-6-luna-2026-09-30"
DEFAULT_RATES: dict[str, dict[str, str]] = {
    "gpt-6-luna": {"input": "0.10", "cached_input": "0.01", "cache_write": "0.125", "output": "0.50"},
    "text-embedding-3-small": {"input": "0.02"},
}
ALLOWED_GENERATION_MODELS = ("gpt-6-luna",)
REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
ALLOWED_EMBEDDING_MODELS = ("text-embedding-3-small",)
# Embedding endpoint limits as documented in the pinned SDK (openai 3.22.1, embedding_create_params.py):
# 8192 tokens per input, at most 2048 inputs per array, 300,000 tokens summed across one request.
EMBEDDING_MAX_TOKENS_PER_INPUT = 8192
EMBEDDING_MAX_INPUTS_PER_REQUEST = 2048
EMBEDDING_MAX_TOKENS_PER_REQUEST = 300_000
EMBEDDING_MAX_DIMENSIONS = {"text-embedding-3-small": 1536}
EMBEDDING_TOKENIZER = {"text-embedding-3-small": "cl100k_base"}


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    source_dir: Path
    data_dir: Path
    hwp_converter: Path | None
    provider: str = "openai"  # "openai" or "fake"; fake refuses to build a real SDK client
    generation_model: str = "gpt-6-luna"
    generation_reasoning_effort: str = "low"
    embedding_model: str = "text-embedding-3-small"
    evidence_target_tokens: int = 3000
    evidence_max_tokens: int = 5000  # keeps every prompt far below the 272K long-context price tier
    evidence_max_units: int = 6
    generation_max_output_tokens: int = 2000  # includes reasoning tokens
    question_max_characters: int = 2000
    retrieval_mode: str = "kiwi_bm25"  # default until `activate-run` records a measured selection
    reranker_enabled: bool = False
    embedding_dimensions: int = 1536
    embedding_batch_inputs: int = 256
    embedding_batch_tokens: int = 100_000
    embedding_estimate_ttl_hours: int = 24
    channel_top_k: int = 20  # BM25 top 20 and dense top 20
    fused_top_k: int = 20
    rrf_k: int = 60
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_revision: str = ""  # commit hash from the model card; the trial refuses to load an unpinned model
    reranker_max_length: int = 512
    reranker_max_concurrency: int = 1
    request_timeout_seconds: float = 60.0
    converter_timeout_seconds: float = 300.0
    framing_margin_tokens: int = 200
    extra: dict = field(default_factory=dict)

    @property
    def csv_path(self) -> Path:
        return self.source_dir / "data_list.csv"

    @property
    def files_dir(self) -> Path:
        return self.source_dir / "files"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rfp.sqlite3"

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
    if s.provider not in ("openai", "fake"):
        raise SettingsError("provider must be 'openai' or 'fake'")
    if s.generation_model not in ALLOWED_GENERATION_MODELS or s.generation_model not in DEFAULT_RATES:
        raise SettingsError(f"generation model {s.generation_model!r} is not allowlisted with a known rate")
    if s.embedding_model not in ALLOWED_EMBEDDING_MODELS:
        raise SettingsError(f"embedding model {s.embedding_model!r} is not allowlisted")
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
    if not 1 <= s.embedding_dimensions <= EMBEDDING_MAX_DIMENSIONS.get(s.embedding_model, 0):
        raise SettingsError("embedding_dimensions exceeds the model's native size")
    if not 1 <= s.embedding_batch_inputs <= EMBEDDING_MAX_INPUTS_PER_REQUEST:
        raise SettingsError(f"embedding_batch_inputs must be within 1..{EMBEDDING_MAX_INPUTS_PER_REQUEST}")
    if not 1 <= s.embedding_batch_tokens <= EMBEDDING_MAX_TOKENS_PER_REQUEST:
        raise SettingsError(f"embedding_batch_tokens must be within 1..{EMBEDDING_MAX_TOKENS_PER_REQUEST}")
    if s.rrf_k < 1 or s.channel_top_k < 1 or s.fused_top_k < 1 or s.reranker_max_concurrency < 1:
        raise SettingsError("rrf_k, top-k depths and reranker concurrency must be positive")
    s.data_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(s.data_dir, os.W_OK):
        raise SettingsError("runtime directory is not writable")


def rate_card(model: str) -> dict[str, Decimal]:
    try:
        return {k: Decimal(v) for k, v in DEFAULT_RATES[model].items()}
    except KeyError:
        raise SettingsError(f"no verified rate for model {model!r}") from None


def read_api_key(name: str) -> str | None:
    """Process environment, then the repository's gitignored .env, then Streamlit secrets. Never logged."""
    key = os.environ.get(name)
    if key:
        return key
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            found, sep, value = line.strip().partition("=")
            if sep and found.strip() == name and value.strip().strip("'\""):
                return value.strip().strip("'\"")
    try:  # private Streamlit secrets, when running inside Streamlit
        import streamlit as st

        return st.secrets.get(name)
    except Exception:  # noqa: BLE001 - no secrets file is a normal state
        return None
