"""The account-timezone lookup behind every incremental JQL floor (`_updated_floor`).

On Server/DC an unresolvable /myself (anonymous access, e.g. the public Apache Jira this repo
tests against) degrades to UTC. On Cloud it must not: Cloud can answer a search made with bad
credentials with an empty result rather than a 401, so a silent UTC fallback there turns wrong
credentials into a successful, empty extraction.
"""

from dataclasses import replace
from unittest.mock import patch

import pytest
import requests
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth, HttpBasicAuth

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline.source import JiraCredentialsError, _updated_floor

CLOUD = DeploymentProfile(
    is_cloud=True,
    base_url="https://example.atlassian.net",
    version="1001.0.0",
    auth=HttpBasicAuth("svc@example.com", "wrong-token"),
    instance_id="example-atlassian-net",
)
SERVER = replace(
    CLOUD, is_cloud=False, base_url="https://issues.apache.org/jira", auth=BearerTokenAuth("x")
)
CURSOR = "2024-01-15T09:30:00.000+0000"
TIMEZONE = "openflowbi.pipeline.source.deployment_mod.account_timezone"


def _unauthorised() -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = 401
    return requests.exceptions.HTTPError("401 Client Error: Unauthorized", response=response)


def test_cloud_credentials_failure_is_fatal_not_a_silent_utc_fallback():
    with (
        patch(TIMEZONE, side_effect=_unauthorised()),
        pytest.raises(JiraCredentialsError, match="FLOWBI_JIRA_EMAIL"),
    ):
        _updated_floor(CLOUD, CURSOR)


def test_server_dc_still_falls_back_to_utc():
    with patch(TIMEZONE, side_effect=_unauthorised()):
        assert _updated_floor(SERVER, CURSOR) == "2024-01-15 09:30"


def test_cloud_uses_the_account_timezone_when_credentials_work():
    with patch(TIMEZONE, return_value="America/New_York"):
        assert _updated_floor(CLOUD, CURSOR) == "2024-01-15 04:30"
