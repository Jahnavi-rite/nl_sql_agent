"""Async Redis client with session-scoped helpers."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings

redis_client = aioredis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
)

# ---------------------------------------------------------------------------
# Key templates
# ---------------------------------------------------------------------------
SESSION_CONTEXT_KEY = "session:{session_id}:context"
SESSION_SANDBOX_KEY = "session:{session_id}:sandbox"

CONTEXT_TTL_SECONDS = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx_key(session_id: str) -> str:
    return SESSION_CONTEXT_KEY.format(session_id=session_id)


def _sbx_key(session_id: str) -> str:
    return SESSION_SANDBOX_KEY.format(session_id=session_id)


async def get_session_context(session_id: str) -> list[dict[str, Any]]:
    """Return the rolling iteration memory for *session_id*."""
    raw = await redis_client.get(_ctx_key(session_id))
    if raw is None:
        return []
    return json.loads(raw)


async def append_session_context(
    session_id: str,
    entry: dict[str, Any],
    max_entries: int = 20,
) -> list[dict[str, Any]]:
    """Append *entry* to the rolling context window and refresh TTL.

    Keeps at most *max_entries* most-recent items.
    """
    ctx = await get_session_context(session_id)
    ctx.append(entry)
    ctx = ctx[-max_entries:]
    await redis_client.set(
        _ctx_key(session_id),
        json.dumps(ctx),
        ex=CONTEXT_TTL_SECONDS,
    )
    return ctx


async def clear_session_context(session_id: str) -> None:
    """Delete the context key for *session_id*."""
    await redis_client.delete(_ctx_key(session_id))


async def set_sandbox_handle(
    session_id: str,
    handle: dict[str, Any],
    ttl: int = 3600,
) -> None:
    """Store the active sandbox handle for *session_id*."""
    await redis_client.set(
        _sbx_key(session_id),
        json.dumps(handle),
        ex=ttl,
    )


async def get_sandbox_handle(session_id: str) -> dict[str, Any] | None:
    """Return the active sandbox handle, or ``None`` if expired/absent."""
    raw = await redis_client.get(_sbx_key(session_id))
    if raw is None:
        return None
    return json.loads(raw)


async def clear_sandbox_handle(session_id: str) -> None:
    """Remove the sandbox handle for *session_id*."""
    await redis_client.delete(_sbx_key(session_id))
