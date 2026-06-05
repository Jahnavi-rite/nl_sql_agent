"""Startup CSV ingestion service — auto-detect and load CSV files into PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pandas as pd
import structlog
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = structlog.get_logger()

SCHEMA_CACHE: dict[str, Any] = {}


def find_csv_files(workspace_root: str | None = None) -> list[str]:
    """Recursively find all .csv files in the workspace and known sub-directories."""
    if workspace_root is None:
        root: Path = Path(__file__).resolve().parent.parent.parent.parent
        if str(root) == "/" and Path("/app").is_dir():
            root = Path("/app")
    else:
        root = Path(workspace_root)

    search_dirs: list[Path] = [root]
    for sub in ("datasets", "data", "csv"):
        candidate = root / sub
        if candidate.is_dir():
            search_dirs.append(candidate)

    csv_files: list[str] = []
    seen: set[str] = set()
    for search_dir in search_dirs:
        for root_dir, dirs, files in os.walk(str(search_dir)):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d
                not in (
                    "node_modules",
                    "venv",
                    ".venv",
                    "__pycache__",
                    ".git",
                    ".next",
                    ".egg-info",
                )
            ]
            for f in files:
                if f.lower().endswith(".csv") and not f.startswith("."):
                    full = os.path.join(root_dir, f)
                    if full not in seen:
                        seen.add(full)
                        csv_files.append(full)

    return csv_files


def sanitize_table_name(filename: str) -> str:
    """Derive a safe PostgreSQL table name from a CSV filename."""
    base = os.path.splitext(os.path.basename(filename))[0]
    sanitized = "".join(c if c.isalnum() or c == "_" else "_" for c in base)
    sanitized = sanitized.lower().strip("_")
    if not sanitized or sanitized[0].isdigit():
        sanitized = "t_" + sanitized
    return sanitized[:63]


def _get_sync_database_url() -> str:
    return (
        f"postgresql://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
    )


def table_exists(sync_engine: Any, table_name: str) -> bool:
    inspector = inspect(sync_engine)
    return table_name in inspector.get_table_names(schema="public")


def ingest_csv_to_postgres(csv_path: str, sync_engine: Any) -> dict[str, Any]:
    """Read a CSV file and ingest into PostgreSQL using pandas.to_sql()."""
    filename = os.path.basename(csv_path)
    table_name = sanitize_table_name(filename)

    logger.info("csv_discovered", file=csv_path, table_name=table_name)

    if table_exists(sync_engine, table_name):
        with sync_engine.connect() as conn:
            result = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
            row_count = result.scalar() or 0
        logger.info("table_already_exists", table_name=table_name, rows=row_count)
        return {"table_name": table_name, "filename": filename, "row_count": row_count}

    df = pd.read_csv(csv_path)
    df.columns = [sanitize_name(c) for c in df.columns]

    with sync_engine.begin() as conn:
        df.to_sql(
            name=table_name,
            con=conn,
            if_exists="replace",
            index=False,
            chunksize=5000,
            method="multi",
        )

    with sync_engine.connect() as conn:
        result = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
        row_count = result.scalar() or 0

    logger.info("csv_ingested", table_name=table_name, rows=row_count)
    return {"table_name": table_name, "filename": filename, "row_count": row_count}


def sanitize_name(name: str) -> str:
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in str(name)).lower()
    if not safe or safe[0].isdigit():
        safe = "c_" + safe
    return safe[:63]


def extract_db_schema(sync_engine: Any) -> dict[str, Any]:
    """Extract schema metadata for all user tables in the public schema."""
    inspector = inspect(sync_engine)
    all_tables: list[str] = inspector.get_table_names(schema="public")

    skip_tables = {
        "alembic_version",
        "sessions",
        "requests",
        "iterations",
        "feedbacks",
        "datasets",
        "agent_traces",
    }
    user_tables = [t for t in all_tables if t not in skip_tables]

    tables_meta: list[dict[str, Any]] = []
    for table_name in user_tables:
        columns = inspector.get_columns(table_name, schema="public")
        col_meta = [
            {
                "name": col["name"],
                "dtype": str(col["type"]),
                "nullable": col.get("nullable", True),
            }
            for col in columns
        ]
        row_count = 0
        try:
            with sync_engine.connect() as conn:
                result = conn.execute(
                    text(f'SELECT COUNT(*) FROM "{table_name}"')
                )
                row_count = result.scalar() or 0
        except Exception:
            pass
        tables_meta.append(
            {"table_name": table_name, "columns": col_meta, "row_count": row_count}
        )

    return {"tables": tables_meta}


def build_schema_description(schema: dict[str, Any]) -> str:
    lines: list[str] = []
    for table in schema.get("tables", []):
        lines.append(f"Table: {table['table_name']} ({table['row_count']} rows)")
        for col in table.get("columns", []):
            nullable = "NULL" if col.get("nullable", True) else "NOT NULL"
            lines.append(f"  - {col['name']} {col['dtype']} {nullable}")
        lines.append("")
    return "\n".join(lines)


async def run_startup_ingestion() -> dict[str, Any]:
    """Find CSVs, ingest them, extract schema, and cache in memory.

    Runs synchronous DB/pandas work in a thread pool to avoid
    blocking the async event loop during startup.
    """
    loop = asyncio.get_running_loop()

    def _sync_ingest() -> dict[str, Any]:
        engine = create_engine(_get_sync_database_url(), pool_pre_ping=True)
        try:
            csv_files = find_csv_files()
            if not csv_files:
                logger.info("no_csv_files_found")
                schema = extract_db_schema(engine)
                SCHEMA_CACHE["schema"] = schema
                SCHEMA_CACHE["description"] = build_schema_description(schema)
                SCHEMA_CACHE["ingested_at"] = pd.Timestamp.now().isoformat()
                return {"csv_files": [], "ingested": [], "schema_tables": len(schema["tables"])}

            logger.info("csv_files_discovered", count=len(csv_files), files=csv_files)

            ingested: list[dict[str, Any]] = []
            for csv_path in csv_files:
                try:
                    result = ingest_csv_to_postgres(csv_path, engine)
                    ingested.append(result)
                except Exception as exc:
                    logger.error("csv_ingestion_failed", file=csv_path, error=str(exc))

            schema = extract_db_schema(engine)
            SCHEMA_CACHE["schema"] = schema
            SCHEMA_CACHE["description"] = build_schema_description(schema)
            SCHEMA_CACHE["ingested_at"] = pd.Timestamp.now().isoformat()

            logger.info(
                "schema_cached",
                tables=len(schema["tables"]),
                description_length=len(SCHEMA_CACHE["description"]),
            )

            return {
                "csv_files": csv_files,
                "ingested": ingested,
                "schema_tables": len(schema["tables"]),
            }
        finally:
            engine.dispose()

    return await loop.run_in_executor(None, _sync_ingest)


def get_cached_schema() -> dict[str, Any]:
    return dict(SCHEMA_CACHE) if SCHEMA_CACHE else {}


def get_schema_description() -> str:
    desc = SCHEMA_CACHE.get("description")
    return desc if isinstance(desc, str) else ""


async def update_schema_cache(db: AsyncSession) -> None:
    """Dynamically query the database schema and update the cached SCHEMA_CACHE."""
    def _sync_extract(session: Any) -> dict[str, Any]:
        from sqlalchemy import inspect
        connection = session.connection()
        inspector = inspect(connection)

        # SQLite doesn't use the schema="public" prefix, PostgreSQL does.
        schema_name = "public" if connection.dialect.name == "postgresql" else None
        all_tables: list[str] = inspector.get_table_names(schema=schema_name)

        skip_tables = {
            "alembic_version",
            "sessions",
            "requests",
            "iterations",
            "feedbacks",
            "datasets",
            "agent_traces",
        }
        user_tables = [t for t in all_tables if t not in skip_tables]

        tables_meta: list[dict[str, Any]] = []
        for table_name in user_tables:
            columns = inspector.get_columns(table_name, schema=schema_name)
            col_meta = [
                {
                    "name": col["name"],
                    "dtype": str(col["type"]),
                    "nullable": col.get("nullable", True),
                }
                for col in columns
            ]
            row_count = 0
            try:
                from sqlalchemy import text
                # Quote table name to avoid syntax errors with reserved keywords
                result = connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"'))
                row_count = result.scalar() or 0
            except Exception:
                pass
            tables_meta.append(
                {"table_name": table_name, "columns": col_meta, "row_count": row_count}
            )

        return {"tables": tables_meta}

    schema = await db.run_sync(_sync_extract)
    SCHEMA_CACHE["schema"] = schema
    SCHEMA_CACHE["description"] = build_schema_description(schema)
    SCHEMA_CACHE["ingested_at"] = pd.Timestamp.now().isoformat()
