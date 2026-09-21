"""Discover Jira field definitions and compute fill-rate stats from Postgres.

docs/phase3-field-selection-design.md §2.1 / Phase 3a.1. Two independent
writes, both idempotent (safe to re-run `flowbi fields discover` any time):

1. `refresh_field_definitions` — GET /field, upsert into flowbi_ops.field_definition.
2. `compute_field_stats` — sample jira_raw.issues.fields and recompute flowbi_ops.field_stats.

No new dependency: SQLAlchemy Core (arrives with Alembic) for the writes,
jira/fields.py (dlt's RESTClient) for the /field read. Stats are computed by
pulling a bounded sample into Python and aggregating once, not one SQL query
per known field_id — a real instance can have hundreds of fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import sqlalchemy as sa
from dlt.sources.helpers.rest_client.auth import AuthConfigBase
from sqlalchemy.dialects.postgresql import insert as pg_insert

from openflowbi.jira import fields as jira_fields
from openflowbi.ops.tables import field_definition, field_stats

DEFAULT_SAMPLE_SIZE = 5000

# jsonb values that count as "not filled" alongside SQL NULL — Jira represents
# an empty multi-select/array field as `[]` and some string fields as `""`,
# neither of which is a real value.
_EMPTY_VALUES: tuple[object, ...] = (None, "", [])


@dataclass
class DiscoverResult:
    fields_seen: int
    fields_disappeared: int
    sampled_issues: int
    fields_with_stats: int


def refresh_field_definitions(
    dsn: str, instance_id: str, base_url: str, auth: AuthConfigBase
) -> tuple[int, int]:
    """GET /field, upsert into flowbi_ops.field_definition.

    A field previously seen for this instance but absent from this fetch is
    stamped `disappeared_at` (once — re-running doesn't bump the timestamp);
    a field that reappears has it cleared. Returns (fields_seen, fields_disappeared).
    """
    fetched = jira_fields.fetch(base_url, auth)
    seen_ids = {f.id for f in fetched}

    engine = sa.create_engine(dsn)
    try:
        with engine.begin() as conn:
            if fetched:
                stmt = pg_insert(field_definition).values(
                    [
                        {
                            "instance_id": instance_id,
                            "field_id": f.id,
                            "name": f.name,
                            "is_custom": f.custom,
                            "schema_type": f.schema_type,
                        }
                        for f in fetched
                    ]
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["instance_id", "field_id"],
                    set_={
                        "name": stmt.excluded.name,
                        "is_custom": stmt.excluded.is_custom,
                        "schema_type": stmt.excluded.schema_type,
                        "last_seen_at": sa.func.now(),
                        "disappeared_at": None,
                    },
                )
                conn.execute(stmt)

            missing = field_definition.c.field_id.notin_(seen_ids) if seen_ids else sa.true()
            result = conn.execute(
                field_definition.update()
                .where(field_definition.c.instance_id == instance_id)
                .where(missing)
                .where(field_definition.c.disappeared_at.is_(None))
                .values(disappeared_at=sa.func.now())
            )
        return len(fetched), result.rowcount
    finally:
        engine.dispose()


def compute_field_stats(
    dsn: str,
    instance_id: str,
    project: str | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> tuple[int, int]:
    """Sample jira_raw.issues.fields and recompute flowbi_ops.field_stats.

    Reads jira_raw (dlt-owned) but never writes to it — a SELECT is not the
    change P2-D1 forbids. Only recomputes stats for field_ids already in
    field_definition, so call refresh_field_definitions first. Returns
    (fields_with_stats, sampled_issues).
    """
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as conn:
            known_field_ids = [
                row[0]
                for row in conn.execute(
                    sa.select(field_definition.c.field_id).where(
                        field_definition.c.instance_id == instance_id
                    )
                ).fetchall()
            ]
            if not known_field_ids:
                return 0, 0

            query = sa.text(
                "SELECT fields FROM jira_raw.issues WHERE instance_id = :instance_id "
                + ("AND fields->'project'->>'key' = :project " if project else "")
                + "LIMIT :sample_size"
            )
            params: dict[str, object] = {"instance_id": instance_id, "sample_size": sample_size}
            if project:
                params["project"] = project
            sampled = [row[0] or {} for row in conn.execute(query, params).fetchall()]

        sampled_count = len(sampled)
        stats_rows = [
            _field_stats_row(instance_id, field_id, sampled) for field_id in known_field_ids
        ]

        with engine.begin() as conn:
            stmt = pg_insert(field_stats).values(stats_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["instance_id", "field_id"],
                set_={
                    "sampled_issues": stmt.excluded.sampled_issues,
                    "non_null_count": stmt.excluded.non_null_count,
                    "fill_rate": stmt.excluded.fill_rate,
                    "distinct_count": stmt.excluded.distinct_count,
                    "project_count": stmt.excluded.project_count,
                    "sample_values": stmt.excluded.sample_values,
                    "computed_at": sa.func.now(),
                },
            )
            conn.execute(stmt)
        return len(stats_rows), sampled_count
    finally:
        engine.dispose()


def _field_stats_row(instance_id: str, field_id: str, sampled: list[dict]) -> dict:
    sampled_count = len(sampled)
    non_null = 0
    distinct_keys: set[str] = set()  # full count — every distinct value seen
    samples: list[object] = []  # capped preview, §2.1: "up to 10, for the preview"
    projects_with_value: set[str] = set()

    for issue_fields in sampled:
        value = issue_fields.get(field_id)
        if value in _EMPTY_VALUES:
            continue
        non_null += 1
        project_key = (issue_fields.get("project") or {}).get("key")
        if project_key:
            projects_with_value.add(project_key)
        key = json.dumps(value, sort_keys=True, default=str)
        if key not in distinct_keys:
            distinct_keys.add(key)
            if len(samples) < 10:
                samples.append(value)

    return {
        "instance_id": instance_id,
        "field_id": field_id,
        "sampled_issues": sampled_count,
        "non_null_count": non_null,
        "fill_rate": round(non_null / sampled_count, 4) if sampled_count else 0.0,
        "distinct_count": len(distinct_keys),
        "project_count": len(projects_with_value),
        "sample_values": samples,
    }


def discover(
    dsn: str,
    instance_id: str,
    base_url: str,
    auth: AuthConfigBase,
    project: str | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> DiscoverResult:
    """Refresh field_definition, then recompute field_stats. The `flowbi fields discover` body."""
    fields_seen, fields_disappeared = refresh_field_definitions(dsn, instance_id, base_url, auth)
    fields_with_stats, sampled_issues = compute_field_stats(
        dsn, instance_id, project=project, sample_size=sample_size
    )
    return DiscoverResult(
        fields_seen=fields_seen,
        fields_disappeared=fields_disappeared,
        sampled_issues=sampled_issues,
        fields_with_stats=fields_with_stats,
    )
