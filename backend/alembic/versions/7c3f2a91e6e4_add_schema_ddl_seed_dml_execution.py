"""add schema_ddl seed_dml execution_results to iterations

Revision ID: 7c3f2a91e6e4
Revises: 5b41f798d01d
Create Date: 2026-06-01 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7c3f2a91e6e4"
down_revision: str | Sequence[str] | None = "5b41f798d01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("iterations", sa.Column("schema_ddl", sa.Text, nullable=True))
    op.add_column("iterations", sa.Column("seed_dml", sa.Text, nullable=True))
    op.add_column("iterations", sa.Column("execution_results", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("iterations", "execution_results")
    op.drop_column("iterations", "seed_dml")
    op.drop_column("iterations", "schema_ddl")
