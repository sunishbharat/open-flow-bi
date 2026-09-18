from pathlib import Path

import pytest

CASSETTE_ROOT = Path(__file__).parent / "cassettes"


@pytest.fixture(scope="module")
def vcr_config():
    return {"filter_headers": ["authorization"]}


@pytest.fixture
def vcr_cassette_dir(request):
    # Convention: a test named ..._cloud[...] or ..._dc[...] records into the
    # matching tests/cassettes/{cloud,dc}/ tree — both must pass (CLAUDE.md).
    name = request.node.name
    if "cloud" in name:
        subdir = "cloud"
    elif "dc" in name:
        subdir = "dc"
    else:
        subdir = "misc"
    return str(CASSETTE_ROOT / subdir)
