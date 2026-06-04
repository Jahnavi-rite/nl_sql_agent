from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TerminationReason = Literal["approved", "deadlock", "token_budget", "max_rounds", "no_sql"]


@dataclass
class DebateSettings:
    max_rounds: int = 3
    token_budget: int = 6_000
    approval_threshold: float = 0.75


@dataclass
class DebateTurn:
    speaker: Literal["DebateAuthor", "DebateCritic"]
    prompt: str
    content: str
    latency_ms: float
    token_usage: dict[str, int] = field(default_factory=dict)


@dataclass
class TranscriptEntry:
    speaker: Literal["DebateAuthor", "DebateCritic"]
    timestamp: float
    round_number: int
    content: str
    sql_candidate: str = ""
    query_hash: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    objections: list[str] = field(default_factory=list)
    rationale: str = ""
    confidence: float | None = None
    approved: bool | None = None
    token_usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "timestamp": self.timestamp,
            "round_number": self.round_number,
            "content": self.content,
            "sql_candidate": self.sql_candidate,
            "query_hash": self.query_hash,
            "scores": self.scores,
            "objections": self.objections,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "approved": self.approved,
            "token_usage": self.token_usage,
            "latency_ms": self.latency_ms,
        }


@dataclass
class DebateResult:
    query_sql: str
    rationale: str
    critic_score: float
    debate_transcript: dict[str, Any]
    final_confidence: float
    author_confidence: float
    termination_reason: TerminationReason
    rounds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_sql": self.query_sql,
            "rationale": self.rationale,
            "critic_score": self.critic_score,
            "debate_transcript": self.debate_transcript,
            "final_confidence": self.final_confidence,
            "author_confidence": self.author_confidence,
            "termination_reason": self.termination_reason,
            "rounds": self.rounds,
        }
