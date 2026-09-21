"""field_discovery

Revision ID: b4a3b4638006
Revises: c44c33c8584c
Create Date: 2026-09-21 10:42:48.900068

Phase 3a.1 (docs/phase3-field-selection-design.md §2.1): field_definition
("what exists", refreshed every run from /field) and field_stats ("what we
measured", recomputed by `flowbi fields discover`). Column shapes here must
match src/openflowbi/ops/tables.py exactly (that module is
migrations/env.py's target_metadata) — confirmed by autogenerate detecting
only these two added tables, no drift elsewhere.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b4a3b4638006"
down_revision: str | Sequence[str] | None = "c44c33c8584c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "field_definition",
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("field_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_custom", sa.Boolean(), nullable=False),
        sa.Column("schema_type", sa.Text()),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("disappeared_at", sa.DateTime(timezone=True)),
        schema="flowbi_ops",
    )

    op.create_table(
        "field_stats",
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("field_id", sa.Text(), primary_key=True),
        sa.Column("sampled_issues", sa.Integer(), nullable=False),
        sa.Column("non_null_count", sa.Integer(), nullable=False),
        sa.Column("fill_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("distinct_count", sa.Integer()),
        sa.Column("project_count", sa.Integer()),
        sa.Column("sample_values", postgresql.JSONB()),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="flowbi_ops",
    )


def downgrade() -> None:
    op.drop_table("field_stats", schema="flowbi_ops")
    op.drop_table("field_definition", schema="flowbi_ops")
