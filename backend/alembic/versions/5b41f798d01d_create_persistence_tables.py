"""create persistence tables

Revision ID: 5b41f798d01d
Revises:
Create Date: 2026-05-29 23:07:14.294524

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b41f798d01d"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- sessions ----
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("dialect", sa.String(20), nullable=False, server_default="postgresql"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("sandbox_container_id", sa.String(128), nullable=True),
        sa.Column("sandbox_image", sa.String(256), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB, nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_status", "sessions", ["status"])
    op.create_index("ix_sessions_user_status", "sessions", ["user_id", "status"])

    # ---- requests ----
    op.create_table(
        "requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("context_json", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_requests_session_id", "requests", ["session_id"])
    op.create_index("ix_requests_session_created", "requests", ["session_id", "created_at"])

    # ---- iterations ----
    op.create_table(
        "iterations",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("generated_sql", sa.Text, nullable=False),
        sa.Column("redacted_sql", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("rationale", sa.Text, nullable=True),
        sa.Column("critic_score", sa.Float, nullable=True),
        sa.Column("critic_notes", sa.Text, nullable=True),
        sa.Column("validation_passed", sa.Boolean, nullable=True),
        sa.Column("validation_reasons", postgresql.JSONB, nullable=True),
        sa.Column("explain_plan", postgresql.JSONB, nullable=True),
        sa.Column("execution_rows", sa.Integer, nullable=True),
        sa.Column("execution_ms", sa.Float, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_iterations_request_id", "iterations", ["request_id"])
    op.create_index("ix_iterations_request_attempt", "iterations", ["request_id", "attempt_number"], unique=True)
    op.create_index("ix_iterations_status", "iterations", ["status"])

    # ---- feedbacks ----
    op.create_table(
        "feedbacks",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("iteration_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("iterations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("edited_sql", sa.Text, nullable=True),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_feedbacks_iteration_id", "feedbacks", ["iteration_id"])
    op.create_index("ix_feedbacks_iteration_action", "feedbacks", ["iteration_id", "action"])

    # ---- agent_traces ----
    op.create_table(
        "agent_traces",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("iteration_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("iterations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_name", sa.String(64), nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("response", sa.Text, nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("latency_ms", sa.Float, nullable=True),
        sa.Column("metadata_json", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_traces_iteration_id", "agent_traces", ["iteration_id"])
    op.create_index("ix_agent_traces_iteration", "agent_traces", ["iteration_id", "created_at"])
    op.create_index("ix_agent_traces_agent", "agent_traces", ["agent_name"])


def downgrade() -> None:
    op.drop_table("agent_traces")
    op.drop_table("feedbacks")
    op.drop_table("iterations")
    op.drop_table("requests")
    op.drop_table("sessions")
