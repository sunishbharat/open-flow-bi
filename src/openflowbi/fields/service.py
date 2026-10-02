"""Field-selection service calls: the layer both the CLI and a future UI
call, so the logic is written once. Functions are added when something needs
them, rather than stubbed out ahead of time.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa

from openflowbi.fields.selection import current_selection
from openflowbi.ops.tables import field_definition, field_stats, rebuild_request


@dataclass
class FieldRow:
    field_id: str
    name: str
    schema_type: str | None
    is_custom: bool
    fill_rate: Decimal | None
    distinct_count: int | None
    project_count: int | None
    disappeared_at: datetime | None


def list_fields(
    dsn: str,
    instance_id: str,
    *,
    custom_only: bool = False,
    min_fill: float | None = None,
    include_disappeared: bool = False,
) -> list[FieldRow]:
    """definition + stats, joined, sorted by fill rate — the screen and the CLI table."""
    query = (
        sa.select(
            field_definition.c.field_id,
            field_definition.c.name,
            field_definition.c.schema_type,
            field_definition.c.is_custom,
            field_stats.c.fill_rate,
            field_stats.c.distinct_count,
            field_stats.c.project_count,
            field_definition.c.disappeared_at,
        )
        .select_from(
            field_definition.outerjoin(
                field_stats,
                sa.and_(
                    field_definition.c.instance_id == field_stats.c.instance_id,
                    field_definition.c.field_id == field_stats.c.field_id,
                ),
            )
        )
        .where(field_definition.c.instance_id == instance_id)
        .order_by(sa.func.coalesce(field_stats.c.fill_rate, 0).desc())
    )
    if custom_only:
        query = query.where(field_definition.c.is_custom.is_(True))
    if min_fill is not None:
        query = query.where(field_stats.c.fill_rate >= min_fill)
    if not include_disappeared:
        query = query.where(field_definition.c.disappeared_at.is_(None))

    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as conn:
            rows = conn.execute(query).fetchall()
        return [FieldRow(*row) for row in rows]
    finally:
        engine.dispose()


@dataclass
class RebuildStatus:
    request_id: int
    instance_id: str
    version: int
    scope: str | None
    status: str
    requested_at: datetime
    requested_by: str
    started_at: datetime | None
    finished_at: datetime | None
    rows_written: int | None
    error: str | None


def request_rebuild(
    dsn: str,
    instance_id: str,
    *,
    scope: str | None,
    actor: str,
    version: int | None = None,
) -> int:
    """Queue a full rebuild of promoted columns at `version` (default:
    latest): a rebuild takes minutes, so callers queue one instead of
    running it. Returns the new request_id. The caller
    never runs the rebuild itself; `flowbi transform` drains this queue
    (transform/runner.py:drain_rebuild_queue).
    """
    resolved_version = version
    if resolved_version is None:
        resolved_version, _ = current_selection(dsn, instance_id)
        if resolved_version == 0:
            raise ValueError(
                "no field selection exists yet for this instance - promote a field first"
            )

    engine = sa.create_engine(dsn)
    try:
        with engine.begin() as conn:
            result = conn.execute(
                sa.insert(rebuild_request)
                .values(
                    instance_id=instance_id,
                    version=resolved_version,
                    scope=scope,
                    requested_by=actor,
                )
                .returning(rebuild_request.c.request_id)
            )
            return result.scalar_one()
    finally:
        engine.dispose()


def rebuild_status(dsn: str, request_id: int) -> RebuildStatus:
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(rebuild_request).where(rebuild_request.c.request_id == request_id)
            ).fetchone()
    finally:
        engine.dispose()
    if row is None:
        raise ValueError(f"no rebuild_request with id {request_id}")
    return RebuildStatus(**row._mapping)
