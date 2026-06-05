from __future__ import annotations

import time
from typing import Any

from app.agents.debate.models import DebateResult, TerminationReason
from app.agents.debate.participants import DebateAuthor, DebateCritic
from app.agents.debate.termination import (
    compute_sql_hash,
)
from app.agents.debate.transcript import DebateTranscriptBuilder
from app.core.config import settings
from app.core.metrics import AGENT_PHASE_COUNT, AGENT_PHASE_LATENCY
from app.core.telemetry import add_span_attribute, get_tracer


async def run_debate(
    *,
    prompt: str,
    dialect: str,
    schema_metadata: str,
    session_id: str,
    request_id: str,
    max_rounds: int | None = None,
    token_budget: int | None = None,
    approval_threshold: float | None = None,
    emit_event: Any | None = None,
) -> DebateResult:
    from app.agents.debate.models import DebateSettings

    settings_obj = DebateSettings(
        max_rounds=max_rounds or settings.DEBATE_MAX_ROUNDS,
        token_budget=token_budget or settings.DEBATE_TOKEN_BUDGET,
        approval_threshold=approval_threshold or 0.75,
    )

    tracer = get_tracer()
    with tracer.start_as_current_span("debate") as span:
        span.set_attribute("prompt.preview", prompt[:200])
        span.set_attribute("dialect", dialect)
        span.set_attribute("session_id", session_id)
        span.set_attribute("request_id", request_id)
        span.set_attribute("debate.max_rounds", settings_obj.max_rounds)
        span.set_attribute("debate.token_budget", settings_obj.token_budget)

        start_debate = time.perf_counter()

        author = DebateAuthor()
        critic = DebateCritic()
        transcript = DebateTranscriptBuilder(turns=[], metadata={})

        round_number = 1
        author_confidence = 0.0
        critic_score = 0.0
        final_query_sql = ""
        final_rationale = ""
        final_status: TerminationReason = "max_rounds"

        debate_history: list[dict[str, Any]] = []
        previous_hashes: set[str] = set()
        total_tokens: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

        schema_context = _build_schema_context(schema_metadata)

        while True:
            author_messages = _build_author_messages(
                prompt=prompt,
                dialect=dialect,
                schema=schema_context,
                feedback=debate_history[-1] if debate_history else None,
                round_number=round_number,
            )

            author_start = time.perf_counter()
            author_result = await author.a_generate_reply(messages=author_messages)
            author_elapsed = (time.perf_counter() - author_start) * 1000

            query_sql = _extract_sql(author_result)
            query_hash = compute_sql_hash(query_sql) if query_sql else ""
            author_confidence = float(author_result.get("confidence", 0.0) or 0.0)

            AGENT_PHASE_COUNT.labels(agent="debateauthor", phase="generate").inc()
            AGENT_PHASE_LATENCY.labels(agent="debateauthor", phase="generate").observe(round(author_elapsed, 1))

            transcript.add_turn({
                "speaker": "DebateAuthor",
                "timestamp": time.time(),
                "round_number": round_number,
                "content": author_result.get("description", author_result.get("rationale", "")),
                "sql_candidate": query_sql,
                "query_hash": query_hash,
                "scores": {},
                "objections": [],
                "rationale": author_result.get("rationale", ""),
                "confidence": author_confidence,
                "approved": None,
                "token_usage": author_result.get("token_usage", {}),
                "latency_ms": round(author_elapsed, 1),
            })

            if emit_event:
                from app.services.stream_events import make_debate_round
                emit_event(make_debate_round(
                    round_number=round_number,
                    speaker="DebateAuthor",
                    sql_candidate=query_sql,
                    confidence=author_confidence,
                    rationale=author_result.get("rationale", ""),
                    query_hash=query_hash,
                    status=f"Round {round_number} — Author generated SQL",
                    request_id=request_id,
                ))

            add_span_attribute("debate.author.round", round_number)
            add_span_attribute("debate.author.confidence", author_confidence)
            add_span_attribute("debate.author.query_hash", query_hash)

            final_query_sql = query_sql
            final_rationale = author_result.get("rationale", "")

            critic_messages = _build_critic_messages(
                prompt=prompt,
                dialect=dialect,
                schema=schema_context,
                query_sql=query_sql,
                author_rationale=author_result.get("rationale", ""),
                author_confidence=author_confidence,
                round_number=round_number,
            )

            critic_start = time.perf_counter()
            critic_result = await critic.a_generate_reply(messages=critic_messages)
            critic_elapsed = (time.perf_counter() - critic_start) * 1000

            scores = critic_result.get("scores", {})
            critic_score = float(critic_result.get("confidence", 0.0) or 0.0)

            AGENT_PHASE_COUNT.labels(agent="debatecritic", phase="review").inc()
            AGENT_PHASE_LATENCY.labels(agent="debatecritic", phase="review").observe(round(critic_elapsed, 1))

            transcript.add_turn({
                "speaker": "DebateCritic",
                "timestamp": time.time(),
                "round_number": round_number,
                "content": critic_result.get("rationale", ""),
                "sql_candidate": query_sql,
                "query_hash": query_hash,
                "scores": scores,
                "objections": critic_result.get("objections", []),
                "rationale": critic_result.get("rationale", ""),
                "confidence": critic_score,
                "approved": bool(critic_result.get("approved", False)),
                "token_usage": critic_result.get("token_usage", {}),
                "latency_ms": round(critic_elapsed, 1),
            })

            if emit_event:
                from app.services.stream_events import make_debate_round
                emit_event(make_debate_round(
                    round_number=round_number,
                    speaker="DebateCritic",
                    sql_candidate=query_sql,
                    scores=scores,
                    objections=critic_result.get("objections", []),
                    approved=bool(critic_result.get("approved", False)),
                    confidence=critic_score,
                    rationale=critic_result.get("rationale", ""),
                    query_hash=query_hash,
                    status=f"Round {round_number} — Critic reviewed SQL",
                    request_id=request_id,
                ))

            add_span_attribute("debate.critic.round", round_number)
            add_span_attribute("debate.critic.approved", bool(critic_result.get("approved", False)))
            add_span_attribute("debate.critic.score", critic_score)

            debate_history.append({
                "round": round_number,
                "author_sql": query_sql,
                "author_confidence": author_confidence,
                "critic_approved": bool(critic_result.get("approved", False)),
                "critic_objections": critic_result.get("objections", []),
                "critic_score": critic_score,
            })

            author_tokens = author_result.get("token_usage", {}) or {}
            critic_tokens = critic_result.get("token_usage", {}) or {}
            total_tokens["input_tokens"] = total_tokens.get("input_tokens", 0) + (author_tokens.get("input_tokens", 0) or 0) + (critic_tokens.get("input_tokens", 0) or 0)
            total_tokens["output_tokens"] = total_tokens.get("output_tokens", 0) + (author_tokens.get("output_tokens", 0) or 0) + (critic_tokens.get("output_tokens", 0) or 0)

            from app.agents.debate.termination import (
                DebateState,
            )
            from app.agents.debate.termination import (
                check_termination as _check,
            )

            state = DebateState(
                round_number=round_number,
                token_usage=total_tokens,
                previous_hashes=previous_hashes,
            )
            done, termination_reason = _check(
                state=state,
                settings=settings_obj,
                author_result=author_result,
                critic_result=critic_result,
                current_hash=query_hash,
            )

            if query_hash:
                previous_hashes.add(query_hash)

            if done:
                assert termination_reason != "continue"
                final_status = termination_reason
                if emit_event:
                    from app.services.stream_events import make_artifact
                    status_msg = f"Debate terminated: {termination_reason} (round {round_number})"
                    emit_event(make_artifact(
                        "debate",
                        {"termination_reason": termination_reason, "rounds": round_number, "final_confidence": 0.4 * author_confidence + 0.6 * critic_score},
                        status_msg,
                        request_id,
                    ))
                break

            round_number += 1

        debate_transcript_dict = transcript.to_dict()
        debate_transcript_dict["rounds"] = debate_history

        final_confidence = 0.4 * author_confidence + 0.6 * critic_score

        result = DebateResult(
            query_sql=final_query_sql,
            rationale=final_rationale,
            critic_score=critic_score,
            debate_transcript=debate_transcript_dict,
            final_confidence=final_confidence,
            author_confidence=author_confidence,
            termination_reason=final_status,
            rounds=round_number,
        )

        elapsed = (time.perf_counter() - start_debate) * 1000
        span.set_attribute("debate.termination_reason", final_status)
        span.set_attribute("debate.rounds", round_number)
        span.set_attribute("debate.final_confidence", result.final_confidence)
        span.set_attribute("debate.total_ms", round(elapsed, 1))

        return result


def _build_schema_context(schema_metadata: str) -> str:
    if not schema_metadata:
        return "No schema metadata available."
    return schema_metadata


def _build_author_messages(
    *,
    prompt: str,
    dialect: str,
    schema: str,
    feedback: dict[str, Any] | None,
    round_number: int,
) -> list[dict[str, str]]:
    header = (
        f"Round {round_number} - DebateAuthor\n\n"
        f"User Request: {prompt}\n"
        f"Dialect: {dialect}\n"
        f"Schema:\n{schema}\n"
    )

    if feedback is not None:
        critic_approved = feedback.get("critic_approved", False)
        if critic_approved:
            feedback_section = "The critic APPROVED your last SQL. Stop here."
        else:
            objections = feedback.get("critic_objections", [])
            score_breakdown = ""
            if feedback.get("critic_score"):
                score_breakdown = f"Previous critic score: {feedback['critic_score']:.2f}\n"
            feedback_section = (
                "FEEDBACK FROM DEBATECRITIC:\n"
                f"{score_breakdown}"
                f"Objections:\n" + "\n".join(f"- {o}" for o in objections) + "\n\n"
                "Revise your SQL to address ALL objections above. Be specific about what changed."
            )
        body = f"{header}\n{feedback_section}\n\nGenerate revised SQL as JSON."
    else:
        body = f"{header}\nGenerate initial SQL as JSON."

    return [
        {"role": "system", "content": "You are the DebateAuthor SQL generation agent."},
        {"role": "user", "content": body},
    ]


def _build_critic_messages(
    *,
    prompt: str,
    dialect: str,
    schema: str,
    query_sql: str,
    author_rationale: str,
    author_confidence: float,
    round_number: int,
) -> list[dict[str, str]]:
    body = (
        f"Round {round_number} - DebateCritic Review\n\n"
        f"User Request: {prompt}\n"
        f"Dialect: {dialect}\n"
        f"Schema:\n{schema}\n\n"
        f"Proposed SQL:\n```sql\n{query_sql}\n```\n\n"
        f"Author's rationale: {author_rationale}\n"
        f"Author's confidence: {author_confidence:.2f}\n\n"
        "Review this SQL query rigorously. Find at least one concrete flaw before approving.\n\n"
        "Output ONLY JSON with approved, objections, scores, rationale, confidence, and suggestions."
    )
    return [
        {"role": "system", "content": "You are the DebateCritic reviewing SQL queries."},
        {"role": "user", "content": body},
    ]


def _extract_sql(author_result: dict[str, Any]) -> str:
    import json

    raw = author_result.get("query_sql", "")
    if raw:
        try:
            candidate = json.loads(raw)
            if isinstance(candidate, dict) and "query_sql" in candidate:
                return str(candidate["query_sql"])
            if isinstance(candidate, str):
                return candidate
        except json.JSONDecodeError:
            pass
    sql = author_result.get("query_sql", "")
    if isinstance(sql, str):
        return sql.strip()

    try:
        for key in ("query_sql", "sql", "sql_query"):
            value = author_result.get(key, "")
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("query_sql") or value.get("sql") or value.get("sql_query", "")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    except Exception:
        pass

    return ""
