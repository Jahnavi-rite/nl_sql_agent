"""Integration tests for real database sandbox containers.

These tests intentionally talk to Docker and real PostgreSQL / Oracle XE
instances. Run them with:

    pytest -m integration
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from contextlib import suppress
from typing import Any

import asyncpg
import docker
import pytest
from docker.errors import NotFound

from app.sandbox.manager import Sandbox, SandboxManager

pytestmark = pytest.mark.integration

SANDBOX_LABEL = {"label": "nlsql-sandbox=true"}


@pytest.fixture(autouse=True)
def clean_sandbox_docker_resources() -> Generator[None]:
    """Keep tests independent and assert we do not leave Docker resources behind."""
    _cleanup_sandbox_resources()
    yield
    _cleanup_sandbox_resources()
    assert _sandbox_container_names() == []


def _docker() -> docker.DockerClient:
    return docker.from_env()


def _sandbox_container_names() -> list[str]:
    client = _docker()
    return [container.name for container in client.containers.list(all=True, filters=SANDBOX_LABEL)]


def _cleanup_sandbox_resources() -> None:
    """Remove test-owned containers, networks, and volumes by label.

    Docker labels are important here: they give us a precise cleanup handle
    without touching any unrelated developer containers.
    """
    client = _docker()

    for container in client.containers.list(all=True, filters=SANDBOX_LABEL):
        container.remove(force=True, v=True)

    for network in client.networks.list(filters=SANDBOX_LABEL):
        with suppress(Exception):
            network.remove()

    for volume in client.volumes.list(filters=SANDBOX_LABEL):
        with suppress(Exception):
            volume.remove(force=True)


def _inspect(container_id: str) -> dict[str, Any]:
    return _docker().api.inspect_container(container_id)


def _resource_limits_are_visible(
    container_id: str,
    *,
    memory_bytes: int,
    read_only: bool,
) -> None:
    attrs = _inspect(container_id)
    host_config = attrs["HostConfig"]

    assert host_config["CpuQuota"] == 100000
    assert host_config["CpuPeriod"] == 100000
    assert host_config["Memory"] == memory_bytes
    assert host_config["ReadonlyRootfs"] is read_only
    assert "no-new-privileges:true" in host_config["SecurityOpt"]


async def _wait_for_warm(manager: SandboxManager, dialect: str, minimum: int = 1) -> None:
    """Wait until the background warm pool has at least one ready sandbox."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        queue = manager._pool._queues.get(dialect)  # integration test introspection
        if queue is not None and queue.qsize() >= minimum:
            return
        await asyncio.sleep(1)
    raise AssertionError(f"Timed out waiting for warm {dialect} sandbox")


async def _destroy_all(*sandboxes: Sandbox) -> None:
    for sandbox in sandboxes:
        await sandbox.destroy()


@pytest.mark.asyncio
async def test_postgres_cold_start_executes_sql_explain_and_cleans_up() -> None:
    manager = SandboxManager(pool_warm={"postgres": 0, "oracle": 0}, publish_port=True)
    sandbox = await manager.create("postgres")
    container_id = sandbox._container.container_id

    assert container_id is not None
    assert await sandbox.health() is True
    _resource_limits_are_visible(
        container_id,
        memory_bytes=512 * 1024 * 1024,
        read_only=True,
    )

    await sandbox.exec_ddl("CREATE TABLE items (id INT PRIMARY KEY, name TEXT NOT NULL)")
    await sandbox.exec_ddl("INSERT INTO items (id, name) VALUES (1, 'alpha')")

    rows = await sandbox.exec_query("SELECT id, name FROM items ORDER BY id")
    assert rows == [{"id": 1, "name": "alpha"}]

    plan = await sandbox.explain("SELECT * FROM items")
    assert plan

    await sandbox.destroy()
    with pytest.raises(NotFound):
        _docker().containers.get(container_id)


@pytest.mark.asyncio
async def test_postgres_warm_pool_reuses_ready_container() -> None:
    manager = SandboxManager(pool_warm={"postgres": 2, "oracle": 0}, publish_port=True)
    await manager.start()
    try:
        await _wait_for_warm(manager, "postgres", minimum=1)
        queue = manager._pool._queues["postgres"]
        warm_ids = {sandbox._container.container_id for sandbox in list(queue._queue)}

        sandbox = await manager.create("postgres")
        assert sandbox._container.container_id in warm_ids
        assert await sandbox.exec_query("SELECT 1 AS one") == [{"one": 1}]
        await sandbox.destroy()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_postgres_timeout_terminates_pg_sleep_at_default_30s() -> None:
    manager = SandboxManager(pool_warm={"postgres": 0, "oracle": 0}, publish_port=True)
    sandbox = await manager.create("postgres")

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await sandbox.exec_query("SELECT pg_sleep(60)")
    elapsed = time.monotonic() - started

    assert 25 <= elapsed < 45
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_postgres_containers_are_isolated() -> None:
    manager = SandboxManager(pool_warm={"postgres": 0, "oracle": 0}, publish_port=True)
    first = await manager.create("postgres")
    second = await manager.create("postgres")

    try:
        await first.exec_ddl("CREATE TABLE isolated_value (id INT)")
        await first.exec_ddl("INSERT INTO isolated_value VALUES (7)")

        assert await first.exec_query("SELECT id FROM isolated_value") == [{"id": 7}]
        with pytest.raises(asyncpg.UndefinedTableError):
            await second.exec_query("SELECT id FROM isolated_value")
    finally:
        await _destroy_all(first, second)


@pytest.mark.asyncio
async def test_oracle_cold_start_executes_sql_and_cleans_up() -> None:
    manager = SandboxManager(pool_warm={"postgres": 0, "oracle": 0}, publish_port=True)
    sandbox = await manager.create("oracle")
    container_id = sandbox._container.container_id

    assert container_id is not None
    assert await sandbox.health() is True
    _resource_limits_are_visible(
        container_id,
        memory_bytes=2 * 1024 * 1024 * 1024,
        read_only=False,
    )

    await sandbox.exec_ddl("CREATE TABLE items (id NUMBER PRIMARY KEY, name VARCHAR2(20))")
    await sandbox.exec_ddl("INSERT INTO items (id, name) VALUES (1, 'alpha')")

    rows = await sandbox.exec_query("SELECT id, name FROM items ORDER BY id")
    assert rows == [{"ID": 1, "NAME": "alpha"}]

    plan = await sandbox.explain("SELECT * FROM items")
    assert plan

    await sandbox.destroy()
    with pytest.raises(NotFound):
        _docker().containers.get(container_id)
