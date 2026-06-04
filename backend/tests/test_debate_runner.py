from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.debate.models import DebateSettings
from app.agents.debate.termination import check_termination, compute_sql_hash, DebateState
from app.agents.debate.transcript import DebateTranscriptBuilder
from app.services.stream_events import make_debate_round


@pytest.mark.asyncio
async def test_transcript_builder_round_trip():
    builder = DebateTranscriptBuilder(
        turns=[],
        metadata={"rounds": 2, "termination_reason": "deadlock", "final_confidence": 0.6},
    )
    builder.add_turn(
        {
            "speaker": "DebateAuthor",
            "timestamp": time.time(),
            "round_number": 1,
            "sql_candidate": "SELECT 1",
            "query_hash": "abcd1234",
            "scores": {},
            "objections": [],
            "rationale": "initial",
            "confidence": 0.8,
            "approved": None,
            "token_usage": {"input_tokens": 10, "output_tokens": 5},
            "latency_ms": 100.0,
        }
    )
    builder.add_turn(
        {
            "speaker": "DebateCritic",
            "timestamp": time.time(),
            "round_number": 1,
            "content": "Missing index",
            "sql_candidate": "SELECT 1",
            "query_hash": "abcd1234",
            "scores": {"semantic_correctness": 0.7},
            "objections": ["Missing WHERE clause"],
            "rationale": "risky",
            "confidence": 0.4,
            "approved": False,
            "token_usage": {"input_tokens": 12, "output_tokens": 8},
            "latency_ms": 80.0,
        }
    )
    result = builder.to_dict()
    assert result["summary"]["total_turns"] == 2
    assert result["summary"]["termination_reason"] == "deadlock"
    assert result["turns"][0]["speaker"] == "DebateAuthor"
    assert result["turns"][1]["objections"] == ["Missing WHERE clause"]


@pytest.mark.asyncio
async def test_hash_changes_between_rounds():
    sql_v1 = "SELECT id, name FROM users"
    sql_v2 = "SELECT id, name FROM users WHERE active = true"
    h1 = compute_sql_hash(sql_v1)
    h2 = compute_sql_hash(sql_v2)
    assert h1 != h2


@pytest.mark.asyncio
async def test_termination_continues_before_thresholds():
    settings = DebateSettings(max_rounds=10, token_budget=10000, approval_threshold=0.75)
    state = DebateState(round_number=1, token_usage={"input_tokens": 50, "output_tokens": 25}, previous_hashes=set())
    done, reason = check_termination(state, settings, author_result={"confidence": 0.6, "token_usage": {"input_tokens": 50, "output_tokens": 25}}, critic_result={"approved": False, "confidence": 0.3, "token_usage": {"input_tokens": 50, "output_tokens": 25}})
    assert done is False
    assert reason == "continue"


@pytest.mark.asyncio
async def test_sql_normalization_sqlglot_available():
    try:
        import sqlglot  # noqa: F401
    except ImportError:
        pytest.skip("sqlglot not installed")
    sql1 = "SELECT id FROM users WHERE id = 1"
    sql2 = "select id from users where id = 1"
    h1 = compute_sql_hash(sql1)
    h2 = compute_sql_hash(sql2)
    assert h1 == h2


@pytest.mark.asyncio
async def test_make_debate_round_event():
    event = make_debate_round(
        round_number=1,
        speaker="DebateAuthor",
        sql_candidate="SELECT id FROM users",
        scores={"semantic_correctness": 0.9},
        objections=[],
        approved=None,
        confidence=0.85,
        rationale="Simple select",
        query_hash="abc123",
        request_id="test-rid",
    )
    data = event.to_dict()
    assert data["type"] == "event"
    assert data["agent"] == "debateauthor"
    assert data["phase"] == "progress"
    assert data["artifact"]["round"] == 1
    assert data["artifact"]["speaker"] == "DebateAuthor"
    assert data["artifact"]["sql_candidate"] == "SELECT id FROM users"
    assert data["request_id"] == "test-rid"


@pytest.mark.asyncio
async def test_make_debate_round_critic_event():
    event = make_debate_round(
        round_number=2,
        speaker="DebateCritic",
        sql_candidate="SELECT id FROM users WHERE active = 1",
        scores={"semantic_correctness": 0.7, "edge_cases": 0.5, "performance": 0.8, "best_practices": 0.9},
        objections=["Missing index on active column", "No NULL handling"],
        approved=False,
        confidence=0.72,
        rationale="Missing performance considerations",
        query_hash="def456",
        request_id="test-rid",
    )
    data = event.to_dict()
    assert data["type"] == "event"
    assert data["agent"] == "debatecritic"
    assert data["artifact"]["approved"] is False
    assert len(data["artifact"]["objections"]) == 2
    assert data["artifact"]["scores"]["semantic_correctness"] == 0.7
    assert data["artifact"]["confidence"] == 0.72


@pytest.mark.asyncio
async def test_debate_runner_emit_event_called():
    from app.agents.debate.debate_runner import run_debate

    mock_emit = MagicMock()

    with (
        patch("app.agents.debate.debate_runner.DebateAuthor") as MockAuthor,
        patch("app.agents.debate.debate_runner.DebateCritic") as MockCritic,
    ):
        mock_author_instance = AsyncMock()
        mock_author_instance.a_generate_reply.return_value = {
            "query_sql": "SELECT id FROM users",
            "rationale": "Simple select",
            "confidence": 0.85,
            "description": "Initial SQL",
            "token_usage": {"input_tokens": 50, "output_tokens": 25},
        }
        MockAuthor.return_value = mock_author_instance

        mock_critic_instance = AsyncMock()
        mock_critic_instance.a_generate_reply.return_value = {
            "approved": True,
            "objections": [],
            "scores": {"semantic_correctness": 0.9, "edge_cases": 0.85, "performance": 0.8, "best_practices": 0.9},
            "rationale": "Looks good",
            "confidence": 0.88,
            "suggestions": "None",
            "token_usage": {"input_tokens": 60, "output_tokens": 30},
        }
        MockCritic.return_value = mock_critic_instance

        result = await run_debate(
            prompt="Show all users",
            dialect="postgres",
            schema_metadata="users(id, name, email)",
            session_id="test-sid",
            request_id="test-rid",
            max_rounds=3,
            emit_event=mock_emit,
        )

    assert result.query_sql == "SELECT id FROM users"
    assert result.termination_reason == "approved"
    assert result.rounds == 1
    assert result.final_confidence > 0

    assert mock_emit.call_count >= 2
    call_args_list = [call[0][0].to_dict() for call in mock_emit.call_args_list]
    author_events = [e for e in call_args_list if e["agent"] == "debateauthor"]
    critic_events = [e for e in call_args_list if e["agent"] == "debatecritic"]
    assert len(author_events) >= 1
    assert len(critic_events) >= 1


@pytest.mark.asyncio
async def test_debate_runner_emit_event_max_rounds():
    from app.agents.debate.debate_runner import run_debate

    mock_emit = MagicMock()

    with (
        patch("app.agents.debate.debate_runner.DebateAuthor") as MockAuthor,
        patch("app.agents.debate.debate_runner.DebateCritic") as MockCritic,
    ):
        mock_author_instance = AsyncMock()
        mock_author_instance.a_generate_reply.side_effect = [
            {
                "query_sql": "SELECT id FROM users",
                "rationale": "Simple select",
                "confidence": 0.85,
                "description": "Round 1",
                "token_usage": {"input_tokens": 50, "output_tokens": 25},
            },
            {
                "query_sql": "SELECT id, name FROM users ORDER BY name",
                "rationale": "Added ORDER BY",
                "confidence": 0.87,
                "description": "Round 2",
                "token_usage": {"input_tokens": 55, "output_tokens": 28},
            },
            {
                "query_sql": "SELECT id, name, email FROM users ORDER BY name LIMIT 10",
                "rationale": "Added email and LIMIT",
                "confidence": 0.88,
                "description": "Round 3",
                "token_usage": {"input_tokens": 58, "output_tokens": 30},
            },
        ]
        MockAuthor.return_value = mock_author_instance

        mock_critic_instance = AsyncMock()
        mock_critic_instance.a_generate_reply.side_effect = [
            {
                "approved": False,
                "objections": ["Missing ORDER BY"],
                "scores": {"semantic_correctness": 0.5, "edge_cases": 0.4, "performance": 0.6, "best_practices": 0.7},
                "rationale": "Needs ORDER BY",
                "confidence": 0.55,
                "suggestions": "Add ORDER BY",
                "token_usage": {"input_tokens": 60, "output_tokens": 30},
            },
            {
                "approved": False,
                "objections": ["Missing LIMIT"],
                "scores": {"semantic_correctness": 0.5, "edge_cases": 0.4, "performance": 0.6, "best_practices": 0.7},
                "rationale": "Needs LIMIT",
                "confidence": 0.55,
                "suggestions": "Add LIMIT",
                "token_usage": {"input_tokens": 60, "output_tokens": 30},
            },
            {
                "approved": False,
                "objections": ["Still not good enough"],
                "scores": {"semantic_correctness": 0.5, "edge_cases": 0.4, "performance": 0.6, "best_practices": 0.7},
                "rationale": "Not enough",
                "confidence": 0.55,
                "suggestions": "Improve more",
                "token_usage": {"input_tokens": 60, "output_tokens": 30},
            },
        ]
        MockCritic.return_value = mock_critic_instance

        result = await run_debate(
            prompt="Show all users",
            dialect="postgres",
            schema_metadata="users(id, name)",
            session_id="test-sid",
            request_id="test-rid",
            max_rounds=3,
            emit_event=mock_emit,
        )

    assert result.termination_reason == "max_rounds"
    assert result.rounds == 3

    # Verify termination event was emitted
    termination_events = [call[0][0].to_dict() for call in mock_emit.call_args_list]
    debate_events = [e for e in termination_events if e.get("agent") == "debate"]
    assert len(debate_events) >= 1
    assert any("termination_reason" in (e.get("artifact") or {}) for e in debate_events)
