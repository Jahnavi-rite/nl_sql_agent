"""Session service — CRUD, history, and Redis state management."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, cast

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
    RequestStatus,
    SessionStatus,
)
from app.models.session import (
    MAX_ITERATIONS,
    AgentTrace,
    Feedback,
    Iteration,
    Request,
    Session,
)
from app.sandbox.container import SandboxContainer
from app.sandbox.executor import DatabaseExecutor
from app.sandbox.manager import Sandbox

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

_REQUEST_WITH_FULL = (
    selectinload(Request.iterations)
    .selectinload(Iteration.feedbacks)
)
_REQUEST_TRACES = (
    selectinload(Request.iterations)
    .selectinload(Iteration.traces)
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
    session.closed_at = datetime.now(timezone.utc)  # noqa: UP017 — Python 3.10 compat
    await db.commit()
    await db.refresh(session)
    # Retrieve and destroy sandbox container if any exists
    handle = await get_sandbox(str(session_id))
    if handle:
        try:
            from app.sandbox.executor import create_executor

            dialect = handle.get("dialect", "postgres")
            container = SandboxContainer(dialect)
            container.container_id = cast(str, handle.get("container_id"))
            container.network_id = cast(str, handle.get("network_id"))
            container.volume_id = cast(str, handle.get("volume_id"))
            container.host = cast(str, handle.get("host", ""))
            container.port = cast(int, handle.get("port", 0))

            executor = create_executor(dialect)
            sandbox = Sandbox(dialect, container, executor)
            await sandbox.destroy()
        except Exception as exc:
            logger.warning("failed_to_destroy_sandbox_on_close", session_id=str(session_id), error=str(exc))

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


async def get_request_with_full(
    db: AsyncSession,
    request_id: uuid.UUID,
) -> Request | None:
    """Retrieve a request with all iterations, feedbacks, and traces."""
    stmt = select(Request).where(Request.id == request_id).options(_REQUEST_WITH_FULL, _REQUEST_TRACES)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_request_status(
    db: AsyncSession,
    request_id: uuid.UUID,
    status: RequestStatus,
    *,
    approved_iteration_id: uuid.UUID | None = None,
) -> Request | None:
    """Update the status of a request."""
    req = await get_request(db, request_id)
    if req is None:
        return None
    req.status = status
    if approved_iteration_id is not None:
        req.approved_iteration_id = approved_iteration_id
    await db.commit()
    logger.info("request_status_updated", request_id=str(request_id), status=str(status))
    return req


async def get_iteration(
    db: AsyncSession,
    iteration_id: uuid.UUID,
) -> Iteration | None:
    """Retrieve a single iteration by ID."""
    stmt = select(Iteration).where(Iteration.id == iteration_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Iteration CRUD
# ---------------------------------------------------------------------------

class IterationCapError(Exception):
    def __init__(self, request_id: uuid.UUID, max_iterations: int = MAX_ITERATIONS) -> None:
        self.request_id = request_id
        self.max_iterations = max_iterations
        super().__init__(f"Iteration cap of {max_iterations} reached for request {request_id}")


async def count_iterations(db: AsyncSession, request_id: uuid.UUID) -> int:
    stmt = (
        select(Iteration.attempt_number)
        .where(Iteration.request_id == request_id)
        .order_by(Iteration.attempt_number.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    last = result.scalar_one_or_none()
    return last or 0


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
    debate_transcript: dict[str, Any] | None = None,
    status: IterationStatus = IterationStatus.PENDING,
    supersede_previous: bool = False,
) -> Iteration:
    """Append a new iteration (SQL generation attempt) to a request.

    Automatically determines the next attempt_number.
    Raises IterationCapError if MAX_ITERATIONS has been reached.
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

    if next_attempt > MAX_ITERATIONS:
        raise IterationCapError(request_id)

    # Supersede previous iterations if requested (for regeneration)
    if supersede_previous and last_attempt is not None:
        prev_stmt = (
            select(Iteration)
            .where(Iteration.request_id == request_id)
            .where(Iteration.status != IterationStatus.SUPERSEDED)
        )
        prev_result = await db.execute(prev_stmt)
        for prev_it in prev_result.scalars().all():
            if prev_it.status != IterationStatus.APPROVED:
                prev_it.status = IterationStatus.SUPERSEDED
        await db.commit()

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
        debate_transcript_json=debate_transcript,
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


# ---------------------------------------------------------------------------
# Test Environment Sandbox Fallbacks
# ---------------------------------------------------------------------------

class DummyContainer(SandboxContainer):
    def __init__(self, dialect: str = "postgres") -> None:
        self.dialect = dialect
        self.container_id = None
        self.network_id = None
        self.volume_id = None
        self.host = "127.0.0.1"
        self.port = 0

    def is_running(self) -> bool:
        return True

    def stop(self) -> None:
        pass



class TestDatabaseExecutor(DatabaseExecutor):
    def __init__(self, bind_engine: Any) -> None:
        self._engine = bind_engine

    async def connect(self, host: str, port: int, **kwargs: Any) -> None:
        pass

    async def execute(self, sql: str, timeout: int = 30) -> list[dict[str, Any]]:
        from sqlalchemy import text
        async with self._engine.connect() as conn:
            res = await conn.execute(text(sql))
            if res.returns_rows:
                raw_rows = res.fetchall()
                column_names = list(res.keys())
                return [dict(zip(column_names, row, strict=False)) for row in raw_rows]
            return []

    async def execute_ddl(self, sql: str, timeout: int = 30) -> None:
        from sqlalchemy import text
        async with self._engine.begin() as conn:
            await conn.execute(text(sql))

    async def explain(self, sql: str, timeout: int = 30) -> list[dict[str, Any]]:
        from sqlalchemy import text
        async with self._engine.connect() as conn:
            res = await conn.execute(text(f"EXPLAIN QUERY PLAN {sql}"))
            raw_rows = res.fetchall()
            column_names = list(res.keys())
            return [dict(zip(column_names, row, strict=False)) for row in raw_rows]

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        pass


async def get_or_create_session_sandbox(
    db: AsyncSession,
    session_id: uuid.UUID,
) -> Sandbox:
    """Retrieve or create the Sandbox container for the session."""
    from app.core.config import settings

    session = await get_session(db, session_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found.")

    dialect_raw = session.dialect.value if hasattr(session.dialect, "value") else str(session.dialect)
    dialect_mapped = cast(Literal["postgres", "oracle"], "postgres" if dialect_raw == "postgresql" else dialect_raw)

    # Fallback for testing when Docker daemon is not running
    if settings.APP_ENV == "testing":
        docker_available = False
        try:
            import docker
            docker.from_env().ping()
            docker_available = True
        except Exception:
            pass

        if not docker_available:
            logger.info("using_test_sqlite_fallback_sandbox", session_id=str(session_id))
            container: SandboxContainer = DummyContainer()
            executor: DatabaseExecutor = TestDatabaseExecutor(db.bind)
            return Sandbox(dialect_mapped, container, executor)

    # 1. Check if a sandbox handle is stored in Redis
    handle = await get_sandbox(str(session_id))
    if handle:
        try:
            from app.sandbox.executor import create_executor

            container = SandboxContainer(dialect_mapped)
            container.container_id = cast(str, handle.get("container_id"))
            container.network_id = cast(str, handle.get("network_id"))
            container.volume_id = cast(str, handle.get("volume_id"))
            container.host = cast(str, handle.get("host", ""))
            container.port = cast(int, handle.get("port", 0))

            if container.is_running():
                executor = create_executor(dialect_mapped)
                await executor.connect(container.host, container.port)
                if await executor.health():
                    logger.info("reusing_session_sandbox", session_id=str(session_id), dialect=dialect_mapped)
                    return Sandbox(dialect_mapped, container, executor)
                else:
                    await executor.close()
        except Exception as exc:
            logger.warning("failed_to_reconstruct_sandbox", session_id=str(session_id), error=str(exc))

    # 2. Create a fresh sandbox if none was found or reconstructed
    from app.sandbox.manager import sandbox_manager
    logger.info("creating_fresh_session_sandbox", session_id=str(session_id), dialect=dialect_mapped)
    sandbox = await sandbox_manager.create(dialect_mapped)

    # 3. Save the handle back to Redis
    handle_data = {
        "dialect": dialect_mapped,
        "container_id": sandbox._container.container_id,
        "network_id": sandbox._container.network_id,
        "volume_id": sandbox._container.volume_id,
        "host": sandbox._container.host,
        "port": sandbox._container.port,
    }
    await set_sandbox(str(session_id), handle_data)

    return sandbox
