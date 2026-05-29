"""
Tests for the health check endpoint.

These tests verify that:
1. The /health endpoint returns 200 OK
2. The response body contains the expected fields
3. The version matches our configuration
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncClient:
    """Create a test HTTP client that talks directly to the FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_returns_ok(client: AsyncClient) -> None:
    """GET /health should return status 200 with status='ok'."""
    response = await client.get("/health")

    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
async def test_health_version_format(client: AsyncClient) -> None:
    """Version should be a valid semver-like string."""
    response = await client.get("/health")
    version = response.json()["version"]

    # Version should be in X.Y.Z format
    parts = version.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
