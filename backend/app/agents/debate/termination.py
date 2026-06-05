from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from app.agents.debate.models import DebateResult, DebateSettings, TerminationReason


@dataclass
class DebateState:
    round_number: int
    token_usage: dict[str, int]
    previous_hashes: set[str]
    termination_reason: TerminationReason | None = None

    def consume_tokens(self, usage: dict[str, int]) -> bool:
        input_tokens = usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("output_tokens", 0) or 0
        self.token_usage["input_tokens"] = self.token_usage.get("input_tokens", 0) + input_tokens
        self.token_usage["output_tokens"] = self.token_usage.get("output_tokens", 0) + output_tokens
        return self.token_usage.get("input_tokens", 0) + self.token_usage.get("output_tokens", 0) >= 6_000


def compute_sql_hash(sql: str) -> str:
    normalized = _normalize_sql(sql)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _normalize_sql(sql: str) -> str:
    try:
        import sqlglot

        parsed = sqlglot.parse_one(sql)
        return parsed.sql()
    except Exception:
        return sql.strip().lower().replace(";", "").strip()


def check_termination(
    state: DebateState,
    settings: DebateSettings,
    author_result: dict[str, Any] | None,
    critic_result: dict[str, Any] | None,
    current_hash: str | None = None,
) -> tuple[bool, TerminationReason | Literal["continue"]]:
    if state.termination_reason is not None:
        return True, state.termination_reason

    if critic_result is not None and critic_result.get("approved", False):
        state.termination_reason = "approved"
        return True, "approved"

    if current_hash is not None and current_hash in state.previous_hashes:
        state.termination_reason = "deadlock"
        return True, "deadlock"

    if state.round_number >= settings.max_rounds:
        state.termination_reason = "max_rounds"
        return True, "max_rounds"

    if author_result is not None:
        author_tokens = author_result.get("token_usage", {})
        critic_tokens = critic_result.get("token_usage", {}) if critic_result else {}
        total_input = (author_tokens.get("input_tokens", 0) or 0) + (critic_tokens.get("input_tokens", 0) or 0)
        total_output = (author_tokens.get("output_tokens", 0) or 0) + (critic_tokens.get("output_tokens", 0) or 0)
        state.token_usage["input_tokens"] = state.token_usage.get("input_tokens", 0) + total_input
        state.token_usage["output_tokens"] = state.token_usage.get("output_tokens", 0) + total_output
        if state.token_usage.get("input_tokens", 0) + state.token_usage.get("output_tokens", 0) >= settings.token_budget:
            state.termination_reason = "token_budget"
            return True, "token_budget"

    return False, "continue"


def build_result(
    *,
    query_sql: str,
    rationale: str,
    critic_score: float,
    debate_transcript: dict[str, Any],
    author_confidence: float,
    termination_reason: TerminationReason,
    rounds: int,
) -> DebateResult:
    final_confidence = 0.4 * author_confidence + 0.6 * critic_score
    return DebateResult(
        query_sql=query_sql,
        rationale=rationale,
        critic_score=critic_score,
        debate_transcript=debate_transcript,
        final_confidence=final_confidence,
        author_confidence=author_confidence,
        termination_reason=termination_reason,
        rounds=rounds,
    )
