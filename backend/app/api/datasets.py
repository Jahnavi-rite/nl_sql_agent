# ruff: noqa: B008, SIM105
"""Dataset upload and management API endpoints."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.enums import SessionStatus
from app.models.session import Dataset
from app.schemas.requests import DatasetUploadResponse
from app.services.dataset_service import (
    combined_suggested_prompts,
    inspect_file,
    suggested_prompts_for_dataset,
)
from app.services.session_service import get_session

logger = structlog.get_logger()
router = APIRouter(tags=["datasets"])


@router.post("/sessions/{session_id}/datasets", response_model=DatasetUploadResponse, status_code=201)
async def upload_dataset(
    session_id: uuid.UUID,
    file: UploadFile,
    dialect: str = "postgres",
    db: AsyncSession = Depends(get_db),
) -> DatasetUploadResponse:
    """Upload a CSV or Excel file as a dataset for the session.

    The file is ingested into an isolated sandbox database container,
    a table is created from the file structure, and metadata is stored.
    """
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    if session.status != SessionStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=f"Session {session_id} is {session.status}")
    if dialect not in ("postgres", "oracle"):
        raise HTTPException(status_code=422, detail=f"Unsupported dialect: {dialect}")

    if not file.filename:
        raise HTTPException(status_code=422, detail="No filename provided")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ("csv", "xls", "xlsx"):
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type: .{ext}. Supported: .csv, .xls, .xlsx",
        )

    # Read file content
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="Empty file")

    try:
        ingested = inspect_file(
            content,
            dialect,
            filename=file.filename,
        )
    except Exception as exc:
        logger.error("dataset_ingestion_failed", session_id=str(session_id), error=str(exc))
        raise HTTPException(status_code=500, detail=f"Dataset ingestion failed: {exc}") from exc

    # Persist dataset metadata
    dataset = Dataset(
        session_id=session_id,
        filename=file.filename,
        table_name=ingested["table_name"],
        dialect=dialect,
        columns_json=ingested["columns"],
        row_count=ingested["row_count"],
        file_content=content,
        status="ingested",
    )
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)

    logger.info(
        "dataset_uploaded",
        dataset_id=str(dataset.id),
        session_id=str(session_id),
        filename=file.filename,
        table_name=ingested["table_name"],
        rows=ingested["row_count"],
    )
    suggested_prompts = suggested_prompts_for_dataset(
        ingested["table_name"],
        ingested["columns"],
    )

    return DatasetUploadResponse(
        dataset_id=dataset.id,
        session_id=session_id,
        filename=file.filename,
        table_name=ingested["table_name"],
        columns=[{"name": c["name"], "dtype": c["dtype"], "nullable": c.get("nullable", True)} for c in ingested["columns"]],
        row_count=ingested["row_count"],
        status="ingested",
        suggested_prompts=suggested_prompts,
        created_at=dataset.created_at,
    )


@router.get("/sessions/{session_id}/datasets")
async def list_datasets(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List all uploaded datasets for a session."""
    from sqlalchemy import select

    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    stmt = (
        select(Dataset)
        .where(Dataset.session_id == session_id)
        .order_by(Dataset.created_at.desc())
    )
    result = await db.execute(stmt)
    datasets = result.scalars().all()

    table_names = [ds.table_name for ds in datasets]
    shared_prompts = combined_suggested_prompts(table_names)

    return [
        {
            "dataset_id": str(ds.id),
            "filename": ds.filename,
            "table_name": ds.table_name,
            "dialect": ds.dialect,
            "columns": ds.columns_json or [],
            "row_count": ds.row_count,
            "status": ds.status,
            "suggested_prompts": [
                *suggested_prompts_for_dataset(ds.table_name, ds.columns_json or []),
                *shared_prompts,
            ],
            "created_at": ds.created_at,
        }
        for ds in datasets
    ]
