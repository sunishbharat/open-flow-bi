"""Reaching Jira from a cloud deployment (Cloud Foundry, containers).

- `http`: the one RESTClient factory every Jira call goes through, applying the mTLS client
  certificate when configured. Pure — jira/ may import it and still touch no filesystem.
- `tls`: materialises the client certificate from base64 settings into private temp files.
  Touches the filesystem, so only the CLI imports it — never jira/.

Trust roots are deliberately not handled here: requests reads REQUESTS_CA_BUNDLE (set by the
deployment image) itself, and that env var overrides any explicit `session.verify` anyway.

Deliberately no import-time side effects, unlike the module this strategy is modelled on.
"""
