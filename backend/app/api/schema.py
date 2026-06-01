"""
Schema introspection endpoint — exposes the cached database schema.
Useful for the frontend to show available tables and columns after startup ingestion.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter

from app.services.startup_ingestion import get_cached_schema

logger = structlog.get_logger()
router = APIRouter(tags=["schema"])


@router.get("/schema")
async def get_schema() -> dict[str, Any]:
    """Return the cached database schema metadata (tables, columns, types)."""
    schema = get_cached_schema()
    return {
        "tables": schema.get("schema", {}).get("tables", []),
        "description": schema.get("description", ""),
        "ingested_at": schema.get("ingested_at"),
    }
