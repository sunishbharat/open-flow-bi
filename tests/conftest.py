import os
from pathlib import Path

import pytest

CASSETTE_ROOT = Path(__file__).parent / "cassettes"

# dlt sends anonymous usage telemetry (machine id, OS, CPU count, execution
# environment) on every pipeline run. Tests must never send it, and a cassette
# must never record it: it is network traffic unrelated to Jira, and it
# fingerprints whichever machine recorded the cassette.
os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"

_UNRECORDED_HOSTS = ("telemetry.scalevector.ai",)


def _drop_unrelated_requests(request):
    # Belt and braces with the env var above: vcrpy skips recording any
    # request for which this returns None.
    if any(host in request.uri for host in _UNRECORDED_HOSTS):
        return None
    return request


@pytest.fixture(scope="module")
def vcr_config():
    return {
        "filter_headers": ["authorization"],
        "before_record_request": _drop_unrelated_requests,
    }


@pytest.fixture
def vcr_cassette_dir(request):
    # Convention: a test named ..._cloud[...] or ..._dc[...] records into the
    # matching tests/cassettes/{cloud,dc}/ tree. Both trees must pass, so Cloud
    # and Server/DC stay covered side by side.
    name = request.node.name
    if "cloud" in name:
        subdir = "cloud"
    elif "dc" in name:
        subdir = "dc"
    else:
        subdir = "misc"
    return str(CASSETTE_ROOT / subdir)
