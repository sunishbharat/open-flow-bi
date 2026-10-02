"""SQLAlchemy Core table definitions for flowbi_ops.

Alembic owns this schema exclusively — dlt owns jira_raw and must never be
touched from here: if dlt created it, only dlt changes it. SQLAlchemy Core,
not the ORM: it arrives with Alembic, and nothing here needs more.

This module is also migrations/env.py's target_metadata: the shapes defined
here and the DDL in migrations/versions/ops_schema.py must match exactly,
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

# Issues a load touched, queued for the next incremental `flowbi transform`.
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

# What exists: refreshed every run from /field. Deliberately trimmed: Jira's
# schema_items, custom_type and clause_names aren't stored, since nothing
# reads them yet and an always-null column is worse than not having it; add
# one back alongside whatever first needs it.
field_definition = sa.Table(
    "field_definition",
    metadata,
    sa.Column("instance_id", sa.Text(), primary_key=True),
    sa.Column("field_id", sa.Text(), primary_key=True),  # 'customfield_10016' | 'duedate'
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("is_custom", sa.Boolean(), nullable=False),
    sa.Column("schema_type", sa.Text()),  # number|string|date|datetime|option|array|user
    sa.Column(
        "first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column(
        "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column("disappeared_at", sa.DateTime(timezone=True)),  # set when it stops appearing
)

# What we measured: recomputed by `flowbi fields discover`.
field_stats = sa.Table(
    "field_stats",
    metadata,
    sa.Column("instance_id", sa.Text(), primary_key=True),
    sa.Column("field_id", sa.Text(), primary_key=True),
    sa.Column("sampled_issues", sa.Integer(), nullable=False),
    sa.Column("non_null_count", sa.Integer(), nullable=False),
    sa.Column("fill_rate", sa.Numeric(5, 4), nullable=False),  # the number the decision hinges on
    sa.Column("distinct_count", sa.Integer()),
    sa.Column("project_count", sa.Integer()),
    sa.Column("sample_values", postgresql.JSONB()),  # up to 10, for the preview
    sa.Column(
        "computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)

# What we decided: append-only, so this table is the audit trail.
# save_selection() never UPDATEs a row: each save writes a full new-version
# snapshot (every currently-selected field, live or deprecated, with the
# requested changes applied), so `WHERE version = max(version)` is always a
# complete, self-consistent view.
#
# Version numbers are scoped per instance_id (each instance's own sequence
# starts at 1), not one global counter shared across every Jira instance ever
# connected: simpler, and a deployment usually has one instance. The unique
# constraint below holds either way.
field_selection = sa.Table(
    "field_selection",
    metadata,
    sa.Column("selection_id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("instance_id", sa.Text(), nullable=False),
    sa.Column("field_name", sa.Text(), nullable=False),  # keyed by NAME: ids are per-instance
    sa.Column("schema_type", sa.Text(), nullable=False),
    sa.Column("field_id", sa.Text()),  # explicit override when the name is ambiguous
    sa.Column("promote", sa.Boolean(), nullable=False),
    sa.Column("column_name", sa.Text()),  # snake_case target
    sa.Column("target", sa.Text(), nullable=False),  # 'column' | 'bridge_table'
    sa.Column("deprecated_at", sa.DateTime(timezone=True)),  # set when demoted; the row survives
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column("created_by", sa.Text(), nullable=False),  # 'cli:<os user>' for CLI writes
    sa.Column("note", sa.Text()),
    sa.UniqueConstraint("version", "instance_id", "field_name", "schema_type"),
)

# A queue, so a caller never runs a rebuild itself: a rebuild takes minutes,
# and `flowbi transform` drains the queue.
#
# Carries instance_id because `version` is scoped per instance (field_selection
# above): a version number alone doesn't say which instance's selection it
# refers to.
rebuild_request = sa.Table(
    "rebuild_request",
    metadata,
    sa.Column("request_id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("instance_id", sa.Text(), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("scope", sa.Text()),  # NULL = all projects, else a project key
    sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
    sa.Column(
        "requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column("requested_by", sa.Text(), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("rows_written", sa.BigInteger()),
    sa.Column("error", sa.Text()),
)
