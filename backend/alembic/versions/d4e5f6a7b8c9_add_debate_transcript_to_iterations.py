"""add debate transcript to iterations

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.models.types import JSONBCompat


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "iterations",
        sa.Column("debate_transcript_json", JSONBCompat(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("iterations", "debate_transcript_json")
