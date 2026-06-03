# ruff: noqa: B008
"""Feedback endpoint — approve/reject/edit iterations with regeneration."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.enums import SessionStatus
from app.services.feedback_service import FeedbackError, handle_feedback
from app.services.session_service import get_session

logger = structlog.get_logger()
router = APIRouter(tags=["feedback"])

FEEDBACK_TIMEOUT_MS = 180_000


class FeedbackRequest(BaseModel):
    iteration_id: uuid.UUID
    action: str = Field(pattern="^(approve|reject|edit)$")
    comment: str | None = Field(None, max_length=5000)
    edited_sql: str | None = Field(None, max_length=50000)


class FeedbackResponse(BaseModel):
    action: str
    status: str
    request_status: str
    iteration_id: str
    attempt_number: int
    query_sql: str
    confidence: float | None = None
    rationale: str | None = None
    execution_results: list[dict] = Field(default_factory=list)
    execution_rows: int = 0
    execution_ms: float | None = None
    error_message: str | None = None
    needs_human_intervention: bool = False
    is_manual_edit: bool = False
    latency_ms: float | None = None


@router.post("/sessions/{session_id}/feedback", status_code=200)
async def submit_feedback(
    session_id: uuid.UUID,
    payload: FeedbackRequest,
    db: AsyncSession = Depends(get_db),
) -> FeedbackResponse:
    """Submit feedback on an iteration.

    - `approve`: mark iteration as approved, close the request
    - `reject`: regenerate SQL with accumulated context (max 5 attempts)
    - `edit`: validate and execute manually edited SQL (bypasses LLM)
    """
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    if session.status != SessionStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=f"Session {session_id} is {session.status}")

    try:
        result = await handle_feedback(
            db,
            session_id=session_id,
            iteration_id=payload.iteration_id,
            action=payload.action,
            comment=payload.comment,
            edited_sql=payload.edited_sql,
        )
    except FeedbackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("feedback_unexpected_error", session_id=str(session_id), error=str(exc))
        raise HTTPException(status_code=500, detail=f"Feedback processing failed: {exc}") from exc

    return FeedbackResponse(
        action=result["action"],
        status=result["status"],
        request_status=result["request_status"],
        iteration_id=result["iteration_id"],
        attempt_number=result["attempt_number"],
        query_sql=result.get("query_sql", ""),
        confidence=result.get("confidence"),
        rationale=result.get("rationale"),
        execution_results=result.get("execution_results", []),
        execution_rows=result.get("execution_rows", 0),
        execution_ms=result.get("execution_ms"),
        error_message=result.get("error_message"),
        needs_human_intervention=result.get("needs_human_intervention", False),
        is_manual_edit=result.get("is_manual_edit", False),
        latency_ms=result.get("latency_ms"),
    )
