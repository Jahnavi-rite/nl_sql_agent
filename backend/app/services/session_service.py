"""Session service — CRUD, history, and Redis state management."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.redis import (
    append_session_context,
    clear_sandbox_handle,
    clear_session_context,
    get_sandbox_handle,
    get_session_context,
    set_sandbox_handle,
)
from app.models.enums import (
    Dialect,
    FeedbackAction,
    IterationStatus,
    SessionStatus,
)
from app.models.session import (
    AgentTrace,
    Feedback,
    Iteration,
    Request,
    Session,
)

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Eager-load options (avoid N+1 queries)
# ---------------------------------------------------------------------------
_SESSION_FULL = selectinload(Session.requests).selectinload(
    Request.iterations
).selectinload(Iteration.feedbacks)

_REQUEST_ITERATIONS = selectinload(Request.iterations).selectinload(
    Iteration.feedbacks
)


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

async def create_session(
    db: AsyncSession,
    *,
    user_id: str,
    dialect: Dialect = Dialect.POSTGRESQL,
    metadata: dict[str, Any] | None = None,
) -> Session:
    """Create and persist a new session."""
    session = Session(
        user_id=user_id,
        dialect=dialect,
        status=SessionStatus.ACTIVE,
        metadata_json=metadata,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    logger.info("session_created", session_id=str(session.id), user_id=user_id)
    return session


async def get_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    eager: bool = False,
) -> Session | None:
    """Retrieve a session by ID, optionally with full history loaded."""
    stmt = select(Session).where(Session.id == session_id)
    if eager:
        stmt = stmt.options(_SESSION_FULL)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_session_history(
    db: AsyncSession,
    session_id: uuid.UUID,
) -> Session | None:
    """Load a session with all requests, iterations, and feedbacks eagerly."""
    return await get_session(db, session_id, eager=True)


async def close_session(
    db: AsyncSession,
    session_id: uuid.UUID,
) -> Session | None:
    """Mark a session as closed and clear its Redis state."""
    session = await get_session(db, session_id)
    if session is None:
        return None
    session.status = SessionStatus.CLOSED
    session.closed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    await clear_session_context(str(session_id))
    await clear_sandbox_handle(str(session_id))
    logger.info("session_closed", session_id=str(session_id))
    return session


# ---------------------------------------------------------------------------
# Request CRUD
# ---------------------------------------------------------------------------

async def create_request(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    question: str,
    context: dict[str, Any] | None = None,
) -> Request:
    """Create a new request (NL question) within a session."""
    request = Request(
        session_id=session_id,
        question=question,
        context_json=context,
    )
    db.add(request)
    await db.commit()
    await db.refresh(request)
    logger.info("request_created", request_id=str(request.id), session_id=str(session_id))
    return request


async def get_request(
    db: AsyncSession,
    request_id: uuid.UUID,
    *,
    eager: bool = False,
) -> Request | None:
    """Retrieve a request by ID."""
    stmt = select(Request).where(Request.id == request_id)
    if eager:
        stmt = stmt.options(_REQUEST_ITERATIONS)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Iteration CRUD
# ---------------------------------------------------------------------------

async def append_iteration(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    generated_sql: str,
    redacted_sql: str | None = None,
    confidence: float | None = None,
    rationale: str | None = None,
    critic_score: float | None = None,
    critic_notes: str | None = None,
    status: IterationStatus = IterationStatus.PENDING,
) -> Iteration:
    """Append a new iteration (SQL generation attempt) to a request.

    Automatically determines the next attempt_number.
    """
    # Determine next attempt number
    stmt = (
        select(Iteration.attempt_number)
        .where(Iteration.request_id == request_id)
        .order_by(Iteration.attempt_number.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    last_attempt = result.scalar_one_or_none()
    next_attempt = (last_attempt or 0) + 1

    iteration = Iteration(
        request_id=request_id,
        attempt_number=next_attempt,
        status=status,
        generated_sql=generated_sql,
        redacted_sql=redacted_sql,
        confidence=confidence,
        rationale=rationale,
        critic_score=critic_score,
        critic_notes=critic_notes,
    )
    db.add(iteration)
    await db.commit()
    await db.refresh(iteration)

    # Update rolling Redis context
    req = await get_request(db, request_id)
    if req is not None:
        await append_session_context(
            str(req.session_id),
            {
                "iteration_id": str(iteration.id),
                "attempt": next_attempt,
                "sql": generated_sql,
                "confidence": confidence,
            },
        )

    logger.info(
        "iteration_appended",
        iteration_id=str(iteration.id),
        request_id=str(request_id),
        attempt=next_attempt,
    )
    return iteration


async def update_iteration_result(
    db: AsyncSession,
    iteration_id: uuid.UUID,
    *,
    status: IterationStatus | None = None,
    validation_passed: bool | None = None,
    validation_reasons: list[str] | None = None,
    explain_plan: dict[str, Any] | None = None,
    execution_rows: int | None = None,
    execution_ms: float | None = None,
    error_message: str | None = None,
) -> Iteration | None:
    """Update an iteration with validation/execution results."""
    stmt = select(Iteration).where(Iteration.id == iteration_id)
    result = await db.execute(stmt)
    iteration = result.scalar_one_or_none()
    if iteration is None:
        return None

    if status is not None:
        iteration.status = status
    if validation_passed is not None:
        iteration.validation_passed = validation_passed
    if validation_reasons is not None:
        iteration.validation_reasons = validation_reasons
    if explain_plan is not None:
        iteration.explain_plan = explain_plan
    if execution_rows is not None:
        iteration.execution_rows = execution_rows
    if execution_ms is not None:
        iteration.execution_ms = execution_ms
    if error_message is not None:
        iteration.error_message = error_message

    await db.commit()
    await db.refresh(iteration)
    return iteration


# ---------------------------------------------------------------------------
# Feedback CRUD
# ---------------------------------------------------------------------------

async def record_feedback(
    db: AsyncSession,
    *,
    iteration_id: uuid.UUID,
    action: FeedbackAction,
    edited_sql: str | None = None,
    comment: str | None = None,
) -> Feedback:
    """Record user feedback on an iteration."""
    feedback = Feedback(
        iteration_id=iteration_id,
        action=action,
        edited_sql=edited_sql,
        comment=comment,
    )
    db.add(feedback)
    await db.commit()
    await db.refresh(feedback)
    logger.info(
        "feedback_recorded",
        feedback_id=str(feedback.id),
        iteration_id=str(iteration_id),
        action=action.value,
    )
    return feedback


# ---------------------------------------------------------------------------
# Agent Trace CRUD
# ---------------------------------------------------------------------------

async def record_trace(
    db: AsyncSession,
    *,
    iteration_id: uuid.UUID,
    agent_name: str,
    prompt: str,
    response: str,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentTrace:
    """Record an LLM agent call for auditing."""
    trace = AgentTrace(
        iteration_id=iteration_id,
        agent_name=agent_name,
        prompt=prompt,
        response=response,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        metadata_json=metadata,
    )
    db.add(trace)
    await db.commit()
    await db.refresh(trace)
    return trace


# ---------------------------------------------------------------------------
# Redis state helpers
# ---------------------------------------------------------------------------

async def get_context(session_id: str) -> list[dict[str, Any]]:
    """Get rolling iteration context from Redis."""
    return await get_session_context(session_id)


async def set_sandbox(
    session_id: str,
    handle: dict[str, Any],
    ttl: int = 3600,
) -> None:
    """Store active sandbox handle in Redis."""
    await set_sandbox_handle(session_id, handle, ttl=ttl)


async def get_sandbox(session_id: str) -> dict[str, Any] | None:
    """Get active sandbox handle from Redis."""
    return await get_sandbox_handle(session_id)


async def clear_state(session_id: str) -> None:
    """Clear all Redis state for a session."""
    await clear_session_context(session_id)
    await clear_sandbox_handle(session_id)
