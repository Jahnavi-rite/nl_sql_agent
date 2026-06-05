from __future__ import annotations

from typing import Any

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.core.config import settings

logger = structlog.get_logger()


class MaintenanceMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not settings.MAINTENANCE_MODE:
            await self.app(scope, receive, send)
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in ("/health", "/metrics"):
            await self.app(scope, receive, send)
            return

        logger.info("maintenance_mode_blocked", path=path)
        response = JSONResponse(
            status_code=503,
            content={
                "error_code": "ERR_MAINTENANCE",
                "message": "System is under maintenance. Please try again later.",
                "retry_hint": "The system will be available shortly.",
            },
            headers={"Retry-After": "120"},
        )
        await response(scope, receive, send)
