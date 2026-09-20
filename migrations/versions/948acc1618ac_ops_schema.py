"""ops schema

Revision ID: 948acc1618ac
Revises:
Create Date: 2026-09-20 13:53:47.480586

Creates flowbi_ops and its four control-plane tables
(docs/phase2-postgres-design.md §8). The schema is created here, in the same
transaction as the tables and before Alembic's own version-table write, since
version_table_schema="flowbi_ops" (migrations/env.py) needs the schema to
already exist by the time Alembic stamps this revision.

Column shapes here must match src/openflowbi/ops/tables.py exactly (that
module is migrations/env.py's target_metadata) — a mismatch is exactly what
`alembic revision --autogenerate` would immediately flag as spurious drift.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "948acc1618ac"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS flowbi_ops")

    op.create_table(
        "jira_instance",
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("deployment", sa.Text(), nullable=False),
        sa.Column("account_tz", sa.Text()),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="flowbi_ops",
    )

    op.create_table(
        "sync_run",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text()),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("pipeline_name", sa.Text(), nullable=False),
        sa.Column("dlt_load_ids", postgresql.ARRAY(sa.Text())),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rows_loaded", sa.BigInteger()),
        sa.Column("api_calls", sa.Integer()),
        sa.Column("budget_spent", sa.Integer()),
        sa.Column("error", sa.Text()),
        schema="flowbi_ops",
    )

    op.create_table(
        "rate_budget",
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("points_spent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("remaining_hint", sa.Integer()),
        sa.Column("near_limit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="flowbi_ops",
    )

    op.create_table(
        "issue_dirty",
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("issue_id", sa.BigInteger(), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "marked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="flowbi_ops",
    )


def downgrade() -> None:
    op.drop_table("issue_dirty", schema="flowbi_ops")
    op.drop_table("rate_budget", schema="flowbi_ops")
    op.drop_table("sync_run", schema="flowbi_ops")
    op.drop_table("jira_instance", schema="flowbi_ops")
    op.execute("DROP SCHEMA IF EXISTS flowbi_ops CASCADE")
