import re
from dataclasses import dataclass
from urllib.parse import urlparse

import pendulum
from dlt.sources.helpers.rest_client.auth import AuthConfigBase, BearerTokenAuth, HttpBasicAuth

from openflowbi.cloud.http import ClientCert, make_client

# Explicit FLOWBI_JIRA_DEPLOYMENT values. When set, detect() skips the /serverInfo probe.
DEPLOYMENTS = ("cloud", "server")


@dataclass(frozen=True)
class DeploymentProfile:
    is_cloud: bool
    base_url: str
    version: str
    auth: AuthConfigBase
    instance_id: str
    # mTLS (cert_path, key_path) — carried here so every Jira call made from a profile
    # presents it; None when the instance doesn't enforce client certificates.
    client_cert: ClientCert | None = None


def select_auth(
    is_cloud: bool,
    *,
    email: str | None = None,
    api_token: str | None = None,
    pat: str | None = None,
) -> AuthConfigBase:
    """Pick the auth strategy for a deployment. Pure — no network, unit-testable directly."""
    if is_cloud:
        if not email or not api_token:
            raise ValueError("Cloud deployment requires email and api_token")
        return HttpBasicAuth(email, api_token)
    if not pat:
        raise ValueError("Server/DC deployment requires a personal access token (pat)")
    return BearerTokenAuth(pat)


def derive_instance_id(base_url: str) -> str:
    """Derive a stable, human-readable instance_id slug from a base URL's host.

    Pure — no network, unit-testable directly. Used as the default when
    FLOWBI_JIRA_INSTANCE_ID isn't set (docs/phase2-postgres-design.md §7),
    e.g. https://issues.apache.org/jira -> issues-apache-org. instance_id
    becomes part of every Postgres primary key, so it is derived from the
    host alone — stable across a re-run even if the path or scheme changes.
    """
    host = urlparse(base_url).hostname or base_url
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")


def detect(
    base_url: str,
    *,
    email: str | None = None,
    api_token: str | None = None,
    pat: str | None = None,
    instance_id: str | None = None,
    deployment: str | None = None,
    client_cert: ClientCert | None = None,
) -> DeploymentProfile:
    """Detect Cloud vs Server/DC via /serverInfo and select the matching auth strategy.

    WRITE: no library distinguishes Jira Cloud from Server/DC or derives an auth
    strategy from it — Cloud = HttpBasicAuth(email, api_token), Server/DC =
    BearerTokenAuth(pat).

    `deployment` ("cloud" | "server") skips the probe entirely. Behind mTLS or a
    restrictive proxy, an unauthenticated first call fails with an opaque connection
    reset before credentials are even considered; declaring the type avoids that call.
    When probing, the client certificate is presented too.
    """
    base_url = base_url.rstrip("/")
    if deployment is not None:
        if deployment not in DEPLOYMENTS:
            raise ValueError(f"deployment must be one of {DEPLOYMENTS}, got {deployment!r}")
        is_cloud = deployment == "cloud"
        version = "not probed (FLOWBI_JIRA_DEPLOYMENT set)"
    else:
        client = make_client(base_url, client_cert=client_cert)
        info = client.get("/rest/api/2/serverInfo").json()
        is_cloud = info.get("deploymentType") == "Cloud"
        version = info.get("version", "unknown")

    return DeploymentProfile(
        is_cloud=is_cloud,
        base_url=base_url,
        version=version,
        auth=select_auth(is_cloud, email=email, api_token=api_token, pat=pat),
        instance_id=instance_id or derive_instance_id(base_url),
        client_cert=client_cert,
    )


def account_timezone(
    base_url: str, auth: AuthConfigBase, *, client_cert: ClientCert | None = None
) -> str:
    """Return the authenticated account's configured timezone via GET /myself.

    JQL `updated` comparisons have minute granularity and are evaluated in the
    calling account's timezone — callers must
    assert this before trusting JQL date math.
    """
    client = make_client(base_url, auth, client_cert=client_cert)
    timezone: str = client.get("/rest/api/2/myself").json()["timeZone"]
    return timezone


def jql_updated_floor(value: str, timezone: str) -> str:
    """Format an ISO `updated_at` cursor value as a JQL `updated >=` literal.

    Pure — no network, unit-testable directly. JQL date/time literals are
    minute-granular and evaluated in the calling account's timezone, so the
    incremental cursor's ISO timestamp (which may
    carry a different offset, e.g. UTC) must be converted into that timezone
    and truncated to the minute before it can be used as a filter — comparing
    raw ISO strings across offsets would silently mis-filter.
    """
    parsed = pendulum.parse(value)
    if not isinstance(parsed, pendulum.DateTime):
        raise ValueError(f"expected a full ISO datetime cursor value, got {value!r}")
    return parsed.in_timezone(timezone).format("YYYY-MM-DD HH:mm")
