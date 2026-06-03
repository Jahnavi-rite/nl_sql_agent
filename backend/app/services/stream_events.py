from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any


AGENT_NAMES = [
    "intent_analyst",
    "schema_designer",
    "query_author",
    "test_executor",
    "critic",
    "debate",
]

PHASE_START = "start"
PHASE_PROGRESS = "progress"
PHASE_PARTIAL_OUTPUT = "partial_output"
PHASE_ARTIFACT = "artifact_generated"
PHASE_WARNING = "warning"
PHASE_ERROR = "error"
PHASE_COMPLETE = "complete"


@dataclass
class AgentEvent:
    agent: str
    phase: str
    timestamp: float = field(default_factory=time.time)
    partial_text: str | None = None
    artifact: dict[str, Any] | None = None
    progress_percent: float | None = None
    status: str = ""
    request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        base = asdict(self)
        base["type"] = "event"
        return base


def make_start(agent: str, status: str = "", request_id: str = "") -> AgentEvent:
    return AgentEvent(
        agent=agent,
        phase=PHASE_START,
        status=status or f"{agent.replace('_', ' ').title()} started",
        request_id=request_id,
        progress_percent=0.0,
    )


def make_progress(
    agent: str,
    progress_percent: float,
    partial_text: str | None = None,
    status: str = "",
    request_id: str = "",
) -> AgentEvent:
    return AgentEvent(
        agent=agent,
        phase=PHASE_PROGRESS,
        progress_percent=progress_percent,
        partial_text=partial_text,
        status=status or f"{agent.replace('_', ' ').title()} working...",
        request_id=request_id,
    )


def make_partial_output(
    agent: str,
    partial_text: str,
    status: str = "",
    request_id: str = "",
) -> AgentEvent:
    return AgentEvent(
        agent=agent,
        phase=PHASE_PARTIAL_OUTPUT,
        partial_text=partial_text,
        status=status or f"{agent.replace('_', ' ').title()} produced output",
        request_id=request_id,
    )


def make_artifact(
    agent: str,
    artifact: dict[str, Any],
    status: str = "",
    request_id: str = "",
) -> AgentEvent:
    return AgentEvent(
        agent=agent,
        phase=PHASE_ARTIFACT,
        artifact=artifact,
        status=status or f"{agent.replace('_', ' ').title()} generated artifact",
        request_id=request_id,
    )


def make_warning(
    agent: str,
    partial_text: str,
    status: str = "",
    request_id: str = "",
) -> AgentEvent:
    return AgentEvent(
        agent=agent,
        phase=PHASE_WARNING,
        partial_text=partial_text,
        status=status or f"Warning from {agent.replace('_', ' ').title()}",
        request_id=request_id,
    )


def make_error(
    agent: str,
    partial_text: str,
    status: str = "",
    request_id: str = "",
) -> AgentEvent:
    return AgentEvent(
        agent=agent,
        phase=PHASE_ERROR,
        partial_text=partial_text,
        status=status or f"Error in {agent.replace('_', ' ').title()}",
        request_id=request_id,
    )


def make_complete(
    agent: str,
    artifact: dict[str, Any] | None = None,
    status: str = "",
    request_id: str = "",
) -> AgentEvent:
    return AgentEvent(
        agent=agent,
        phase=PHASE_COMPLETE,
        artifact=artifact,
        status=status or f"{agent.replace('_', ' ').title()} completed",
        request_id=request_id,
        progress_percent=100.0,
    )
