"""Populate flowbi_ops.issue_dirty from a completed Postgres load.

docs/phase2-postgres-design.md §8: "issue_dirty is populated in Phase 2 (from
the issue ids in each load package, read off load_info) even though nothing
consumes it until Phase 3." dlt's `LoadInfo` carries load ids and per-table
job metadata, but no row-level primary keys — there is no public dlt API to
read "which issue_ids did this load touch" directly off it. The practical
reading of "off load_info" is therefore: use `LoadInfo.loads_ids` to read
back, from the `jira_raw` table dlt just wrote to, exactly the rows stamped
with those load ids (`_dlt_load_id`) — which, under merge write disposition,
is exactly the set of rows this run actually loaded (existing untouched rows
keep their older `_dlt_load_id` and are correctly excluded).

SQLAlchemy Core, not the ORM (library decision register) — arrives
free with Alembic, no new dependency.
"""

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from openflowbi.ops.tables import issue_dirty

logger = structlog.get_logger(__name__)

# dlt resource name -> reason recorded in issue_dirty. Only resources that
# carry issue-level rows participate; `fields` has no issue_id and never
# marks anything dirty.
DIRTY_REASONS = {
    "issues": "issues_extracted",
    "issue_changelog": "changelog_extracted",
}


def mark_dirty(dsn: str, instance_id: str, table: str, load_ids: list[str], reason: str) -> int:
    """Upsert (instance_id, issue_id) pairs touched by `load_ids` into flowbi_ops.issue_dirty.

    `table` is always one of DIRTY_REASONS' keys (our own constants, never
    user input), so building the identifier by f-string is safe here — dlt
    owns `jira_raw` and this only ever reads from it (P2-D1: "if dlt created
    it, only dlt changes it" — a SELECT is not a change).
    """
    if not load_ids:
        return 0
    engine = sa.create_engine(dsn)
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    f'SELECT DISTINCT issue_id FROM "jira_raw"."{table}" '  # noqa: S608
                    "WHERE instance_id = :instance_id AND _dlt_load_id = ANY(:load_ids)"
                ),
                {"instance_id": instance_id, "load_ids": load_ids},
            ).fetchall()
            issue_ids = [row[0] for row in rows]
            if not issue_ids:
                return 0
            stmt = pg_insert(issue_dirty).values(
                [
                    {"instance_id": instance_id, "issue_id": issue_id, "reason": reason}
                    for issue_id in issue_ids
                ]
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["instance_id", "issue_id"],
                set_={"reason": stmt.excluded.reason, "marked_at": sa.func.now()},
            )
            conn.execute(stmt)
        return len(issue_ids)
    finally:
        engine.dispose()
