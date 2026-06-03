from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    dialect: str = Field(default="postgres", pattern="^(postgres|oracle)$")


class CreateSessionResponse(BaseModel):
    session_id: uuid.UUID
    dialect: str
    status: str
    created_at: datetime


class CreateNLRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=10_000)
    dialect: str = Field(default="postgres", pattern="^(postgres|oracle)$")


class ExecutionResult(BaseModel):
    query_sql: str
    confidence: float | None = None
    rationale: str | None = None
    execution_results: list[dict[str, Any]] = Field(default_factory=list)
    execution_rows: int = 0
    execution_ms: float | None = None
    status: str = "pending"
    error_message: str | None = None


class CreateNLResponse(BaseModel):
    request_id: uuid.UUID
    session_id: uuid.UUID
    question: str
    query_sql: str
    confidence: float | None = None
    rationale: str | None = None
    execution_results: list[dict[str, Any]] = Field(default_factory=list)
    execution_rows: int = 0
    execution_ms: float | None = None
    status: str
    error_message: str | None = None
    created_at: datetime | None = None


class IterationDetail(BaseModel):
    iteration_id: uuid.UUID
    attempt_number: int
    status: str
    generated_sql: str
    confidence: float | None = None
    rationale: str | None = None
    execution_results: list[dict[str, Any]] | None = None
    execution_rows: int | None = None
    execution_ms: float | None = None
    error_message: str | None = None
    feedback_action: str | None = None
    feedback_comment: str | None = None
    is_manual_edit: bool = False
    created_at: datetime | None = None


class GetRequestResponse(BaseModel):
    request_id: uuid.UUID
    session_id: uuid.UUID
    question: str
    generated_sql: str
    confidence: float | None = None
    rationale: str | None = None
    execution_results: list[dict[str, Any]] | None = None
    execution_rows: int | None = None
    execution_ms: float | None = None
    status: str
    error_message: str | None = None
    request_status: str | None = None
    iterations: list[IterationDetail] = Field(default_factory=list)
    created_at: datetime | None = None


class ColumnMetadata(BaseModel):
    name: str
    dtype: str
    nullable: bool = True


class DatasetInfo(BaseModel):
    dataset_id: uuid.UUID
    filename: str
    table_name: str
    dialect: str
    columns: list[ColumnMetadata] = Field(default_factory=list)
    row_count: int | None = None
    status: str
    suggested_prompts: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class DatasetUploadResponse(BaseModel):
    dataset_id: uuid.UUID
    session_id: uuid.UUID
    filename: str
    table_name: str
    columns: list[ColumnMetadata] = Field(default_factory=list)
    row_count: int
    status: str
    suggested_prompts: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
