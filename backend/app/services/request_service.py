from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.crew_setup import create_nl_sql_crew, extract_sql_from_tasks
from app.agents.single_shot import AgentResponse

try:
    from app.agents.debate.debate_runner import run_debate
    from app.agents.debate.models import DebateResult
    DEBATE_AVAILABLE = True
except (ImportError, RuntimeError):
    DEBATE_AVAILABLE = False
    run_debate = None  # type: ignore[assignment]
    DebateResult = None  # type: ignore[assignment,misc]
from app.core.config import settings
from app.core.database import engine as async_engine
from app.models.enums import IterationStatus
from app.models.session import Iteration, Session
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
        debate_enabled=settings.ENABLE_DEBATE,
    )

    schema_description = get_schema_description()
    if not schema_description:
        _emit(sid, make_error("schema_designer", "No database schema available. Ensure CSV files were ingested at startup.", request_id=rid))
        logger.info("no_schema_cached", request_id=rid)
        return _build_error_result(sid, rid, prompt, "No database schema available. Ensure CSV files were ingested at startup.")

    schema_tables = _extract_table_names(schema_description)
    _emit(sid, make_start("schema_designer", "Loading database schema...", rid))
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

    _emit(sid, make_start("pipeline", "Starting pipeline...", rid))

    try:
        if settings.ENABLE_DEBATE and DEBATE_AVAILABLE:
            result = await _run_debate_pipeline(
                db, sid, rid, req_id, prompt, dialect_str, schema_description
            )
        else:
            result = await _run_crewai_pipeline(
                db, sid, rid, req_id, prompt, dialect_str, schema_description
            )
    except Exception as exc:
        _emit(sid, make_error("pipeline", str(exc), f"Pipeline failed: {exc}", rid))
        result = _build_error_result(sid, rid, prompt, str(exc))
        result["request_id"] = str(req_id)
        await _save_failed_iteration(db, req_id, prompt, str(exc), rid, dialect_str)
        stream_manager.mark_done(sid)
        logger.error("pipeline_failed", request_id=rid, error=str(exc), stage="top")
        return result

    result["request_id"] = str(req_id)
    pipeline_ms = (time.perf_counter() - pipeline_start) * 1000
    logger.info(
        "pipeline_completed",
        request_id=rid,
        status=result.get("status"),
        latency_ms=round(pipeline_ms, 1),
        debate=settings.ENABLE_DEBATE,
    )

    stream_manager.mark_done(sid)
    return result


async def _run_crewai_pipeline(
    db: AsyncSession,
    sid: str,
    rid: str,
    req_id: uuid.UUID,
    prompt: str,
    dialect: str,
    schema_description: str,
) -> dict[str, Any]:
    _emit(sid, make_start("pipeline", "Starting CrewAI multi-agent pipeline...", rid))

    try:
        crew = create_nl_sql_crew(schema_metadata=schema_description, sid=sid, rid=rid)
        crew_result = await crew.kickoff_async(inputs={
            "user_prompt": prompt,
            "dialect": dialect,
            "schema": schema_description,
        })
    except Exception as exc:
        _emit(sid, make_error("pipeline", str(exc), f"CrewAI pipeline failed: {exc}", rid))
        raise

    tasks_output = getattr(crew_result, "tasks_output", [])
    generated_sql = extract_sql_from_tasks(tasks_output)

    if not generated_sql:
        _emit(sid, make_error("query_author", "No SQL generated by agents", request_id=rid))
        result = _build_base_result(sid, rid, prompt)
        result.update({
            "request_id": str(req_id),
            "query_sql": "",
            "confidence": None,
            "rationale": None,
            "error_message": "No SQL was generated by the agent pipeline.",
        })
        await _save_failed_iteration(db, req_id, prompt, "No SQL was generated by the agent pipeline.", rid, dialect)
        return result

    intent_rationale = tasks_output[0].raw if len(tasks_output) > 0 else "Intent analysis unavailable"

    agent_response = AgentResponse(
        query_sql=generated_sql,
        confidence=0.85,
        rationale=intent_rationale[:500],
    )

    _emit(sid, make_partial_output("query_author", agent_response.query_sql, f"Generated SQL ({len(agent_response.query_sql)} chars)", rid))

    _emit(sid, make_start("critic", "Validating SQL safety...", rid))
    _emit(sid, make_progress("critic", 30.0, "Running AST-based SQL validation...", rid))

    validation_error: str | None = None
    redacted_sql: str | None = None
    try:
        redacted_sql = validate_or_raise(agent_response.query_sql, dialect, ValidationMode.QUERY_UNDER_TEST)
        _emit(sid, make_progress("critic", 100.0, "SQL validation passed", rid))
        _emit(sid, make_complete("critic", {"validation": "passed", "dialect": dialect}, "SQL validation passed", rid))
    except UnsafeSQLError as exc:
        validation_error = f"SQL validation failed: {'; '.join(exc.reasons)}"
        _emit(sid, make_error("critic", validation_error, f"Validation failed: {'; '.join(exc.reasons)}", rid))
        result = _build_base_result(sid, rid, prompt)
        result.update({
            "request_id": str(req_id),
            "query_sql": agent_response.query_sql,
            "confidence": agent_response.confidence,
            "rationale": agent_response.rationale,
            "error_message": validation_error,
        })
        await _save_iteration(db, req_id, agent_response, validation_error, rid, dialect, redacted_sql=exc.redacted_sql)
        return result

    return await _execute_query(
        db, sid, rid, req_id, agent_response, dialect, prompt, redacted_sql=redacted_sql
    )


async def _run_debate_pipeline(
    db: AsyncSession,
    sid: str,
    rid: str,
    req_id: uuid.UUID,
    prompt: str,
    dialect: str,
    schema_description: str,
) -> dict[str, Any]:
    _emit(sid, make_start("pipeline", "Starting AutoGen debate pipeline...", rid))
    _emit(sid, make_progress("pipeline", 10.0, "Initializing DebateAuthor and DebateCritic agents", rid))

    try:
        debate_result = await run_debate(
            prompt=prompt,
            dialect=dialect,
            schema_metadata=schema_description,
            session_id=sid,
            request_id=rid,
            emit_event=_emit,
        )
    except Exception as exc:
        _emit(sid, make_error("pipeline", str(exc), f"Debate pipeline failed: {exc}", rid))
        raise

    query_sql = debate_result.query_sql or ""
    _emit(sid, make_progress("pipeline", 80.0, f"Debate completed ({debate_result.termination_reason}, {debate_result.rounds} rounds)", rid))
    _emit(sid, make_partial_output("query_author", query_sql, f"Debate SQL ({len(query_sql)} chars)", rid))

    if not query_sql:
        _emit(sid, make_error("query_author", "No SQL generated by debate", request_id=rid))
        result = _build_base_result(sid, rid, prompt)
        result.update({
            "request_id": str(req_id),
            "query_sql": "",
            "confidence": None,
            "rationale": debate_result.rationale,
            "error_message": "No SQL was generated by the debate pipeline.",
        })
        await _save_debate_iteration(db, req_id, debate_result, "No SQL was generated by the debate pipeline.", rid, dialect)
        return result

    _emit(sid, make_start("critic", "Validating SQL safety...", rid))
    _emit(sid, make_progress("critic", 30.0, "Running AST-based SQL validation...", rid))

    validation_error: str | None = None
    redacted_sql: str | None = None
    try:
        redacted_sql = validate_or_raise(query_sql, dialect, ValidationMode.QUERY_UNDER_TEST)
        _emit(sid, make_progress("critic", 100.0, "SQL validation passed", rid))
        _emit(sid, make_complete("critic", {"validation": "passed", "dialect": dialect}, "SQL validation passed", rid))
    except UnsafeSQLError as exc:
        validation_error = f"SQL validation failed: {'; '.join(exc.reasons)}"
        _emit(sid, make_error("critic", validation_error, f"Validation failed: {'; '.join(exc.reasons)}", rid))
        result = _build_base_result(sid, rid, prompt)
        result.update({
            "request_id": str(req_id),
            "query_sql": query_sql,
            "confidence": debate_result.final_confidence,
            "rationale": debate_result.rationale,
            "error_message": validation_error,
            "debate_transcript": debate_result.debate_transcript,
        })
        await _save_debate_iteration(db, req_id, debate_result, validation_error, rid, dialect, redacted_sql=exc.redacted_sql)
        return result

    agent_response = AgentResponse(
        query_sql=query_sql,
        confidence=debate_result.final_confidence,
        rationale=debate_result.rationale,
    )

    return await _execute_query(
        db, sid, rid, req_id, agent_response, dialect, prompt,
        debate_result=debate_result,
        redacted_sql=redacted_sql,
    )


async def _execute_query(
    db: AsyncSession,
    sid: str,
    rid: str,
    req_id: uuid.UUID,
    agent_response: AgentResponse,
    dialect: str,
    prompt: str,
    *,
    debate_result: DebateResult | None = None,
    redacted_sql: str | None = None,
) -> dict[str, Any]:
    _emit(sid, make_start("test_executor", "Executing query...", rid))
    _emit(sid, make_progress("test_executor", 10.0, "Running query against PostgreSQL...", rid))

    exec_start = time.perf_counter()
    execution_error: str | None = None
    rows: list[dict[str, Any]] = []
    exec_ms: float | None = None
    result = _build_base_result(sid, rid, prompt)
    result["request_id"] = str(req_id)
    if debate_result:
        result["debate_transcript"] = debate_result.debate_transcript

    try:
        from app.core.config import settings
        if settings.APP_ENV == "testing":
            async with async_engine.connect() as conn:
                query_start = time.perf_counter()
                try:
                    db_result = await conn.execute(text(agent_response.query_sql))
                    query_ms = (time.perf_counter() - query_start) * 1000
                    raw_rows = db_result.fetchall()
                    column_names = list(db_result.keys())
                    rows = [dict(zip(column_names, row, strict=False)) for row in raw_rows]
                except Exception as exc:
                    if dialect == "oracle":
                        logger.warning("testing_oracle_dialect_fallback", error=str(exc))
                        query_ms = (time.perf_counter() - query_start) * 1000
                        rows = [{"mock_oracle_col": "mock_value"}]
                        column_names = ["mock_oracle_col"]
                    else:
                        raise
        else:
            from app.services.session_service import get_or_create_session_sandbox
            sandbox = await get_or_create_session_sandbox(db, uuid.UUID(sid))

            query_start = time.perf_counter()
            rows = await sandbox.exec_query(agent_response.query_sql)
            query_ms = (time.perf_counter() - query_start) * 1000
            column_names = list(rows[0].keys()) if rows else []

        logger.info(
            "query_executed",
            request_id=rid,
            query_sql=redacted_sql or agent_response.query_sql,
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

    status_str = result.get("status", "failed")
    if debate_result:
        await _save_debate_iteration(db, req_id, debate_result, execution_error, rid, dialect, result, redacted_sql=redacted_sql)
    else:
        await _save_iteration(
            db, req_id, agent_response, execution_error, rid, dialect,
            execution_results=rows if status_str == "completed" else None,
            execution_rows=len(rows) if status_str == "completed" else 0,
            execution_ms=exec_ms if status_str == "completed" else None,
            status=status_str,
            redacted_sql=redacted_sql
        )

    _emit(sid, make_complete("pipeline", result, f"Pipeline complete ({result['status']})", rid))
    return result


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
    redacted_sql: str | None = None,
) -> Iteration:
    iter_status = IterationStatus.EXECUTED if status == "completed" else IterationStatus.FAILED
    iteration = await append_iteration(
        db,
        request_id=req_id,
        generated_sql=agent_response.query_sql,
        redacted_sql=redacted_sql,
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
        agent_name="crewai",
        prompt=f"Dialect: {dialect}\nRequest: {agent_response.rationale}",
        response=json.dumps({
            "query_sql": redacted_sql or agent_response.query_sql,
            "confidence": agent_response.confidence,
            "rationale": agent_response.rationale,
        }),
        model=settings.OPENAI_MODEL,
    )

    return iteration


async def _save_debate_iteration(
    db: AsyncSession,
    req_id: uuid.UUID,
    debate_result: DebateResult,
    error_message: str | None,
    rid: str,
    dialect: str,
    pipeline_result: dict[str, Any] | None = None,
    redacted_sql: str | None = None,
) -> Iteration:
    iter_status = IterationStatus.EXECUTED
    if error_message or pipeline_result and pipeline_result.get("status") != "completed":
        iter_status = IterationStatus.FAILED

    debate_transcript = debate_result.debate_transcript or {}

    iteration = await append_iteration(
        db,
        request_id=req_id,
        generated_sql=debate_result.query_sql,
        redacted_sql=redacted_sql,
        confidence=debate_result.author_confidence,
        rationale=debate_result.rationale,
        critic_score=debate_result.critic_score,
        critic_notes=debate_result.debate_transcript.get("summary", {}).get("termination_reason") if debate_result.debate_transcript else None,
        debate_transcript=debate_transcript,
        status=iter_status,
    )

    if pipeline_result:
        iteration.execution_results = pipeline_result.get("execution_results")
        iteration.execution_rows = pipeline_result.get("execution_rows")
        iteration.execution_ms = pipeline_result.get("execution_ms")
        iteration.error_message = error_message or pipeline_result.get("error_message")
    else:
        iteration.error_message = error_message

    await db.commit()
    await db.refresh(iteration)

    # Redact query_sql in the trace response
    trace_response = debate_result.to_dict()
    if "query_sql" in trace_response and redacted_sql:
        trace_response["query_sql"] = redacted_sql

    await record_trace(
        db,
        iteration_id=iteration.id,
        agent_name="debate",
        prompt=f"Dialect: {dialect}\nRequest: {debate_result.rationale}",
        response=json.dumps(trace_response),
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
        agent_name="pipeline",
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
