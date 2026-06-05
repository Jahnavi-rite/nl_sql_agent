from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.core.config import settings
from app.core.metrics import ACTIVE_WS_CONNECTIONS, RATE_LIMIT_BLOCKS

logger = structlog.get_logger()


class SlidingWindowCounter:
    def __init__(self, max_count: int, window_seconds: float = 60.0) -> None:
        self.max_count = max_count
        self.window = window_seconds
        self._slots: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window
        timestamps = self._slots[key]
        timestamps[:] = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= self.max_count:
            return False
        timestamps.append(now)
        return True


_request_counter = SlidingWindowCounter(max_count=settings.RATE_LIMIT_REQUESTS_PER_MINUTE)
_session_counter = SlidingWindowCounter(max_count=settings.RATE_LIMIT_SESSION_REQUESTS_PER_MINUTE)
_ws_connections: dict[str, int] = {}


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if settings.APP_ENV == "testing":
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            await self._handle_http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._handle_ws(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    async def _handle_http(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if path in ("/health", "/metrics", "/favicon.ico"):
            await self.app(scope, receive, send)
            return

        client_host = scope.get("client", ("unknown", 0))[0]

        if not _request_counter.allow(f"global:{client_host}"):
            RATE_LIMIT_BLOCKS.labels(limit_type="global").inc()
            logger.warning("rate_limit_blocked", client=client_host, limit="global")
            response = JSONResponse(
                status_code=429,
                content={
                    "error_code": "RATE_LIMITED",
                    "message": "Too many requests. Please slow down.",
                    "retry_after_seconds": 60,
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    async def _handle_ws(self, scope: Any, receive: Any, send: Any) -> None:
        session_id = scope.get("path", "").split("/")[2] if "/sessions/" in scope.get("path", "") else "unknown"
        current = _ws_connections.get(session_id, 0)
        if current >= settings.RATE_LIMIT_WS_PER_SESSION:
            logger.warning("ws_rate_limit_blocked", session_id=session_id)
            response = JSONResponse(
                status_code=429,
                content={
                    "error_code": "WS_LIMITED",
                    "message": "Too many WebSocket connections for this session.",
                },
            )
            await response(scope, receive, send)
            return

        _ws_connections[session_id] = current + 1
        ACTIVE_WS_CONNECTIONS.set(sum(_ws_connections.values()))

        try:
            await self.app(scope, receive, send)
        finally:
            _ws_connections[session_id] = max(0, _ws_connections.get(session_id, 1) - 1)
            if _ws_connections.get(session_id, 0) == 0:
                _ws_connections.pop(session_id, None)
            ACTIVE_WS_CONNECTIONS.set(sum(_ws_connections.values()))
