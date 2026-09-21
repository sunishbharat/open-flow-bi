"""issue_status_interval

Revision ID: d1f2a4c9e001
Revises: 8ac3badd15cb
Create Date: 2026-09-21 14:00:00.000000

Phase 3a.3 (docs/phase3-field-selection-design.md §2.3, §4). One row per
(issue, status period), seq=0 always seeded from jira_raw.issues.created_at —
the changelog only records transitions, never the initial state. Rebuilt
wholesale (delete+reinsert per affected issue) by
src/openflowbi/transform/runner.py, never partially updated in place.

Column shapes here must match src/openflowbi/ops/analytics_tables.py exactly,
though that module is deliberately NOT part of migrations/env.py's
target_metadata (see its own docstring) — so, unlike the flowbi_ops
migrations, this one is not autogenerate-checked against it and the two must
be kept in sync by hand.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f2a4c9e001"
down_revision: str | Sequence[str] | None = "8ac3badd15cb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issue_status_interval",
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("issue_id", sa.BigInteger(), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("status_id", sa.Text()),
        sa.Column("status_name", sa.Text()),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exited_at", sa.DateTime(timezone=True)),
        sa.Column("duration_seconds", sa.Numeric()),
        schema="analytics",
    )
    op.create_index(
        "ix_issue_status_interval_status",
        "issue_status_interval",
        ["instance_id", "status_id"],
        schema="analytics",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_issue_status_interval_status", "issue_status_interval", schema="analytics"
    )
    op.drop_table("issue_status_interval", schema="analytics")
