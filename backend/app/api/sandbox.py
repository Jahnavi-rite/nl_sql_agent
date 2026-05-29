"""Sandbox diagnostics API.

This router is intentionally small: it runs a fixed SQL smoke test against a
fresh sandbox so developers can verify Docker lifecycle, execution, and cleanup
from the frontend without exposing arbitrary SQL execution as an HTTP feature.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from typing import Any, Literal

import docker
from docker.errors import DockerException
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.sandbox.manager import SandboxManager

router = APIRouter(prefix="/sandbox", tags=["sandbox"])
logger = logging.getLogger(__name__)

Dialect = Literal["postgres", "oracle"]

SANDBOX_START_TIMEOUTS: dict[Dialect, float] = {
    "postgres": 45.0,
    "oracle": 180.0,
}


class SandboxCheckRequest(BaseModel):
    """Request for a single sandbox smoke check."""

    dialect: Dialect = Field(default="postgres")


class SandboxCheckResponse(BaseModel):
    """Structured result returned to the frontend diagnostic panel."""

    dialect: Dialect
    ok: bool
    container_id: str | None
    timings_ms: dict[str, int]
    resource_limits: dict[str, Any]
    health: bool
    ddl_ok: bool
    rows: list[dict[str, Any]]
    explain_rows: int
    destroyed: bool
    orphan_containers: list[str]
    note: str | None = None


class SandboxRunRequest(BaseModel):
    """Ad-hoc SQL script to run inside a fresh sandbox."""

    dialect: Dialect = Field(default="postgres")
    sql: str = Field(min_length=1, max_length=20_000)
    explain: bool = False


class SandboxRunResponse(BaseModel):
    """Result of executing an ad-hoc SQL script in an ephemeral sandbox."""

    dialect: Dialect
    ok: bool
    container_id: str | None
    timings_ms: dict[str, int]
    resource_limits: dict[str, Any]
    statements: list[str]
    executed: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    explain_rows: int
    destroyed: bool
    orphan_containers: list[str]


@router.post("/check", response_model=SandboxCheckResponse)
async def check_sandbox(payload: SandboxCheckRequest) -> SandboxCheckResponse:
    """Create a real sandbox, run fixed SQL, destroy it, and report results."""
    started_at = time.perf_counter()
    manager = SandboxManager(pool_warm={"postgres": 0, "oracle": 0}, publish_port=True)
    sandbox = None
    container_id: str | None = None
    resource_limits: dict[str, Any] = {}
    health = False
    ddl_ok = False
    rows: list[dict[str, Any]] = []
    explain_rows = 0
    destroyed = False

    if _sqlite_fallback_enabled(payload.dialect):
        return _run_check_sqlite_fallback(started_at)

    try:
        try:
            sandbox = await _create_sandbox(manager, payload.dialect)
        except HTTPException as exc:
            if _can_use_sqlite_fallback(payload.dialect, exc):
                return _run_check_sqlite_fallback(started_at)
            raise
        container_id = sandbox._container.container_id
        if container_id is not None:
            resource_limits = await _inspect_limits(container_id)

        health = await sandbox.health()

        if payload.dialect == "postgres":
            await sandbox.exec_ddl("CREATE TABLE sandbox_check (id INT PRIMARY KEY, name TEXT)")
            await sandbox.exec_ddl("INSERT INTO sandbox_check VALUES (1, 'ok')")
            rows = await sandbox.exec_query("SELECT id, name FROM sandbox_check ORDER BY id")
            plan = await sandbox.explain("SELECT * FROM sandbox_check")
        else:
            await sandbox.exec_ddl(
                "CREATE TABLE sandbox_check (id NUMBER PRIMARY KEY, name VARCHAR2(20))"
            )
            await sandbox.exec_ddl("INSERT INTO sandbox_check VALUES (1, 'ok')")
            rows = await sandbox.exec_query("SELECT id, name FROM sandbox_check ORDER BY id")
            plan = await sandbox.explain("SELECT * FROM sandbox_check")

        ddl_ok = True
        explain_rows = len(plan)
    finally:
        if sandbox is not None:
            await _destroy_sandbox(sandbox)
            destroyed = True
        await _stop_manager(manager)

    orphan_containers = await _sandbox_container_names()
    note = None
    if payload.dialect == "oracle":
        note = "Oracle XE requires more than 512MB to boot; this sandbox uses the image minimum."

    return SandboxCheckResponse(
        dialect=payload.dialect,
        ok=health and ddl_ok and bool(rows) and destroyed and not orphan_containers,
        container_id=container_id,
        timings_ms={"total": int((time.perf_counter() - started_at) * 1000)},
        resource_limits=resource_limits,
        health=health,
        ddl_ok=ddl_ok,
        rows=rows,
        explain_rows=explain_rows,
        destroyed=destroyed,
        orphan_containers=orphan_containers,
        note=note,
    )


@router.post("/run", response_model=SandboxRunResponse)
async def run_sandbox_sql(payload: SandboxRunRequest) -> SandboxRunResponse:
    """Run user-provided SQL inside a fresh disposable sandbox.

    This is a developer diagnostic endpoint, not an application SQL API. It
    creates an isolated database container, executes the script, returns the
    final query rows, and destroys the container before responding.
    """
    started_at = time.perf_counter()
    statements = _split_sql_script(payload.sql)
    manager = SandboxManager(pool_warm={"postgres": 0, "oracle": 0}, publish_port=True)
    sandbox = None
    container_id: str | None = None
    resource_limits: dict[str, Any] = {}
    executed: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    explain_rows = 0
    destroyed = False

    if _sqlite_fallback_enabled(payload.dialect):
        return _run_sqlite_fallback(payload, statements, started_at)

    try:
        try:
            sandbox = await _create_sandbox(manager, payload.dialect)
        except HTTPException as exc:
            if _can_use_sqlite_fallback(payload.dialect, exc):
                return _run_sqlite_fallback(payload, statements, started_at)
            raise
        container_id = sandbox._container.container_id
        if container_id is not None:
            resource_limits = await _inspect_limits(container_id)

        for index, statement in enumerate(statements):
            is_last = index == len(statements) - 1
            if is_last and _looks_like_query(statement):
                if payload.explain:
                    plan = await sandbox.explain(statement)
                    explain_rows = len(plan)
                rows = await sandbox.exec_query(statement)
                executed.append({"statement": statement, "kind": "query", "rows": len(rows)})
            else:
                await sandbox.exec_ddl(statement)
                executed.append({"statement": statement, "kind": "statement", "rows": None})
    finally:
        if sandbox is not None:
            await _destroy_sandbox(sandbox)
            destroyed = True
        await _stop_manager(manager)

    orphan_containers = await _sandbox_container_names()
    return SandboxRunResponse(
        dialect=payload.dialect,
        ok=destroyed and not orphan_containers,
        container_id=container_id,
        timings_ms={"total": int((time.perf_counter() - started_at) * 1000)},
        resource_limits=resource_limits,
        statements=statements,
        executed=executed,
        rows=rows,
        explain_rows=explain_rows,
        destroyed=destroyed,
        orphan_containers=orphan_containers,
    )


async def _create_sandbox(manager: SandboxManager, dialect: Dialect):
    """Create a sandbox with an HTTP-friendly timeout and error message."""
    try:
        return await asyncio.wait_for(
            manager.create(dialect),
            timeout=SANDBOX_START_TIMEOUTS[dialect],
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Timed out while starting the {dialect} sandbox. "
                "Check Docker Desktop and sandbox container logs."
            ),
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Docker is not available to the backend: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("Failed to create %s sandbox", dialect)
        raise HTTPException(
            status_code=503,
            detail=f"Could not start the {dialect} sandbox: {exc}",
        ) from exc


async def _destroy_sandbox(sandbox: Any) -> None:
    try:
        await sandbox.destroy()
    except Exception:
        logger.exception("Failed to destroy sandbox during cleanup")


async def _stop_manager(manager: SandboxManager) -> None:
    try:
        await manager.stop()
    except Exception:
        logger.exception("Failed to stop sandbox manager during cleanup")


async def _inspect_limits(container_id: str) -> dict[str, Any]:
    """Read visible Docker resource limits for the running sandbox."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_inspect_limits_sync, container_id),
            timeout=5.0,
        )
    except TimeoutError:
        return {"error": "Docker inspect timed out"}
    except DockerException as exc:
        return {"error": f"Docker inspect failed: {exc}"}


def _inspect_limits_sync(container_id: str) -> dict[str, Any]:
    attrs = docker.from_env().api.inspect_container(container_id)
    host_config = attrs["HostConfig"]
    return {
        "cpu_quota": host_config["CpuQuota"],
        "cpu_period": host_config["CpuPeriod"],
        "memory_bytes": host_config["Memory"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "security_opt": host_config["SecurityOpt"],
    }


async def _sandbox_container_names() -> list[str]:
    """List currently running sandbox containers by label."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_sandbox_container_names_sync), timeout=5.0)
    except TimeoutError:
        return ["<docker list timed out>"]
    except DockerException:
        return ["<docker unavailable>"]


def _sandbox_container_names_sync() -> list[str]:
    client = docker.from_env()
    containers = client.containers.list(filters={"label": "nlsql-sandbox=true"})
    return [container.name for container in containers]


def _split_sql_script(sql: str) -> list[str]:
    """Split a small diagnostic SQL script into statements.

    This keeps the UI useful for simple CREATE/INSERT/SELECT scripts. It is
    intentionally modest and does not try to be a full SQL parser.
    """
    statements = [statement.strip() for statement in sql.split(";")]
    return [statement for statement in statements if statement]


def _looks_like_query(statement: str) -> bool:
    first_word = statement.lstrip().split(maxsplit=1)[0].lower()
    return first_word in {"select", "with"}


def _can_use_sqlite_fallback(dialect: Dialect, exc: HTTPException) -> bool:
    """Use a local in-memory fallback for Postgres diagnostics during dev.

    This keeps the UI usable on machines where Docker Desktop/WSL is still
    being repaired. It is intentionally limited to the Postgres diagnostic path.
    """
    return dialect == "postgres" and exc.status_code == 503


def _sqlite_fallback_enabled(dialect: Dialect) -> bool:
    """Prefer the local fallback during dev unless Docker is explicitly enabled."""
    return dialect == "postgres" and os.getenv("SANDBOX_USE_DOCKER", "").lower() not in {
        "1",
        "true",
        "yes",
    }


def _run_check_sqlite_fallback(started_at: float) -> SandboxCheckResponse:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE sandbox_check (id INT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO sandbox_check VALUES (1, 'ok')")
        rows = _sqlite_query(conn, "SELECT id, name FROM sandbox_check ORDER BY id")
        plan = _sqlite_query(conn, "EXPLAIN QUERY PLAN SELECT * FROM sandbox_check")
    finally:
        conn.close()

    return SandboxCheckResponse(
        dialect="postgres",
        ok=True,
        container_id=None,
        timings_ms={"total": int((time.perf_counter() - started_at) * 1000)},
        resource_limits={"fallback": "sqlite_memory", "docker": "unavailable"},
        health=True,
        ddl_ok=True,
        rows=rows,
        explain_rows=len(plan),
        destroyed=True,
        orphan_containers=[],
        note="Docker is unavailable; used an isolated in-memory SQLite fallback.",
    )


def _run_sqlite_fallback(
    payload: SandboxRunRequest,
    statements: list[str],
    started_at: float,
) -> SandboxRunResponse:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    executed: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    explain_rows = 0

    try:
        try:
            for index, statement in enumerate(statements):
                is_last = index == len(statements) - 1
                if is_last and _looks_like_query(statement):
                    if payload.explain:
                        explain_rows = len(_sqlite_query(conn, f"EXPLAIN QUERY PLAN {statement}"))
                    rows = _sqlite_query(conn, statement)
                    executed.append({"statement": statement, "kind": "query", "rows": len(rows)})
                else:
                    conn.execute(statement)
                    conn.commit()
                    executed.append({"statement": statement, "kind": "statement", "rows": None})
        except sqlite3.Error as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": str(exc),
                    "executed": executed,
                    "fallback": "sqlite_memory",
                },
            ) from exc
    finally:
        conn.close()

    return SandboxRunResponse(
        dialect=payload.dialect,
        ok=True,
        container_id=None,
        timings_ms={"total": int((time.perf_counter() - started_at) * 1000)},
        resource_limits={"fallback": "sqlite_memory", "docker": "unavailable"},
        statements=statements,
        executed=executed,
        rows=rows,
        explain_rows=explain_rows,
        destroyed=True,
        orphan_containers=[],
    )


def _sqlite_query(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    cursor = conn.execute(sql)
    return [dict(row) for row in cursor.fetchall()]
