"""Fixture loader for the M9a.3 cube CI smoke job.

Loads a handful of synthetic issues straight into jira_raw.issues via the
real pipeline.run(..., destination="postgres") path - the same code
`flowbi extract issues` uses - with Jira HTTP mocked at the _search_pages
boundary. That's the same pattern tests/pipeline/test_concurrency.py (M7.4)
uses, required by the M6 finding that cassette-based HTTP mocking must never
touch a dlt-wrapped resource directly (dlt worker-thread teardown races
vcrpy's cassette patching). Run by .github/workflows/ci.yml's cube-smoke
job, not by pytest - the Cube container this feeds lives entirely outside
the pytest process.

Requires FLOWBI_POSTGRES_DSN to point at a database that already has
`alembic upgrade head` and migrations/sql/roles.sql applied. Prints the
total fixture row count on its last line of stdout, which the CI job
captures for scripts/query_cube_smoke.py to assert against.
"""

import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline import run as pipeline_run

INSTANCE_ID = "cube-smoke"

PROFILE = DeploymentProfile(
    is_cloud=False,
    base_url="https://fake.example",
    version="1",
    auth=None,  # type: ignore[arg-type]
    instance_id=INSTANCE_ID,
)

# project_key -> {status_name: count}. Small and hand-countable so
# scripts/query_cube_smoke.py can assert an exact total, not just "some
# rows came back" - the fixture's own numbers are the only "real" values in
# play (open-flow-bi-repo-structure_1.md §4: "asserts shape, not values").
FIXTURE: dict[str, dict[str, int]] = {
    "ALPHA": {"To Do": 2, "In Progress": 1, "Done": 3},
    "BETA": {"To Do": 1, "Done": 2},
}


def _rows() -> Iterator[dict[str, Any]]:
    issue_id = 1
    for project, by_status in FIXTURE.items():
        for status, n in by_status.items():
            for _ in range(n):
                yield {
                    "id": str(issue_id),
                    "key": f"{project}-{issue_id}",
                    "fields": {
                        "created": "2026-01-01T00:00:00.000+0000",
                        "updated": "2026-01-01T00:00:00.000+0000",
                        "project": {"key": project},
                        "status": {"name": status},
                        "assignee": {"displayName": "Smoke Test"},
                    },
                }
                issue_id += 1


def main() -> None:
    dsn = os.environ["FLOWBI_POSTGRES_DSN"]
    rows = list(_rows())

    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch("openflowbi.pipeline.source._search_pages", return_value=iter(rows)),
    ):
        info = pipeline_run.run(
            PROFILE,
            destination="postgres",
            postgres_dsn=dsn,
            resources=("issues",),
        )
    assert not info.has_failed_jobs, info

    print(f"seeded {len(rows)} fixture issues into jira_raw.issues (instance_id={INSTANCE_ID})")
    print(len(rows))


if __name__ == "__main__":
    main()
