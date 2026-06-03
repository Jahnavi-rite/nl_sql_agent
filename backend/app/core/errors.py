from __future__ import annotations

import traceback as tb
import uuid

import structlog
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

logger = structlog.get_logger()

ERROR_CODES = {
    "VALIDATION_ERROR": "ERR_VALIDATION",
    "NOT_FOUND": "ERR_NOT_FOUND",
    "RATE_LIMITED": "ERR_RATE_LIMITED",
    "MAINTENANCE": "ERR_MAINTENANCE",
    "LLM_ERROR": "ERR_LLM",
    "DB_ERROR": "ERR_DATABASE",
    "PIPELINE_ERROR": "ERR_PIPELINE",
    "WS_ERROR": "ERR_WEBSOCKET",
    "INTERNAL": "ERR_INTERNAL",
    "SANDBOX_ERROR": "ERR_SANDBOX",
}

RETRY_HINTS = {
    "ERR_VALIDATION": "Check your input and try again.",
    "ERR_RATE_LIMITED": "Wait a moment and try again.",
    "ERR_LLM": "The AI model is temporarily unavailable. Please retry.",
    "ERR_DATABASE": "A database error occurred. Please retry.",
    "ERR_INTERNAL": "An unexpected error occurred. Please retry.",
    "ERR_MAINTENANCE": "The system is under maintenance. Try again later.",
    "ERR_SANDBOX": "The execution sandbox failed. Please retry.",
}


class StructuredErrorMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = str(uuid.uuid4())[:8]

        async def _send_wrapper(msg):
            if msg.get("type") == "http.response.start":
                headers = dict(msg.get("headers", []))
                headers.setdefault(b"x-trace-id", trace_id.encode())
                msg["headers"] = list(headers.items()) if hasattr(headers, "items") else [(b"x-trace-id", trace_id.encode())]
            await send(msg)

        try:
            await self.app(scope, receive, _send_wrapper)
        except Exception as exc:
            error_code = _classify_error(exc)
            logger.error(
                "unhandled_error",
                trace_id=trace_id,
                error_code=error_code,
                error=str(exc),
                path=scope.get("path", ""),
            )
            response = JSONResponse(
                status_code=_http_status(error_code),
                content=_build_error(exc, error_code, trace_id),
                headers={"x-trace-id": trace_id},
            )
            await response(scope, receive, send)


def _classify_error(exc: Exception) -> str:
    name = type(exc).__name__
    if "NotFound" in name or "not found" in str(exc).lower():
        return "NOT_FOUND"
    if "Validation" in name or "pydantic" in str(type(exc)).lower():
        return "VALIDATION_ERROR"
    if "LLM" in name or "AgentError" in name:
        return "LLM_ERROR"
    if "DB" in name or "Database" in name or "asyncpg" in str(type(exc)).lower():
        return "DB_ERROR"
    if "Sandbox" in name or "Container" in name:
        return "SANDBOX_ERROR"
    if "Pipeline" in name:
        return "PIPELINE_ERROR"
    if "WebSocket" in name or "WS" in name:
        return "WS_ERROR"
    return "INTERNAL"


def _http_status(error_code: str) -> int:
    return {
        "NOT_FOUND": 404,
        "VALIDATION_ERROR": 422,
        "RATE_LIMITED": 429,
        "MAINTENANCE": 503,
        "LLM_ERROR": 502,
        "DB_ERROR": 500,
        "SANDBOX_ERROR": 502,
        "PIPELINE_ERROR": 500,
        "WS_ERROR": 500,
        "INTERNAL": 500,
    }.get(error_code, 500)


def _build_error(exc: Exception, error_code: str, trace_id: str) -> dict:
    return {
        "error_code": ERROR_CODES.get(error_code, "ERR_INTERNAL"),
        "message": _safe_message(exc, error_code),
        "detail": str(exc) if error_code != "INTERNAL" else None,
        "trace_id": trace_id,
        "retry_hint": RETRY_HINTS.get(error_code, "Please try again."),
    }


def _safe_message(exc: Exception, error_code: str) -> str:
    if error_code == "INTERNAL":
        return "An unexpected error occurred. Our team has been notified."
    return str(exc).split("\n")[0] if str(exc) else "An error occurred."
