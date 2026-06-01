"""add datasets table

Revision ID: a1b2c3d4e5f6
Revises: 7c3f2a91e6e4
Create Date: 2026-06-01 12:30:00.000000

"""
from collections.abc import Sequence
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "7c3f2a91e6e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", postgresql.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(), nullable=False),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("table_name", sa.String(128), nullable=False),
        sa.Column("dialect", sa.String(20), nullable=False),
        sa.Column("columns_json", postgresql.JSONB(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("file_content", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_datasets_session_id"), "datasets", ["session_id"])

def downgrade() -> None:
    op.drop_index(op.f("ix_datasets_session_id"), table_name="datasets")
    op.drop_table("datasets")
