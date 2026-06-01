# ruff: noqa: B008
from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.enums import Dialect, SessionStatus
from app.models.session import Iteration as IterationModel
from app.models.session import Request as RequestModel
from app.schemas.requests import (
    CreateNLRequest,
    CreateNLResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    GetRequestResponse,
    IterationDetail,
)
from app.services.request_service import execute_nl_pipeline
from app.services.session_service import create_session, get_session

logger = structlog.get_logger()
router = APIRouter(tags=["requests"])

# Full eager load option for request details
_REQUEST_WITH_ITERATIONS = selectinload(RequestModel.iterations).selectinload(
    IterationModel.feedbacks
)


@router.post("/sessions", response_model=CreateSessionResponse, status_code=201)
async def create_new_session(
    payload: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
) -> CreateSessionResponse:
    dialect = Dialect.POSTGRESQL if payload.dialect == "postgres" else Dialect.ORACLE
    session = await create_session(db, user_id="default", dialect=dialect)
    return CreateSessionResponse(
        session_id=session.id,
        dialect=payload.dialect,
        status=str(session.status),
        created_at=session.created_at,
    )


@router.post("/sessions/{session_id}/requests", response_model=CreateNLResponse, status_code=201)
async def create_nl_request(
    session_id: uuid.UUID,
    payload: CreateNLRequest,
    db: AsyncSession = Depends(get_db),
) -> CreateNLResponse:
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    if session.status != SessionStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=f"Session {session_id} is {session.status}")

    result = await execute_nl_pipeline(db, session, payload.prompt)

    return CreateNLResponse(
        request_id=result["request_id"],
        session_id=result["session_id"],
        question=result["question"],
        query_sql=result["query_sql"],
        confidence=result["confidence"],
        rationale=result["rationale"],
        execution_results=result["execution_results"],
        execution_rows=result["execution_rows"],
        execution_ms=result["execution_ms"],
        status=result["status"],
        error_message=result["error_message"],
        created_at=result.get("created_at"),
    )


@router.get("/sessions/{session_id}/requests/{request_id}", response_model=GetRequestResponse)
async def get_request_details(
    session_id: uuid.UUID,
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> GetRequestResponse:
    stmt = (
        select(RequestModel)
        .where(RequestModel.id == request_id)
        .options(_REQUEST_WITH_ITERATIONS)
    )
    result = await db.execute(stmt)
    req = result.scalar_one_or_none()

    if req is None:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")
    if req.session_id != session_id:
        raise HTTPException(status_code=404, detail="Request not found in this session")

    iteration = req.iterations[-1] if req.iterations else None

    # Build full iteration history
    iterations = []
    for it in req.iterations:
        fb = it.feedbacks[-1] if it.feedbacks else None
        iterations.append(
            IterationDetail(
                iteration_id=it.id,
                attempt_number=it.attempt_number,
                status=str(it.status),
                generated_sql=it.generated_sql,
                confidence=it.confidence,
                rationale=it.rationale,
                execution_results=it.execution_results,
                execution_rows=it.execution_rows,
                execution_ms=it.execution_ms,
                error_message=it.error_message,
                feedback_action=str(fb.action) if fb else None,
                feedback_comment=fb.comment if fb else None,
                created_at=it.created_at,
            )
        )

    return GetRequestResponse(
        request_id=req.id,
        session_id=req.session_id,
        question=req.question,
        generated_sql=iteration.generated_sql if iteration else "",
        confidence=iteration.confidence if iteration else None,
        rationale=iteration.rationale if iteration else None,
        execution_results=iteration.execution_results if iteration else None,
        execution_rows=iteration.execution_rows if iteration else None,
        execution_ms=iteration.execution_ms if iteration else None,
        status=(str(iteration.status) if iteration else "unknown"),
        error_message=iteration.error_message if iteration else None,
        request_status=str(req.status),
        iterations=iterations,
        created_at=req.created_at,
    )


@router.get("/sessions/{session_id}/requests", response_model=list[GetRequestResponse])
async def list_session_requests(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[GetRequestResponse]:
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    stmt = (
        select(RequestModel)
        .where(RequestModel.session_id == session_id)
        .options(_REQUEST_WITH_ITERATIONS)
        .order_by(RequestModel.created_at.desc())
    )
    result = await db.execute(stmt)
    requests = result.scalars().all()

    responses: list[GetRequestResponse] = []
    for req in requests:
        iteration = req.iterations[-1] if req.iterations else None

        iterations = []
        for it in req.iterations:
            fb = it.feedbacks[-1] if it.feedbacks else None
            iterations.append(
                IterationDetail(
                    iteration_id=it.id,
                    attempt_number=it.attempt_number,
                    status=str(it.status),
                    generated_sql=it.generated_sql,
                    confidence=it.confidence,
                    rationale=it.rationale,
                    execution_results=it.execution_results,
                    execution_rows=it.execution_rows,
                    execution_ms=it.execution_ms,
                    error_message=it.error_message,
                    feedback_action=str(fb.action) if fb else None,
                    feedback_comment=fb.comment if fb else None,
                    created_at=it.created_at,
                )
            )

        responses.append(
            GetRequestResponse(
                request_id=req.id,
                session_id=req.session_id,
                question=req.question,
                generated_sql=iteration.generated_sql if iteration else "",
                confidence=iteration.confidence if iteration else None,
                rationale=iteration.rationale if iteration else None,
                execution_results=iteration.execution_results if iteration else None,
                execution_rows=iteration.execution_rows if iteration else None,
                execution_ms=iteration.execution_ms if iteration else None,
                status=(str(iteration.status) if iteration else "unknown"),
                error_message=iteration.error_message if iteration else None,
                request_status=str(req.status),
                iterations=iterations,
                created_at=req.created_at,
            )
        )
    return responses
