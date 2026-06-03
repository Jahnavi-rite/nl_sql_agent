"""add is_manual_edit column to iterations

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-03 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column(
        "iterations",
        sa.Column("is_manual_edit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

def downgrade() -> None:
    op.drop_column("iterations", "is_manual_edit")
