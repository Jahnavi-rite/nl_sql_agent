from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any

import docker
import structlog

from app.core.config import settings
from app.core.metrics import JANITOR_CLEANUPS

logger = structlog.get_logger()

_SANDBOX_LABEL_PREFIX = "nlsql-sandbox"
_SANDBOX_SESSION_LABEL = "session_id"
_SANDBOX_DIALECT_LABEL = "dialect"
_SANDBOX_CREATED_LABEL = "created_at"


class SandboxJanitor:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._docker_client: docker.DockerClient | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._docker_client = docker.from_env()
            self._docker_client.ping()
        except Exception as exc:
            logger.warning("janitor_docker_unavailable", error=str(exc))
            return

        self._task = asyncio.create_task(self._run_loop())
        logger.info("janitor_started", interval_seconds=settings.JANITOR_INTERVAL_SECONDS)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._docker_client = None
        logger.info("janitor_stopped")

    async def _run_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(settings.JANITOR_INTERVAL_SECONDS)
                await self._cleanup_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("janitor_loop_error", error=str(exc))

    async def _cleanup_once(self) -> None:
        if self._docker_client is None:
            return

        loop = asyncio.get_event_loop()
        try:
            containers = await loop.run_in_executor(
                None, self._find_sandbox_containers
            )
        except Exception as exc:
            logger.warning("janitor_find_containers_failed", error=str(exc))
            return

        for container in containers:
            try:
                labels = container.labels
                created_at_str = labels.get(_SANDBOX_CREATED_LABEL, "0")
                try:
                    created_at = float(created_at_str)
                except (ValueError, TypeError):
                    created_at = 0

                age_minutes = (time.time() - created_at) / 60
                session_id = labels.get(_SANDBOX_SESSION_LABEL, "unknown")
                dialect = labels.get(_SANDBOX_DIALECT_LABEL, "unknown")

                if age_minutes > settings.SANDBOX_MAX_IDLE_MINUTES:
                    await loop.run_in_executor(None, self._destroy_container, container)
                    JANITOR_CLEANUPS.labels(action="idle_expired").inc()
                    logger.info(
                        "janitor_destroyed_idle",
                        container_id=container.short_id,
                        session_id=session_id,
                        dialect=dialect,
                        age_minutes=round(age_minutes, 1),
                    )
            except Exception as exc:
                logger.warning("janitor_container_error", container_id=container.short_id, error=str(exc))

    def _find_sandbox_containers(self) -> list[Any]:
        if self._docker_client is None:
            return []
        from typing import cast
        return cast(list[Any], self._docker_client.containers.list(
            all=False,
            filters={"label": _SANDBOX_LABEL_PREFIX},
        ))

    def _destroy_container(self, container: Any) -> None:
        with suppress(Exception):
            container.stop(timeout=5)
            container.remove(force=True, v=True)
        with suppress(Exception):
            container.remove(force=True, v=True)
