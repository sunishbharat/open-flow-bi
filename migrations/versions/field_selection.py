"""field_selection

Revision ID: 8ac3badd15cb
Revises: b4a3b4638006
Create Date: 2026-09-21 11:02:02.767545

Phase 3a.2 (docs/phase3-field-selection-design.md §2.1): "WHAT WE DECIDED
(append-only — this IS the audit trail)". Column shapes here must match
src/openflowbi/ops/tables.py exactly (that module is migrations/env.py's
target_metadata) — confirmed by autogenerate detecting only this one added
table, no drift elsewhere.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ac3badd15cb"
down_revision: str | Sequence[str] | None = "b4a3b4638006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "field_selection",
        sa.Column("selection_id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("field_name", sa.Text(), nullable=False),
        sa.Column("schema_type", sa.Text(), nullable=False),
        sa.Column("field_id", sa.Text()),
        sa.Column("promote", sa.Boolean(), nullable=False),
        sa.Column("column_name", sa.Text()),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.UniqueConstraint("version", "instance_id", "field_name", "schema_type"),
        schema="flowbi_ops",
    )


def downgrade() -> None:
    op.drop_table("field_selection", schema="flowbi_ops")
