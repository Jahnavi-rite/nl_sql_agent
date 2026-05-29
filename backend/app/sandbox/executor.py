"""
Database execution layer for sandbox containers.

Provides async database drivers for PostgreSQL (via ``asyncpg``) and
Oracle XE (via ``oracledb`` thin mode).  Each executor is a context
manager that connects, runs queries with a configurable timeout, and
closes cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# Default per-query timeout in seconds
DEFAULT_QUERY_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Abstract executor
# ---------------------------------------------------------------------------

class DatabaseExecutor(ABC):
    """Interface for database-specific query execution."""

    @abstractmethod
    async def connect(self, host: str, port: int, **kwargs: Any) -> None:
        ...

    @abstractmethod
    async def execute(self, sql: str, timeout: int = DEFAULT_QUERY_TIMEOUT) -> list[dict[str, Any]]:
        """Run *sql* and return the result rows as a list of dicts."""

    @abstractmethod
    async def execute_ddl(self, sql: str, timeout: int = DEFAULT_QUERY_TIMEOUT) -> None:
        """Run a DDL statement (no result rows expected)."""

    @abstractmethod
    async def explain(self, sql: str, timeout: int = DEFAULT_QUERY_TIMEOUT) -> list[dict[str, Any]]:
        """Return the query execution plan for *sql*."""

    @abstractmethod
    async def health(self) -> bool:
        """Return *True* if the database is reachable (SELECT 1)."""

    @abstractmethod
    async def close(self) -> None:
        ...


# ---------------------------------------------------------------------------
# PostgreSQL executor (asyncpg)
# ---------------------------------------------------------------------------

class PostgresExecutor(DatabaseExecutor):
    """Async executor using ``asyncpg``."""

    def __init__(self) -> None:
        self._conn: Any = None

    async def connect(self, host: str, port: int, **kwargs: Any) -> None:
        import asyncpg

        self._conn = await asyncpg.connect(
            host=host,
            port=port,
            user=kwargs.get("user", "sandbox"),
            password=kwargs.get("password", "sandbox"),
            database=kwargs.get("database", "sandbox"),
            timeout=10,
        )
        logger.debug("Connected to PostgreSQL at %s:%s", host, port)

    async def execute(self, sql: str, timeout: int = DEFAULT_QUERY_TIMEOUT) -> list[dict[str, Any]]:
        assert self._conn is not None, "Not connected"
        async with asyncio.timeout(timeout):
            rows = await self._conn.fetch(sql)
        if not rows:
            return []
        return [dict(row) for row in rows]

    async def execute_ddl(self, sql: str, timeout: int = DEFAULT_QUERY_TIMEOUT) -> None:
        assert self._conn is not None, "Not connected"
        async with asyncio.timeout(timeout):
            await self._conn.execute(sql)
        logger.debug("DDL executed (%.80s…)", sql)

    async def explain(self, sql: str, timeout: int = DEFAULT_QUERY_TIMEOUT) -> list[dict[str, Any]]:
        assert self._conn is not None, "Not connected"
        explained_sql = f"EXPLAIN (FORMAT JSON) {sql}"
        async with asyncio.timeout(timeout):
            rows = await self._conn.fetch(explained_sql)
        return [{"QUERY PLAN": row[0]} for row in rows]

    async def health(self) -> bool:
        if not self._conn or self._conn.is_closed():
            return False
        try:
            async with asyncio.timeout(5):
                await self._conn.fetch("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._conn and not self._conn.is_closed():
            await self._conn.close()
            logger.debug("PostgreSQL connection closed")


# ---------------------------------------------------------------------------
# Oracle executor (oracledb thin mode)
# ---------------------------------------------------------------------------

class OracleExecutor(DatabaseExecutor):
    """Async executor using ``oracledb`` thin mode (no Instant Client needed)."""

    def __init__(self) -> None:
        self._conn: Any = None

    async def connect(self, host: str, port: int, **kwargs: Any) -> None:
        import oracledb

        dsn = f"{host}:{port}/XEPDB1"
        self._conn = await oracledb.connect_async(
            user=kwargs.get("user", "sandbox"),
            password=kwargs.get("password", "SandboxPwd1"),
            dsn=dsn,
        )
        logger.debug("Connected to Oracle XE at %s:%s", host, port)

    async def execute(self, sql: str, timeout: int = DEFAULT_QUERY_TIMEOUT) -> list[dict[str, Any]]:
        assert self._conn is not None, "Not connected"
        async with asyncio.timeout(timeout):
            cursor = self._conn.cursor()
            await cursor.execute(sql)
            await self._conn.commit()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = await cursor.fetchall()
        return [dict(zip(columns, row, strict=False)) for row in rows]

    async def execute_ddl(self, sql: str, timeout: int = DEFAULT_QUERY_TIMEOUT) -> None:
        assert self._conn is not None, "Not connected"
        async with asyncio.timeout(timeout):
            cursor = self._conn.cursor()
            await cursor.execute(sql)
        logger.debug("DDL executed (%.80s…)", sql)

    async def explain(self, sql: str, timeout: int = DEFAULT_QUERY_TIMEOUT) -> list[dict[str, Any]]:
        """Oracle ``EXPLAIN PLAN FOR`` then ``DBMS_XPLAN.DISPLAY``."""
        assert self._conn is not None, "Not connected"
        async with asyncio.timeout(timeout):
            cursor = self._conn.cursor()
            await cursor.execute(f"EXPLAIN PLAN FOR {sql}")
            await cursor.execute("SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY)")
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = await cursor.fetchall()
        return [dict(zip(columns, row, strict=False)) for row in rows]

    async def health(self) -> bool:
        if not self._conn:
            return False
        try:
            async with asyncio.timeout(5):
                cursor = self._conn.cursor()
                await cursor.execute("SELECT 1 FROM DUAL")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            logger.debug("Oracle connection closed")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_executor(dialect: str) -> DatabaseExecutor:
    """Return the appropriate executor for *dialect*."""
    if dialect == "postgres":
        return PostgresExecutor()
    if dialect == "oracle":
        return OracleExecutor()
    raise ValueError(f"Unsupported dialect: {dialect!r}")
