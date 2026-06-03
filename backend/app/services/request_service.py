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
from app.models.session import Iteration
from app.services.session_service import append_iteration, create_request, record_trace
from app.services.startup_ingestion import get_schema_description
from app.services.stream_events import (
    make_artifact,
    make_complete,
    make_error,
    make_partial_output,
    make_progress,
    make_start,
)
from app.services.stream_manager import stream_manager
from app.validators.sql_guard import UnsafeSQLError, ValidationMode, validate_or_raise

logger = structlog.get_logger()


class PipelineError(Exception):
    def __init__(self, message: str, stage: str = "") -> None:
        self.stage = stage
        super().__init__(message)


def _emit(sid: str, event: Any) -> None:
    stream_manager.publish_event(sid, event.to_dict())


async def execute_nl_pipeline(
    db: AsyncSession,
    session: Session,
    prompt: str,
) -> dict[str, Any]:
    rid = str(uuid.uuid4())
    sid = str(session.id)
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

    _emit(sid, make_start("intent_analyst", f"Analyzing request: {prompt[:80]}...", rid))

    _emit(sid, make_progress("intent_analyst", 50.0, "Parsing natural language intent...", request_id=rid))

    _emit(sid, make_complete("intent_analyst", {"prompt": prompt[:200], "dialect": dialect_str}, f"Intent recognized: {prompt[:60]}...", rid))

    _emit(sid, make_start("schema_designer", "Loading database schema...", rid))

    schema_description = get_schema_description()
    if not schema_description:
        _emit(sid, make_error("schema_designer", "No database schema available. Ensure CSV files were ingested at startup.", request_id=rid))
        logger.info("no_schema_cached", request_id=rid)
        return _build_error_result(sid, rid, prompt, "No database schema available. Ensure CSV files were ingested at startup.")

    schema_tables = _extract_table_names(schema_description)
    _emit(sid, make_progress("schema_designer", 100.0, f"Loaded schema with {len(schema_tables)} tables", rid))
    _emit(sid, make_artifact("schema_designer", {"tables": schema_tables, "description_length": len(schema_description)}, f"Schema loaded: {len(schema_tables)} tables available", rid))
    logger.info("schema_loaded", request_id=rid, description_length=len(schema_description))

    req = await create_request(
        db,
        session_id=session.id,
        question=prompt,
        context={"dialect": dialect_str, "request_id": rid},
    )
    req_id = req.id

    _emit(sid, make_start("query_author", "Generating SQL...", rid))
    _emit(sid, make_progress("query_author", 10.0, "Calling LLM to generate SQL...", rid))

    try:
        agent_response = await _call_agent(prompt, dialect_str, schema_description, rid)
        _emit(sid, make_progress("query_author", 90.0, "SQL generated, validating...", rid))
        _emit(sid, make_partial_output("query_author", agent_response.query_sql, f"Generated SQL ({len(agent_response.query_sql)} chars)", rid))
    except AgentError as exc:
        _emit(sid, make_error("query_author", str(exc), f"SQL generation failed: {exc}", rid))
        result = _build_error_result(sid, rid, prompt, str(exc))
        result["request_id"] = str(req_id)
        await _save_failed_iteration(db, req_id, prompt, str(exc), rid, dialect_str)
        logger.error("llm_failed", request_id=rid, error=str(exc))
        stream_manager.mark_done(sid)
        return result

    _emit(sid, make_complete("query_author", {"sql": agent_response.query_sql, "confidence": agent_response.confidence}, f"SQL generated (confidence: {agent_response.confidence:.0%})", rid))

    _emit(sid, make_start("critic", "Validating SQL safety...", rid))
    _emit(sid, make_progress("critic", 30.0, "Running AST-based SQL validation...", rid))

    try:
        validate_or_raise(agent_response.query_sql, dialect_str, ValidationMode.QUERY_UNDER_TEST)
        _emit(sid, make_progress("critic", 100.0, "SQL validation passed", rid))
        _emit(sid, make_complete("critic", {"validation": "passed", "dialect": dialect_str}, "SQL validation passed", rid))
    except UnsafeSQLError as exc:
        error_msg = f"SQL validation failed: {'; '.join(exc.reasons)}"
        _emit(sid, make_error("critic", error_msg, f"Validation failed: {'; '.join(exc.reasons)}", rid))
        result = _build_base_result(sid, rid, prompt)
        result.update({
            "request_id": str(req_id),
            "query_sql": agent_response.query_sql,
            "confidence": agent_response.confidence,
            "rationale": agent_response.rationale,
            "error_message": error_msg,
        })
        await _save_iteration(db, req_id, agent_response, error_msg, rid, dialect_str)
        logger.error("validation_failed", request_id=rid, reasons=exc.reasons)
        stream_manager.mark_done(sid)
        return result

    _emit(sid, make_start("test_executor", "Executing query...", rid))
    _emit(sid, make_progress("test_executor", 10.0, "Running query against PostgreSQL...", rid))

    exec_start = time.perf_counter()
    execution_error: str | None = None
    result = _build_base_result(sid, rid, prompt)
    result["request_id"] = str(req_id)

    try:
        async with async_engine.connect() as conn:
            query_start = time.perf_counter()
            db_result = await conn.execute(text(agent_response.query_sql))
            query_ms = (time.perf_counter() - query_start) * 1000

            raw_rows = db_result.fetchall()
            column_names = list(db_result.keys())
            rows = [dict(zip(column_names, row, strict=False)) for row in raw_rows]

            logger.info(
                "query_executed",
                request_id=rid,
                rows=len(rows),
                latency_ms=round(query_ms, 1),
            )

            exec_ms = (time.perf_counter() - exec_start) * 1000

            _emit(sid, make_progress("test_executor", 100.0, f"Query returned {len(rows)} rows in {query_ms:.0f}ms", rid))
            _emit(sid, make_artifact("test_executor", {"rows": len(rows), "columns": column_names, "execution_ms": round(exec_ms, 1), "sample": rows[:3]}, f"Query completed: {len(rows)} rows", rid))
            _emit(sid, make_complete("test_executor", {"rows": len(rows), "columns": column_names}, "Query execution complete", rid))

            result.update({
                "query_sql": agent_response.query_sql,
                "confidence": agent_response.confidence,
                "rationale": agent_response.rationale,
                "execution_results": rows,
                "execution_rows": len(rows),
                "execution_ms": exec_ms,
                "status": "completed",
            })
    except Exception as exc:
        execution_error = f"Query execution failed: {exc}"
        _emit(sid, make_error("test_executor", execution_error, f"Query execution failed: {exc}", rid))
        result.update({
            "query_sql": agent_response.query_sql,
            "confidence": agent_response.confidence,
            "rationale": agent_response.rationale,
            "error_message": execution_error,
        })
        logger.error("execution_failed", request_id=rid, error=str(exc))

    await _save_iteration(
        db, req_id, agent_response, execution_error, rid, dialect_str,
        result["execution_results"], result["execution_rows"],
        result["execution_ms"], result["status"],
    )

    pipeline_ms = (time.perf_counter() - pipeline_start) * 1000
    logger.info(
        "pipeline_completed",
        request_id=rid,
        status=result["status"],
        latency_ms=round(pipeline_ms, 1),
    )

    _emit(sid, make_complete("critic", result, f"Pipeline complete ({result['status']})", rid))
    stream_manager.mark_done(sid)

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


def _build_base_result(sid: str, rid: str, prompt: str) -> dict[str, Any]:
    return {
        "request_id": rid,
        "session_id": sid,
        "question": prompt,
        "query_sql": "",
        "confidence": None,
        "rationale": None,
        "execution_results": [],
        "execution_rows": 0,
        "execution_ms": None,
        "status": "failed",
        "error_message": None,
        "created_at": None,
    }


def _build_error_result(sid: str, rid: str, prompt: str, error_message: str) -> dict[str, Any]:
    result = _build_base_result(sid, rid, prompt)
    result["error_message"] = error_message
    return result


def _extract_table_names(schema_description: str) -> list[str]:
    tables = []
    for line in schema_description.splitlines():
        line = line.strip()
        if line and not line.startswith("-") and not line.startswith("Column"):
            parts = line.split("|")
            if len(parts) >= 2:
                name = parts[0].strip()
                if name and name != "Table":
                    tables.append(name)
    return tables
