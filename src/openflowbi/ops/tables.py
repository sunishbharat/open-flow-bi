"""SQLAlchemy Core table definitions for flowbi_ops.

Alembic owns this schema exclusively — dlt owns jira_raw and must never be
touched from here (docs/phase2-postgres-design.md P2-D1/§5, CLAUDE.md
non-negotiable rules). Core, not ORM (CLAUDE.md library-decision-register:
"SQL toolkit for locks / whole-table checks -> SQLAlchemy Core").

This module is also migrations/env.py's target_metadata: the shapes defined
here and the DDL in migrations/versions/0001_ops_schema.py must match exactly,
or `alembic revision --autogenerate` will propose a spurious diff against
itself the moment a real migration is added on top of this baseline.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData(schema="flowbi_ops")

jira_instance = sa.Table(
    "jira_instance",
    metadata,
    sa.Column("instance_id", sa.Text(), primary_key=True),
    sa.Column("base_url", sa.Text(), nullable=False),
    sa.Column("deployment", sa.Text(), nullable=False),  # 'cloud' | 'datacenter'
    sa.Column("account_tz", sa.Text()),  # asserted at startup; drift is a real bug source
    sa.Column(
        "first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column(
        "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)

sync_run = sa.Table(
    "sync_run",
    metadata,
    sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("instance_id", sa.Text(), nullable=False),
    sa.Column("project", sa.Text()),
    sa.Column("mode", sa.Text(), nullable=False),  # 'issues' | 'changelog' | 'fields'
    sa.Column("pipeline_name", sa.Text(), nullable=False),
    # join key back into jira_raw._dlt_loads — dlt's lineage, our verdict, one query.
    sa.Column("dlt_load_ids", postgresql.ARRAY(sa.Text())),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.Text(), nullable=False),  # running|succeeded|failed|quality_failed
    sa.Column("rows_loaded", sa.BigInteger()),
    sa.Column("api_calls", sa.Integer()),
    sa.Column("budget_spent", sa.Integer()),
    sa.Column("error", sa.Text()),
)

rate_budget = sa.Table(
    "rate_budget",
    metadata,
    sa.Column("instance_id", sa.Text(), primary_key=True),
    sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("points_spent", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("remaining_hint", sa.Integer()),  # last X-RateLimit-Remaining seen
    sa.Column("near_limit", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)

# Created now, consumed in Phase 3 (populating it later would be a migration
# nobody wants) — docs/phase2-postgres-design.md §8.
issue_dirty = sa.Table(
    "issue_dirty",
    metadata,
    sa.Column("instance_id", sa.Text(), primary_key=True),
    sa.Column("issue_id", sa.BigInteger(), primary_key=True),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column(
        "marked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)
