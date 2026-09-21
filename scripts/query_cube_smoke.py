"""Cube smoke query for the M9a.3 CI job.

Queries the `flow` view that Cube compiles from cube/model/ against the
fixture scripts/seed_cube_smoke_fixture.py just loaded, and checks the
result's *shape* - expected member names, non-empty rows, a total that
matches the fixture's own known count - rather than hard-coded production
numbers (open-flow-bi-repo-structure_1.md §4: "asserts shape, not values").
A `cube/model/` break (renamed dimension, broken join, bad SQL expression)
fails this the same way it would fail a real dashboard, before it reaches
anyone.

Reads EXPECTED_TOTAL from the environment (the CI job sets it from
seed_cube_smoke_fixture.py's own stdout, so the two scripts never need to
import each other or restate the fixture numbers).
"""

import json
import os
import sys

import requests

CUBE_LOAD_URL = "http://localhost:4000/cubejs-api/v1/load"


def main() -> None:
    expected_total = int(os.environ["EXPECTED_TOTAL"])

    query = {"measures": ["flow.count"], "dimensions": ["flow.project_key"]}
    resp = requests.get(CUBE_LOAD_URL, params={"query": json.dumps(query)}, timeout=30)
    resp.raise_for_status()
    data = resp.json()["data"]

    assert data, "flow view returned zero rows against the smoke fixture"
    for row in data:
        assert "flow.project_key" in row, row
        assert "flow.count" in row, row

    total = sum(int(row["flow.count"]) for row in data)
    if total != expected_total:
        print(
            f"flow view shape check FAILED: expected {expected_total} total issues, "
            f"got {total} across {len(data)} project groups: {data}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"flow view OK: {len(data)} project groups, {total} issues match the fixture")


if __name__ == "__main__":
    main()
