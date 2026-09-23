"""rebuild_request

Revision ID: f3b4c6e1a203
Revises: e2a3b5d0f102
Create Date: 2026-09-21 14:10:00.000000

Phase 3a.5 (docs/phase3-field-selection-design.md §2.2): "This table is why
the UI stays simple. A rebuild takes minutes; a web request must not." A
caller writes a queued row and returns immediately; `flowbi transform` drains
it.

One correction to the design doc, made here rather than left as silent drift
(trust reality, fix the doc in the same change): the doc's §2.2
DDL omits instance_id, but `version` is scoped per instance (§2.1's
resolution of open question #5, src/openflowbi/fields/selection.py) — a
version number alone doesn't say which instance's selection it names.

Column shapes here must match src/openflowbi/ops/tables.py exactly (that
module is migrations/env.py's target_metadata for flowbi_ops).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3b4c6e1a203"
down_revision: str | Sequence[str] | None = "e2a3b5d0f102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rebuild_request",
        sa.Column("request_id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("rows_written", sa.BigInteger()),
        sa.Column("error", sa.Text()),
        schema="flowbi_ops",
    )


def downgrade() -> None:
    op.drop_table("rebuild_request", schema="flowbi_ops")
