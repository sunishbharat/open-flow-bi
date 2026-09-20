import pytest
import requests

from openflowbi.jira import deployment

# Real, publicly reachable Server/DC instance — no credentials needed for GET.
APACHE_JIRA = "https://issues.apache.org/jira"

# No Cloud tenant is available yet; this cassette is hand-authored from
# Atlassian's documented /serverInfo shape, not recorded from live traffic.
# Replace with a real recording once Cloud credentials are available.
CLOUD_STUB = "https://example.atlassian.net"


@pytest.mark.vcr
def test_detect_dc():
    profile = deployment.detect(APACHE_JIRA, pat="dummy-pat")
    assert profile.is_cloud is False
    assert profile.base_url == APACHE_JIRA
    assert profile.version
    # Default instance_id, derived from the host (M7.2: no FLOWBI_JIRA_INSTANCE_ID set).
    assert profile.instance_id == "issues-apache-org"


@pytest.mark.vcr
def test_detect_cloud():
    profile = deployment.detect(CLOUD_STUB, email="a@example.com", api_token="dummy-token")
    assert profile.is_cloud is True
    assert profile.base_url == CLOUD_STUB


@pytest.mark.vcr
def test_detect_dc_honours_explicit_instance_id():
    profile = deployment.detect(APACHE_JIRA, pat="dummy-pat", instance_id="my-custom-slug")
    assert profile.instance_id == "my-custom-slug"


def test_derive_instance_id_slugifies_host():
    assert deployment.derive_instance_id("https://issues.apache.org/jira") == "issues-apache-org"


def test_derive_instance_id_strips_port_and_userinfo():
    url = "https://user:pw@jira.example.com:8080"
    assert deployment.derive_instance_id(url) == "jira-example-com"


def test_derive_instance_id_is_stable_across_scheme_and_path():
    a = deployment.derive_instance_id("https://issues.apache.org/jira")
    b = deployment.derive_instance_id("http://issues.apache.org/jira/browse/KAFKA-1")
    assert a == b


@pytest.mark.vcr
def test_account_timezone_dc_raises_on_anonymous_access():
    # Apache's public Jira has no authenticated account behind the dummy PAT
    # this repo's live-test setup uses - GET /myself returns a real 401 with
    # an XML (not JSON) body, live-recorded, so .json() fails with
    # JSONDecodeError rather than a clean HTTPError. requests.exceptions.
    # JSONDecodeError still subclasses RequestException, so pipeline/
    # source.py's issues resource and cli.py's `doctor` (which both catch
    # RequestException broadly) degrade correctly rather than crash - this
    # is the real shape a caller must handle, not a hypothetical one.
    with pytest.raises(requests.exceptions.RequestException):
        deployment.account_timezone(APACHE_JIRA, None)


@pytest.mark.vcr
def test_account_timezone_cloud():
    # No Cloud tenant available; hand-authored cassette matching Atlassian's
    # documented /myself response shape (see CLOUD_STUB comment above).
    tz = deployment.account_timezone(CLOUD_STUB, None)
    assert tz == "America/New_York"


def test_select_auth_cloud_requires_email_and_token():
    with pytest.raises(ValueError, match="email and api_token"):
        deployment.select_auth(is_cloud=True)


def test_select_auth_dc_requires_pat():
    with pytest.raises(ValueError, match="personal access token"):
        deployment.select_auth(is_cloud=False)


def test_select_auth_cloud_ok():
    auth = deployment.select_auth(is_cloud=True, email="a@example.com", api_token="tok")
    assert auth is not None


def test_select_auth_dc_ok():
    auth = deployment.select_auth(is_cloud=False, pat="pat-value")
    assert auth is not None


def test_jql_updated_floor_truncates_to_minute_in_account_timezone():
    # UTC input, UTC account timezone: straight truncation, no offset shift.
    floor = deployment.jql_updated_floor("2024-01-15T09:30:45.123+0000", "UTC")
    assert floor == "2024-01-15 09:30"


def test_jql_updated_floor_converts_across_offsets():
    # A cursor value recorded in UTC must land on the correct local wall-clock
    # time for an account in a different timezone - JQL literals are
    # evaluated in the calling account's timezone (CLAUDE.md "Jira API facts"),
    # not the offset the cursor value happened to carry.
    floor = deployment.jql_updated_floor("2024-01-15T09:30:00.000+0000", "America/New_York")
    assert floor == "2024-01-15 04:30"


def test_jql_updated_floor_rejects_a_non_datetime_value():
    # pendulum.parse coerces a bare date/time string to midnight-DateTime, but
    # a duration-shaped string ("P1D") parses to something else entirely -
    # guard against ever building a JQL clause from that.
    with pytest.raises(ValueError, match="full ISO datetime"):
        deployment.jql_updated_floor("P1D", "UTC")
