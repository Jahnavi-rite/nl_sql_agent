"""
FastAPI Application — Entry Point.

This is the main module that creates and configures the FastAPI app.
It sets up:
- CORS (allows the frontend to call the backend)
- Structured logging (JSON logs for production)
- Route registration
- Lifespan events (startup/shutdown hooks for DB, Redis, etc.)
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# Windows + asyncpg fix: use SelectorEventLoop instead of ProactorEventLoop
# to avoid "unexpected connection_lost() call" errors.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from app.core.logging import setup_logging
from app.core.redis import redis_client
from app.services.stream_manager import stream_manager

# Initialize structured logging
setup_logging(settings.LOG_LEVEL)
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Lifespan context manager — runs on startup and shutdown.

    Use this to initialize resources (DB connections, Redis, etc.)
    and clean them up when the app stops.

    Startup code runs before `yield`, shutdown code runs after.
    """
    # --- Startup ---
    logger.info(
        "app_starting",
        version=settings.APP_VERSION,
        env=settings.APP_ENV,
        port=settings.BACKEND_PORT,
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

    # Start WebSocket stream manager cleanup task
    await stream_manager.start_cleanup_task()
    logger.info("stream_manager_cleanup_started")

    logger.info("app_ready", path="/health", port=settings.BACKEND_PORT)
    yield  # App is running

    # --- Shutdown ---
    await stream_manager.stop_cleanup_task()
    await engine.dispose()
    await redis_client.aclose()
    logger.info("app_shutting_down")


# Create the FastAPI application
app = FastAPI(
    title="NL SQL Agent",
    version=settings.APP_VERSION,
    description="AI-powered SQL agent API",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — Allow the frontend to make requests to this backend
# ---------------------------------------------------------------------------
# Without CORS, browsers block requests from localhost:3000 → localhost:8000
# because they're considered different "origins" (different ports = different origins).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,  # Which frontend URLs can call us
    allow_origin_regex=settings.CORS_ORIGIN_REGEX,
    allow_credentials=True,               # Allow cookies/auth headers
    allow_methods=["*"],                   # Allow all HTTP methods (GET, POST, etc.)
    allow_headers=["*"],                   # Allow all request headers
)

# ---------------------------------------------------------------------------
# Routes — Register all API routers
# ---------------------------------------------------------------------------
app.include_router(datasets_router)
app.include_router(feedback_router)
app.include_router(health_router)
app.include_router(sandbox_router)
app.include_router(schema_router)
app.include_router(stream_router)
app.include_router(validate_router)
app.include_router(requests_router)
