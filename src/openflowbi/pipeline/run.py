from pathlib import Path
from typing import Any

import dlt

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline.source import DEFAULT_INCREMENTAL_START, jira_source

OUT_DIR = Path("out").resolve()

# dlt.pipeline(destination="postgres") is the one-line future change for
# Postgres (out of scope per CLAUDE.md) — do not build a hand-written writer.


def run(
    profile: DeploymentProfile,
    project: str | None = None,
    limit: int | None = None,
    out_dir: Path | None = None,
    pipelines_dir: str | None = None,
    resources: tuple[str, ...] = ("issues",),
    incremental_start: str = DEFAULT_INCREMENTAL_START,
) -> Any:
    # Default layout ({table_name}/{load_id}.{file_id}.{ext}): dlt only allows
    # {schema_name} before {table_name} in a layout, so a run=<load_id> prefix
    # directory (as CLAUDE.md's original example assumed) isn't possible —
    # every row still carries _dlt_load_id for run-to-run diffing.
    destination = dlt.destinations.filesystem(bucket_url=(out_dir or OUT_DIR).resolve().as_uri())
    pipeline_kwargs: dict[str, Any] = {}
    if pipelines_dir is not None:
        pipeline_kwargs["pipelines_dir"] = pipelines_dir
    pipeline = dlt.pipeline(
        pipeline_name="openflowbi",
        destination=destination,
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
    return pipeline.run(source, loader_file_format="parquet")
