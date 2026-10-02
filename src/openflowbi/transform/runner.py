"""The transform runner behind `flowbi transform`.

Two independent things, both driven by `flowbi transform`:

1. Incremental/full rebuild of `analytics.issue_status_interval` and
   `analytics.issue`'s promoted columns, for a set of issue ids (`None` means
   every issue for the instance - "a full rebuild is the same SQL with the id
   set replaced by all", so there is exactly one code path for both).
2. Draining `flowbi_ops.rebuild_request` - a promotion needs existing rows
   backfilled for fields that didn't exist as columns before; that has
   nothing to do with new Jira data, so it only touches promoted columns,
   never intervals.

SQLAlchemy Core throughout, no ORM.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
import structlog

from openflowbi.fields.selection import (
    SelectionRow,
    current_selection,
    validate_field_id,
    validate_promoted_name,
)
from openflowbi.ops.tables import issue_dirty, rebuild_request
from openflowbi.pipeline.locks import transform_lock

logger = structlog.get_logger(__name__)

_SQL_DIR = Path(__file__).parent / "sql"

# schema_type -> Postgres column type for a promoted `column`-target field.
# Everything not listed here (string, option, user, priority, status,
# issuetype, resolution, and any other Jira object shape) becomes TEXT -
# see _extract_expr's COALESCE below for why one expression covers all of
# those without per-type special-casing.
_SQL_TYPE_BY_SCHEMA_TYPE = {"number": "NUMERIC", "date": "DATE", "datetime": "TIMESTAMPTZ"}
_CAST_TYPE_BY_SCHEMA_TYPE = {"number": "numeric", "date": "date", "datetime": "timestamptz"}

# How information_schema.columns.data_type spells each column type above.
_DATA_TYPE_BY_SQL_TYPE = {
    "NUMERIC": "numeric",
    "DATE": "date",
    "TIMESTAMPTZ": "timestamp with time zone",
    "TEXT": "text",
}


@functools.cache
def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text(encoding="utf-8")


@dataclass
class TransformResult:
    claimed: int = 0
    skipped_incomplete: int = 0
    interval_rows: int = 0
    issue_rows: int = 0
    bridge_rows: int = 0


@dataclass
class RebuildOutcome:
    request_id: int
    status: str
    rows_written: int | None = None
    error: str | None = None


def claim_dirty(conn: sa.Connection, instance_id: str) -> list[int]:
    """Claim-by-delete: a crash after this point re-queues nothing twice —
    the claimed ids only exist in this function's return value from here on.
    """
    result = conn.execute(
        sa.delete(issue_dirty)
        .where(issue_dirty.c.instance_id == instance_id)
        .returning(issue_dirty.c.issue_id)
    )
    return [row[0] for row in result.fetchall()]


def partition_incomplete(
    conn: sa.Connection, instance_id: str, issue_ids: list[int]
) -> tuple[list[int], list[int]]:
    """Split `issue_ids` into (changelog-complete, incomplete).

    Complete means jira_raw.issue_changelog_status says the changelog was
    extracted whole, for the issue's current version (extracted at or after
    the issue's `updated_at`). Anything else is incomplete: no status row
    (changelog never extracted), a partial extraction, or one older than the
    issue. Building intervals from those would seed seq=0 with the current
    status and produce a plausible, wrong time-in-status. Skipped issues keep their previous
    intervals and are re-marked dirty the next time `extract changelog`
    touches them, so nothing is lost, only delayed.
    """
    if not issue_ids:
        return [], []
    status_table = conn.execute(
        sa.text("SELECT to_regclass('jira_raw.issue_changelog_status')")
    ).scalar_one()
    if status_table is None:
        logger.warning("changelog_status_missing", hint="run `flowbi extract changelog`")
        return [], sorted(issue_ids)
    rows = conn.execute(
        sa.text(
            "SELECT s.issue_id FROM jira_raw.issue_changelog_status s "
            "LEFT JOIN jira_raw.issues i USING (instance_id, issue_id) "
            "WHERE s.instance_id = :instance_id AND s.issue_id = ANY(:issue_ids) "
            "AND s.changelog_complete "
            "AND (i.updated_at IS NULL OR s.updated_at >= i.updated_at)"
        ),
        {"instance_id": instance_id, "issue_ids": issue_ids},
    ).fetchall()
    complete = {row[0] for row in rows}
    return (
        [i for i in issue_ids if i in complete],
        sorted(i for i in issue_ids if i not in complete),
    )


def _issue_ids_for_project(
    conn: sa.Connection,
    instance_id: str,
    project: str | None,
    restrict_to: list[int] | None = None,
) -> list[int]:
    """Resolve a project key (None: every project) to a concrete issue_id
    list, optionally narrowed to `restrict_to`. Covers `--rebuild-all` with
    or without `--project` (restrict_to=None) and the incremental dirty-set
    narrowed by --project (restrict_to=the claimed ids) with one query.
    """
    rows = conn.execute(
        sa.text(
            "SELECT issue_id FROM jira_raw.issues WHERE instance_id = :instance_id "
            "AND (CAST(:project AS text) IS NULL OR fields -> 'project' ->> 'key' = :project) "
            "AND (CAST(:restrict_to AS bigint[]) IS NULL OR issue_id = ANY(:restrict_to))"
        ),
        {"instance_id": instance_id, "project": project, "restrict_to": restrict_to},
    ).fetchall()
    return [row[0] for row in rows]


def rebuild_intervals(conn: sa.Connection, instance_id: str, issue_ids: list[int] | None) -> int:
    """Full delete+reinsert per issue in scope, not an upsert-by-seq: a later
    run can produce fewer intervals than a prior one (e.g. corrected
    changelog data), and upserting by seq alone would leave stale trailing
    rows from a longer previous build.
    """
    if issue_ids is not None and not issue_ids:
        return 0
    conn.execute(
        sa.text(
            "DELETE FROM analytics.issue_status_interval WHERE instance_id = :instance_id "
            "AND (CAST(:issue_ids AS bigint[]) IS NULL OR issue_id = ANY(:issue_ids))"
        ),
        {"instance_id": instance_id, "issue_ids": issue_ids},
    )
    result = conn.execute(
        sa.text(_load_sql("issue_status_interval.sql")),
        {"instance_id": instance_id, "issue_ids": issue_ids},
    )
    return result.rowcount


def _extract_expr(field_id: str, schema_type: str) -> str:
    """SQL expression reading `field_id` out of jira_raw.issues.fields (jsonb)
    as the given schema_type. field_id/column_name are validated (caller) as
    safe identifiers before ever reaching an f-string - bind params can't
    parameterise a jsonb key any more than they can an identifier.

    Typed fields cast only values Postgres accepts, anything else becomes
    NULL. A hard cast let one malformed value abort the transaction that
    claimed issue_dirty, so every later run hit the same row and analytics
    stopped updating. _count_invalid
    reports what was dropped. pg_input_is_valid needs Postgres 16+.
    """
    cast_type = _CAST_TYPE_BY_SCHEMA_TYPE.get(schema_type)
    if cast_type is not None:
        raw = f"src.fields ->> '{field_id}'"
        return f"CASE WHEN pg_input_is_valid({raw}, '{cast_type}') THEN ({raw})::{cast_type} END"
    if schema_type == "string":
        return f"src.fields ->> '{field_id}'"
    # option/user/priority/status/issuetype/resolution/... - Jira's various
    # object shapes, handled by one COALESCE instead of per-type
    # special-casing: option fields carry .value, priority/status/issuetype/
    # resolution carry .name, user fields carry .displayName. The final
    # fallback handles a plain scalar under a schema_type this list has never
    # seen, rather than producing NULL for it.
    return (
        f"COALESCE(src.fields -> '{field_id}' ->> 'value', "
        f"src.fields -> '{field_id}' ->> 'name', "
        f"src.fields -> '{field_id}' ->> 'displayName', "
        f"src.fields ->> '{field_id}')"
    )


def _issue_column_types(conn: sa.Connection) -> dict[str, str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'analytics' AND table_name = 'issue'"
        )
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _warn_invalid_values(
    conn: sa.Connection, instance_id: str, issue_ids: list[int] | None, row: SelectionRow
) -> None:
    """Log how many values of a typed field _extract_expr turned into NULL."""
    cast_type = _CAST_TYPE_BY_SCHEMA_TYPE.get(row.schema_type)
    if cast_type is None:
        return
    raw = f"fields ->> '{row.field_id}'"
    invalid = conn.execute(
        sa.text(
            f"SELECT count(*) FROM jira_raw.issues WHERE instance_id = :instance_id "
            "AND (CAST(:issue_ids AS bigint[]) IS NULL OR issue_id = ANY(:issue_ids)) "
            f"AND {raw} IS NOT NULL AND NOT pg_input_is_valid({raw}, '{cast_type}')"
        ),
        {"instance_id": instance_id, "issue_ids": issue_ids},
    ).scalar_one()
    if invalid:
        logger.warning(
            "promoted_values_not_castable",
            field_id=row.field_id,
            column=row.column_name,
            type=cast_type,
            count=invalid,
            written_as="NULL",
        )


def rebuild_issue_columns(
    conn: sa.Connection,
    instance_id: str,
    issue_ids: list[int] | None,
    selection: dict[tuple[str, str], SelectionRow],
) -> tuple[int, int]:
    """Upsert analytics.issue's fixed columns, then add/populate whatever's
    currently promoted. Returns (issue_rows, bridge_rows).
    """
    if issue_ids is not None and not issue_ids:
        return 0, 0

    base_result = conn.execute(
        sa.text(_load_sql("issue_base.sql")), {"instance_id": instance_id, "issue_ids": issue_ids}
    )
    issue_rows = base_result.rowcount

    live = [row for row in selection.values() if row.promote and row.deprecated_at is None]
    column_fields = [row for row in live if row.target == "column"]
    bridge_fields = [row for row in live if row.target == "bridge_table"]

    set_clauses = []
    existing_types = _issue_column_types(conn) if column_fields else {}
    for row in column_fields:
        assert row.column_name and row.field_id  # save_selection() requires both
        validate_promoted_name(row.column_name, row.target)
        validate_field_id(row.field_id)
        col_type = _SQL_TYPE_BY_SCHEMA_TYPE.get(row.schema_type, "TEXT")
        existing = existing_types.get(row.column_name)
        if existing is not None and existing != _DATA_TYPE_BY_SQL_TYPE[col_type]:
            # The field was re-promoted with another type under the same
            # column name. Writing it would fail the whole transaction on
            # every run, so leave the column as it is.
            logger.warning(
                "promoted_column_type_mismatch",
                column=row.column_name,
                column_type=existing,
                field_type=col_type,
                hint="demote the field, or promote it under a new column name",
            )
            continue
        conn.execute(
            sa.text(
                f'ALTER TABLE analytics.issue ADD COLUMN IF NOT EXISTS "{row.column_name}" '
                f"{col_type}"
            )
        )
        set_clauses.append(f'"{row.column_name}" = {_extract_expr(row.field_id, row.schema_type)}')
        _warn_invalid_values(conn, instance_id, issue_ids, row)

    if set_clauses:
        conn.execute(
            sa.text(
                f"UPDATE analytics.issue a SET {', '.join(set_clauses)}, rebuilt_at = now() "
                "FROM jira_raw.issues src "
                "WHERE a.instance_id = src.instance_id AND a.issue_id = src.issue_id "
                "AND src.instance_id = :instance_id "
                "AND (CAST(:issue_ids AS bigint[]) IS NULL OR src.issue_id = ANY(:issue_ids))"
            ),
            {"instance_id": instance_id, "issue_ids": issue_ids},
        )

    bridge_rows = 0
    for row in bridge_fields:
        assert row.column_name and row.field_id
        validate_promoted_name(row.column_name, row.target)
        validate_field_id(row.field_id)
        # Quoted: a valid identifier can still be a reserved word ("order").
        table = f'analytics."{row.column_name}"'
        conn.execute(
            sa.text(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                "instance_id TEXT NOT NULL, issue_id BIGINT NOT NULL, seq INT NOT NULL, "
                "value TEXT, PRIMARY KEY (instance_id, issue_id, seq))"
            )
        )
        # cube_reader's blanket `GRANT SELECT ON ALL TABLES IN SCHEMA
        # analytics` + default privileges (migrations/sql/roles.sql) only
        # auto-covers tables created by whichever role ran that ALTER
        # DEFAULT PRIVILEGES - not this dynamically-created one. Guarded so
        # it's a no-op (not an error) wherever cube_reader doesn't exist,
        # e.g. tests - same idempotent style as migrations/sql/cube_reader_grants.sql.
        conn.execute(
            sa.text(
                "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cube_reader') "
                f"THEN GRANT SELECT ON {table} TO cube_reader; END IF; END $$;"
            )
        )
        conn.execute(
            sa.text(
                f"DELETE FROM {table} WHERE instance_id = :instance_id "
                "AND (CAST(:issue_ids AS bigint[]) IS NULL OR issue_id = ANY(:issue_ids))"
            ),
            {"instance_id": instance_id, "issue_ids": issue_ids},
        )
        result = conn.execute(
            sa.text(
                f"INSERT INTO {table} (instance_id, issue_id, seq, value) "
                "SELECT i.instance_id, i.issue_id, elem.seq::int, "
                "COALESCE(elem.value ->> 'name', elem.value ->> 'value', elem.value #>> '{}') "
                "FROM jira_raw.issues i, "
                f"LATERAL jsonb_array_elements(COALESCE(i.fields -> '{row.field_id}', "
                "'[]'::jsonb)) WITH ORDINALITY AS elem(value, seq) "
                "WHERE i.instance_id = :instance_id "
                "AND (CAST(:issue_ids AS bigint[]) IS NULL OR i.issue_id = ANY(:issue_ids))"
            ),
            {"instance_id": instance_id, "issue_ids": issue_ids},
        )
        bridge_rows += result.rowcount

    return issue_rows, bridge_rows


def run_transform(
    dsn: str, instance_id: str, *, rebuild_all: bool = False, project: str | None = None
) -> TransformResult:
    """The top-level entry point behind `flowbi transform`. Always drains
    flowbi_ops.rebuild_request afterwards, incremental pass or not.

    Holds the transform lock throughout, so overlapping runs take turns.
    """
    with transform_lock(dsn):
        result = _transform(dsn, instance_id, rebuild_all=rebuild_all, project=project)
        _drain_rebuild_queue(dsn, instance_id)
    return result


def _transform(
    dsn: str, instance_id: str, *, rebuild_all: bool, project: str | None
) -> TransformResult:
    _, selection = current_selection(dsn, instance_id)

    engine = sa.create_engine(dsn)
    try:
        with engine.begin() as conn:
            claimed_count = 0
            if rebuild_all:
                issue_ids = (
                    _issue_ids_for_project(conn, instance_id, project) if project else None
                )
                candidates = (
                    issue_ids
                    if issue_ids is not None
                    else _issue_ids_for_project(conn, instance_id, None)
                )
            else:
                claimed = claim_dirty(conn, instance_id)
                claimed_count = len(claimed)
                issue_ids = (
                    _issue_ids_for_project(conn, instance_id, project, restrict_to=claimed)
                    if project
                    else claimed
                )
                candidates = issue_ids

            # Only intervals depend on the changelog. analytics.issue's
            # columns come from jira_raw.issues alone, so they are rebuilt
            # for every issue in scope, complete changelog or not.
            complete, incomplete = partition_incomplete(conn, instance_id, candidates)
            skipped = len(incomplete)
            interval_rows = rebuild_intervals(conn, instance_id, complete)
            issue_rows, bridge_rows = rebuild_issue_columns(conn, instance_id, issue_ids, selection)

        result = TransformResult(
            claimed=claimed_count,
            skipped_incomplete=skipped,
            interval_rows=interval_rows,
            issue_rows=issue_rows,
            bridge_rows=bridge_rows,
        )
    finally:
        engine.dispose()
    return result


def drain_rebuild_queue(dsn: str, instance_id: str) -> list[RebuildOutcome]:
    """Claim every queued flowbi_ops.rebuild_request row for this instance
    and process it - promoted columns only, never intervals (a promotion
    doesn't change anything about status history). Each request runs in its
    own transaction so one failure can't block the rest of the queue.
    """
    with transform_lock(dsn):
        return _drain_rebuild_queue(dsn, instance_id)


def _drain_rebuild_queue(dsn: str, instance_id: str) -> list[RebuildOutcome]:
    """Caller holds the transform lock. That makes the stale-`running` sweep
    safe: no other drain can be running, so a `running` row is one a crashed
    run left behind, and would otherwise stay stuck forever.
    """
    engine = sa.create_engine(dsn)
    try:
        with engine.begin() as conn:
            requeued = conn.execute(
                sa.update(rebuild_request)
                .where(
                    rebuild_request.c.instance_id == instance_id,
                    rebuild_request.c.status == "running",
                )
                .values(status="queued", started_at=None)
            ).rowcount
            if requeued:
                logger.warning("rebuild_request_requeued_after_crash", count=requeued)
            queued = conn.execute(
                sa.select(rebuild_request)
                .where(
                    rebuild_request.c.instance_id == instance_id,
                    rebuild_request.c.status == "queued",
                )
                .order_by(rebuild_request.c.requested_at)
            ).fetchall()
            for row in queued:
                conn.execute(
                    sa.update(rebuild_request)
                    .where(rebuild_request.c.request_id == row.request_id)
                    .values(status="running", started_at=datetime.now(UTC))
                )

        outcomes: list[RebuildOutcome] = []
        for row in queued:
            try:
                with engine.begin() as conn:
                    _, selection = current_selection(dsn, instance_id, version=row.version)
                    issue_ids = (
                        _issue_ids_for_project(conn, instance_id, row.scope)
                        if row.scope
                        else None
                    )
                    issue_rows, bridge_rows = rebuild_issue_columns(
                        conn, instance_id, issue_ids, selection
                    )
                    rows_written = issue_rows + bridge_rows
                    conn.execute(
                        sa.update(rebuild_request)
                        .where(rebuild_request.c.request_id == row.request_id)
                        .values(
                            status="succeeded",
                            finished_at=datetime.now(UTC),
                            rows_written=rows_written,
                        )
                    )
                outcomes.append(
                    RebuildOutcome(
                        request_id=row.request_id, status="succeeded", rows_written=rows_written
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad request must not block the rest
                logger.warning("rebuild_request_failed", request_id=row.request_id, exc_info=True)
                with engine.begin() as conn:
                    conn.execute(
                        sa.update(rebuild_request)
                        .where(rebuild_request.c.request_id == row.request_id)
                        .values(status="failed", finished_at=datetime.now(UTC), error=str(exc))
                    )
                outcomes.append(
                    RebuildOutcome(request_id=row.request_id, status="failed", error=str(exc))
                )
        return outcomes
    finally:
        engine.dispose()
