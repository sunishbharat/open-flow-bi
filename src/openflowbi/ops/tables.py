"""SQLAlchemy Core table definitions for flowbi_ops.

Alembic owns this schema exclusively — dlt owns jira_raw and must never be
touched from here (docs/phase2-postgres-design.md P2-D1/§5). Core, not ORM (library decision:
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

# Phase 3a (docs/phase3-field-selection-design.md §2.1) — "WHAT EXISTS,
# refreshed every run from /field". Trimmed from the design doc's fuller
# column list (schema_items, custom_type, clause_names) — nothing in 3a.1
# reads them yet, and an always-null column is worse than not having it;
# add them back alongside whichever later milestone actually needs one.
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

# "WHAT WE MEASURED, recomputed by `flowbi fields discover`" — §2.1.
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

# "WHAT WE DECIDED (append-only — this IS the audit trail)" — §2.1, Phase 3a.2.
# save_selection() never UPDATEs a row: each save writes a full new-version
# snapshot (every currently-selected field, live or deprecated, with the
# requested changes applied), so `WHERE version = max(version)` is always a
# complete, self-consistent view.
#
# Resolves an ambiguity the design doc leaves open: version numbers here are
# scoped per instance_id (each instance's own sequence starts at 1), not one
# global counter shared across every Jira instance ever connected — simpler,
# and this codebase only has one instance in practice (design doc §14 open
# question #5). The unique constraint below holds either way.
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
    sa.Column("created_by", sa.Text(), nullable=False),  # 'cli:<os user>' until 3b adds a JWT
    sa.Column("note", sa.Text()),
    sa.UniqueConstraint("version", "instance_id", "field_name", "schema_type"),
)

# Phase 3a.5 (docs/phase3-field-selection-design.md §2.2) — "This table is why
# the UI stays simple. A rebuild takes minutes; a web request must not."
#
# One correction to the design doc: it omits instance_id, but `version` is
# scoped per instance (field_selection above, §14 open question #5's
# resolution) — a version number alone doesn't say which instance's selection
# it refers to, so instance_id is added here (trust reality, fix
# the doc in the same change).
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
