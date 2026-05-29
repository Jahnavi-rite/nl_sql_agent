"""
Health check endpoint.

This is the simplest endpoint in the app. It's used by:
- Docker health checks to know if the service is running
- Load balancers to route traffic
- Monitoring systems to track uptime
- The frontend to display backend status
"""

from fastapi import APIRouter

from app.core.config import settings

# Create a router — groups related endpoints together
router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """
    GET /health — Returns service status.

    Response:
        {"status": "ok", "version": "0.1.0"}
    """
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
    }
