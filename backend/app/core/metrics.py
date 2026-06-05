from __future__ import annotations

import time
from typing import Any

import structlog
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import settings

logger = structlog.get_logger()

# --- Counters ---
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)

REQUEST_ERRORS = Counter(
    "http_errors_total",
    "Total HTTP errors by type",
    ["error_type"],
)

AGENT_PHASE_COUNT = Counter(
    "agent_phases_total",
    "Agent phase invocations",
    ["agent", "phase"],
)

WS_CONNECTIONS_TOTAL = Counter(
    "ws_connections_total",
    "Total WebSocket connections",
)

WS_EVENTS_EMITTED = Counter(
    "ws_events_emitted_total",
    "Total WebSocket events emitted",
)

SANDBOX_CREATED = Counter(
    "sandbox_created_total",
    "Total sandboxes created",
    ["dialect", "method"],
)

SANDBOX_DESTROYED = Counter(
    "sandbox_destroyed_total",
    "Total sandboxes destroyed",
    ["dialect"],
)

ITERATION_COUNT = Counter(
    "iterations_total",
    "Total iterations created",
    ["status"],
)

LLM_CALL_COUNT = Counter(
    "llm_calls_total",
    "Total LLM calls",
    ["model", "status"],
)

RATE_LIMIT_BLOCKS = Counter(
    "rate_limit_blocks_total",
    "Requests blocked by rate limiter",
    ["limit_type"],
)

JANITOR_CLEANUPS = Counter(
    "janitor_cleanups_total",
    "Janitor cleanup operations",
    ["action"],
)

# --- Gauges ---
ACTIVE_SESSIONS = Gauge("active_sessions", "Currently active sessions")
ACTIVE_WS_CONNECTIONS = Gauge("active_ws_connections", "Currently active WebSocket connections")
ACTIVE_SANDBOXES = Gauge("active_sandboxes", "Currently active sandbox containers")
ACTIVE_REQUESTS = Gauge("active_requests", "Currently in-flight NL requests")
POOL_WARM_SANDBOXES = Gauge("pool_warm_sandboxes", "Warm sandboxes in pool", ["dialect"])

# --- Histograms ---
REQUEST_LATENCY = Histogram(
    "http_request_duration_ms",
    "HTTP request latency in ms",
    ["method", "endpoint"],
    buckets=(50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000),
)

AGENT_PHASE_LATENCY = Histogram(
    "agent_phase_duration_ms",
    "Agent phase latency in ms",
    ["agent", "phase"],
    buckets=(10, 50, 100, 200, 500, 1000, 2000, 5000, 10000),
)

LLM_LATENCY = Histogram(
    "llm_call_duration_ms",
    "LLM call latency in ms",
    ["model"],
    buckets=(100, 500, 1000, 2000, 5000, 10000, 20000, 30000, 60000),
)

DB_QUERY_LATENCY = Histogram(
    "db_query_duration_ms",
    "Database query latency in ms",
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 5000),
)

STREAM_DURATION = Histogram(
    "stream_duration_ms",
    "WebSocket stream duration in ms",
    buckets=(1000, 5000, 10000, 30000, 60000, 120000, 300000),
)


def inc_request(method: str, endpoint: str, status: int) -> None:
    if settings.METRICS_ENABLED:
        REQUEST_COUNT.labels(method=method, endpoint=_norm(endpoint), status=str(status)).inc()


def observe_request_latency(method: str, endpoint: str, ms: float) -> None:
    if settings.METRICS_ENABLED:
        REQUEST_LATENCY.labels(method=method, endpoint=_norm(endpoint)).observe(ms)


def _norm(endpoint: str) -> str:
    parts = endpoint.strip("/").split("/")
    normalized = []
    for p in parts:
        if p and p != "":
            try:
                int(p)
                normalized.append("{id}")
            except ValueError:
                normalized.append(p)
    return "/" + "/".join(normalized)


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/unknown")
        method = scope.get("method", "GET")

        start = time.perf_counter()
        status = [200]

        async def _send_wrapper(msg: Any) -> None:
            if msg.get("type") == "http.response.start":
                status[0] = msg.get("status", 200)
            await send(msg)

        try:
            await self.app(scope, receive, _send_wrapper)
        finally:
            ms = (time.perf_counter() - start) * 1000
            inc_request(method, path, status[0])
            observe_request_latency(method, path, ms)


def metrics_response() -> Response:
    from prometheus_client import CONTENT_TYPE_LATEST
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
