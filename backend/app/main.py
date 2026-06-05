"""
FastAPI Application — Entry Point.

This is the main module that creates and configures the FastAPI app.
It sets up:
- CORS (allows the frontend to call the backend)
- Structured logging (JSON logs for production)
- OpenTelemetry tracing (Jaeger export)
- Prometheus /metrics endpoint
- Rate limiting middleware
- Structured error handling
- Maintenance mode
- Route registration
- Lifespan events (startup/shutdown hooks for DB, Redis, janitor, etc.)
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse as _JSONResponse

from app.api.datasets import router as datasets_router
from app.api.feedback import router as feedback_router
from app.api.health import router as health_router
from app.api.requests import router as requests_router
from app.api.sandbox import router as sandbox_router
from app.api.schema import router as schema_router
from app.api.stream import router as stream_router
from app.api.validate import router as validate_router
from app.core.config import settings
from app.core.database import engine
from app.core.errors import StructuredErrorMiddleware
from app.core.logging import setup_logging
from app.core.maintenance import MaintenanceMiddleware
from app.core.metrics import MetricsMiddleware, metrics_response
from app.core.rate_limiter import RateLimitMiddleware
from app.core.redis import redis_client
from app.core.telemetry import TracingMiddleware, setup_telemetry
from app.services.janitor import SandboxJanitor
from app.services.stream_manager import stream_manager

setup_logging(settings.LOG_LEVEL)
logger = structlog.get_logger()

janitor = SandboxJanitor()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "app_starting",
        version=settings.APP_VERSION,
        env=settings.APP_ENV,
        port=settings.BACKEND_PORT,
        maintenance=settings.MAINTENANCE_MODE,
    )

    # Verify database connectivity
    try:
        import sqlalchemy

        async with engine.connect() as conn:
            await conn.execute(sqlalchemy.text("SELECT 1"))
        logger.info("database_connected", url=settings.DATABASE_URL.split("@")[-1])
    except Exception as exc:
        logger.warning("database_unavailable", error=str(exc))

    # Verify Redis connectivity
    try:
        await redis_client.ping()
        logger.info("redis_connected", url=settings.REDIS_URL)
    except Exception as exc:
        logger.warning("redis_unavailable", error=str(exc))

    # Auto-ingest CSV files into PostgreSQL and cache schema
    try:
        from app.services.startup_ingestion import find_csv_files, run_startup_ingestion

        found = find_csv_files()
        if found:
            logger.info("startup_ingestion_starting", csv_files=found)
        else:
            logger.info("startup_no_csvs_to_ingest")

        ingestion_result = await run_startup_ingestion()
        csv_count = len(ingestion_result["csv_files"])
        table_count = ingestion_result["schema_tables"]
        logger.info(
            "startup_ingestion_complete",
            csv_files=csv_count,
            tables_created=table_count,
            ingested=[
                {
                    "table": i["table_name"],
                    "rows": i["row_count"],
                    "file": i.get("filename", ""),
                }
                for i in ingestion_result["ingested"]
            ],
        )
    except Exception as exc:
        logger.warning("startup_ingestion_error", error=str(exc))

    # Start background services
    await stream_manager.start_cleanup_task()
    logger.info("stream_manager_cleanup_started")

    await janitor.start()
    logger.info("sandbox_janitor_started")

    logger.info(
        "app_ready",
        path="/health",
        port=settings.BACKEND_PORT,
        metrics="/metrics",
    )
    yield

    # --- Graceful Shutdown ---
    logger.info("app_shutting_down", shutdown_timeout=settings.SHUTDOWN_TIMEOUT_SECONDS)
    shutdown_start = asyncio.get_event_loop().time()

    await stream_manager.stop_cleanup_task()
    await janitor.stop()

    await engine.dispose()
    await redis_client.aclose()

    elapsed = (asyncio.get_event_loop().time() - shutdown_start) * 1000
    logger.info("app_shutdown_complete", duration_ms=round(elapsed, 1))


app = FastAPI(
    title="NL SQL Agent",
    version=settings.APP_VERSION,
    description="AI-powered SQL agent API — Phase 8: Robustness & Observability",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Middleware — Order matters: outermost first, innermost last
# ---------------------------------------------------------------------------
# 1. CORS (outermost)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_origin_regex=settings.CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Maintenance mode (checks before any processing)
app.add_middleware(MaintenanceMiddleware)

# 3. Structured error handler (catches all downstream exceptions)
app.add_middleware(StructuredErrorMiddleware)

# 4. Rate limiting (per-user and per-session)
app.add_middleware(RateLimitMiddleware)

# 5. Metrics collection (latency, counters)
app.add_middleware(MetricsMiddleware)

# 6. OpenTelemetry tracing (innermost — captures all spans)
app.add_middleware(TracingMiddleware)

# ---------------------------------------------------------------------------
# OpenTelemetry instrumentation
# ---------------------------------------------------------------------------
setup_telemetry(app)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
app.include_router(datasets_router)
app.include_router(feedback_router)
app.include_router(health_router)
app.include_router(sandbox_router)
app.include_router(schema_router)
app.include_router(stream_router)
app.include_router(validate_router)
app.include_router(requests_router)

# ---------------------------------------------------------------------------
# Prometheus /metrics — must be after middleware but registered directly
# ---------------------------------------------------------------------------
@app.get("/metrics", include_in_schema=False)
async def metrics() -> Any:
    return metrics_response()

# ---------------------------------------------------------------------------
# Structured 404/405 error handlers
# ---------------------------------------------------------------------------

@app.exception_handler(404)
async def not_found_handler(request: Any, exc: Any) -> Any:
    return _JSONResponse(
        status_code=404,
        content={
            "error_code": "ERR_NOT_FOUND",
            "message": f"The requested resource was not found: {request.url.path}",
            "retry_hint": "Check the URL and try again.",
        },
    )

@app.exception_handler(405)
async def method_not_allowed_handler(request: Any, exc: Any) -> Any:
    return _JSONResponse(
        status_code=405,
        content={
            "error_code": "ERR_METHOD_NOT_ALLOWED",
            "message": f"Method {request.method} not allowed for {request.url.path}",
            "retry_hint": "Check the HTTP method and try again.",
        },
    )
