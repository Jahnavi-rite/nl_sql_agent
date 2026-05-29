"""SQL validation endpoint — wraps sql_guard for interactive testing."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.validators.sql_guard import validate

router = APIRouter(tags=["validate"])


class ValidateRequest(BaseModel):
    sql: str
    dialect: str = "postgres"
    mode: str = "query_under_test"


class ValidateResponse(BaseModel):
    is_safe: bool
    reasons: list[str]
    redacted_sql: str


@router.post("/validate", response_model=ValidateResponse)
async def validate_sql(req: ValidateRequest) -> ValidateResponse:
    """
    POST /validate — Check if SQL is safe before execution.

    Request body:
        {"sql": "SELECT * FROM users", "dialect": "postgres", "mode": "query_under_test"}

    Response:
        {"is_safe": true, "reasons": [], "redacted_sql": "SELECT * FROM users"}
    """
    result = validate(req.sql, req.dialect, req.mode)
    return ValidateResponse(
        is_safe=result.is_safe,
        reasons=result.reasons,
        redacted_sql=result.redacted_sql,
    )
