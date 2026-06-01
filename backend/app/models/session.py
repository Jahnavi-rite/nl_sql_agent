"""Persistence models for sessions, requests, iterations, feedback, and agent traces."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import (
    Dialect,
    FeedbackAction,
    IterationStatus,
    RequestStatus,
    SessionStatus,
)
from app.models.types import JSONBCompat, UUIDCompat


class Session(UUIDMixin, TimestampMixin, Base):
    """Top-level container for a user's NL→SQL conversation.

    Tracks user identity, dialect preference, session lifecycle,
    and sandbox allocation.
    """

    __tablename__ = "sessions"

    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dialect: Mapped[Dialect] = mapped_column(
        String(20), nullable=False, default=Dialect.POSTGRESQL
    )
    status: Mapped[SessionStatus] = mapped_column(
        String(20), nullable=False, default=SessionStatus.ACTIVE, index=True
    )
    sandbox_container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sandbox_image: Mapped[str | None] = mapped_column(String(256), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONBCompat, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- relationships (cascade delete all children) ---
    datasets: Mapped[list[Dataset]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Dataset.created_at",
    )
    requests: Mapped[list[Request]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Request.created_at",
    )

    __table_args__ = (
        Index("ix_sessions_user_status", "user_id", "status"),
    )


MAX_ITERATIONS = 5


class Request(UUIDMixin, TimestampMixin, Base):
    """A single natural-language question from the user within a session."""

    __tablename__ = "requests"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[dict[str, Any] | None] = mapped_column(JSONBCompat, nullable=True)
    status: Mapped[RequestStatus] = mapped_column(
        String(30), nullable=False, default=RequestStatus.OPEN, index=True
    )
    approved_iteration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), nullable=True
    )

    # --- relationships ---
    session: Mapped[Session] = relationship(back_populates="requests")
    iterations: Mapped[list[Iteration]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Iteration.attempt_number",
    )

    __table_args__ = (
        Index("ix_requests_session_created", "session_id", "created_at"),
    )


class Iteration(UUIDMixin, TimestampMixin, Base):
    """One SQL generation attempt for a given request.

    Stores the generated SQL, validation/execution results, confidence
    scores, and critic feedback.  Multiple iterations per request
    represent the agent's retry/refine loop.
    """

    __tablename__ = "iterations"

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[IterationStatus] = mapped_column(
        String(20), nullable=False, default=IterationStatus.PENDING
    )
    generated_sql: Mapped[str] = mapped_column(Text, nullable=False)
    redacted_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_ddl: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed_dml: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONBCompat, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    critic_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    critic_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_passed: Mapped[bool | None] = mapped_column(nullable=True)
    validation_reasons: Mapped[list[str] | None] = mapped_column(JSONBCompat, nullable=True)
    explain_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONBCompat, nullable=True)
    execution_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- relationships ---
    request: Mapped[Request] = relationship(back_populates="iterations")
    feedbacks: Mapped[list[Feedback]] = relationship(
        back_populates="iteration",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Feedback.created_at",
    )
    traces: Mapped[list[AgentTrace]] = relationship(
        back_populates="iteration",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AgentTrace.created_at",
    )

    __table_args__ = (
        Index("ix_iterations_request_attempt", "request_id", "attempt_number", unique=True),
        Index("ix_iterations_status", "status"),
    )


class Feedback(UUIDMixin, TimestampMixin, Base):
    """User feedback on a specific iteration (approve / reject / edit)."""

    __tablename__ = "feedbacks"

    iteration_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("iterations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[FeedbackAction] = mapped_column(String(20), nullable=False)
    edited_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- relationships ---
    iteration: Mapped[Iteration] = relationship(back_populates="feedbacks")

    __table_args__ = (
        Index("ix_feedbacks_iteration_action", "iteration_id", "action"),
    )


class Dataset(UUIDMixin, TimestampMixin, Base):
    """A user-uploaded dataset (CSV/Excel) associated with a session."""

    __tablename__ = "datasets"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dialect: Mapped[str] = mapped_column(String(20), nullable=False)
    columns_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONBCompat, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    session: Mapped[Session] = relationship(back_populates="datasets")


class AgentTrace(UUIDMixin, TimestampMixin, Base):
    """Audit log of every LLM call the agent makes.

    Stores the full prompt/response pair, token counts, and wall-clock
    latency so we can debug agent behaviour and track costs.
    """

    __tablename__ = "agent_traces"

    iteration_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("iterations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONBCompat, nullable=True)

    # --- relationships ---
    iteration: Mapped[Iteration] = relationship(back_populates="traces")

    __table_args__ = (
        Index("ix_agent_traces_iteration", "iteration_id", "created_at"),
        Index("ix_agent_traces_agent", "agent_name"),
    )
