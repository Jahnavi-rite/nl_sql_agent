"""
Sandbox orchestration — high-level API for creating, using, and destroying
isolated database sandboxes.

Public classes
--------------
Sandbox
    A running database sandbox with methods to execute SQL.
SandboxPool
    Maintains a configurable number of *warm* containers (connected and
    healthy) so that ``SandboxManager.create()`` returns in milliseconds
    instead of waiting for a cold start.
SandboxManager
    Entry point — call ``await manager.create("postgres")`` to get a
    ready-to-use ``Sandbox``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Literal

from app.sandbox.container import SandboxContainer
from app.sandbox.executor import (
    DEFAULT_QUERY_TIMEOUT,
    DatabaseExecutor,
    create_executor,
)

logger = logging.getLogger(__name__)

Dialect = Literal["postgres", "oracle"]


# ======================================================================
# Sandbox — the object users interact with
# ======================================================================

class Sandbox:
    """A running database sandbox backed by a Docker container.

    Typical usage::

        sandbox = await manager.create("postgres")
        await sandbox.exec_ddl("CREATE TABLE foo (id INT)")
        rows = await sandbox.exec_query("SELECT * FROM foo")
        plan = await sandbox.explain("SELECT * FROM foo")
        await sandbox.destroy()
    """

    def __init__(
        self,
        dialect: Dialect,
        container: SandboxContainer,
        executor: DatabaseExecutor,
        *,
        query_timeout: int = DEFAULT_QUERY_TIMEOUT,
    ) -> None:
        self.dialect = dialect
        self._container = container
        self._executor = executor
        self._query_timeout = query_timeout
        self._destroyed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def exec_ddl(self, sql: str, *, timeout: int | None = None) -> None:
        """Execute a DDL (or any non-result-returning) statement."""
        self._require_alive()
        await self._executor.execute_ddl(sql, timeout or self._query_timeout)

    async def exec_query(self, sql: str, *, timeout: int | None = None) -> list[dict[str, Any]]:
        """Execute a query and return results as a list of dicts."""
        self._require_alive()
        return await self._executor.execute(sql, timeout or self._query_timeout)

    async def explain(self, sql: str, *, timeout: int | None = None) -> list[dict[str, Any]]:
        """Return the query execution plan.

        The dialect-specific ``EXPLAIN`` command is used (``EXPLAIN (FORMAT
        JSON)`` for PostgreSQL, ``EXPLAIN PLAN FOR`` + ``DBMS_XPLAN`` for
        Oracle).
        """
        self._require_alive()
        return await self._executor.explain(sql, timeout or self._query_timeout)

    async def destroy(self) -> None:
        """Tear down the container and release all resources.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._destroyed:
            return
        self._destroyed = True
        await self._executor.close()
        self._container.stop()
        logger.info("Sandbox (%s) destroyed", self.dialect)

    # ------------------------------------------------------------------
    # Health / internal
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        """Return *True* if the container is running and the DB is reachable."""
        if self._destroyed or not self._container.is_running():
            return False
        return await self._executor.health()

    def _require_alive(self) -> None:
        if self._destroyed:
            raise RuntimeError("This sandbox has already been destroyed.")
        if not self._container.is_running():
            raise RuntimeError("The underlying container is no longer running.")

    async def __aenter__(self) -> Sandbox:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.destroy()


# ======================================================================
# SandboxPool — warm container cache
# ======================================================================

class SandboxPool:
    """Pre-allocated pool of warm sandbox containers.

    The pool eagerly creates *warm_count* containers per dialect so that
    ``acquire()`` returns a ready-to-use ``Sandbox`` almost instantly.

    When a sandbox is acquired, the pool spawns a background task to
    replenish the slot so the warm count stays constant.
    """

    def __init__(
        self,
        warm: dict[Dialect, int] | None = None,
        *,
        publish_port: bool = False,
        query_timeout: int = DEFAULT_QUERY_TIMEOUT,
    ) -> None:
        self._publish_port = publish_port
        self._query_timeout = query_timeout
        self._warm_targets: dict[Dialect, int] = warm or {
            "postgres": 2,
            "oracle": 1,
        }
        # Each dialect has an asyncio.Queue of pre-warmed Sandbox instances
        self._queues: dict[Dialect, asyncio.Queue[Sandbox]] = {}
        self._replenish_tasks: list[asyncio.Task] = []
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Eagerly create warm containers in the background.

        This method returns as soon as the creation tasks are launched;
        individual containers become available as they finish starting up.
        """
        if self._started:
            return
        self._started = True

        for dialect, count in self._warm_targets.items():
            self._queues[dialect] = asyncio.Queue(maxsize=count)
            for _ in range(count):
                task = asyncio.create_task(
                    self._create_and_enqueue(dialect),
                    name=f"warm-{dialect}",
                )
                self._replenish_tasks.append(task)

        logger.info(
            "SandboxPool started: warm targets %s",
            dict(self._warm_targets),
        )

    async def stop(self) -> None:
        """Cancel replenishment tasks and destroy all warm sandboxes."""
        for task in self._replenish_tasks:
            task.cancel()
        await asyncio.gather(*self._replenish_tasks, return_exceptions=True)
        self._replenish_tasks.clear()

        for queue in self._queues.values():
            while not queue.empty():
                sandbox = queue.get_nowait()
                await sandbox.destroy()
        self._queues.clear()
        self._started = False

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    async def acquire(self, dialect: Dialect) -> Sandbox | None:
        """Return a warm sandbox, or *None* if none is available.

        If a warm sandbox is taken, a replacement is created in the
        background.
        """
        queue = self._queues.get(dialect)
        if queue is None or queue.empty():
            return None

        sandbox = queue.get_nowait()

        # Spawn replenishment for this dialect
        task = asyncio.create_task(
            self._create_and_enqueue(dialect),
            name=f"replenish-{dialect}",
        )
        self._replenish_tasks.append(task)

        return sandbox

    async def _create_and_enqueue(self, dialect: Dialect) -> None:
        """Create a warm sandbox and put it in the queue.

        If creation fails, the slot is simply left empty and the caller
        (``acquire`` or ``start``) is responsible for retrying.
        """
        try:
            sandbox = await _cold_start(
                dialect,
                publish_port=self._publish_port,
                query_timeout=self._query_timeout,
            )
            queue = self._queues.get(dialect)
            if queue is not None:
                await queue.put(sandbox)
                logger.debug("Warm %s sandbox enqueued", dialect)
        except Exception:
            logger.exception("Failed to create warm %s sandbox", dialect)


# ======================================================================
# SandboxManager — public entry point
# ======================================================================

class SandboxManager:
    """Entry point for creating and managing database sandboxes.

    Usage::

        manager = SandboxManager()
        await manager.start()

        sandbox = await manager.create("postgres")
        rows = await sandbox.exec_query("SELECT 1 AS x")
        await sandbox.destroy()

        await manager.stop()
    """

    def __init__(
        self,
        pool_warm: dict[Dialect, int] | None = None,
        *,
        publish_port: bool = True,
        query_timeout: int = DEFAULT_QUERY_TIMEOUT,
    ) -> None:
        self._publish_port = publish_port
        self._query_timeout = query_timeout
        self._pool = SandboxPool(
            warm=pool_warm,
            publish_port=publish_port,
            query_timeout=query_timeout,
        )
        self._started = False

    async def start(self) -> None:
        """Start the warm-container pool.

        Call once at application startup.
        """
        if self._started:
            return
        await self._pool.start()
        self._started = True
        logger.info("SandboxManager started")

    async def stop(self) -> None:
        """Stop the pool and destroy all warm containers.

        Call once at application shutdown.
        """
        if not self._started:
            return
        await self._pool.stop()
        self._started = False
        logger.info("SandboxManager stopped")

    async def create(
        self,
        dialect: Dialect,
        *,
        query_timeout: int = DEFAULT_QUERY_TIMEOUT,
    ) -> Sandbox:
        """Obtain a sandbox for *dialect*.

        Returns a warm sandbox from the pool if one is available,
        otherwise performs a cold start (creates the container from
        scratch).  The returned ``Sandbox`` is ready to execute SQL.
        """
        # Try warm path
        sandbox = await self._pool.acquire(dialect)
        if sandbox is not None:
            logger.info("Reusing warm %s sandbox", dialect)
            sandbox._query_timeout = query_timeout
            return sandbox

        # Cold path
        logger.info("Cold-starting %s sandbox …", dialect)
        sandbox = await _cold_start(
            dialect,
            publish_port=self._publish_port,
            query_timeout=query_timeout,
        )
        return sandbox


# ======================================================================
# Internal helpers
# ======================================================================

async def _cold_start(
    dialect: Dialect,
    *,
    publish_port: bool = True,
    query_timeout: int = DEFAULT_QUERY_TIMEOUT,
) -> Sandbox:
    """Create a sandbox from scratch (container + connection)."""
    container = SandboxContainer(dialect)
    executor = create_executor(dialect)

    try:
        # Step 1: Start the Docker container
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, container.start, publish_port)

        # Step 2: Wait for the database to become reachable (health check)
        retries = 90 if dialect == "oracle" else 30
        await _wait_for_db(executor, container.host, container.port, dialect, retries=retries)

        sandbox = Sandbox(dialect, container, executor, query_timeout=query_timeout)
        logger.info("Cold-started %s sandbox on %s:%s", dialect, container.host, container.port)
        return sandbox
    except Exception:
        await executor.close()
        container.stop()
        raise


async def _wait_for_db(
    executor: DatabaseExecutor,
    host: str,
    port: int,
    dialect: str,
    *,
    retries: int = 30,
    delay: float = 2.0,
) -> None:
    """Poll ``SELECT 1`` until the database accepts connections.

    PostgreSQL starts quickly (<5 s).  Oracle XE can take 30–60 s
    on first run because it initialises the data dictionary.
    """
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            await executor.connect(host, port)
            healthy = await executor.health()
            if healthy:
                logger.info("%s ready after %d attempt(s)", dialect, attempt)
                return
        except Exception as exc:
            last_error = exc
            logger.debug(
                "%s not ready yet (attempt %d/%d): %s",
                dialect,
                attempt,
                retries,
                exc,
            )
        # Close connection before next attempt (ignore errors)
        with suppress(Exception):
            await executor.close()
        await asyncio.sleep(delay)

    raise RuntimeError(
        f"{dialect} did not become healthy after {retries * delay}s. "
        f"Last error: {last_error}"
    )
