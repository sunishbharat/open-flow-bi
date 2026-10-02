"""SQL assertions for whole-table invariants, run against Postgres.

pandera (quality/checks.py) validates row
shape from a bounded sample; it is the wrong tool for "is this unique across
25 million rows" — that's a `GROUP BY ... HAVING count(*) > 1` and the
database is better at it than any frame library. SQLAlchemy Core (arrives
with Alembic, no new dependency), not the ORM.

BLOCKING checks must all return 0 — a non-zero result is what "the quality
gate has teeth" means: `flowbi quality check --destination postgres`
exits 1 and records `sync_run.status = 'quality_failed'`. ALERTING checks are
informational only and never fail the gate.

Not scoped by instance_id: these are whole-table checks, not per-instance
ones. A multi-instance deployment would need scoping here; until one exists,
the checks stay as simple as possible rather than guessing at it.
"""

from dataclasses import dataclass, field

import sqlalchemy as sa

# name -> SQL returning a single count; must be 0 to pass.
BLOCKING: dict[str, str] = {
    "issues_pk_unique": (
        "SELECT count(*) FROM (SELECT instance_id, issue_id FROM jira_raw.issues "
        "GROUP BY 1, 2 HAVING count(*) > 1) d"
    ),
    "changelog_pk_unique": (
        "SELECT count(*) FROM (SELECT instance_id, issue_id, history_id, item_index "
        "FROM jira_raw.issue_changelog GROUP BY 1, 2, 3, 4 HAVING count(*) > 1) d"
    ),
    # Scoped to the whole table, not one load package: simpler.
    # Documented consequence, not a bug to work around: this is non-zero
    # until every changelog issue has a matching row in `issues` — run
    # `extract issues` before `extract changelog` for a given project (see
    # docs/cli.md), rather than weakening the check to tolerate the gap.
    "no_orphan_changelog": (
        "SELECT count(*) FROM jira_raw.issue_changelog c "
        "LEFT JOIN jira_raw.issues i USING (instance_id, issue_id) "
        "WHERE i.issue_id IS NULL"
    ),
    "issue_id_not_null": "SELECT count(*) FROM jira_raw.issues WHERE issue_id IS NULL",
}

# name -> SQL. Reported alongside BLOCKING, never fails the gate.
#
# Completeness is recorded per issue in jira_raw.issue_changelog_status
# (flatten.changelog_status). The earlier version of this check counted
# changelog_complete=false item rows, and could only ever return 0: an
# incomplete issue has no item rows.
ALERTING: dict[str, str] = {
    "changelog_incomplete": (
        "SELECT count(*) FROM jira_raw.issue_changelog_status WHERE NOT changelog_complete"
    ),
}

# Which BLOCKING checks apply to which `flowbi quality check <table>` target.
CHECKS_BY_TABLE: dict[str, tuple[str, ...]] = {
    "issues": ("issues_pk_unique", "issue_id_not_null"),
    "issue_changelog": ("changelog_pk_unique", "no_orphan_changelog"),
}

# Which ALERTING checks apply to which target. Scoped the same way as
# CHECKS_BY_TABLE (not run unconditionally for every table) so `quality check
# issues` on a checkout that has never run `extract changelog` doesn't hit
# `UndefinedTable` on jira_raw.issue_changelog.
ALERTING_BY_TABLE: dict[str, tuple[str, ...]] = {
    "issues": (),
    "issue_changelog": ("changelog_incomplete",),
}

# table -> flowbi_ops.sync_run.mode value (ops/tables.py: 'issues' | 'changelog' | 'fields').
TABLE_MODE: dict[str, str] = {"issues": "issues", "issue_changelog": "changelog"}


@dataclass
class QualityResult:
    blocking: dict[str, int] = field(default_factory=dict)
    alerting: dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(count == 0 for count in self.blocking.values())

    @property
    def failing(self) -> list[str]:
        return [name for name, count in self.blocking.items() if count]


def run_checks(dsn: str, table: str) -> QualityResult:
    """Run every BLOCKING check registered for `table`, plus all ALERTING checks."""
    if table not in CHECKS_BY_TABLE:
        raise ValueError(f"no SQL checks registered for table {table!r}")

    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as conn:
            blocking = {
                name: conn.execute(sa.text(BLOCKING[name])).scalar_one()
                for name in CHECKS_BY_TABLE[table]
            }
            alerting = {
                name: conn.execute(sa.text(ALERTING[name])).scalar_one()
                for name in ALERTING_BY_TABLE.get(table, ())
            }
        return QualityResult(blocking=blocking, alerting=alerting)
    finally:
        engine.dispose()
