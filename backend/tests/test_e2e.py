"""End-to-end tests for the NL-to-SQL pipeline.

These tests require:
- A running Docker environment (for sandbox containers)
- An LLM API endpoint configured via OPENAI_* env vars
- A running PostgreSQL and/or Redis (or use mocked DB)

Mark with ``@pytest.mark.e2e`` — run via ``pytest -m e2e``.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.main import app

# ---------------------------------------------------------------------------
# Test prompts covering diverse analytical patterns
# ---------------------------------------------------------------------------

E2E_PROMPTS: list[tuple[str, str]] = [
    ("postgres", "List all tables with their column names and types"),
    ("postgres", "How many total columns are there across all tables?"),
    ("postgres", "Which table has the most columns?"),
    ("oracle", "List all tables with their column names and types"),
    ("oracle", "How many total columns are there across all tables?"),
    ("oracle", "Which table has the most columns?"),
]

# Sample CSV data for testing — small table metadata dataset
SAMPLE_CSV = b"object_name,column_name,data_type\nusers,id,INTEGER\nusers,name,VARCHAR\nusers,email,VARCHAR\norders,id,INTEGER\norders,user_id,INTEGER\norders,total,DECIMAL\norders,status,VARCHAR\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def async_client(db: AsyncSession) -> AsyncClient:
    """Override the DB dependency to use the test in-memory SQLite DB."""

    async def override_get_db() -> AsyncClient:
        yield db

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _assert_valid_response(data: dict, dialect: str, prompt: str) -> None:
    """Assert that the pipeline response contains all expected fields."""
    assert data["query_sql"], f"{dialect}: query_sql is empty for prompt: {prompt[:50]}"
    assert data["confidence"] is not None, f"{dialect}: confidence is None"
    assert 0 <= data["confidence"] <= 1, f"{dialect}: confidence out of range: {data['confidence']}"
    assert data["rationale"], f"{dialect}: rationale is empty"
    assert data["status"] == "completed", f"{dialect}: unexpected status: {data['status']}, error: {data.get('error_message')}"
    assert isinstance(data["execution_results"], list), f"{dialect}: execution_results not a list"
    assert data["execution_ms"] is not None, f"{dialect}: execution_ms is None"


# ---------------------------------------------------------------------------
# E2E Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("dialect,prompt", E2E_PROMPTS)
async def test_nl_pipeline_e2e(
    async_client: AsyncClient,
    dialect: str,
    prompt: str,
) -> None:
    """Full end-to-end test: create session, upload dataset, submit NL prompt, verify response."""
    # 1. Create session
    sess_resp = await async_client.post(
        "/sessions",
        json={"dialect": dialect},
    )
    assert sess_resp.status_code == 201, f"Session creation failed: {sess_resp.text}"
    session_id = sess_resp.json()["session_id"]
    assert session_id, "No session_id in response"

    # 2. Upload dataset
    dataset_resp = await async_client.post(
        f"/sessions/{session_id}/datasets?dialect={dialect}",
        files={"file": ("test_tables.csv", SAMPLE_CSV, "text/csv")},
    )
    assert dataset_resp.status_code == 201, f"Dataset upload failed: {dataset_resp.text}"
    dataset_data = dataset_resp.json()
    assert dataset_data["table_name"], "No table_name in dataset response"
    assert dataset_data["row_count"] > 0, "No rows loaded"
    assert len(dataset_data["columns"]) > 0, "No columns extracted"

    # 3. Submit NL request
    req_resp = await async_client.post(
        f"/sessions/{session_id}/requests",
        json={"prompt": prompt, "dialect": dialect},
        timeout=180,
    )
    assert req_resp.status_code == 201, f"Request failed: {req_resp.text}"
    data = req_resp.json()
    _assert_valid_response(data, dialect, prompt)

    request_id = data["request_id"]
    assert request_id, "No request_id in response"

    # 4. Fetch request details
    get_resp = await async_client.get(
        f"/sessions/{session_id}/requests/{request_id}",
    )
    assert get_resp.status_code == 200, f"GET request failed: {get_resp.text}"
    get_data = get_resp.json()
    assert get_data["generated_sql"], "No generated_sql in GET response"
    assert get_data["status"] == "executed", f"Unexpected persisted status: {get_data['status']}, error: {get_data.get('error_message')}"

    # 5. List all requests for session
    list_resp = await async_client.get(f"/sessions/{session_id}/requests")
    assert list_resp.status_code == 200, f"LIST requests failed: {list_resp.text}"
    list_data = list_resp.json()
    assert isinstance(list_data, list), "LIST response should be a list"
    request_ids = [r["request_id"] for r in list_data]
    assert request_id in request_ids, "Submitted request not found in list"

    # 6. List datasets
    ds_list_resp = await async_client.get(f"/sessions/{session_id}/datasets")
    assert ds_list_resp.status_code == 200, f"LIST datasets failed: {ds_list_resp.text}"
    ds_list = ds_list_resp.json()
    assert len(ds_list) > 0, "No datasets in list"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_session_not_found(async_client: AsyncClient) -> None:
    """Test 404 when session does not exist."""
    fake_id = str(uuid.uuid4())
    resp = await async_client.post(
        f"/sessions/{fake_id}/requests",
        json={"prompt": "test", "dialect": "postgres"},
    )
    assert resp.status_code == 404


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_invalid_dialect(async_client: AsyncClient) -> None:
    """Test that invalid dialect is rejected."""
    resp = await async_client.post(
        "/sessions",
        json={"dialect": "mysql"},
    )
    assert resp.status_code == 422


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_dataset_upload(async_client: AsyncClient) -> None:
    """Test dataset upload and schema extraction."""
    sess_resp = await async_client.post("/sessions", json={"dialect": "postgres"})
    assert sess_resp.status_code == 201
    session_id = sess_resp.json()["session_id"]

    ds_resp = await async_client.post(
        f"/sessions/{session_id}/datasets?dialect=postgres",
        files={"file": ("test.csv", SAMPLE_CSV, "text/csv")},
    )
    assert ds_resp.status_code == 201
    data = ds_resp.json()
    assert data["table_name"] == "test_csv"
    assert data["row_count"] == 7
    assert len(data["columns"]) == 3
    assert data["status"] == "ingested"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_invalid_file_type(async_client: AsyncClient) -> None:
    """Test that unsupported file types are rejected."""
    sess_resp = await async_client.post("/sessions", json={"dialect": "postgres"})
    assert sess_resp.status_code == 201
    session_id = sess_resp.json()["session_id"]

    ds_resp = await async_client.post(
        f"/sessions/{session_id}/datasets?dialect=postgres",
        files={"file": ("test.pdf", b"%PDF-binary", "application/pdf")},
    )
    assert ds_resp.status_code == 422, f"Expected 422, got {ds_resp.status_code}: {ds_resp.text}"
