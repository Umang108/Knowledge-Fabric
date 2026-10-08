"""Langfuse tracing: one trace per user operation (chat question, extraction, build, ingest, MCP tool call).

    with trace_context(user_id, session_id, "chat", kb_name):     # the trace: name, user, session, tags
        with observe_span("TurboQuant Retrieval", {...}): ...           # steps inside it
        with observe_llm("llm_text", messages) as gen: ...         # every model call (generation)

Switched on by LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY; without them every helper is a no-op.

Rules that keep traces complete and reliable (each one fixes a way traces went missing):
- Langfuse gets its OWN OpenTelemetry TracerProvider. If it became the global provider, FastAPI (>= 0.120 has
  built-in OpenTelemetry) would export every HTTP request - the UI polls every 1.5-3 s - to Langfuse: thousands
  of "GET /api/jobs/{id}" traces that bury the real ones and use up the ingestion quota / rate limit, after which
  real traces are dropped.
- Only the root observation sets the trace's name, user, session and tags. Children never touch them;
  otherwise the trace name flips between "Chat", "llm_text", "embedding_query", ... depending on which span
  Langfuse processes last, and filtering by name finds a trace one minute and not the next.
- Nothing is flushed inside a request or job. Spans are exported by the SDK's background thread every
  LANGFUSE_FLUSH_INTERVAL seconds; the app flushes once on shutdown. A synchronous flush per call used to add a
  network round trip (up to the 5 s timeout) to every LLM call.
- Inputs and outputs are trimmed to LANGFUSE_MAX_FIELD_CHARS. Full-sheet extraction prompts are hundreds of KB;
  an oversized export is rejected as a whole, taking every span in that batch (whole traces) with it.
- Tracing never breaks the app: any Langfuse error is logged and the operation continues untraced.
"""

import hashlib
import logging
import os
import re
import threading
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

WORKFLOW_NAMES = {
    "kg_extraction": "KG Extraction",
    "kg_build": "KG Build",
    "add_data": "Add Data",
    "chat": "Chat",
    "rag_chat": "RAG Chat",
    "rag_ingest": "Embedding",
    "embedding": "Embedding",
}


@dataclass(frozen=True)
class LLMContext:
    user_id: str | None = None
    session_id: str | None = None
    kb_name: str | None = None


_context: ContextVar[LLMContext | None] = ContextVar("llm_context", default=None)
_workflow: ContextVar[str | None] = ContextVar("llm_workflow", default=None)  # name of the open root, if any

_clients: dict[tuple, Any] = {}
_clients_lock = threading.Lock()


def current_llm_context() -> LLMContext:
    return _context.get() or LLMContext()


@contextmanager
def llm_context(user_id: str | None, session_id: str | None = None, kb_name: str | None = None):
    token = _context.set(LLMContext(user_id, session_id, kb_name))
    try:
        yield
    finally:
        _context.reset(token)


# ------------------------------------------------------------------ client
def _settings():
    from app.config import get_settings

    return get_settings()


def _trim(value, limit: int):
    """Shorten long strings anywhere inside the payload (Langfuse `mask` hook)."""
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]}... [{len(value) - limit:,} more characters]"
    if isinstance(value, dict):
        return {k: _trim(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_trim(v, limit) for v in value]
    return value


def _new_client(s):
    from langfuse import Langfuse
    from langfuse._client.attributes import LangfuseOtelSpanAttributes
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    # Langfuse 3.3.x ignores flush_at / flush_interval given in code unless the env variables exist
    # (operator precedence in its span processor), so provide them both ways.
    os.environ.setdefault("LANGFUSE_FLUSH_AT", str(s.langfuse_flush_at))
    os.environ.setdefault("LANGFUSE_FLUSH_INTERVAL", str(s.langfuse_flush_interval))
    resource = {LangfuseOtelSpanAttributes.ENVIRONMENT: s.langfuse_environment} if s.langfuse_environment else {}
    limit = s.langfuse_max_field_chars
    return Langfuse(
        public_key=s.langfuse_public_key,
        secret_key=s.langfuse_secret_key,
        host=s.langfuse_base_url.rstrip("/"),
        timeout=s.langfuse_timeout,
        flush_at=s.langfuse_flush_at,
        flush_interval=s.langfuse_flush_interval,
        environment=s.langfuse_environment or None,
        mask=lambda *, data, **_: _trim(data, limit),
        tracer_provider=TracerProvider(resource=Resource.create(resource)),  # private: see module docstring
    )


def _client():
    s = _settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    key = (s.langfuse_public_key, s.langfuse_secret_key, s.langfuse_base_url)
    with _clients_lock:
        if key not in _clients:
            try:
                _clients[key] = _new_client(s)
                log.info("Langfuse tracing on (%s)", s.langfuse_base_url)
            except Exception:  # noqa: BLE001 - tracing must never break the app
                log.exception("Langfuse could not be initialised; continuing without tracing")
                _clients[key] = None
        return _clients[key]


def flush_langfuse() -> None:
    """Send whatever is still queued (app / MCP server shutdown)."""
    with _clients_lock:
        clients = [c for c in _clients.values() if c is not None]
    for c in clients:
        try:
            c.flush()
        except Exception:  # noqa: BLE001
            log.warning("Langfuse flush failed", exc_info=True)


def _session_label(session_id: str | None) -> str | None:
    """Web sessions are identified by the hash of the cookie (the sessions table key); Langfuse gets a
    derived label, never that key."""
    if session_id and re.fullmatch(r"[0-9a-f]{64}", session_id):
        return "web-" + hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return session_id


def _content(value):
    return value if _settings().langfuse_capture_content else "[not captured: LANGFUSE_CAPTURE_CONTENT=false]"


# ------------------------------------------------------------------ observations
@contextmanager
def _observe(name: str, as_type: str, input_data=None, metadata: dict | None = None, **kwargs):
    """One Langfuse observation. The first one in a context is the root and carries the trace identity."""
    client = _client()
    if client is None:
        yield None
        return
    root = _workflow.get() is None
    try:
        cm = client.start_as_current_observation(
            name=name, as_type=as_type, input=input_data, metadata=metadata, **kwargs
        )
        obs = cm.__enter__()
    except Exception:  # noqa: BLE001
        log.warning("Langfuse: could not start %s", name, exc_info=True)
        yield None
        return
    token = _workflow.set(name) if root else None
    try:
        if root:
            ctx = current_llm_context()
            trace = {"name": name, "tags": ["graphbase", name]}
            if ctx.user_id:
                trace["user_id"] = ctx.user_id
            if ctx.session_id:
                trace["session_id"] = _session_label(ctx.session_id)
            if ctx.kb_name or metadata:
                trace["metadata"] = {**(metadata or {}), **({"knowledge_base": ctx.kb_name} if ctx.kb_name else {})}
            try:
                obs.update_trace(**trace)
            except Exception:  # noqa: BLE001
                log.warning("Langfuse: could not set trace attributes", exc_info=True)
        yield obs
    except BaseException as exc:
        with suppress(Exception):
            obs.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}"[:1000])
        _exit(cm, exc)
        raise
    else:
        _exit(cm, None)
    finally:
        if token is not None:
            _workflow.reset(token)


def _exit(cm, exc) -> None:
    try:
        cm.__exit__(type(exc) if exc else None, exc, exc.__traceback__ if exc else None)
    except Exception:  # noqa: BLE001
        log.warning("Langfuse: could not end an observation", exc_info=True)


@contextmanager
def trace_context(
    user_id: str | None,
    session_id: str | None = None,
    operation: str | None = None,
    kb_name: str | None = None,
    **metadata,
):
    """Identify the user/session for everything inside, and open the operation's trace."""
    with llm_context(user_id, session_id, kb_name):
        if not operation:
            yield
            return
        name = WORKFLOW_NAMES.get(operation, operation)
        details = {k: v for k, v in metadata.items() if v is not None}
        with observe_workflow(name, {"operation": operation, "knowledge_base": kb_name, **details}, details or None):
            yield


@contextmanager
def observe_workflow(name: str, input_data=None, metadata: dict | None = None):
    """The operation's trace. Inside another operation (a job calling a pipeline) it is just a step."""
    active = _workflow.get()
    if active == name:
        yield None
        return
    with _observe(name, "chain", input_data, metadata=metadata) as obs:
        yield obs


@contextmanager
def observe_span(name: str, input_data=None):
    with _observe(name, "span", input_data) as obs:
        yield obs


@contextmanager
def observe_llm(run_name: str, input_data, model: str | None = None, as_type: str = "generation"):
    """One model call (generation) or embedding request."""
    s = _settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        yield None
        return
    ctx = current_llm_context()
    model = model or (s.ollama_chat_model if s.llm_provider == "ollama" else s.azure_openai_chat_deployment)
    with _observe(
        run_name,
        as_type,
        _content(input_data),
        metadata={"knowledge_base": ctx.kb_name} if ctx.kb_name else None,
        model=model,
    ) as obs:
        yield obs


def record_output(observation, output) -> None:
    if observation is not None:
        try:
            observation.update(output=_content(output))
        except Exception:  # noqa: BLE001
            log.warning("Langfuse: could not record output", exc_info=True)


def update_llm_usage(generation, response) -> None:
    """Copy provider token usage into the Langfuse generation when the provider reports it."""
    if not generation:
        return
    usage = getattr(response, "usage_metadata", None) or {}
    response_metadata = getattr(response, "response_metadata", None) or {}
    usage = usage or response_metadata.get("token_usage") or response_metadata.get("usage") or {}
    details = {
        key: value
        for key, value in {
            "input": usage.get("input_tokens", usage.get("prompt_tokens")),
            "output": usage.get("output_tokens", usage.get("completion_tokens")),
            "total": usage.get("total_tokens"),
        }.items()
        if isinstance(value, int)
    }
    if details:
        try:
            generation.update(usage_details=details)
        except Exception:  # noqa: BLE001
            log.warning("Langfuse: could not record usage", exc_info=True)
