"""Dataset ingestion service — parse CSV/Excel, create tables in sandbox."""

from __future__ import annotations

import io
import math
import numbers
import os
from typing import Any

import pandas as pd
import structlog

from app.sandbox.executor import DatabaseExecutor

logger = structlog.get_logger()

# Maximum rows to load into sandbox to avoid overwhelming the container
MAX_DATASET_ROWS = 50000


def _infer_postgres_type(dtype: str, col_name: str) -> str:
    """Map pandas dtype to PostgreSQL column type."""
    if dtype.startswith("int") or dtype.startswith("uint"):
        return "BIGINT"
    if dtype.startswith("float"):
        return "DOUBLE PRECISION"
    if dtype == "bool":
        return "BOOLEAN"
    if dtype == "datetime64[ns]":
        return "TIMESTAMP"
    if dtype.startswith("datetime64"):
        return "TIMESTAMP"
    return "TEXT"


def _infer_oracle_type(dtype: str, col_name: str) -> str:
    """Map pandas dtype to Oracle column type."""
    if dtype.startswith("int") or dtype.startswith("uint"):
        return "NUMBER"
    if dtype.startswith("float"):
        return "BINARY_DOUBLE"
    if dtype == "bool":
        return "CHAR(1)"
    if dtype == "datetime64[ns]":
        return "TIMESTAMP"
    if dtype.startswith("datetime64"):
        return "TIMESTAMP"
    return "VARCHAR2(4000)"


def _sanitize_name(name: str) -> str:
    """Sanitize column/table name for SQL."""
    sanitized = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    if not sanitized or sanitized[0].isdigit():
        sanitized = "c_" + sanitized
    return sanitized.lower()


def _sanitize_table_name(filename: str) -> str:
    """Derive a safe table name from the filename."""
    base = os.path.basename(filename)
    return _sanitize_name(base)[:60]


async def load_csv_into_sandbox(
    executor: DatabaseExecutor,
    file_content: str | bytes,
    dialect: str,
    *,
    filename: str = "upload.csv",
    timeout: int = 120,
) -> dict[str, Any]:
    """Parse a CSV file, create a table in the sandbox, and load the data.

    Returns metadata about the created table:
        {table_name, columns: [{name, dtype, nullable}], row_count}
    """
    table_name = _sanitize_table_name(filename)
    logger.info("ingesting_csv", filename=filename, table_name=table_name)

    df = parse_csv(file_content)
    return await _load_dataframe_into_sandbox(executor, df, dialect, table_name, timeout=timeout)


async def load_excel_into_sandbox(
    executor: DatabaseExecutor,
    file_content: bytes,
    dialect: str,
    *,
    filename: str = "upload.xlsx",
    sheet_name: str | int = 0,
    timeout: int = 120,
) -> dict[str, Any]:
    """Parse an Excel file, create a table in the sandbox, and load the data."""
    table_name = _sanitize_table_name(filename)
    logger.info("ingesting_excel", filename=filename, table_name=table_name, sheet=sheet_name)

    df = parse_excel(file_content, sheet_name=sheet_name)
    return await _load_dataframe_into_sandbox(executor, df, dialect, table_name, timeout=timeout)


async def load_dataframe_into_sandbox(
    executor: DatabaseExecutor,
    df: pd.DataFrame,
    dialect: str,
    table_name: str,
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    return await _load_dataframe_into_sandbox(executor, df, dialect, table_name, timeout=timeout)


async def _load_dataframe_into_sandbox(
    executor: DatabaseExecutor,
    df: pd.DataFrame,
    dialect: str,
    table_name: str,
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    """Core ingestion: create table, insert rows."""
    df.columns = _sanitize_column_names(df.columns)
    columns = _dataframe_columns(df, dialect)
    col_defs = [f'"{c["name"]}" {c["dtype"]}' for c in columns]

    # 1. Create table
    create_sql = f'CREATE TABLE "{table_name}" ({", ".join(col_defs)})'
    logger.info("creating_table", sql=create_sql[:200])
    await executor.execute_ddl(create_sql, timeout=timeout)

    # 2. Insert data in batches
    batch_size = 500 if dialect == "postgres" else 1
    total_rows = 0
    for start in range(0, len(df), batch_size):
        batch = df.iloc[start : start + batch_size]
        if dialect == "postgres":
            sql = _build_postgres_insert(table_name, batch)
        else:
            sql = _build_oracle_insert(table_name, batch)
        await executor.execute_ddl(sql, timeout=timeout)
        total_rows += len(batch)

    logger.info(
        "ingestion_complete",
        table_name=table_name,
        rows=total_rows,
        columns=len(columns),
    )

    return {
        "table_name": table_name,
        "columns": columns,
        "row_count": total_rows,
    }


def _build_postgres_insert(table_name: str, batch: pd.DataFrame) -> str:
    """Build a PostgreSQL multi-value INSERT statement."""
    col_names = [f'"{c}"' for c in batch.columns]
    values: list[str] = []
    for _, row in batch.iterrows():
        row_vals: list[str] = []
        for val in row:
            if pd.isna(val):
                row_vals.append("NULL")
            elif isinstance(val, bool):
                row_vals.append("TRUE" if val else "FALSE")
            elif isinstance(val, numbers.Real):
                row_vals.append(_format_number(val))
            else:
                escaped = str(val).replace("'", "''")
                row_vals.append(f"'{escaped}'")
        values.append(f"({', '.join(row_vals)})")
    return f"INSERT INTO \"{table_name}\" ({', '.join(col_names)}) VALUES {', '.join(values)}"


def _build_oracle_insert(table_name: str, batch: pd.DataFrame) -> str:
    """Build a single Oracle INSERT statement."""
    col_names = [f'"{c}"' for c in batch.columns]
    rows: list[str] = []
    for _, row in batch.iterrows():
        row_vals: list[str] = []
        for val in row:
            if pd.isna(val):
                row_vals.append("NULL")
            elif isinstance(val, bool):
                row_vals.append("'Y'" if val else "'N'")
            elif isinstance(val, numbers.Real):
                row_vals.append(_format_number(val))
            else:
                escaped = str(val).replace("'", "''")
                row_vals.append(f"'{escaped}'")
        rows.append(f"INSERT INTO \"{table_name}\" ({', '.join(col_names)}) VALUES ({', '.join(row_vals)})")
    return "\n".join(rows)


def _sanitize_column_names(columns: Any) -> list[str]:
    """Sanitize and deduplicate dataframe column names."""
    seen: dict[str, int] = {}
    result: list[str] = []
    for raw in columns:
        base = _sanitize_name(str(raw))
        count = seen.get(base, 0)
        seen[base] = count + 1
        result.append(base if count == 0 else f"{base}_{count + 1}")
    return result


def _format_number(val: numbers.Real) -> str:
    if isinstance(val, numbers.Integral):
        return str(int(val))
    as_float = float(val)
    if math.isfinite(as_float):
        return repr(as_float)
    return "NULL"


def parse_csv(file_content: str | bytes) -> pd.DataFrame:
    if isinstance(file_content, str):
        file_content = file_content.encode("utf-8")
    df = pd.read_csv(io.BytesIO(file_content), nrows=MAX_DATASET_ROWS)
    df.columns = _sanitize_column_names(df.columns)
    return df


def parse_excel(file_content: bytes, *, sheet_name: str | int = 0) -> pd.DataFrame:
    df = pd.read_excel(io.BytesIO(file_content), sheet_name=sheet_name, nrows=MAX_DATASET_ROWS)
    df.columns = _sanitize_column_names(df.columns)
    return df


def _dataframe_columns(df: pd.DataFrame, dialect: str) -> list[dict[str, Any]]:
    infer_fn = _infer_postgres_type if dialect == "postgres" else _infer_oracle_type
    return [
        {
            "name": col_name,
            "dtype": infer_fn(str(df[col_name].dtype), col_name),
            "nullable": True,
        }
        for col_name in df.columns
    ]


def inspect_file(
    file_content: bytes,
    dialect: str,
    *,
    filename: str = "upload.csv",
) -> dict[str, Any]:
    """Parse an uploaded file and return the same metadata ingestion exposes."""
    ext = os.path.splitext(filename)[1].lower()
    df = parse_excel(file_content) if ext in (".xls", ".xlsx") else parse_csv(file_content)
    return {
        "table_name": _sanitize_table_name(filename),
        "columns": _dataframe_columns(df, dialect),
        "row_count": len(df),
    }


def suggested_prompts_for_dataset(table_name: str, columns: list[dict[str, Any]]) -> list[str]:
    """Return NL prompt suggestions tailored to known uploaded metadata CSVs."""
    column_names = {str(column["name"]).lower() for column in columns}

    if table_name == "consolidated_fbdi_csv" or {
        "object_name",
        "parent_object_name",
        "column_name",
        "null_allowed_flag",
        "column_type",
    }.issubset(column_names):
        return [
            "Which FBDI parent_object_name values have the most distinct object_name entries?",
            "Which FBDI columns are required where null_allowed_flag is N, grouped by parent_object_name?",
            "Show the most common FBDI column_name values across different object_name entries",
            "Count FBDI columns by column_type and include average column_width",
            "Find FBDI columns whose column_descrption mentions invoice, asset, or supplier",
        ]

    if table_name == "jde_tables_csv" or {
        "name",
        "table_description",
        "columns_count",
        "type",
        "report",
        "category",
        "field",
        "description",
        "data_type",
    }.issubset(column_names):
        return [
            "Which JDE report areas have the most distinct table names?",
            "List General Accounting JDE tables ordered by columns_count descending",
            "Count JDE fields by data_type and nullable",
            "Find JDE fields whose description contains Amount with name, field, data_type, length, and decimals",
            "Which JDE table types have the most tables and average columns_count?",
        ]

    return [
        f"Show the first 20 rows from {table_name}",
        f"Count rows in {table_name}",
        f"List columns available in {table_name}",
    ]


def combined_suggested_prompts(table_names: list[str]) -> list[str]:
    names = set(table_names)
    if {"consolidated_fbdi_csv", "jde_tables_csv"}.issubset(names):
        return [
            "Compare FBDI column_type counts with JDE data_type counts",
            "Find similar FBDI column_name and JDE field names, ignoring underscores and case",
            "Compare required FBDI columns with nullable JDE fields by name similarity",
        ]
    return []


async def ingest_file(
    executor: DatabaseExecutor,
    file_content: bytes,
    dialect: str,
    *,
    filename: str = "upload.csv",
    timeout: int = 120,
) -> dict[str, Any]:
    """Auto-detect file type (CSV or Excel) and ingest into sandbox."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".xls", ".xlsx"):
        return await load_excel_into_sandbox(
            executor, file_content, dialect, filename=filename, timeout=timeout
        )
    return await load_csv_into_sandbox(
        executor, file_content, dialect, filename=filename, timeout=timeout
    )
