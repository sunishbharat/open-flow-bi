from pathlib import Path
from typing import Any

import dlt

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline.source import DEFAULT_INCREMENTAL_START, jira_source

OUT_DIR = Path("out").resolve()


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
        # dlt string swap, per CLAUDE.md's library-first rule — no
        # hand-written writer. Compound instance_id primary keys and the
        # per-pipeline staging dataset land in M7.2/M7.4; this milestone only
        # wires the destination through.
        if not postgres_dsn:
            raise ValueError("FLOWBI_POSTGRES_DSN is required for --destination postgres")
        dest: Any = dlt.destinations.postgres(credentials=postgres_dsn)
        # csv is materially faster than the insert_values default for a
        # backfill (§4.2) — not yet measured against a real project (open
        # question #2), kept as the doc's stated default.
        loader_file_format = "csv"
    else:
        # Default layout ({table_name}/{load_id}.{file_id}.{ext}): dlt only
        # allows {schema_name} before {table_name} in a layout, so a
        # run=<load_id> prefix directory (as CLAUDE.md's original example
        # assumed) isn't possible — every row still carries _dlt_load_id for
        # run-to-run diffing.
        dest = dlt.destinations.filesystem(bucket_url=(out_dir or OUT_DIR).resolve().as_uri())
        loader_file_format = "parquet"

    pipeline = dlt.pipeline(
        pipeline_name="openflowbi",
        destination=dest,
        dataset_name="jira_raw",
        **pipeline_kwargs,
    )
    source = jira_source(
        profile, project=project, incremental_start=incremental_start
    ).with_resources(*resources)
    if limit is not None:
        # Rule 6 (CLAUDE.md): a debug run must never walk a whole project by
        # accident, even when writing to a real destination, not just --sink table.
        for name in resources:
            source.resources[name].add_limit(limit)
    return pipeline.run(source, loader_file_format=loader_file_format)
