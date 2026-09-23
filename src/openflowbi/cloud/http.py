from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.auth import AuthConfigBase
from dlt.sources.helpers.rest_client.paginators import BasePaginator

# (cert_path, key_path) for mutual TLS — requests' own `cert=` tuple shape.
ClientCert = tuple[str, str]


def make_client(
    base_url: str,
    auth: AuthConfigBase | None = None,
    *,
    client_cert: ClientCert | None = None,
    paginator: BasePaginator | None = None,
) -> RESTClient:
    """Build the RESTClient every Jira call goes through, so none can forget the client cert.

    Some corporate Jira instances enforce mutual TLS: without a client certificate the
    connection is reset (`RemoteDisconnected`) before any HTTP response, which reads like a
    firewall block. `session.cert` is applied by requests on every send path, including the
    direct `session.post` in changelog.fetch_bulk.

    No CA-bundle option here on purpose: requests lets an ambient REQUESTS_CA_BUNDLE override
    an explicit `session.verify`, so the env var (set by the deployment image) is the one
    source of truth for trust roots.
    """
    client = RESTClient(base_url=base_url.rstrip("/"), auth=auth, paginator=paginator)
    if client_cert is not None:
        client.session.cert = client_cert
    return client
