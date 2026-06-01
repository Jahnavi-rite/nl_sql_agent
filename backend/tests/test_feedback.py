"""Tests for the feedback workflow (approve/reject/edit).

Uses in-memory SQLite (from conftest.py) with mocked LLM and SQL execution.
Tests exercise the full API -> service -> persistence path.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.main import app
from app.models.enums import Dialect, FeedbackAction, IterationStatus
from app.services.session_service import (
    append_iteration,
    create_request,
    create_session,
    record_feedback,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def async_client(db: AsyncSession) -> AsyncClient:
    """Override DB dependency for API tests."""

    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def session_and_iteration(db: AsyncSession):
    """Create a session, request, and first iteration for feedback testing."""
    session = await create_session(db, user_id="feedback_test", dialect=Dialect.POSTGRESQL)
    request = await create_request(
        db,
        session_id=session.id,
        question="Show all users",
        context={"dialect": "postgres"},
    )
    iteration = await append_iteration(
        db,
        request_id=request.id,
        generated_sql="SELECT id, name FROM users",
        confidence=0.85,
        rationale="Simple user query",
        status=IterationStatus.EXECUTED,
    )
    iteration.execution_results = [{"id": 1, "name": "Alice"}]
    iteration.execution_rows = 1
    iteration.execution_ms = 12.5
    await db.commit()
    await db.refresh(iteration)
    return session, request, iteration


@pytest_asyncio.fixture
async def session_with_five_iterations(db: AsyncSession):
    """Create a session with a request that has 5 iterations (at cap)."""
    session = await create_session(db, user_id="cap_test", dialect=Dialect.POSTGRESQL)
    request = await create_request(
        db,
        session_id=session.id,
        question="Show all orders",
        context={"dialect": "postgres"},
    )
    iterations = []
    for i in range(5):
        it = await append_iteration(
            db,
            request_id=request.id,
            generated_sql=f"SELECT * FROM orders -- attempt {i + 1}",
            confidence=0.5 + i * 0.1,
            rationale=f"Attempt {i + 1}",
            status=IterationStatus.EXECUTED,
        )
        it.execution_results = [{"id": i}]
        it.execution_rows = 1
        await db.commit()
        await db.refresh(it)
        iterations.append(it)
    return session, request, iterations


# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

MOCK_AGENT_RESPONSE = MagicMock()
MOCK_AGENT_RESPONSE.query_sql = "SELECT id, name, email FROM users WHERE active = true"
MOCK_AGENT_RESPONSE.confidence = 0.92
MOCK_AGENT_RESPONSE.rationale = "Added active filter per user feedback"


class _FakeAsyncResult:
    def fetchall(self):
        return [(2, "Bob", "bob@test.com")]

    def keys(self):
        return ["id", "name", "email"]


class _FakeConnection:
    async def execute(self, stmt):
        return _FakeAsyncResult()


class _FakeConnectContext:
    async def __aenter__(self):
        return _FakeConnection()

    async def __aexit__(self, *args):
        pass


def _make_mock_engine():
    """Create a mock async engine that returns fake query results."""
    mock_engine = MagicMock()
    mock_engine.connect.return_value = _FakeConnectContext()
    return mock_engine


# ---------------------------------------------------------------------------
# Tests: Approve
# ---------------------------------------------------------------------------


class TestApprove:
    async def test_approve_closes_request(
        self, async_client: AsyncClient, session_and_iteration
    ):
        session, request, iteration = session_and_iteration
        sid = str(session.id)
        rid = str(request.id)
        iid = str(iteration.id)

        response = await async_client.post(
            f"/sessions/{sid}/feedback",
            json={"iteration_id": iid, "action": "approve"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "approve"
        assert data["status"] == "approved"
        assert data["request_status"] == "approved"
        assert data["needs_human_intervention"] is False
        assert data["iteration_id"] == iid

        # Verify GET request shows approved
        get_resp = await async_client.get(f"/sessions/{sid}/requests/{rid}")
        assert get_resp.status_code == 200
        assert get_resp.json()["request_status"] == "approved"


# ---------------------------------------------------------------------------
# Tests: Reject
# ---------------------------------------------------------------------------


class TestReject:
    @patch("app.services.feedback_service.get_schema_description", return_value="users(id, name)")
    @patch("app.services.feedback_service.validate_or_raise", return_value="")
    @patch("app.services.feedback_service.async_engine")
    @patch("app.services.feedback_service.llm_generate", return_value=MOCK_AGENT_RESPONSE)
    async def test_reject_regenerates_sql(
        self,
        mock_llm,
        mock_engine,
        mock_validate,
        mock_schema,
        async_client: AsyncClient,
        session_and_iteration,
    ):
        session, request, iteration = session_and_iteration
        sid = str(session.id)
        iid = str(iteration.id)
        mock_engine_obj = _make_mock_engine()
        mock_engine.connect = mock_engine_obj.connect

        response = await async_client.post(
            f"/sessions/{sid}/feedback",
            json={"iteration_id": iid, "action": "reject", "comment": "Please add email column"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reject"
        assert data["status"] == "completed"
        assert data["attempt_number"] == 2
        assert "email" in data["query_sql"]
        assert data["needs_human_intervention"] is False
        assert data["confidence"] == 0.92

    @patch("app.services.feedback_service.get_schema_description", return_value="users(id, name)")
    @patch("app.services.feedback_service.validate_or_raise", return_value="")
    @patch("app.services.feedback_service.async_engine")
    @patch("app.services.feedback_service.llm_generate", return_value=MOCK_AGENT_RESPONSE)
    async def test_reject_without_comment(
        self,
        mock_llm,
        mock_engine,
        mock_validate,
        mock_schema,
        async_client: AsyncClient,
        session_and_iteration,
    ):
        session, request, iteration = session_and_iteration
        sid = str(session.id)
        iid = str(iteration.id)
        mock_engine_obj = _make_mock_engine()
        mock_engine.connect = mock_engine_obj.connect

        response = await async_client.post(
            f"/sessions/{sid}/feedback",
            json={"iteration_id": iid, "action": "reject"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reject"
        assert data["attempt_number"] == 2


# ---------------------------------------------------------------------------
# Tests: Edit
# ---------------------------------------------------------------------------


class TestEdit:
    @patch("app.services.feedback_service.validate_or_raise", return_value="")
    @patch("app.services.feedback_service.async_engine")
    async def test_edit_executes_directly(
        self,
        mock_engine,
        mock_validate,
        async_client: AsyncClient,
        session_and_iteration,
    ):
        session, request, iteration = session_and_iteration
        sid = str(session.id)
        iid = str(iteration.id)
        mock_engine_obj = _make_mock_engine()
        mock_engine.connect = mock_engine_obj.connect

        edited_sql = "SELECT id, name, email FROM users ORDER BY name"
        response = await async_client.post(
            f"/sessions/{sid}/feedback",
            json={"iteration_id": iid, "action": "edit", "edited_sql": edited_sql},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "edit"
        assert data["status"] == "completed"
        assert data["confidence"] == 1.0
        assert data["query_sql"] == edited_sql
        assert data["attempt_number"] == 2
        assert data["needs_human_intervention"] is False

    async def test_edit_without_sql_returns_400(
        self, async_client: AsyncClient, session_and_iteration
    ):
        session, _, iteration = session_and_iteration
        sid = str(session.id)
        iid = str(iteration.id)

        response = await async_client.post(
            f"/sessions/{sid}/feedback",
            json={"iteration_id": iid, "action": "edit"},
        )
        assert response.status_code == 400

    async def test_edit_empty_sql_returns_400(
        self, async_client: AsyncClient, session_and_iteration
    ):
        session, _, iteration = session_and_iteration
        sid = str(session.id)
        iid = str(iteration.id)

        response = await async_client.post(
            f"/sessions/{sid}/feedback",
            json={"iteration_id": iid, "action": "edit", "edited_sql": "   "},
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Tests: Iteration Cap
# ---------------------------------------------------------------------------


class TestIterationCap:
    @patch("app.services.feedback_service.get_schema_description", return_value="orders(id)")
    @patch("app.services.feedback_service.validate_or_raise", return_value="")
    @patch("app.services.feedback_service.async_engine")
    @patch("app.services.feedback_service.llm_generate", return_value=MOCK_AGENT_RESPONSE)
    async def test_iteration_cap_at_5(
        self,
        mock_llm,
        mock_engine,
        mock_validate,
        mock_schema,
        async_client: AsyncClient,
        session_with_five_iterations,
    ):
        session, request, iterations = session_with_five_iterations
        sid = str(session.id)
        last_iid = str(iterations[-1].id)

        response = await async_client.post(
            f"/sessions/{sid}/feedback",
            json={"iteration_id": last_iid, "action": "reject", "comment": "Still not right"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reject"
        assert data["needs_human_intervention"] is True
        assert data["status"] == "needs_human_intervention"


# ---------------------------------------------------------------------------
# Tests: Error Cases
# ---------------------------------------------------------------------------


class TestFeedbackErrors:
    async def test_feedback_on_nonexistent_session(self, async_client: AsyncClient):
        fake_id = str(uuid.uuid4())
        response = await async_client.post(
            f"/sessions/{fake_id}/feedback",
            json={
                "iteration_id": str(uuid.uuid4()),
                "action": "approve",
            },
        )
        assert response.status_code == 404

    async def test_feedback_on_nonexistent_iteration(
        self, async_client: AsyncClient, session_and_iteration
    ):
        session, _, _ = session_and_iteration
        sid = str(session.id)
        response = await async_client.post(
            f"/sessions/{sid}/feedback",
            json={"iteration_id": str(uuid.uuid4()), "action": "approve"},
        )
        assert response.status_code == 400

    async def test_feedback_on_wrong_session(
        self, async_client: AsyncClient, session_and_iteration
    ):
        _, _, iteration = session_and_iteration
        iid = str(iteration.id)
        other_session_id = str(uuid.uuid4())
        response = await async_client.post(
            f"/sessions/{other_session_id}/feedback",
            json={"iteration_id": iid, "action": "approve"},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Request Details Include Iterations
# ---------------------------------------------------------------------------


class TestRequestDetails:
    async def test_get_request_includes_iterations(
        self, async_client: AsyncClient, session_and_iteration
    ):
        session, request, iteration = session_and_iteration
        sid = str(session.id)
        rid = str(request.id)
        iid = str(iteration.id)

        response = await async_client.get(f"/sessions/{sid}/requests/{rid}")
        assert response.status_code == 200
        data = response.json()
        assert data["request_status"] == "open"
        assert len(data["iterations"]) == 1
        it = data["iterations"][0]
        assert it["iteration_id"] == iid
        assert it["attempt_number"] == 1
        assert it["status"] == "executed"
        assert it["generated_sql"] == "SELECT id, name FROM users"
        assert it["confidence"] == 0.85

    async def test_list_requests_includes_iterations(
        self, async_client: AsyncClient, session_and_iteration
    ):
        session, request, _ = session_and_iteration
        sid = str(session.id)
        rid = str(request.id)

        response = await async_client.get(f"/sessions/{sid}/requests")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        found = [r for r in data if r["request_id"] == rid]
        assert len(found) == 1
        assert len(found[0]["iterations"]) == 1


# ---------------------------------------------------------------------------
# Tests: Regen context includes prior attempts
# ---------------------------------------------------------------------------


class TestRegenContext:
    @patch("app.services.feedback_service.get_schema_description", return_value="users(id, name)")
    @patch("app.services.feedback_service.validate_or_raise", return_value="")
    @patch("app.services.feedback_service.async_engine")
    @patch("app.services.feedback_service.llm_generate", return_value=MOCK_AGENT_RESPONSE)
    async def test_regen_context_includes_prior_attempts(
        self,
        mock_llm,
        mock_engine,
        mock_validate,
        mock_schema,
        async_client: AsyncClient,
        session_and_iteration,
        db: AsyncSession,
    ):
        """Verify the regeneration prompt includes original request, prior SQL, and feedback."""
        session, request, iteration = session_and_iteration
        sid = str(session.id)

        # Add a second iteration with feedback
        it2 = await append_iteration(
            db,
            request_id=request.id,
            generated_sql="SELECT id FROM users WHERE active = true",
            confidence=0.7,
            rationale="Added active filter",
            status=IterationStatus.FAILED,
        )
        it2.error_message = "column active does not exist"
        await db.commit()
        iid2 = str(it2.id)

        # Add feedback on first iteration
        await record_feedback(
            db,
            iteration_id=iteration.id,
            action=FeedbackAction.REJECT,
            comment="Missing email column",
        )

        mock_engine_obj = _make_mock_engine()
        mock_engine.connect = mock_engine_obj.connect

        response = await async_client.post(
            f"/sessions/{sid}/feedback",
            json={"iteration_id": iid2, "action": "reject", "comment": "Fix the column error"},
        )
        assert response.status_code == 200

        # Verify the prompt passed to llm_generate contains prior context
        call_args = mock_llm.call_args
        prompt_arg = call_args[0][0] if call_args[0] else call_args[1].get("prompt", "")
        assert "Show all users" in prompt_arg
        assert "SELECT id, name FROM users" in prompt_arg
        assert "Attempt" in prompt_arg
