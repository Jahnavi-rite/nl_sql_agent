from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.debate.models import DebateSettings
from app.agents.debate.termination import check_termination, compute_sql_hash, DebateState
from app.agents.debate.transcript import DebateTranscriptBuilder
from app.models.session import (
    MAX_ITERATIONS,
    AgentTrace,
    Feedback,
    Iteration,
    Request,
    Session,
)
from app.models.enums import Dialect, IterationStatus, RequestStatus, SessionStatus
from app.services.request_service import execute_nl_pipeline
from app.services.stream_manager import stream_manager


@pytest.mark.asyncio
async def test_sql_hash_convergence_same_sql():
    sql = "SELECT id, name FROM users WHERE active = true"
    h1 = compute_sql_hash(sql)
    h2 = compute_sql_hash(sql)
    assert h1 == h2
    assert len(h1) == 16


@pytest.mark.asyncio
async def test_sql_hash_normalization_sqlglot():
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
async def test_transcript_persistence_into_iteration(db: AsyncSession):
    session = Session(user_id="test-user", dialect=Dialect.POSTGRESQL, status=SessionStatus.ACTIVE)
    db.add(session)
    await db.commit()
    await db.refresh(session)

    req = Request(session_id=session.id, question="test", context_json={})
    db.add(req)
    await db.commit()
    await db.refresh(req)

    transcript = {
        "turns": [
            {
                "speaker": "DebateAuthor",
                "timestamp": time.time(),
                "round_number": 1,
                "sql_candidate": "SELECT 1",
                "query_hash": "abcd1234",
                "confidence": 0.8,
            }
        ],
        "summary": {"total_turns": 1, "termination_reason": "approved"},
    }

    iteration = Iteration(
        request_id=req.id,
        attempt_number=1,
        status=IterationStatus.EXECUTED,
        generated_sql="SELECT 1",
        confidence=0.8,
        rationale="test",
        critic_score=0.9,
        debate_transcript_json=transcript,
    )
    db.add(iteration)
    await db.commit()
    await db.refresh(iteration)

    assert iteration.debate_transcript_json is not None
    assert iteration.debate_transcript_json["summary"]["termination_reason"] == "approved"
    assert iteration.debate_transcript_json["turns"][0]["speaker"] == "DebateAuthor"

    await db.delete(iteration)
    await db.delete(req)
    await db.delete(session)
    await db.commit()


@pytest.mark.asyncio
async def test_approval_termination():
    settings = DebateSettings(max_rounds=3, token_budget=6000, approval_threshold=0.75)
    state = DebateState(round_number=1, token_usage={"input_tokens": 100, "output_tokens": 50}, previous_hashes=set())
    critic = {"approved": True, "confidence": 0.9}
    done, reason = check_termination(state, settings, author_result=None, critic_result=critic)
    assert done is True
    assert reason == "approved"


@pytest.mark.asyncio
async def test_deadlock_detection_same_hash():
    settings = DebateSettings(max_rounds=3, token_budget=6000, approval_threshold=0.75)
    prev_hash = "abcd1234"
    state = DebateState(round_number=2, token_usage={"input_tokens": 100, "output_tokens": 50}, previous_hashes={prev_hash})
    done, reason = check_termination(state, settings, author_result=None, critic_result={"approved": False}, current_hash=prev_hash)
    assert done is True
    assert reason == "deadlock"


@pytest.mark.asyncio
async def test_max_rounds_termination():
    settings = DebateSettings(max_rounds=3, token_budget=6000, approval_threshold=0.75)
    state = DebateState(round_number=3, token_usage={"input_tokens": 100, "output_tokens": 50}, previous_hashes=set())
    done, reason = check_termination(state, settings, author_result=None, critic_result={"approved": False})
    assert done is True
    assert reason == "max_rounds"


@pytest.mark.asyncio
async def test_token_budget_enforcement():
    settings = DebateSettings(max_rounds=10, token_budget=1000, approval_threshold=0.75)
    state = DebateState(round_number=1, token_usage={"input_tokens": 500, "output_tokens": 600}, previous_hashes=set())
    author = {"query_sql": "SELECT 1", "confidence": 0.5, "token_usage": {"input_tokens": 100, "output_tokens": 100}}
    critic = {"approved": False, "confidence": 0.3, "token_usage": {"input_tokens": 100, "output_tokens": 100}}
    done, reason = check_termination(state, settings, author_result=author, critic_result=critic)
    assert done is True
    assert reason == "token_budget"


@pytest.mark.asyncio
async def test_stream_reconnect_buffer_preserves_debate_events():
    """Verify that the StreamManager buffer preserves debate events for reconnect replay."""
    from app.services.stream_events import make_debate_round

    event1 = make_debate_round(1, "DebateAuthor", "SELECT 1", request_id="r1")
    event2 = make_debate_round(1, "DebateCritic", "SELECT 1",
                                scores={"semantic_correctness": 0.9},
                                objections=[], approved=False,
                                request_id="r1")

    stream = stream_manager.get_or_create_stream("reconnect-session")

    stream.publish(event1.to_dict())
    stream.publish(event2.to_dict())

    buffer = stream.replay_buffer()
    assert len(buffer) >= 2
    debate_events = [e for e in buffer if e.get("agent", "").startswith("debate")]
    assert len(debate_events) >= 2
    assert debate_events[0]["artifact"]["speaker"] == "DebateAuthor"
    assert debate_events[1]["artifact"]["speaker"] == "DebateCritic"

    # Simulate reconnect — replay the buffer
    reconnected_events = []
    for ev in buffer:
        reconnected_events.append(ev)
    assert len(reconnected_events) >= 2

    # Cleanup
    stream_manager._cleanup_stream("reconnect-session")


@pytest.mark.asyncio
async def test_debate_quality_improvement_comparison():
    """Verify that debate confidence formula produces higher quality scores
    than single-pass when critic scores are high."""
    from app.agents.debate.termination import build_result
    from app.agents.debate.transcript import DebateTranscriptBuilder

    # Single-pass equivalent: only author confidence
    author_conf = 0.70
    critic_score = 0.95

    # Debate: final_confidence = 0.4 * author + 0.6 * critic
    builder = DebateTranscriptBuilder(turns=[], metadata={"rounds": 2, "termination_reason": "approved"})
    result = build_result(
        query_sql="SELECT * FROM users",
        rationale="test",
        critic_score=critic_score,
        debate_transcript=builder.to_dict(),
        author_confidence=author_conf,
        termination_reason="approved",
        rounds=2,
    )

    # The debate blends both: 0.4 * 0.70 + 0.6 * 0.95 = 0.85
    assert abs(result.final_confidence - 0.85) < 1e-10

    # When critic is strong, debate confidence > author confidence alone
    assert result.final_confidence > author_conf

    # When critic is weak, the final confidence reflects the blend
    weak_critic = build_result(
        query_sql="SELECT * FROM users",
        rationale="test",
        critic_score=0.30,
        debate_transcript=builder.to_dict(),
        author_confidence=0.80,
        termination_reason="max_rounds",
        rounds=3,
    )
    assert abs(weak_critic.final_confidence - 0.50) < 1e-10  # 0.4 * 0.80 + 0.6 * 0.30 = 0.50
    assert weak_critic.final_confidence < 0.80  # Critic drags it down


@pytest.mark.asyncio
async def test_debate_transcript_persisted_through_pipeline(db: AsyncSession):
    """Verify that debate transcripts are correctly persisted when saving iterations."""
    from app.agents.debate.transcript import DebateTranscriptBuilder

    session = Session(user_id="test-user", dialect=Dialect.POSTGRESQL, status=SessionStatus.ACTIVE)
    db.add(session)
    await db.commit()
    await db.refresh(session)

    req = Request(session_id=session.id, question="test quality", context_json={})
    db.add(req)
    await db.commit()
    await db.refresh(req)

    builder = DebateTranscriptBuilder(turns=[], metadata={"rounds": 2, "termination_reason": "approved", "final_confidence": 0.85})
    builder.add_turn({
        "speaker": "DebateAuthor", "timestamp": time.time(), "round_number": 1,
        "sql_candidate": "SELECT * FROM users", "query_hash": "abc123",
        "scores": {}, "objections": [], "rationale": "initial", "confidence": 0.8,
        "approved": None, "token_usage": {"input_tokens": 10, "output_tokens": 5}, "latency_ms": 100,
    })
    builder.add_turn({
        "speaker": "DebateCritic", "timestamp": time.time(), "round_number": 1,
        "sql_candidate": "SELECT * FROM users", "query_hash": "abc123",
        "scores": {"semantic_correctness": 0.9, "edge_cases": 0.8}, "objections": ["Add WHERE clause"],
        "rationale": "needs filter", "confidence": 0.85, "approved": False,
        "token_usage": {"input_tokens": 12, "output_tokens": 8}, "latency_ms": 80,
    })
    builder.add_turn({
        "speaker": "DebateAuthor", "timestamp": time.time(), "round_number": 2,
        "sql_candidate": "SELECT * FROM users WHERE active = 1", "query_hash": "def456",
        "scores": {}, "objections": [], "rationale": "added filter", "confidence": 0.9,
        "approved": None, "token_usage": {"input_tokens": 15, "output_tokens": 7}, "latency_ms": 90,
    })
    builder.add_turn({
        "speaker": "DebateCritic", "timestamp": time.time(), "round_number": 2,
        "sql_candidate": "SELECT * FROM users WHERE active = 1", "query_hash": "def456",
        "scores": {"semantic_correctness": 0.95, "edge_cases": 0.9, "performance": 0.85, "best_practices": 0.9},
        "objections": [], "rationale": "approved", "confidence": 0.92, "approved": True,
        "token_usage": {"input_tokens": 14, "output_tokens": 9}, "latency_ms": 75,
    })

    transcript_dict = builder.to_dict()

    iteration = Iteration(
        request_id=req.id,
        attempt_number=1,
        status=IterationStatus.EXECUTED,
        generated_sql="SELECT * FROM users WHERE active = 1",
        confidence=0.9,
        rationale="added filter after critic review",
        critic_score=0.92,
        debate_transcript_json=transcript_dict,
    )
    db.add(iteration)
    await db.commit()
    await db.refresh(iteration)

    assert iteration.debate_transcript_json is not None
    assert iteration.debate_transcript_json["summary"]["total_turns"] == 4
    assert iteration.debate_transcript_json["summary"]["termination_reason"] == "approved"
    assert len(iteration.debate_transcript_json["turns"]) == 4
    assert iteration.debate_transcript_json["turns"][0]["speaker"] == "DebateAuthor"
    assert iteration.debate_transcript_json["turns"][1]["speaker"] == "DebateCritic"
    assert iteration.debate_transcript_json["turns"][3]["approved"] is True

    await db.delete(iteration)
    await db.delete(req)
    await db.delete(session)
    await db.commit()
