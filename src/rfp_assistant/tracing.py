"""Langfuse traces of /api/ask requests and drafting runs. Observability only (docs/rag/budget.md): admission,
reservation and settlement never read anything from here.

`Tracing` is owned like the OpenAI transport: `service.Resources` or a standalone drafting run creates it, lends it
to its workers and closes it (flush, shutdown, unregister). The SDK keeps one resource manager per public key for
the whole process, so a second owner of the same key is refused instead of silently sharing the first's threads.
Every SDK call is guarded: a missing, unreachable or failing Langfuse never changes an answer or the ledger.
`mask_otel_spans` is the single exit gate: every exported string attribute passes `mask` first."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
from contextlib import contextmanager

from .settings import Settings, tracing_credentials

log = logging.getLogger(__name__)

SECRET = re.compile(
    r"(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"  # OpenAI, Langfuse (pk-lf-/sk-lf-), Anthropic, Stripe-style keys
    r"|AIza[0-9A-Za-z_\-]{30,}"  # Google API keys
    r"|(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[abpors]-[A-Za-z0-9\-]{10,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"  # JWT
    r"|(?i:bearer|basic)\s+[A-Za-z0-9._~+/=\-]{16,}"
    r"|(?i:api[_-]?key|secret[_-]?key|client[_-]?secret|password|passwd|access[_-]?token|auth[_-]?token)"
    r"[\\\"']*\s*[:=]\s*[\\\"']*[^\s\\\"',}]{8,}")  # quotes may be JSON-escaped once or more
REDACTED = "[REDACTED]"


def mask(value: str) -> str:
    return SECRET.sub(REDACTED, value)


def mask_otel_spans(*, params):
    """Langfuse export hook: sparse patches replacing every secret-shaped substring in string attributes."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        changed = {}
        for key, value in span.attributes.items():
            if isinstance(value, str):
                masked = mask(value)
            elif isinstance(value, (list, tuple)) and value and all(isinstance(v, str) for v in value):
                masked = [mask(v) for v in value]
                masked = masked if masked != list(value) else value
            else:
                continue
            if masked != value:
                changed[key] = masked
        if changed:
            patches[identifier] = OtelSpanPatch(set_attributes=changed)
    return MaskOtelSpansResult(span_patches=patches)


class TracingError(RuntimeError):
    pass


_REGISTRY_LOCK = threading.Lock()


class Tracing:
    """One Langfuse client on its own TracerProvider (never the global one), owned by whoever created it."""

    def __init__(self, host: str, public_key: str, secret_key: str, *, span_exporter=None) -> None:
        from langfuse import Langfuse
        from langfuse._client.resource_manager import LangfuseResourceManager
        from langfuse.span_filter import is_langfuse_span
        from opentelemetry.sdk.trace import TracerProvider

        self._registry = LangfuseResourceManager._instances  # private: the SDK offers no per-key release
        self._key = public_key
        self._closed = False
        with _REGISTRY_LOCK:
            if public_key in self._registry:
                raise TracingError("this Langfuse public key already has an owner in this process")
            self._provider = TracerProvider()
            self.client = Langfuse(public_key=public_key, secret_key=secret_key, base_url=host, timeout=5,
                                   tracer_provider=self._provider, should_export_span=is_langfuse_span,
                                   mask_otel_spans=mask_otel_spans, span_exporter=span_exporter)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Tracing | None":
        """None when any LANGFUSE_* value is missing or the fake provider is selected: no client is built."""
        creds = tracing_credentials()
        if settings.provider != "openai" or creds is None:
            return None
        try:
            return cls(*creds)
        except Exception as exc:  # noqa: BLE001 - tracing is optional; answering continues without it
            log.warning("Langfuse tracing disabled: %s", type(exc).__name__)
            return None

    def close(self) -> None:
        """Flushes pending traces and scores, stops the SDK's threads and frees the key for a later owner."""
        with _REGISTRY_LOCK:
            if self._closed:
                return
            self._closed = True
            try:
                _guard(lambda: self.client.shutdown())
                _guard(lambda: self._provider.shutdown())
            finally:
                self._registry.pop(self._key, None)


_ACTIVE: contextvars.ContextVar[Tracing | None] = contextvars.ContextVar("rfp_tracing", default=None)


class Step:
    """Handle of one observation; every method is a no-op without one and never raises."""

    def __init__(self, obs) -> None:
        self.obs = obs

    def update(self, **kw) -> None:
        if self.obs is not None:
            _guard(self.obs.update, **kw)

    def score_trace(self, name: str, value: float, data_type: str) -> None:
        if self.obs is not None:
            _guard(self.obs.score_trace, name=name, value=value, data_type=data_type)


@contextmanager
def run(tracing: Tracing | None, name: str, *, seed: str, input, user_id: str, metadata: dict[str, str],
        tags: list[str]):
    """The root observation of one trace (trace ID derived from `seed`). Steps opened on this thread inside it
    nest under it; outside any run, `step` records nothing (verifier traces, evaluation runs)."""
    if tracing is None or tracing._closed:
        yield Step(None)
        return
    from langfuse import propagate_attributes

    client = tracing.client
    token = _ACTIVE.set(tracing)
    try:
        with _observe(lambda: propagate_attributes(user_id=user_id, metadata=metadata, tags=tags, trace_name=name)):
            with _observe(lambda: client.start_as_current_observation(
                    name=name, as_type="span", input=input,
                    trace_context={"trace_id": client.create_trace_id(seed=seed)})) as root:
                yield root
    finally:
        _ACTIVE.reset(token)


@contextmanager
def step(name: str, as_type: str = "span", **kw):
    """A child observation of the current run, or nothing when no run is active on this thread."""
    tracing = _ACTIVE.get()
    if tracing is None:
        yield Step(None)
        return
    with _observe(lambda: tracing.client.start_as_current_observation(name=name, as_type=as_type, **kw)) as s:
        yield s


def readable(content: str | None):
    """Structured output shown as the object it is; anything unparsable stays the raw text."""
    try:
        return json.loads(content or "")
    except ValueError:
        return content


def usage_and_cost(usage: dict, settlement: dict) -> dict:
    """Provider-reported tokens and the ledger's settled cost (display only; Langfuse has no price for the model)."""
    cached = int(usage.get("cached_tokens") or 0)
    prompt, completion = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return {"usage_details": {"input": prompt - cached, "cache_read_input_tokens": cached, "output": completion,
                              "total": prompt + completion},
            "cost_details": {"total": settlement["settled_micro_usd"] / 1_000_000}}


@contextmanager
def _observe(open_cm):
    """Enters an SDK context manager without letting its failures reach the body, and never hides the body's."""
    cm = obs = None
    try:
        cm = open_cm()
        obs = cm.__enter__()
    except Exception as exc:  # noqa: BLE001
        log.warning("Langfuse observation skipped: %s", type(exc).__name__)
        cm = None
    exc_info = (None, None, None)
    try:
        yield Step(obs)
    except BaseException as exc:
        exc_info = (type(exc), exc, exc.__traceback__)
        raise
    finally:
        if cm is not None:
            try:
                cm.__exit__(*exc_info)
            except Exception as exc:  # noqa: BLE001
                log.warning("Langfuse observation not closed cleanly: %s", type(exc).__name__)


def _guard(fn, *args, **kw) -> None:
    try:
        fn(*args, **kw)
    except Exception as exc:  # noqa: BLE001 - tracing must never change the traced work
        log.warning("Langfuse call %s failed: %s", getattr(fn, "__name__", "?"), type(exc).__name__)
