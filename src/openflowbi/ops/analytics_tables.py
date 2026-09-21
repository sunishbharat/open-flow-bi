"""SQLAlchemy Core table definitions for the `analytics` schema.

Phase 3a.3/3a.4 (docs/phase3-field-selection-design.md §2.3). Only
`issue_status_interval` lives here, and only that table — its schema is fully
fixed. `analytics.issue` is deliberately *not* represented as a Core Table:
its promoted columns are added at runtime by `transform/runner.py` from the
live `field_selection`, not by a migration, and a Core Table object can't
represent "these columns are fixed, the rest are whatever's currently
promoted" without lying to anything that reads it (Alembic autogenerate,
most of all).

For the same reason this module's metadata is NOT wired into
migrations/env.py's target_metadata — `alembic revision --autogenerate`
would otherwise try to "fix" analytics.issue's dynamic columns it has no way
to know about. Both `analytics` tables' migrations are hand-written, the same
way migrations/versions/c44c33c8584c_analytics_schema.py already is.
"""

import sqlalchemy as sa

metadata = sa.MetaData(schema="analytics")

issue_status_interval = sa.Table(
    "issue_status_interval",
    metadata,
    sa.Column("instance_id", sa.Text(), primary_key=True),
    sa.Column("issue_id", sa.BigInteger(), primary_key=True),
    sa.Column("seq", sa.Integer(), primary_key=True),
    sa.Column("status_id", sa.Text()),
    sa.Column("status_name", sa.Text()),
    sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("exited_at", sa.DateTime(timezone=True)),  # NULL = still the current status
    sa.Column("duration_seconds", sa.Numeric()),  # NULL while exited_at IS NULL
)
