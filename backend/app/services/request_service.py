# ruff: noqa: SIM105
from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.single_shot import AgentError, AgentResponse
from app.agents.single_shot import generate as llm_generate
from app.core.config import settings
from app.core.database import engine as async_engine
from app.models.enums import IterationStatus
from app.models.session import Iteration, Session
from app.services.session_service import append_iteration, create_request, record_trace
from app.services.startup_ingestion import get_schema_description
from app.validators.sql_guard import UnsafeSQLError, ValidationMode, validate_or_raise

logger = structlog.get_logger()


class PipelineError(Exception):
    def __init__(self, message: str, stage: str = "") -> None:
        self.stage = stage
        super().__init__(message)


async def execute_nl_pipeline(
    db: AsyncSession,
    session: Session,
    prompt: str,
) -> dict[str, Any]:
    rid = str(uuid.uuid4())
    dialect_raw = (
        session.dialect.value if hasattr(session.dialect, "value") else str(session.dialect)
    )
    dialect_str = "postgres" if dialect_raw.startswith("postgres") else "oracle"
    pipeline_start = time.perf_counter()

    logger.info(
        "pipeline_started",
        request_id=rid,
        dialect=dialect_str,
        prompt_preview=prompt[:100],
    )

    # 1. Persist the request
    req = await create_request(
        db,
        session_id=session.id,
        question=prompt,
        context={"dialect": dialect_str, "request_id": rid},
    )
    req_id = req.id

    result: dict[str, Any] = {
        "request_id": req_id,
        "session_id": session.id,
        "question": prompt,
        "query_sql": "",
        "confidence": None,
        "rationale": None,
        "execution_results": [],
        "execution_rows": 0,
        "execution_ms": None,
        "status": "failed",
        "error_message": None,
        "created_at": req.created_at,
    }

    # 2. Retrieve cached schema
    schema_description = get_schema_description()
    if not schema_description:
        logger.info("no_schema_cached", request_id=rid)
        result["error_message"] = "No database schema available. Ensure CSV files were ingested at startup."
        return result

    logger.info("schema_loaded", request_id=rid, description_length=len(schema_description))

    # 3. LLM call with cached schema metadata
    try:
        agent_response = await _call_agent(prompt, dialect_str, schema_description, rid)
    except AgentError as exc:
        result["error_message"] = str(exc)
        await _save_failed_iteration(db, req_id, prompt, str(exc), rid, dialect_str)
        logger.error("llm_failed", request_id=rid, error=str(exc))
        return result

    result.update({
        "query_sql": agent_response.query_sql,
        "confidence": agent_response.confidence,
        "rationale": agent_response.rationale,
    })

    # 4. Validate SQL safety (strict: only single SELECT allowed)
    try:
        validate_or_raise(agent_response.query_sql, dialect_str, ValidationMode.QUERY_UNDER_TEST)
    except UnsafeSQLError as exc:
        error_msg = f"SQL validation failed: {'; '.join(exc.reasons)}"
        result["error_message"] = error_msg
        await _save_iteration(db, req_id, agent_response, error_msg, rid, dialect_str)
        logger.error("validation_failed", request_id=rid, reasons=exc.reasons)
        return result

    # 5. Execute query against main PostgreSQL database
    exec_start = time.perf_counter()
    execution_error: str | None = None
    try:
        async with async_engine.connect() as conn:
            query_start = time.perf_counter()
            db_result = await conn.execute(text(agent_response.query_sql))
            query_ms = (time.perf_counter() - query_start) * 1000

            # Fetch all results
            raw_rows = db_result.fetchall()
            column_names = list(db_result.keys())
            rows = [dict(zip(column_names, row, strict=False)) for row in raw_rows]

            logger.info(
                "query_executed",
                request_id=rid,
                rows=len(rows),
                latency_ms=round(query_ms, 1),
            )

            result["execution_results"] = rows
            result["execution_rows"] = len(rows)
            result["execution_ms"] = (time.perf_counter() - exec_start) * 1000
            result["status"] = "completed"
    except Exception as exc:
        execution_error = f"Query execution failed: {exc}"
        result["error_message"] = execution_error
        logger.error("execution_failed", request_id=rid, error=str(exc))

    # 6. Persist iteration
    await _save_iteration(
        db,
        req_id,
        agent_response,
        execution_error,
        rid,
        dialect_str,
        result["execution_results"],
        result["execution_rows"],
        result["execution_ms"],
        result["status"],
    )

    pipeline_ms = (time.perf_counter() - pipeline_start) * 1000
    logger.info(
        "pipeline_completed",
        request_id=rid,
        status=result["status"],
        latency_ms=round(pipeline_ms, 1),
    )

    return result


async def _call_agent(
    prompt: str, dialect: str, schema_metadata: str, rid: str
) -> AgentResponse:
    return await llm_generate(prompt, dialect, schema_metadata=schema_metadata, request_id=rid)


async def _save_iteration(
    db: AsyncSession,
    req_id: uuid.UUID,
    agent_response: AgentResponse,
    error_message: str | None,
    rid: str,
    dialect: str,
    execution_results: list[dict[str, Any]] | None = None,
    execution_rows: int = 0,
    execution_ms: float | None = None,
    status: str = "failed",
) -> Iteration:
    iter_status = IterationStatus.EXECUTED if status == "completed" else IterationStatus.FAILED
    iteration = await append_iteration(
        db,
        request_id=req_id,
        generated_sql=agent_response.query_sql,
        confidence=agent_response.confidence,
        rationale=agent_response.rationale,
        status=iter_status,
    )

    iteration.execution_results = execution_results
    iteration.execution_rows = execution_rows
    iteration.execution_ms = execution_ms
    iteration.error_message = error_message
    await db.commit()
    await db.refresh(iteration)

    await record_trace(
        db,
        iteration_id=iteration.id,
        agent_name="single_shot",
        prompt=f"Dialect: {dialect}\nRequest: {agent_response.rationale}",
        response=json.dumps({
            "query_sql": agent_response.query_sql,
            "confidence": agent_response.confidence,
            "rationale": agent_response.rationale,
        }),
        model=settings.OPENAI_MODEL,
    )

    return iteration


async def _save_failed_iteration(
    db: AsyncSession,
    req_id: uuid.UUID,
    prompt: str,
    error_message: str,
    rid: str,
    dialect: str,
) -> Iteration:
    iteration = await append_iteration(
        db,
        request_id=req_id,
        generated_sql="",
        status=IterationStatus.FAILED,
    )
    iteration.error_message = error_message
    await db.commit()
    await db.refresh(iteration)

    await record_trace(
        db,
        iteration_id=iteration.id,
        agent_name="single_shot",
        prompt=f"Dialect: {dialect}\nRequest: {prompt}",
        response=f"ERROR: {error_message}",
        model=settings.OPENAI_MODEL,
    )

    return iteration
