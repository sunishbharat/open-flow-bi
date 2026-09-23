import re
from pathlib import Path
from typing import Any

import dlt
import structlog

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline import dirty
from openflowbi.pipeline.locks import load_lock
from openflowbi.pipeline.source import DEFAULT_INCREMENTAL_START, jira_source

logger = structlog.get_logger(__name__)

OUT_DIR = Path("out").resolve()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def pipeline_name(instance_id: str, project: str | None) -> str:
    """One dlt pipeline per (instance, project) — docs/phase2-postgres-design.md
    P2-D3. Isolates incremental watermarks (and, for Postgres, staging
    datasets — see the `staging_dataset_name_layout` use below) between
    projects that would otherwise share one local `.dlt` state directory or
    one physical Postgres staging table. Noted as a gap in M7.1, fixed here.
    """
    return f"openflowbi_{_slug(instance_id)}_{_slug(project or 'all')}"


def run(
    profile: DeploymentProfile,
    project: str | None = None,
    limit: int | None = None,
    out_dir: Path | None = None,
    pipelines_dir: str | None = None,
    resources: tuple[str, ...] = ("issues",),
    incremental_start: str = DEFAULT_INCREMENTAL_START,
    destination: str = "filesystem",
    postgres_dsn: str | None = None,
) -> Any:
    pipeline_kwargs: dict[str, Any] = {}
    if pipelines_dir is not None:
        pipeline_kwargs["pipelines_dir"] = pipelines_dir

    if destination == "postgres":
        # M7.1 (docs/phase2-postgres-design.md §4.2): the destination is one
        # dlt string swap, per the library-first rule — no
        # hand-written writer.
        if not postgres_dsn:
            raise ValueError("FLOWBI_POSTGRES_DSN is required for --destination postgres")
        dest: Any = dlt.destinations.postgres(
            credentials=postgres_dsn,
            # M7.4 (§6a): a per-project staging dataset, so two projects
            # loading concurrently never share one physical staging table
            # (dlt#4297's TRUNCATE/INSERT race). "%s" is dlt's own
            # placeholder for dataset_name ("jira_raw" below) — this becomes
            # e.g. "jira_raw_staging_kafka".
            staging_dataset_name_layout=f"%s_staging_{_slug(project or 'all')}",
        )
        # csv is materially faster than the insert_values default for a
        # backfill (§4.2) — not yet measured against a real project (open
        # question #2), kept as the doc's stated default.
        loader_file_format = "csv"
    else:
        # Default layout ({table_name}/{load_id}.{file_id}.{ext}): dlt only
        # allows {schema_name} before {table_name} in a layout, so a
        # run=<load_id> prefix directory (as an earlier doc example
        # assumed) isn't possible — every row still carries _dlt_load_id for
        # run-to-run diffing.
        dest = dlt.destinations.filesystem(bucket_url=(out_dir or OUT_DIR).resolve().as_uri())
        loader_file_format = "parquet"

    pipeline = dlt.pipeline(
        pipeline_name=pipeline_name(profile.instance_id, project),
        destination=dest,
        dataset_name="jira_raw",
        **pipeline_kwargs,
    )
    source = jira_source(
        profile, project=project, incremental_start=incremental_start
    ).with_resources(*resources)
    if limit is not None:
        # Project rule: a debug run must never walk a whole project by
        # accident, even when writing to a real destination, not just --sink table.
        for name in resources:
            source.resources[name].add_limit(limit)

    def _load() -> Any:
        return pipeline.run(
            source,
            loader_file_format=loader_file_format,
            # P2-D4 (docs/phase2-postgres-design.md §2/§4.2): new tables/columns
            # (e.g. a new custom field) must not break a load, but a column
            # silently changing type should — that is exactly the class of bug
            # M7.2's issue_id string->bigint change needs to be protected against
            # from here on.
            schema_contract={"tables": "evolve", "columns": "evolve", "data_type": "freeze"},
        )

    if destination == "postgres":
        assert postgres_dsn  # validated above
        # M7.4 (§6b): belt-and-braces around the per-project staging dataset
        # above, while dlt's own merge_scope_by_load_id fix is still
        # opt-in/unreleased — serializes only the load step across every
        # process sharing this DSN, not extraction.
        with load_lock(postgres_dsn):
            info = _load()
        _mark_dirty(info, postgres_dsn, profile.instance_id, resources)
        return info
    return _load()


def _mark_dirty(
    info: Any, postgres_dsn: str, instance_id: str, resources: tuple[str, ...]
) -> None:
    """M7.5 (docs/phase2-postgres-design.md §8): best-effort issue_dirty upkeep.

    Non-critical by design — nothing consumes issue_dirty until Phase 3, and
    `flowbi_ops` may not be migrated yet (`alembic upgrade head`) on a
    checkout that only ever ran `extract issues`/`extract changelog` against
    filesystem or a fresh Postgres. A failure here must not fail the
    extraction that already succeeded, the same graceful-degradation pattern
    pipeline/source.py uses for an unavailable account timezone.
    """
    for name in resources:
        reason = dirty.DIRTY_REASONS.get(name)
        if reason is None:
            continue
        try:
            dirty.mark_dirty(postgres_dsn, instance_id, name, list(info.loads_ids), reason)
        except Exception:  # noqa: BLE001 - see docstring
            logger.warning("issue_dirty_mark_failed", table=name, exc_info=True)
