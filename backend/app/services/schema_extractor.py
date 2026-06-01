"""Schema extraction — converts database metadata into a compact schema description."""

from __future__ import annotations

from typing import Any

import structlog

from app.sandbox.executor import DatabaseExecutor

logger = structlog.get_logger()


def build_schema_description(
    tables: list[dict[str, Any]],
) -> str:
    """Build a compact schema description string from table metadata.

    Each table dict should have:
        - table_name: str
        - columns: list[dict] with keys: name, dtype, nullable
    """
    lines: list[str] = []
    for table in tables:
        lines.append(f"Table: {table['table_name']}")
        for col in table.get("columns", []):
            nullable = "NULL" if col.get("nullable", True) else "NOT NULL"
            lines.append(f"  - {col['name']} {col['dtype']} {nullable}")
        lines.append("")
    return "\n".join(lines)


async def extract_schema(
    executor: DatabaseExecutor,
    dialect: str,
    table_names: list[str],
    *,
    timeout: int = 10,
) -> list[dict[str, Any]]:
    """Extract column metadata for the given tables from the sandbox database.

    Returns a list of dicts:
        [{"table_name": "...", "columns": [{"name": "...", "dtype": "...", "nullable": bool}, ...]}, ...]
    """
    schemas: list[dict[str, Any]] = []

    if dialect == "postgres":
        for table in table_names:
            rows = await executor.execute(
                f"""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = '{table}'
                ORDER BY ordinal_position
                """,
                timeout=timeout,
            )
            columns = [
                {
                    "name": r["column_name"],
                    "dtype": r["data_type"],
                    "nullable": r["is_nullable"] == "YES",
                }
                for r in rows
            ]
            schemas.append({"table_name": table, "columns": columns})
    elif dialect == "oracle":
        for table in table_names:
            rows = await executor.execute(
                f"""
                SELECT column_name, data_type, nullable
                FROM all_tab_columns
                WHERE table_name = '{table.upper()}'
                ORDER BY column_id
                """,
                timeout=timeout,
            )
            columns = [
                {
                    "name": r["column_name"].lower(),
                    "dtype": r["data_type"],
                    "nullable": r["nullable"] == "Y",
                }
                for r in rows
            ]
            schemas.append({"table_name": table, "columns": columns})
    else:
        raise ValueError(f"Unsupported dialect: {dialect}")

    return schemas
