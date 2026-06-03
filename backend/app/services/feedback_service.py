"""Feedback service — approve/reject/edit iteration orchestration."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.single_shot import AgentError
from app.agents.single_shot import generate as llm_generate
from app.core.config import settings
from app.core.database import engine as async_engine
from app.models.enums import FeedbackAction, IterationStatus, RequestStatus
from app.models.session import Iteration, Request
from app.services.session_service import (
    append_iteration,
    count_iterations,
    get_iteration,
    get_request_with_full,
    record_trace,
    update_request_status,
)
from app.services.session_service import (
    record_feedback as persist_feedback,
)
from app.services.startup_ingestion import get_schema_description
from app.validators.sql_guard import UnsafeSQLError, ValidationMode, validate_or_raise

logger = structlog.get_logger()


class FeedbackError(Exception):
    pass


async def handle_feedback(
    db: AsyncSession,
    session_id: uuid.UUID,
    iteration_id: uuid.UUID,
    action: str,
    *,
    comment: str | None = None,
    edited_sql: str | None = None,
) -> dict[str, Any]:
    """Main orchestrator for user feedback on an iteration.

    Returns a result dict with status, iteration data, and regeneration info.
    """
    action_enum = FeedbackAction(action)
    iteration = await get_iteration(db, iteration_id)
    if iteration is None:
        raise FeedbackError(f"Iteration {iteration_id} not found")

    req = await get_request_with_full(db, iteration.request_id)
    if req is None:
        raise FeedbackError(f"Request {iteration.request_id} not found")
    if req.session_id != session_id:
        raise FeedbackError("Iteration does not belong to this session")

    dialect_str = "postgres"
    if req.context_json:
        dialect_str = req.context_json.get("dialect", "postgres")

    start_time = time.perf_counter()

    # Persist the feedback record
    fb = await persist_feedback(
        db,
        iteration_id=iteration_id,
        action=action_enum,
        edited_sql=edited_sql,
        comment=comment,
    )

    logger.info(
        "feedback_received",
        iteration_id=str(iteration_id),
        request_id=str(req.id),
        action=action,
        has_comment=bool(comment),
        has_edit=bool(edited_sql),
    )

    if action == "approve":
        return await _handle_approve(db, req, iteration, fb, dialect_str, start_time)
    elif action == "reject":
        return await _handle_reject(db, req, iteration, fb, comment, dialect_str, start_time)
    elif action == "edit":
        return await _handle_edit(db, req, iteration, fb, edited_sql, dialect_str, start_time)
    else:
        raise FeedbackError(f"Unknown action: {action}")


async def _handle_approve(
    db: AsyncSession,
    req: Request,
    iteration: Iteration,
    feedback: Any,
    dialect: str,
    start_time: float,
) -> dict[str, Any]:
    """Mark iteration and request as approved."""
    # Capture values before commit expires ORM attributes
    iter_id = str(iteration.id)
    attempt = iteration.attempt_number
    gen_sql = iteration.generated_sql
    exec_results = iteration.execution_results or []
    exec_rows = iteration.execution_rows or 0
    exec_ms = iteration.execution_ms

    iteration.status = IterationStatus.APPROVED
    await db.commit()
    await update_request_status(
        db, req.id, RequestStatus.APPROVED, approved_iteration_id=iteration.id
    )

    elapsed = (time.perf_counter() - start_time) * 1000
    logger.info(
        "iteration_approved",
        request_id=str(req.id),
        iteration_id=iter_id,
        attempt=attempt,
        latency_ms=round(elapsed, 1),
    )

    return {
        "action": "approve",
        "status": "approved",
        "request_status": "approved",
        "iteration_id": iter_id,
        "attempt_number": attempt,
        "query_sql": gen_sql,
        "execution_results": exec_results,
        "execution_rows": exec_rows,
        "execution_ms": exec_ms,
        "latency_ms": round(elapsed, 1),
        "needs_human_intervention": False,
    }


async def _handle_reject(
    db: AsyncSession,
    req: Request,
    iteration: Iteration,
    feedback: Any,
    comment: str | None,
    dialect: str,
    start_time: float,
) -> dict[str, Any]:
    """Regenerate SQL using accumulated context from all prior iterations."""
    # Check iteration cap
    current_count = await count_iterations(db, req.id)
    if current_count >= 5:
        iter_id = str(iteration.id)
        await update_request_status(db, req.id, RequestStatus.NEEDS_INTERVENTION)
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.warning(
            "iteration_cap_reached",
            request_id=str(req.id),
            max_iterations=5,
        )
        return {
            "action": "reject",
            "status": "needs_human_intervention",
            "request_status": "needs_human_intervention",
            "iteration_id": iter_id,
            "attempt_number": current_count,
            "query_sql": "",
            "error_message": "Iteration cap of 5 reached. Cannot regenerate further.",
            "needs_human_intervention": True,
            "latency_ms": round(elapsed, 1),
        }

    # Build regeneration context from previous iterations
    regen_context = _build_regen_context(req, comment)
    schema_description = get_schema_description()

    logger.info(
        "regen_started",
        request_id=str(req.id),
        previous_attempts=current_count,
        feedback=comment or "",
    )

    # Call LLM with accumulated context
    try:
        agent_response = await llm_generate(
            regen_context, dialect, schema_metadata=schema_description, request_id=str(req.id)
        )
    except AgentError as exc:
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.error("regen_llm_failed", request_id=str(req.id), error=str(exc))
        return {
            "action": "reject",
            "status": "failed",
            "request_status": "open",
            "iteration_id": str(iteration.id),
            "attempt_number": iteration.attempt_number,
            "query_sql": "",
            "error_message": f"LLM regeneration failed: {exc}",
            "needs_human_intervention": False,
            "latency_ms": round(elapsed, 1),
        }

    # Validate the regenerated SQL
    try:
        validate_or_raise(agent_response.query_sql, dialect, ValidationMode.QUERY_UNDER_TEST)
    except UnsafeSQLError as exc:
        elapsed = (time.perf_counter() - start_time) * 1000
        reasons = "; ".join(exc.reasons)
        logger.error("regen_validation_failed", request_id=str(req.id), reasons=exc.reasons)
        # Still save the iteration so user can see what was generated
        new_iter = await append_iteration(
            db, request_id=req.id,
            generated_sql=agent_response.query_sql,
            confidence=agent_response.confidence,
            rationale=agent_response.rationale,
            status=IterationStatus.FAILED,
            supersede_previous=False,
        )
        new_iter.error_message = f"Validation failed: {reasons}"
        new_iter.execution_results = []
        await db.commit()

        await record_trace(
            db, iteration_id=new_iter.id, agent_name="single_shot",
            prompt=regen_context,
            response=json.dumps({
                "query_sql": agent_response.query_sql,
                "confidence": agent_response.confidence,
                "rationale": agent_response.rationale,
            }),
            model=settings.OPENAI_MODEL,
        )

        return {
            "action": "reject",
            "status": "failed",
            "request_status": "open",
            "iteration_id": str(iteration.id),
            "attempt_number": iteration.attempt_number,
            "query_sql": agent_response.query_sql,
            "error_message": f"SQL validation failed: {reasons}",
            "needs_human_intervention": False,
            "latency_ms": round(elapsed, 1),
        }

    # Execute the regenerated SQL
    exec_start = time.perf_counter()
    execution_error: str | None = None
    status = "completed"
    rows = []
    row_count = 0
    query_ms = 0.0
    try:
        async with async_engine.connect() as conn:
            db_result = await conn.execute(text(agent_response.query_sql))
            query_ms = (time.perf_counter() - exec_start) * 1000
            raw_rows = db_result.fetchall()
            col_names = list(db_result.keys())
            rows = [dict(zip(col_names, row, strict=False)) for row in raw_rows]
            row_count = len(rows)
            logger.info(
                "regen_query_executed",
                request_id=str(req.id),
                rows=row_count,
                latency_ms=round(query_ms, 1),
            )
    except Exception as exc:
        execution_error = f"Query execution failed: {exc}"
        status = "failed"
        logger.error("regen_execution_failed", request_id=str(req.id), error=str(exc))

    # Persist the new iteration
    new_iter = await append_iteration(
        db,
        request_id=req.id,
        generated_sql=agent_response.query_sql,
        confidence=agent_response.confidence,
        rationale=agent_response.rationale,
        status=IterationStatus.EXECUTED if status == "completed" else IterationStatus.FAILED,
        supersede_previous=False,
    )
    new_iter.execution_results = rows if status == "completed" else []
    new_iter.execution_rows = row_count
    new_iter.execution_ms = query_ms if status == "completed" else None
    new_iter.error_message = execution_error
    await db.commit()

    await record_trace(
        db, iteration_id=new_iter.id, agent_name="single_shot",
        prompt=regen_context,
        response=json.dumps({
            "query_sql": agent_response.query_sql,
            "confidence": agent_response.confidence,
            "rationale": agent_response.rationale,
        }),
        model=settings.OPENAI_MODEL,
    )

    elapsed = (time.perf_counter() - start_time) * 1000
    logger.info(
        "regen_completed",
        request_id=str(req.id),
        new_iteration_id=str(new_iter.id),
        attempt=new_iter.attempt_number,
        status=status,
        latency_ms=round(elapsed, 1),
    )

    return {
        "action": "reject",
        "status": status,
        "request_status": "open",
        "iteration_id": str(new_iter.id),
        "attempt_number": new_iter.attempt_number,
        "query_sql": agent_response.query_sql,
        "confidence": agent_response.confidence,
        "rationale": agent_response.rationale,
        "execution_results": rows if status == "completed" else [],
        "execution_rows": row_count,
        "execution_ms": query_ms if status == "completed" else None,
        "error_message": execution_error,
        "needs_human_intervention": False,
        "latency_ms": round(elapsed, 1),
    }


async def _handle_edit(
    db: AsyncSession,
    req: Request,
    iteration: Iteration,
    feedback: Any,
    edited_sql: str | None,
    dialect: str,
    start_time: float,
) -> dict[str, Any]:
    """Validate and execute a manually edited SQL — bypasses LLM entirely."""
    if not edited_sql or not edited_sql.strip():
        raise FeedbackError("edited_sql is required for edit action")

    # Validate the edited SQL
    try:
        validate_or_raise(edited_sql, dialect, ValidationMode.QUERY_UNDER_TEST)
    except UnsafeSQLError as exc:
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.warning(
            "edit_validation_failed",
            request_id=str(req.id),
            reasons=exc.reasons,
        )
        return {
            "action": "edit",
            "status": "validation_failed",
            "request_status": "open",
            "iteration_id": str(iteration.id),
            "attempt_number": iteration.attempt_number,
            "query_sql": edited_sql,
            "error_message": f"SQL validation failed: {'; '.join(exc.reasons)}",
            "needs_human_intervention": False,
            "latency_ms": round(elapsed, 1),
        }

    # Execute the edited SQL directly (no LLM call)
    exec_start = time.perf_counter()
    execution_error: str | None = None
    status = "completed"
    rows = []
    row_count = 0
    query_ms = 0.0
    try:
        async with async_engine.connect() as conn:
            db_result = await conn.execute(text(edited_sql))
            query_ms = (time.perf_counter() - exec_start) * 1000
            raw_rows = db_result.fetchall()
            col_names = list(db_result.keys())
            rows = [dict(zip(col_names, row, strict=False)) for row in raw_rows]
            row_count = len(rows)
            logger.info(
                "edit_query_executed",
                request_id=str(req.id),
                rows=row_count,
                latency_ms=round(query_ms, 1),
            )
    except Exception as exc:
        execution_error = f"Query execution failed: {exc}"
        status = "failed"
        logger.error("edit_execution_failed", request_id=str(req.id), error=str(exc))

    # Save as a new iteration (bypassing LLM)
    new_iter = await append_iteration(
        db,
        request_id=req.id,
        generated_sql=edited_sql,
        confidence=1.0,
        rationale="User-edited SQL — executed directly without LLM",
        status=IterationStatus.EXECUTED if status == "completed" else IterationStatus.FAILED,
        supersede_previous=False,
    )
    new_iter.execution_results = rows if status == "completed" else []
    new_iter.execution_rows = row_count
    new_iter.execution_ms = query_ms if status == "completed" else None
    new_iter.error_message = execution_error
    await db.commit()

    await record_trace(
        db, iteration_id=new_iter.id, agent_name="manual_edit",
        prompt="User edited SQL manually",
        response=f"SQL: {edited_sql}\nStatus: {status}",
        model="manual",
    )

    elapsed = (time.perf_counter() - start_time) * 1000
    logger.info(
        "edit_completed",
        request_id=str(req.id),
        new_iteration_id=str(new_iter.id),
        attempt=new_iter.attempt_number,
        status=status,
        latency_ms=round(elapsed, 1),
    )

    return {
        "action": "edit",
        "status": status,
        "request_status": "open",
        "iteration_id": str(new_iter.id),
        "attempt_number": new_iter.attempt_number,
        "query_sql": edited_sql,
        "confidence": 1.0,
        "rationale": "User-edited SQL",
        "execution_results": rows if status == "completed" else [],
        "execution_rows": row_count,
        "execution_ms": query_ms if status == "completed" else None,
        "error_message": execution_error,
        "needs_human_intervention": False,
        "latency_ms": round(elapsed, 1),
    }


def _build_regen_context(req: Request, latest_comment: str | None) -> str:
    """Build a regeneration context string from all previous iterations."""
    parts: list[str] = []
    parts.append("=== ORIGINAL REQUEST ===")
    parts.append(req.question)
    parts.append("")

    if req.iterations:
        parts.append("=== PREVIOUS ATTEMPTS ===")
        for it in req.iterations:
            parts.append(f"--- Attempt {it.attempt_number} ---")
            parts.append(f"SQL: {it.generated_sql}")
            if it.rationale:
                parts.append(f"Rationale: {it.rationale}")
            if it.confidence is not None:
                parts.append(f"Confidence: {it.confidence}")
            if it.execution_rows is not None:
                parts.append(f"Results: {it.execution_rows} rows returned")
            if it.execution_ms is not None:
                parts.append(f"Execution time: {it.execution_ms:.0f}ms")
            if it.error_message:
                parts.append(f"Error: {it.error_message}")

            # Attach feedback for this iteration
            if it.feedbacks:
                for fb in it.feedbacks:
                    action_str = fb.action.value if hasattr(fb.action, "value") else str(fb.action)
                    parts.append(f"User feedback ({action_str}): {fb.comment or 'No comment'}")
                    if fb.edited_sql:
                        parts.append(f"User edited SQL: {fb.edited_sql}")
            parts.append("")

    if latest_comment:
        parts.append("=== USER FEEDBACK FOR THIS ATTEMPT ===")
        parts.append(latest_comment)
        parts.append("")

    parts.append("=== INSTRUCTIONS ===")
    parts.append("Based on all previous attempts and user feedback above, generate a NEW AND IMPROVED SQL query.")
    parts.append("The new query should address the user's feedback and fix any issues from prior attempts.")
    if latest_comment:
        parts.append(f"Key feedback to address: {latest_comment}")
    parts.append("Output ONLY the JSON object with query_sql, confidence, and rationale.")

    return "\n".join(parts)
