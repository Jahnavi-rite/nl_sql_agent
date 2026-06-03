"""add missing status and approved_iteration_id to requests

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-03 10:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column(
        "requests",
        sa.Column("status", sa.String(30), nullable=False, server_default="open"),
    )
    op.add_column(
        "requests",
        sa.Column("approved_iteration_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_requests_status", "requests", ["status"])

def downgrade() -> None:
    op.drop_index("ix_requests_status", table_name="requests")
    op.drop_column("requests", "approved_iteration_id")
    op.drop_column("requests", "status")
