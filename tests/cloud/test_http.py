"""The client factory must put the mTLS cert on the wire, on every requests send path.

HTTP is stubbed at requests.adapters.HTTPAdapter.send, the layer that receives the final
`cert=` argument, so this checks what requests would actually present - not just that an
attribute was set on a session.
"""

from typing import Any
from unittest.mock import patch

import requests

from openflowbi.cloud.http import make_client

CERT = ("/certs/client-cert.pem", "/certs/client-key.pem")


def _capture() -> tuple[Any, list[Any]]:
    seen: list[Any] = []

    def send(self: Any, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        seen.append(kwargs.get("cert"))
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = b"{}"
        response.url = request.url
        response.request = request
        return response

    return send, seen


def test_client_cert_reaches_the_adapter_on_every_send_path():
    send, seen = _capture()
    with patch.object(requests.adapters.HTTPAdapter, "send", send):
        client = make_client("https://jira.example.com/", client_cert=CERT)
        client.get("/rest/api/2/serverInfo")  # RESTClient request path
        client.session.post(  # the direct-session path changelog.fetch_bulk uses
            "https://jira.example.com/rest/api/3/changelog/bulkfetch", json={}
        )

    assert [tuple(c) for c in seen] == [CERT, CERT]


def test_no_client_cert_by_default():
    send, seen = _capture()
    with patch.object(requests.adapters.HTTPAdapter, "send", send):
        make_client("https://jira.example.com").get("/rest/api/2/serverInfo")

    assert seen == [None]


def test_trailing_slash_is_normalised():
    assert make_client("https://jira.example.com/").base_url == "https://jira.example.com"
