"""Compared embedding models and rerankers: pinned identity, documented prefixes, and the backends that run them.

Every compared model has one registry entry. Its key is what a run records and what `Settings.embedding_model` or
`reranker_model` names. Serving and the comparison runner go through the same entry, so an activated row runs the
model exactly as it was measured. Local models run on the GPU in the calling process; API embeddings go through a
ledger: OpenAI through the shared allowance (`budget`), Gemini through its own cap (`external_*` tables).
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from dataclasses import dataclass, field

import numpy as np

LEGACY_POLICY = "nfc-strip:l2-unit:float32"  # dense.EMBED_POLICY: the OpenAI vectors keep their cache identity


class ModelError(RuntimeError):
    pass


# ---------------------------------------------------------------- registry


@dataclass(frozen=True)
class EmbeddingSpec:
    key: str
    backend: str  # "openai" | "gemini" | "local"
    dims: int
    licence: str
    context: int  # documented maximum input tokens
    revision: str | None = None
    query_prompt: str = ""  # literal prefix from the model card / its sentence-transformers config
    doc_prompt: str = ""
    task: tuple[str, str] | None = None  # (query, document) task names: jina LoRA adapters, Gemini task types
    precision: str = "fp16"
    trust_remote_code: bool = False
    note: str = ""

    @property
    def policy(self) -> str:
        """Part of every payload hash: a changed revision, prefix or task makes different vectors."""
        if self.backend == "openai":
            return LEGACY_POLICY
        digest = hashlib.sha256(json.dumps([self.revision, self.query_prompt, self.doc_prompt, self.task,
                                            self.precision]).encode()).hexdigest()[:16]
        return f"{LEGACY_POLICY}:{self.backend}:{digest}"

    @property
    def kinds_differ(self) -> bool:
        """Query and document vectors of one text differ (a prefix or a task type)."""
        return bool(self.query_prompt or self.doc_prompt or self.task)


_QWEN_QUERY = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
EMBEDDINGS: dict[str, EmbeddingSpec] = {s.key: s for s in (
    EmbeddingSpec("text-embedding-3-large", "openai", 1536, "OpenAI API terms", 8192,
                  note="existing vectors, native 3072 shortened by the API to 1536"),
    EmbeddingSpec("text-embedding-3-small", "openai", 1536, "OpenAI API terms", 8192),
    EmbeddingSpec("gemini-embedding-001", "gemini", 3072, "Gemini API terms", 2048,
                  task=("RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT")),
    EmbeddingSpec("BAAI/bge-m3", "local", 1024, "mit", 8192, "5617a9f61b028005a4858fdac845db406aefb181",
                  note="dense output only"),
    EmbeddingSpec("nlpai-lab/KURE-v1", "local", 1024, "mit", 8192, "8b418a58414668e75532ed045c22d9ca018ae2b2"),
    EmbeddingSpec("dragonkue/BGE-m3-ko", "local", 1024, "apache-2.0", 8192,
                  "7074d66aa46562342193ca4feb3d89bf9dad71b4"),
    EmbeddingSpec("Qwen/Qwen3-Embedding-0.6B", "local", 1024, "apache-2.0", 32768,
                  "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3", query_prompt=_QWEN_QUERY),
    EmbeddingSpec("Snowflake/snowflake-arctic-embed-l-v2.0", "local", 1024, "apache-2.0", 8192,
                  "ac6544c8a46e00af67e330e85a9028c66b8cfd9a", query_prompt="query: "),
    EmbeddingSpec("dragonkue/snowflake-arctic-embed-l-v2.0-ko", "local", 1024, "apache-2.0", 8192,
                  "55ec6e9358a56d56af759bc8372e970caf8c305f", query_prompt="query: "),
    EmbeddingSpec("ibm-granite/granite-embedding-278m-multilingual", "local", 768, "apache-2.0", 512,
                  "a9cb5338491faf32b73dd17b714a31821c021bbf"),
    EmbeddingSpec("google/embeddinggemma-300m", "local", 768, "gemma", 2048,
                  "57c266a740f537b4dc058e1b0cda161fd15afa75", query_prompt="task: search result | query: ",
                  doc_prompt="title: none | text: ", precision="bf16",
                  note="gated on Hugging Face: needs an accepted licence and HF_TOKEN; float16 is unsupported"),
    EmbeddingSpec("jinaai/jina-embeddings-v3", "local", 1024, "cc-by-nc-4.0", 8194,
                  "ab036b023d30b4d1138c4c3bfa9f0c445ab455d6", task=("retrieval.query", "retrieval.passage"),
                  trust_remote_code=True, note="non-commercial licence; task LoRA adapters"),
    EmbeddingSpec("Alibaba-NLP/gte-multilingual-base", "local", 768, "apache-2.0", 8192,
                  "9bbca17d9273fd0d03d5725c7a4b0f6b45142062", trust_remote_code=True),
)}


@dataclass(frozen=True)
class RerankerSpec:
    key: str
    backend: str  # "cross" (sequence classification) | "yesno" (causal LM token score) | "layerwise"
    revision: str
    licence: str
    precision: str = "fp16"
    max_length: int = 1024  # the same input length for every compared reranker, in the model's own tokens
    trust_remote_code: bool = False
    template: str = ""  # yes/no prompt family: "bge-llm", "qwen3", "mxbai"
    layer: int | None = None  # layerwise cutoff
    batch: int = 16


RERANKERS: dict[str, RerankerSpec] = {s.key: s for s in (
    RerankerSpec("BAAI/bge-reranker-v2-m3", "cross", "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e", "apache-2.0"),
    RerankerSpec("dragonkue/bge-reranker-v2-m3-ko", "cross", "2aca5884ecac490192af9ebd86836d9073d826cd",
                 "apache-2.0"),
    RerankerSpec("BAAI/bge-reranker-v2-gemma", "yesno", "1787044f8b6fb740a9de4557c3a12377f84d9e17", "apache-2.0",
                 precision="bf16", template="bge-llm", batch=4),
    RerankerSpec("BAAI/bge-reranker-v2-minicpm-layerwise", "layerwise", "47b5332b296c4d8cb6ee2c60502cc62a0d708881",
                 "apache-2.0", precision="bf16", trust_remote_code=True, template="bge-llm", layer=28, batch=4),
    RerankerSpec("Qwen/Qwen3-Reranker-0.6B", "yesno", "e61197ed45024b0ed8a2d74b80b4d909f1255473", "apache-2.0",
                 precision="bf16", template="qwen3", batch=8),
    RerankerSpec("mixedbread-ai/mxbai-rerank-base-v2", "yesno", "3ea9d4dffa7d12a4f366be8e275c349de9fc9865",
                 "apache-2.0", precision="bf16", template="mxbai", batch=8),
    RerankerSpec("mixedbread-ai/mxbai-rerank-large-v2", "yesno", "ca7e1ee484c37c0ddd8d178a9a5c33cec575c5e6",
                 "apache-2.0", precision="bf16", template="mxbai", batch=4),
    RerankerSpec("Alibaba-NLP/gte-multilingual-reranker-base", "cross", "8215cf04918ba6f7b6a62bb44238ce2953d8831c",
                 "apache-2.0", trust_remote_code=True),
)}


def embedding_spec(key: str) -> EmbeddingSpec:
    try:
        return EMBEDDINGS[key]
    except KeyError:
        raise ModelError(f"embedding model {key!r} is not a compared model") from None


def reranker_spec(key: str) -> RerankerSpec:
    try:
        return RERANKERS[key]
    except KeyError:
        raise ModelError(f"reranker {key!r} is not a compared model") from None


def model_size_bytes(key: str, revision: str | None) -> int | None:
    """Weight files of the pinned snapshot on local disk (safetensors, else PyTorch .bin)."""
    try:
        from huggingface_hub import snapshot_download

        root = snapshot_download(key, revision=revision, local_files_only=True)
    except Exception:  # noqa: BLE001 - not downloaded: unknown
        return None
    from pathlib import Path

    files = [p for p in Path(root).rglob("*.safetensors")] or [p for p in Path(root).rglob("*.bin")]
    return sum(p.stat().st_size for p in files) or None


GPU_HEADROOM_MB = 512  # dedicated memory left free for the CUDA context, the desktop and cuBLAS workspaces
_GPU_CAPPED = False


def _cuda():
    """torch on the CUDA GPU; local models never run on the CPU. The first call caps this process's allocator at the
    dedicated memory free right then: on Windows (WDDM) an allocation past dedicated VRAM silently spills into
    shared system memory and runs over PCIe at a fraction of the speed, so an out-of-memory error (which the
    callers answer by halving the batch) is the better outcome."""
    global _GPU_CAPPED
    import torch

    if not torch.cuda.is_available():
        raise ModelError("no CUDA GPU: local embedding models and rerankers run only on the GPU, never on the CPU")
    if not _GPU_CAPPED:
        free, total = torch.cuda.mem_get_info()
        torch.cuda.set_per_process_memory_fraction(max(0.1, (free - GPU_HEADROOM_MB * 2 ** 20) / total))
        _GPU_CAPPED = True
    return torch


def _halving(run, batch: int, what: str) -> tuple:
    """run(batch) on the GPU, halving the batch after an out-of-memory error; (result, the batch that fit)."""
    import torch

    while True:
        try:
            return run(batch), batch
        except torch.OutOfMemoryError:
            free_gpu()
            if batch == 1:
                raise ModelError(f"{what}: out of GPU memory at batch 1") from None
            batch = max(1, batch // 2)


def _dtype(precision: str):
    import torch

    return {"fp16": torch.float16, "bf16": torch.bfloat16, "fp32": torch.float32}[precision]


def free_gpu() -> None:
    import gc

    gc.collect()
    import torch

    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _extended_attention_mask(self, attention_mask, input_shape=None, device=None, dtype=None):
    """transformers 4's encoder `get_extended_attention_mask`, which gte's remote code still calls: 1 keeps a
    position, 0 adds the dtype's minimum before the softmax."""
    import torch

    dtype = dtype or self.dtype
    ext = attention_mask[:, None, :, :] if attention_mask.dim() == 3 else attention_mask[:, None, None, :]
    ext = ext.to(dtype=dtype)
    return (1.0 - ext) * torch.finfo(dtype).min


def prepare_remote_class(key: str, revision: str | None) -> None:
    """jina-v3's `XLMRobertaLoRA` never runs transformers 5's `post_init`, which sets `all_tied_weights_keys`; the
    loader then fails. The model ties no weights, so the empty default is the value post_init would set."""
    from transformers import AutoConfig
    from transformers.dynamic_module_utils import get_class_from_dynamic_module

    ref = (getattr(AutoConfig.from_pretrained(key, revision=revision, trust_remote_code=True), "auto_map", None)
           or {}).get("AutoModel")
    if ref:
        cls = get_class_from_dynamic_module(ref, key, revision=revision)
        if not hasattr(cls, "all_tied_weights_keys"):
            cls.all_tied_weights_keys = {}


def _legacy_causal_config(spec: "RerankerSpec"):
    """minicpm-layerwise's remote code (written for transformers 4) under transformers 5: the removed
    `is_torch_fx_available` (fx is always present now), the normalized `rope_scaling` it reads as a legacy dict
    (`{"rope_type": "default"}` means the null in its config.json), and its list-form `_tied_weights_keys`
    (scoring uses the per-layer heads, never the tied LM head)."""
    import transformers.utils.import_utils as import_utils
    from transformers import AutoConfig
    from transformers.dynamic_module_utils import get_class_from_dynamic_module

    if not hasattr(import_utils, "is_torch_fx_available"):
        import_utils.is_torch_fx_available = lambda: True
    config = AutoConfig.from_pretrained(spec.key, revision=spec.revision, trust_remote_code=True)
    if (getattr(config, "rope_scaling", None) or {}).get("rope_type") == "default":
        config.rope_scaling = None
    ref = (getattr(config, "auto_map", None) or {}).get("AutoModelForCausalLM")
    if ref:
        cls = get_class_from_dynamic_module(ref, spec.key, revision=spec.revision)
        if isinstance(getattr(cls, "_tied_weights_keys", None), list):
            cls._tied_weights_keys = None
    return config


def restore_buffers(module) -> int:
    """Recompute the non-persistent buffers of remote-code models (gte's `NewModel`). transformers 5 builds models on
    the meta device and loads only the saved weights, so a buffer a constructor computed (position IDs, rotary
    frequencies and caches) is left as uninitialized memory: gte then gathers out of bounds. The values are the
    constructor's own formulas."""
    import types

    import torch

    fixed = 0
    for m in module.modules():
        if type(m).__name__ == "NewModel" and not hasattr(m, "get_extended_attention_mask"):
            m.get_extended_attention_mask = types.MethodType(_extended_attention_mask, m)  # removed in transformers 5
        buf = getattr(m, "position_ids", None)
        if isinstance(buf, torch.Tensor) and buf.dim() == 1 and "position_ids" in dict(m.named_buffers(recurse=False)):
            buf.copy_(torch.arange(buf.numel(), device=buf.device))
            fixed += 1
        if all(hasattr(m, a) for a in ("inv_freq", "dim", "base", "_set_cos_sin_cache", "max_seq_len_cached")):
            dtype = m.cos_cached.dtype if hasattr(m, "cos_cached") else torch.float32
            device = m.inv_freq.device
            m.inv_freq = 1.0 / (m.base ** (torch.arange(0, m.dim, 2, device=device).float() / m.dim))
            m._set_cos_sin_cache(m.max_seq_len_cached, device, dtype)
            fixed += 1
    return fixed


def _versions() -> dict:
    import importlib.metadata as md

    out = {}
    for pkg in ("torch", "transformers", "sentence-transformers"):
        try:
            out[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            out[pkg] = None
    return out


# ---------------------------------------------------------------- local embeddings


class LocalEmbedder:
    """A sentence-transformers model loaded once on the GPU; calls are serialized (one shared tokenizer)."""

    def __init__(self, spec: EmbeddingSpec) -> None:
        from sentence_transformers import SentenceTransformer

        torch = _cuda()
        device = "cuda"
        torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        self.spec = spec
        self.batch = 32  # lowered for good after an out-of-memory error
        if spec.trust_remote_code:
            prepare_remote_class(spec.key, spec.revision)
        self.model = SentenceTransformer(spec.key, revision=spec.revision, device=device,
                                         trust_remote_code=spec.trust_remote_code,
                                         model_kwargs={"dtype": _dtype(spec.precision)})
        self.restored_buffers = restore_buffers(self.model) if spec.trust_remote_code else 0
        self.cold_load_seconds = round(time.perf_counter() - t0, 2)
        prompts = getattr(self.model, "prompts", None) or {}
        for name, mine in (("query", spec.query_prompt), ("document", spec.doc_prompt)):
            if prompts.get(name) not in (None, mine):  # the card's prompt is the documented one; never guess
                raise ModelError(f"{spec.key}: the model's {name} prompt {prompts[name]!r} differs from the registry")
        self.max_length = int(self.model.max_seq_length)
        dim = self.model.get_sentence_embedding_dimension() if hasattr(self.model,
                                                                       "get_sentence_embedding_dimension") else None
        if dim is not None and dim != spec.dims:
            raise ModelError(f"{spec.key}: {dim} dimensions, the registry says {spec.dims}")
        self._lock = threading.Lock()
        self.info = {"model": spec.key, "revision": spec.revision, "device": device, "precision": spec.precision,
                     "max_length": self.max_length, "cold_load_seconds": self.cold_load_seconds,
                     "versions": _versions()}
        self.info["vram_after_load_mb"] = round(torch.cuda.max_memory_allocated() / 2 ** 20, 1)

    def prefixed(self, texts: list[str], kind: str) -> list[str]:
        prompt = self.spec.query_prompt if kind == "query" else self.spec.doc_prompt
        return [prompt + t for t in texts]

    def truncated(self, texts: list[str], kind: str) -> int:
        tok = self.model.tokenizer
        return sum(len(ids) > self.max_length for ids in tok(self.prefixed(texts, kind),
                                                             add_special_tokens=True)["input_ids"])

    def embed(self, texts: list[str], kind: str, batch_size: int | None = None) -> np.ndarray:
        extra = {}
        if self.spec.task:
            extra["task"] = self.spec.task[0 if kind == "query" else 1]
        prefixed = self.prefixed(texts, kind)
        with self._lock:
            vecs, self.batch = _halving(lambda b: self.model.encode(
                prefixed, batch_size=b, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False,
                **extra), min(batch_size or self.batch, self.batch), self.spec.key)
        vecs = np.asarray(vecs, dtype=np.float32)
        if vecs.shape != (len(texts), self.spec.dims) or not np.isfinite(vecs).all():
            raise ModelError(f"{self.spec.key}: invalid output {vecs.shape}")
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        if (norms == 0).any():
            raise ModelError(f"{self.spec.key}: zero vector")
        return vecs / norms

    def peak_vram_mb(self) -> float:
        return round(_cuda().cuda.max_memory_allocated() / 2 ** 20, 1)


_EMBEDDERS: dict[str, LocalEmbedder] = {}
_EMBEDDERS_LOCK = threading.Lock()


def local_embedder(key: str) -> LocalEmbedder:
    """The process's one loaded copy of a local embedding model (serving loads the activated one on first use)."""
    with _EMBEDDERS_LOCK:
        if key not in _EMBEDDERS:
            _EMBEDDERS[key] = LocalEmbedder(embedding_spec(key))
        return _EMBEDDERS[key]


def unload_embedders(keep: str | None = None) -> None:
    with _EMBEDDERS_LOCK:
        for key in [k for k in _EMBEDDERS if k != keep]:
            del _EMBEDDERS[key]
    free_gpu()


# ---------------------------------------------------------------- Gemini embeddings and their own ledger

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:{method}"
GEMINI_BATCH = 100  # batchEmbedContents accepts at most 100 requests
GEMINI_PRICE_PER_MTOK = "0.15"  # USD per million input tokens, gemini-embedding-001 paid tier (ai.google.dev pricing)
GEMINI_PROVIDER = "gemini"


class GeminiError(ModelError):
    def __init__(self, message: str, pre_execution: bool) -> None:
        super().__init__(message)
        self.pre_execution = pre_execution


class GeminiClient:
    """REST client for gemini-embedding-001. The key stays in the request header, never in a URL or log."""

    def __init__(self, key: str, timeout: float = 60.0) -> None:
        import httpx

        self._http = httpx.Client(timeout=timeout, headers={"x-goog-api-key": key})

    def close(self) -> None:
        self._http.close()

    def _post(self, model: str, method: str, body: dict) -> dict:
        import httpx

        for wait in (5, 15, 45, None):  # a 429 is refused before execution: waiting and resending is not billed
            try:
                r = self._http.post(GEMINI_URL.format(model=model, method=method), json=body)
            except httpx.TimeoutException as exc:
                raise GeminiError(f"timeout: {exc}", pre_execution=False) from None
            except httpx.TransportError as exc:
                raise GeminiError(f"transport: {type(exc).__name__}", pre_execution=False) from None
            if r.status_code == 429 and wait is not None:
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                # 4xx is refused before any work; 5xx may have done the work: keep it uncertain
                raise GeminiError(f"HTTP {r.status_code}: {r.text[:200]}", pre_execution=r.status_code < 500)
            return r.json()
        raise GeminiError("rate limited", pre_execution=True)

    def count_tokens(self, texts: list[str]) -> int:
        body = {"contents": [{"parts": [{"text": t}]} for t in texts]}
        return int(self._post("gemini-embedding-001", "countTokens", body)["totalTokens"])

    def embed(self, texts: list[str], task: str, dims: int) -> list[list[float]]:
        body = {"requests": [{"model": "models/gemini-embedding-001", "content": {"parts": [{"text": t}]},
                              "taskType": task, "outputDimensionality": dims} for t in texts]}
        out = self._post("gemini-embedding-001", "batchEmbedContents", body)
        return [e["values"] for e in out["embeddings"]]


def gemini_cost_micro(tokens: int) -> int:
    from decimal import ROUND_CEILING, Decimal

    return int((Decimal(tokens) * Decimal(GEMINI_PRICE_PER_MTOK)).to_integral_value(rounding=ROUND_CEILING))


EXTERNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_ledger (
    provider text PRIMARY KEY, cap_micro_usd bigint NOT NULL CHECK(cap_micro_usd >= 0), price_json text NOT NULL,
    updated_by text NOT NULL, reason text NOT NULL, updated_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS external_attempts (
    attempt_id text PRIMARY KEY, provider text NOT NULL REFERENCES external_ledger(provider), purpose text NOT NULL,
    state text NOT NULL CHECK(state IN ('reserved','settled','released','unknown')),
    input_tokens bigint NOT NULL, reserved_micro_usd bigint NOT NULL, settled_micro_usd bigint,
    detail text, created_at text NOT NULL, finished_at text
);
CREATE TABLE IF NOT EXISTS rerank_scores (
    model_key text NOT NULL, pair_hash text NOT NULL, score double precision NOT NULL,
    truncated boolean NOT NULL, PRIMARY KEY(model_key, pair_hash)
);
"""


def external_status(db, provider: str = GEMINI_PROVIDER) -> dict:
    from .store import open_db

    with open_db(db) as conn:
        row = conn.execute("SELECT * FROM external_ledger WHERE provider = ?", (provider,)).fetchone()
        used = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN state='settled' THEN settled_micro_usd WHEN state IN ('reserved','unknown') "
            "THEN reserved_micro_usd ELSE 0 END),0), COALESCE(SUM(CASE WHEN state='unknown' THEN 1 ELSE 0 END),0) "
            "FROM external_attempts WHERE provider = ?", (provider,)).fetchone()
    if row is None:
        return {"provider": provider, "cap_micro_usd": None, "used_micro_usd": int(used[0]), "unknown": int(used[1])}
    return {"provider": provider, "cap_micro_usd": row["cap_micro_usd"], "used_micro_usd": int(used[0]),
            "unknown": int(used[1]), "available_micro_usd": row["cap_micro_usd"] - int(used[0]),
            "price": json.loads(row["price_json"]), "updated_by": row["updated_by"], "updated_at": row["updated_at"]}


def set_external_cap(db, cap_micro: int, actor: str, reason: str, provider: str = GEMINI_PROVIDER) -> dict:
    """The person's own cap for a provider outside the shared OpenAI allowance; never below what is committed."""
    from .store import dumps, open_db, tx, utcnow

    if not actor.strip() or not reason.strip():
        raise ModelError("setting a cap needs the person's name and a reason")
    with open_db(db) as conn, tx(conn, immediate=True):
        used = conn.execute("SELECT COALESCE(SUM(CASE WHEN state='settled' THEN settled_micro_usd WHEN state IN "
                            "('reserved','unknown') THEN reserved_micro_usd ELSE 0 END),0) FROM external_attempts "
                            "WHERE provider = ?", (provider,)).fetchone()[0]
        if cap_micro < used:
            raise ModelError(f"the cap would fall below the {used} micro-USD already committed")
        conn.execute("INSERT INTO external_ledger VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(provider) DO UPDATE SET "
                     "cap_micro_usd = excluded.cap_micro_usd, price_json = excluded.price_json, "
                     "updated_by = excluded.updated_by, reason = excluded.reason, updated_at = excluded.updated_at",
                     (provider, cap_micro, dumps({"usd_per_million_input_tokens": GEMINI_PRICE_PER_MTOK}), actor,
                      reason, utcnow()))
    return external_status(db, provider)


def external_reserve(db, tokens: int, purpose: str, provider: str = GEMINI_PROVIDER) -> dict:
    from .store import open_db, tx, utcnow

    amount = gemini_cost_micro(tokens)
    with open_db(db) as conn, tx(conn, immediate=True):
        row = conn.execute("SELECT cap_micro_usd FROM external_ledger WHERE provider = ?", (provider,)).fetchone()
        if row is None:
            return {"admitted": False, "reason": f"no {provider} cap set; the person sets it first"}
        used = conn.execute("SELECT COALESCE(SUM(CASE WHEN state='settled' THEN settled_micro_usd WHEN state IN "
                            "('reserved','unknown') THEN reserved_micro_usd ELSE 0 END),0), "
                            "COALESCE(SUM(CASE WHEN state='unknown' THEN 1 ELSE 0 END),0) FROM external_attempts "
                            "WHERE provider = ?", (provider,)).fetchone()
        if used[1]:
            return {"admitted": False, "reason": f"{provider} has attempts with unknown billing; resolve them first"}
        if row[0] - used[0] < amount:
            return {"admitted": False, "reason": f"{provider}_cap_exhausted", "reserved_micro_usd": amount}
        attempt_id = str(uuid.uuid4())
        conn.execute("INSERT INTO external_attempts(attempt_id, provider, purpose, state, input_tokens, "
                     "reserved_micro_usd, created_at) VALUES (?, ?, ?, 'reserved', ?, ?, ?)",
                     (attempt_id, provider, purpose, tokens, amount, utcnow()))
    return {"admitted": True, "attempt_id": attempt_id, "reserved_micro_usd": amount}


def external_finish(db, attempt_id: str, state: str, settled_micro: int | None = None, detail: str = "") -> None:
    from .store import open_db, tx, utcnow

    with open_db(db) as conn, tx(conn, immediate=True):
        conn.execute("UPDATE external_attempts SET state = ?, settled_micro_usd = ?, detail = ?, finished_at = ? "
                     "WHERE attempt_id = ? AND state = 'reserved'", (state, settled_micro, detail[:300], utcnow(),
                                                                     attempt_id))


def external_resolve(db, attempt_id: str, charged: bool, actor: str, reason: str) -> dict:
    """The person's resolution of an attempt with unknown billing (a timeout after dispatch): charged settles it at
    its reserved amount, not charged releases it. Its vectors were never cached, so a later run asks for them again."""
    from .store import open_db, tx, utcnow

    if not actor.strip() or not reason.strip():
        raise ModelError("resolving an attempt needs the person's name and a reason")
    with open_db(db) as conn, tx(conn, immediate=True):
        row = conn.execute("SELECT state, reserved_micro_usd FROM external_attempts WHERE attempt_id = ?",
                           (attempt_id,)).fetchone()
        if row is None or row[0] != "unknown":
            raise ModelError(f"attempt {attempt_id} is not an attempt with unknown billing")
        conn.execute("UPDATE external_attempts SET state = ?, settled_micro_usd = ?, detail = ?, finished_at = ? "
                     "WHERE attempt_id = ?", ("settled" if charged else "released", row[1] if charged else None,
                                              f"resolved by {actor}: {reason}"[:300], utcnow(), attempt_id))
    return {"attempt_id": attempt_id, "state": "settled" if charged else "released",
            "settled_micro_usd": row[1] if charged else 0}


def gemini_embed(db, client: GeminiClient, texts: list[str], kind: str, purpose: str, tokens: int) -> np.ndarray:
    """One metered batch under the Gemini cap. Gemini reports no usage for embeddings, so settlement uses the
    counted input tokens (countTokens, free). An uncertain outcome stays `unknown` and blocks further calls."""
    spec = EMBEDDINGS["gemini-embedding-001"]
    admission = external_reserve(db, tokens, purpose)
    if not admission["admitted"]:
        raise ModelError(admission["reason"])
    try:
        values = client.embed(texts, spec.task[0 if kind == "query" else 1], spec.dims)
    except GeminiError as exc:
        external_finish(db, admission["attempt_id"], "released" if exc.pre_execution else "unknown", None, str(exc))
        raise
    external_finish(db, admission["attempt_id"], "settled", gemini_cost_micro(tokens), "counted tokens")
    vecs = np.asarray(values, dtype=np.float32)
    if vecs.shape != (len(texts), spec.dims) or not np.isfinite(vecs).all():
        raise ModelError(f"gemini returned {vecs.shape}")
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


# ---------------------------------------------------------------- rerankers

_BGE_LLM_PROMPT = ("Given a query A and a passage B, determine whether the passage contains an answer to the query by "
                   "providing a prediction of either 'Yes' or 'No'.")
_QWEN3_PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the "
                 "Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n")
_QWEN3_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_QWEN3_INSTRUCT = "Given a web search query, retrieve relevant passages that answer the query"
_MXBAI_PREFIX = ("<|im_start|>system\nYou are Qwen, created by Alibaba Cloud. You are a helpful assistant.<|im_end|>\n"
                 "<|im_start|>user\n")
_MXBAI_TASK = ("You are a search relevance expert who evaluates how well documents match search queries. For each "
               "query-document pair, carefully analyze the semantic relationship between them, then provide your "
               "binary relevance judgment (0 for not relevant, 1 for relevant).\nRelevance:")
_MXBAI_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n"


@dataclass
class _Loaded:
    spec: RerankerSpec
    info: dict = field(default_factory=dict)


class LocalRerankerModel:
    """One compared reranker, loaded once. `rerank` keeps the interface of `dense.LocalReranker`: (order by score,
    ties by chunk ID; info with truncated pairs, queue and inference time). Scores order passages; they are never
    shown as probabilities."""

    def __init__(self, spec: RerankerSpec) -> None:
        torch = _cuda()
        self.device = "cuda"
        torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        self.spec = spec
        self.batch = spec.batch  # lowered for good after an out-of-memory error
        self.max_length = spec.max_length
        self._sem = threading.Lock()
        self._wait = threading.Lock()
        if spec.backend == "cross":
            from sentence_transformers import CrossEncoder

            self.model = CrossEncoder(spec.key, revision=spec.revision, max_length=spec.max_length, device=self.device,
                                      trust_remote_code=spec.trust_remote_code,
                                      model_kwargs={"dtype": _dtype(spec.precision)})
            self.tokenizer = self.model.tokenizer
        else:
            from transformers import AutoModelForCausalLM, AutoTokenizer

            self.tokenizer = AutoTokenizer.from_pretrained(spec.key, revision=spec.revision,
                                                           trust_remote_code=spec.trust_remote_code)
            self.tokenizer.padding_side = "left"
            if self.tokenizer.pad_token is None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            kwargs = {"config": _legacy_causal_config(spec)} if spec.trust_remote_code else {}
            self.model = AutoModelForCausalLM.from_pretrained(
                spec.key, revision=spec.revision, trust_remote_code=spec.trust_remote_code,
                dtype=_dtype(spec.precision), **kwargs).to(self.device).eval()
            self._prepare_template()
        if spec.trust_remote_code:
            restore_buffers(self.model)
        self.cold_load_seconds = round(time.perf_counter() - t0, 2)
        self.info = {"model": spec.key, "revision": spec.revision, "device": self.device, "precision": spec.precision,
                     "max_length": spec.max_length, "max_concurrency": 1, "backend": spec.backend,
                     "layer": spec.layer, "cold_load_seconds": self.cold_load_seconds, "license": spec.licence,
                     "versions": _versions()}
        self.info["vram_after_load_mb"] = round(torch.cuda.max_memory_allocated() / 2 ** 20, 1)

    # -- causal LM templates
    def _ids(self, text: str) -> list[int]:
        return self.tokenizer(text, add_special_tokens=False)["input_ids"]

    def _prepare_template(self) -> None:
        t = self.spec.template
        if t == "bge-llm":
            self._sep = self._ids("\n")
            self._prompt = self._ids(_BGE_LLM_PROMPT)
            self._yes = self._ids("Yes")[0]
        elif t == "qwen3":
            self._prefix, self._suffix = self._ids(_QWEN3_PREFIX), self._ids(_QWEN3_SUFFIX)
            self._yes, self._no = self.tokenizer.convert_tokens_to_ids("yes"), self.tokenizer.convert_tokens_to_ids("no")
        elif t == "mxbai":
            self._prefix, self._suffix = self._ids(_MXBAI_PREFIX), self._ids(_MXBAI_SUFFIX)
            self._task, self._sep = self._ids(_MXBAI_TASK), self._ids("\n")
            self._yes, self._no = self._ids("1")[0], self._ids("0")[0]
        else:
            raise ModelError(f"unknown reranker template {t!r}")

    def _pair_ids(self, q: str, p: str) -> tuple[list[int], bool]:
        """Token IDs of one pair within max_length; the passage is cut from its end when it does not fit (counted
        as a truncated pair)."""
        t, budget = self.spec.template, self.max_length
        if t == "bge-llm":
            q_ids = self._ids(f"A: {q}")[: budget * 3 // 4]
            bos = [self.tokenizer.bos_token_id] if self.tokenizer.bos_token_id is not None else []
            fixed = len(bos) + len(q_ids) + 2 * len(self._sep) + len(self._prompt)
            p_ids = self._ids(f"B: {p}")
            room = max(1, budget - fixed)
            return bos + q_ids + self._sep + p_ids[:room] + self._sep + self._prompt, len(p_ids) > room
        if t == "qwen3":
            head = self._ids(f"<Instruct>: {_QWEN3_INSTRUCT}\n<Query>: {q}\n<Document>: ")
            p_ids = self._ids(p)
            room = max(1, budget - len(self._prefix) - len(self._suffix) - len(head))
            return self._prefix + head + p_ids[:room] + self._suffix, len(p_ids) > room
        q_ids = self._ids(f"query: {q}")[: budget * 3 // 4]
        p_ids = self._ids(f"document: {p}")
        room = max(1, budget - len(self._prefix) - len(self._suffix) - len(self._task) - 2 * len(self._sep) - len(q_ids))
        return (self._prefix + q_ids + self._sep + p_ids[:room] + self._sep + self._task + self._suffix,
                len(p_ids) > room)

    def _score_causal(self, pairs: list[tuple[str, str]]) -> tuple[list[float], list[bool]]:
        import torch

        built = [self._pair_ids(q, p) for q, p in pairs]
        order = sorted(range(len(built)), key=lambda i: len(built[i][0]))
        out = [0.0] * len(built)

        def forward(idx: list[int]) -> list[float]:
            batch = self.tokenizer.pad({"input_ids": [built[i][0] for i in idx]}, padding=True, return_tensors="pt")
            batch = {k: v.to(self.device) for k, v in batch.items()}
            with torch.inference_mode():
                if self.spec.backend == "layerwise":
                    res = self.model(**batch, return_dict=True, use_cache=False, cutoff_layers=[self.spec.layer])
                    vals = res[0][0][:, -1].view(-1).float()
                else:
                    # Only the last position's logits: the full [batch, length, vocabulary] tensor is about 2 GB per
                    # Gemma batch (256k vocabulary) and pushed the GPU into shared system memory. No KV cache either.
                    logits = self.model(**batch, use_cache=False, logits_to_keep=1).logits[:, -1, :].float()
                    if self.spec.template == "bge-llm":
                        vals = logits[:, self._yes]
                    elif self.spec.template == "qwen3":
                        vals = torch.log_softmax(torch.stack([logits[:, self._no], logits[:, self._yes]], 1), 1)[:, 1]
                    else:
                        vals = logits[:, self._yes] - logits[:, self._no]
            return vals.cpu().tolist()

        start = 0
        while start < len(order):
            idx_of = lambda b: order[start:start + b]  # noqa: E731 - the batch that fits decides the slice
            vals, self.batch = _halving(lambda b: forward(idx_of(b)), self.batch, self.spec.key)
            for i, v in zip(idx_of(self.batch), vals):
                out[i] = float(v)
            start += self.batch
        return out, [t for _, t in built]

    def score(self, pairs: list[tuple[str, str]]) -> tuple[list[float], list[bool]]:
        """(score, truncated) per pair. Serialized: the tokenizer keeps mutable padding/truncation state."""
        if not pairs:
            return [], []
        with self._sem:
            if self.spec.backend == "cross":
                tok = self.tokenizer
                flags = [len(tok(q, p)["input_ids"]) > self.max_length for q, p in pairs]
                vals, self.batch = _halving(lambda b: self.model.predict(pairs, batch_size=b, show_progress_bar=False),
                                            self.batch, self.spec.key)
                return [float(v) for v in vals], flags
            return self._score_causal(pairs)

    def rerank(self, question: str, chunks: list[dict]) -> tuple[list[tuple[int, float]], dict]:
        pairs = [(question, c["payload"]) for c in chunks]
        t0 = time.perf_counter()
        with self._wait:  # queue time: waiting for the one model
            t1 = time.perf_counter()
            scores, flags = self.score(pairs)
            t2 = time.perf_counter()
        truncated = sum(flags)
        if any(not math.isfinite(s) for s in scores):
            raise ModelError(f"{self.spec.key}: nonfinite score")
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], chunks[i]["chunk_id"]))
        return [(i, scores[i]) for i in order], {"truncated": truncated, "queue_ms": round((t1 - t0) * 1000, 1),
                                                 "infer_ms": round((t2 - t1) * 1000, 1)}

    def peak_vram_mb(self) -> float:
        return round(_cuda().cuda.max_memory_allocated() / 2 ** 20, 1)


def load_reranker_model(key: str) -> tuple[LocalRerankerModel | None, dict]:
    """(model, info) or (None, the failure as a row reason): an unknown model, missing download, out of memory or a
    crash in remote code all keep the bypass."""
    try:
        spec = reranker_spec(key)
        model = LocalRerankerModel(spec)
        return model, model.info
    except Exception as exc:  # noqa: BLE001 - every load failure is a reported row, never a crash of the caller
        free_gpu()
        return None, {"error": f"{type(exc).__name__}: {exc}"[:500], "model": key,
                      "revision": RERANKERS[key].revision if key in RERANKERS else None}
