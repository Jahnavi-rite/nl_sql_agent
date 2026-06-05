from __future__ import annotations

import httpx
import sqlalchemy
import structlog
from fastapi import APIRouter

from app.core.config import settings
from app.core.database import engine
from app.core.redis import redis_client

logger = structlog.get_logger()
router = APIRouter(tags=["health"])


HEALTH_CACHE: dict[str, dict] = {}
_HEALTH_CACHE_TTL = 10.0
_last_health_check = 0.0


@router.get("/health")
async def health_check():
    global _last_health_check
    import time
    now = time.time()

    if now - _last_health_check < _HEALTH_CACHE_TTL and HEALTH_CACHE:
        return HEALTH_CACHE

    checks = {}

    checks["status"] = "ok"
    checks["version"] = settings.APP_VERSION
    checks["maintenance"] = settings.MAINTENANCE_MODE

    checks["postgres"] = await _check_postgres()
    checks["redis"] = await _check_redis()
    checks["docker"] = await _check_docker()
    checks["llm"] = await _check_llm()

    if any(v.get("status") == "error" for v in checks.values() if isinstance(v, dict)):
        checks["status"] = "degraded"

    if settings.MAINTENANCE_MODE:
        checks["status"] = "maintenance"

    HEALTH_CACHE.clear()
    HEALTH_CACHE.update(checks)
    _last_health_check = now

    return checks


async def _check_postgres() -> dict:
    try:
        async with engine.connect() as conn:
            result = await conn.execute(sqlalchemy.text("SELECT 1"))
            result.scalar()
        return {"status": "ok"}
    except Exception as exc:
        logger.warning("health_postgres_failed", error=str(exc))
        return {"status": "error", "detail": str(exc)}


async def _check_redis() -> dict:
    try:
        await redis_client.ping()
        return {"status": "ok"}
    except Exception as exc:
        logger.warning("health_redis_failed", error=str(exc))
        return {"status": "error", "detail": str(exc)}


async def _check_docker() -> dict:
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


async def _check_llm() -> dict:
    try:
        base_url = settings.OPENAI_API_BASE.rstrip("/")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            )
            if resp.status_code == 200:
                return {"status": "ok", "models_available": True}
            return {"status": "degraded", "detail": f"HTTP {resp.status_code}"}
    except httpx.TimeoutException:
        return {"status": "degraded", "detail": "timeout"}
    except Exception as exc:
        return {"status": "degraded", "detail": str(exc)}
