import re
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

import dlt
import structlog

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline import dirty
from openflowbi.pipeline.locks import load_lock
from openflowbi.pipeline.source import DEFAULT_INCREMENTAL_START, jira_source, parse_cursor

logger = structlog.get_logger(__name__)

OUT_DIR = Path("out").resolve()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def pipeline_name(instance_id: str, project: str | None) -> str:
    """One dlt pipeline per (instance, project). Isolates incremental
    watermarks (and, for Postgres, staging datasets — see the
    `staging_dataset_name_layout` use below) between projects that would
    otherwise share one local `.dlt` state directory or one physical
    Postgres staging table.
    """
    return f"openflowbi_{_slug(instance_id)}_{_slug(project or 'all')}"


_CURSOR_STATE_KEYS = ("initial_value", "start_value", "last_value")


def _migrate_string_cursors(pipeline: Any) -> None:
    """Convert `updated_at` cursors stored as strings into aware datetimes.

    Older versions stored the cursor as Jira's raw `updated` string, which
    compares lexically rather than as an instant. dlt cannot compare a stored
    string with the datetime rows it gets now (TypeError at extract), so
    every pipeline that ran before the fix needs its state converted once.
    The caller syncs destination state first, so a fresh container (no local
    `.dlt`) is migrated too.
    """
    with pipeline.managed_state() as state:
        resources = state.get("sources", {}).get("jira", {}).get("resources", {})
        for resource_state in resources.values():
            cursor = resource_state.get("incremental", {}).get("updated_at")
            if not cursor:
                continue
            for key in _CURSOR_STATE_KEYS:
                if isinstance(cursor.get(key), str):
                    cursor[key] = parse_cursor(cursor[key])


def _reset_watermarks(pipeline: Any, resources: tuple[str, ...]) -> None:
    """Forget the incremental cursor of each resource, so this run walks from
    the configured incremental start again.

    The way to backfill `issue_changelog_status` rows for issues whose
    changelog was extracted before that table existed: `transform` skips those issues until a fresh
    extraction records them as complete. Called after `sync_destination()`,
    so it also resets a watermark that only the destination holds. The
    reset is written to local state immediately and to the destination with
    the next successful load.
    """
    with pipeline.managed_state() as state:
        resource_states = state.get("sources", {}).get("jira", {}).get("resources", {})
        for name in resources:
            resource_states.get(name, {}).pop("incremental", None)
    logger.info("watermark_reset", pipeline=pipeline.pipeline_name, resources=list(resources))


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
    reset_watermark: bool = False,
) -> Any:
    pipeline_kwargs: dict[str, Any] = {}
    if pipelines_dir is not None:
        pipeline_kwargs["pipelines_dir"] = pipelines_dir

    if destination == "postgres":
        # The destination is one dlt string swap, per the library-first rule — no
        # hand-written writer.
        if not postgres_dsn:
            raise ValueError("FLOWBI_POSTGRES_DSN is required for --destination postgres")
        dest: Any = dlt.destinations.postgres(
            credentials=postgres_dsn,
            # A per-project staging dataset, so two projects
            # loading concurrently never share one physical staging table
            # (dlt#4297's TRUNCATE/INSERT race). "%s" is dlt's own
            # placeholder for dataset_name ("jira_raw" below) — this becomes
            # e.g. "jira_raw_staging_kafka".
            staging_dataset_name_layout=f"%s_staging_{_slug(project or 'all')}",
        )
        # csv is expected to be materially faster than the insert_values
        # default for a backfill. Not yet measured against a real project.
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
    # What pipeline.run() does before touching data, done once here because
    # run() is split into its steps below: restore state from the
    # destination (a fresh container has no local `.dlt`), then convert any
    # pre-finding-1 string cursors in it.
    pipeline.sync_destination()
    _migrate_string_cursors(pipeline)
    if reset_watermark:
        _reset_watermarks(pipeline, resources)
    source = jira_source(
        profile,
        project=project,
        incremental_start=incremental_start,
        changelog_issue_limit=limit,
    ).with_resources(*resources)
    if limit is not None:
        # Project rule: a debug run must never walk a whole project by
        # accident, even when writing to a real destination, not just --sink table.
        # issue_changelog applies its own limit, counted in issues (see jira_source).
        for name in resources:
            if name != "issue_changelog":
                source.resources[name].add_limit(limit)

    if destination == "postgres":
        assert postgres_dsn  # validated above
        dsn = postgres_dsn

        # Belt-and-braces around the per-project staging dataset
        # above, while dlt's own merge_scope_by_load_id fix is still
        # opt-in/unreleased. Held around pipeline.load() only, never extract
        # or normalize: extraction is the
        # slow, quota-bound part and stays parallel across projects, and the
        # lock's connection is never left idle for the length of a backfill.
        def lock() -> AbstractContextManager[None]:
            return load_lock(dsn)

        def after_load(info: Any) -> None:
            # Every dirty-marking table, not just `resources`: load() also
            # loads any package a killed run left behind, which may hold the
            # other resource. mark_dirty finds nothing where a load wrote nothing.
            _mark_dirty(info, dsn, profile.instance_id, tuple(dirty.DIRTY_REASONS))
    else:
        lock = nullcontext

        def after_load(info: Any) -> None:
            pass

    pipeline.extract(
        source,
        loader_file_format=loader_file_format,
        # New tables/columns (e.g. a new custom field) must not break a load,
        # but a column silently changing type should, such as issue_id
        # drifting from bigint back to a string.
        schema_contract={"tables": "evolve", "columns": "evolve", "data_type": "freeze"},
    )
    # normalize() and load() also pick up packages a killed run left
    # behind, as pipeline.run() would.
    pipeline.normalize()
    with lock():
        info = pipeline.load()
    after_load(info)
    return info


class DirtyMarkError(RuntimeError):
    """The load committed, but its issues could not be queued for `transform`."""


def _mark_dirty(
    info: Any, postgres_dsn: str, instance_id: str, resources: tuple[str, ...]
) -> None:
    """Queue the loaded issues in
    flowbi_ops.issue_dirty for the next incremental `flowbi transform`.

    Not best-effort: incremental
    transform is driven entirely by issue_dirty, so a silently failed mark
    means those issues are never rebuilt. A failure fails the command. The
    load has already committed and the watermark moved past these issues,
    so re-running the extract won't re-mark them; the error says how to
    recover. Only a database where `flowbi_ops` isn't migrated yet (nothing
    can consume issue_dirty there) degrades to a warning.
    """
    if not dirty.ops_ready(postgres_dsn):
        logger.warning("issue_dirty_unavailable", hint="run `alembic upgrade head`")
        return
    for name in resources:
        reason = dirty.DIRTY_REASONS.get(name)
        if reason is None:
            continue
        try:
            dirty.mark_dirty(
                postgres_dsn, instance_id, dirty.DIRTY_TABLES[name], list(info.loads_ids), reason
            )
        except Exception as exc:
            raise DirtyMarkError(
                f"Load {', '.join(info.loads_ids)} committed, but marking its {name} issues "
                "dirty failed, so an incremental `flowbi transform` would never rebuild them. "
                "Fix the cause below, then run `flowbi transform --rebuild-all`."
            ) from exc
