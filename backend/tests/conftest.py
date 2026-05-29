"""Shared fixtures for persistence-layer tests.

Uses in-memory SQLite (fast, no Docker required).
Foreign keys are enabled via PRAGMA for cascade-delete testing.
Redis is mocked with in-memory dict stubs.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base

# ---------------------------------------------------------------------------
# In-memory SQLite async engine
# ---------------------------------------------------------------------------
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DB_URL, echo=False)
async_test_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# Enable foreign keys for SQLite (required for ON DELETE CASCADE)
@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture(autouse=True)
async def _setup_db():
    """Create all tables before each test, drop after."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db():
    """Yield a fresh async session for each test."""
    async with async_test_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Redis mock (avoids needing a live Redis for unit tests)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def mock_redis():
    """Patch all redis helpers with in-memory stubs."""
    store: dict[str, str] = {}

    async def fake_get(key: str):
        return store.get(key)

    async def fake_set(key: str, value: str, ex: int | None = None):
        store[key] = value

    async def fake_delete(key: str):
        store.pop(key, None)

    # All side_effects are sync (not async) because unittest.mock
    # does not await side_effects — it passes the return value directly.
    with (
        patch("app.core.redis.redis_client") as mock_client,
        patch("app.services.session_service.get_session_context", side_effect=lambda sid: _load_ctx(store, sid)),
        patch("app.services.session_service.append_session_context", side_effect=lambda sid, entry, max_entries=20: _append_ctx_sync(store, sid, entry, max_entries)),
        patch("app.services.session_service.clear_session_context", side_effect=lambda sid: _clear_ctx(store, sid)),
        patch("app.services.session_service.set_sandbox_handle", side_effect=lambda sid, handle, ttl=3600: _set_sbx(store, sid, handle)),
        patch("app.services.session_service.get_sandbox_handle", side_effect=lambda sid: _get_sbx(store, sid)),
        patch("app.services.session_service.clear_sandbox_handle", side_effect=lambda sid: _clear_sbx(store, sid)),
    ):
        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_client.set = AsyncMock(side_effect=fake_set)
        mock_client.delete = AsyncMock(side_effect=fake_delete)
        yield store


# --- helpers for in-memory Redis simulation ---


def _ctx_key(sid: str) -> str:
    return f"session:{sid}:context"


def _sbx_key(sid: str) -> str:
    return f"session:{sid}:sandbox"


def _load_ctx(store: dict, sid: str) -> list:
    raw = store.get(_ctx_key(sid))
    return json.loads(raw) if raw else []


def _append_ctx_sync(store: dict, sid: str, entry: dict, max_entries: int) -> list:
    """Synchronous version — called via side_effect (not awaited)."""
    ctx = _load_ctx(store, sid)
    ctx.append(entry)
    ctx = ctx[-max_entries:]
    store[_ctx_key(sid)] = json.dumps(ctx)
    return ctx


def _clear_ctx(store: dict, sid: str) -> None:
    store.pop(_ctx_key(sid), None)


def _set_sbx(store: dict, sid: str, handle: dict) -> None:
    store[_sbx_key(sid)] = json.dumps(handle)


def _get_sbx(store: dict, sid: str) -> dict | None:
    raw = store.get(_sbx_key(sid))
    return json.loads(raw) if raw else None


def _clear_sbx(store: dict, sid: str) -> None:
    store.pop(_sbx_key(sid), None)
