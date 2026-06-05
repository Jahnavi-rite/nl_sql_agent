from __future__ import annotations

from typing import Any, cast

import structlog
from langfuse import Langfuse

from app.core.config import settings

logger = structlog.get_logger()

_langfuse: Langfuse | None = None
_initialized = False


def get_langfuse() -> Langfuse | None:
    global _initialized, _langfuse
    if not settings.LANGFUSE_ENABLED:
        return None
    if _initialized:
        return _langfuse
    _initialized = True

    if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
        logger.warning("langfuse_disabled_no_keys")
        return None

    try:
        _langfuse = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
            release=settings.APP_VERSION,
        )
        logger.info(
            "langfuse_initialized",
            host=settings.LANGFUSE_HOST,
        )
    except Exception as exc:
        logger.warning("langfuse_init_failed", error=str(exc))
        _langfuse = None

    return _langfuse


def trace_llm_call(
    *,
    trace_name: str = "llm_generate",
    request_id: str,
    model: str,
    dialect: str,
    prompt: str,
    system_prompt: str | None = None,
    response: str | None = None,
    query_sql: str | None = None,
    confidence: float | None = None,
    latency_ms: float | None = None,
    temperature: float | None = None,
    error: str | None = None,
    token_input: int | None = None,
    token_output: int | None = None,
    iteration_id: str | None = None,
    session_id: str | None = None,
    **tags: Any,
) -> str | None:
    lf = get_langfuse()
    if lf is None:
        return None

    try:
        trace = lf.trace(
            name=trace_name,
            session_id=session_id or "default",
            metadata={
                "request_id": request_id,
                "dialect": dialect,
                "model": model,
                "iteration_id": iteration_id,
                "temperature": temperature or settings.LLM_TEMPERATURE,
            },
            tags=[dialect, "nl-sql-agent"],
        )

        generation = trace.generation(
            name="llm_call",
            model=model,
            model_parameters={
                "temperature": temperature or settings.LLM_TEMPERATURE,
                "max_tokens": 4096,
            },
            input=[{"role": "system", "content": system_prompt or ""}, {"role": "user", "content": prompt}],
            output=response or error or "",
            usage={
                "input": token_input or len((system_prompt or "") + prompt),
                "output": token_output or len(response or error or ""),
                "unit": "CHARACTERS",
            },
            level="ERROR" if error else "DEFAULT",
            status_message=error or "ok",
            metadata={
                "dialect": dialect,
                "request_id": request_id,
                "query_sql": query_sql,
                "confidence": confidence,
                "latency_ms": latency_ms,
            },
        )

        generation.end()
        lf.flush()
        return cast(str, trace.id)

    except Exception as exc:
        logger.warning("langfuse_trace_failed", error=str(exc))
        return None


def trace_agent_phase(
    *,
    trace_id: str | None = None,
    agent: str,
    phase: str,
    request_id: str,
    session_id: str | None = None,
    duration_ms: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    lf = get_langfuse()
    if lf is None:
        return None

    try:
        trace = lf.trace(
            id=trace_id,
            name=f"agent_{agent}",
            session_id=session_id or "default",
            metadata={
                "request_id": request_id,
                "agent": agent,
                "phase": phase,
                **(metadata or {}),
            },
        )

        span = trace.span(
            name=f"{agent}_{phase}",
            input={"phase": phase, "agent": agent},
            metadata={
                "duration_ms": duration_ms,
                **(metadata or {}),
            },
        )

        span.end()
        lf.flush()
        return cast(str, trace.id)

    except Exception as exc:
        logger.warning("langfuse_span_failed", agent=agent, error=str(exc))
        return None
