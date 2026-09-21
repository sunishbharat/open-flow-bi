"""analytics.issue

Revision ID: e2a3b5d0f102
Revises: d1f2a4c9e001
Create Date: 2026-09-21 14:05:00.000000

Phase 3a.4 (docs/phase3-field-selection-design.md §2.3). Fixed base columns
only — the wide table's promoted columns (story_points, business_unit, ...)
are added by `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` at rebuild time
(src/openflowbi/transform/runner.py), driven by the live field_selection, not
by a migration. That is the entire point of self-service field promotion: no
Alembic revision per newly promoted field.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a3b5d0f102"
down_revision: str | Sequence[str] | None = "d1f2a4c9e001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issue",
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("issue_id", sa.BigInteger(), primary_key=True),
        sa.Column("issue_key", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "rebuilt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="analytics",
    )


def downgrade() -> None:
    op.drop_table("issue", schema="analytics")
