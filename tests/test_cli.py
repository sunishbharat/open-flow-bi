from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq
from typer.testing import CliRunner

from openflowbi.cli import app

runner = CliRunner()


def test_help_exits_zero():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def _write_issues_parquet(out_dir, issue_ids):
    table_dir = out_dir / "jira_raw" / "issues"
    table_dir.mkdir(parents=True)
    # created_at/updated_at as a real timestamp type, not a string: matches
    # what dlt's normalizer actually writes for these columns (see
    # quality/checks.py's comment), confirmed against a live extract.
    timestamp = pa.timestamp("us", tz="UTC")
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    table = pa.table(
        {
            "instance_id": ["inst-a"] * len(issue_ids),
            # bigint from M7.2 (docs/phase2-postgres-design.md §3).
            "issue_id": pa.array(issue_ids, type=pa.int64()),
            "issue_key": [f"PROJ-{i}" for i in issue_ids],
            "created_at": pa.array([ts] * len(issue_ids), type=timestamp),
            "updated_at": pa.array([ts] * len(issue_ids), type=timestamp),
        }
    )
    pq.write_table(table, table_dir / "load1.1.parquet")


def test_quality_check_passes_on_well_formed_issues(tmp_path):
    _write_issues_parquet(tmp_path, [1, 2])
    result = runner.invoke(app, ["quality", "check", "issues", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_quality_check_fails_on_duplicate_issue_id(tmp_path):
    # issue_id is the only identity — never duplicated.
    _write_issues_parquet(tmp_path, [1, 1])
    result = runner.invoke(app, ["quality", "check", "issues", "--out-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "FAILED" in result.stdout


def test_quality_check_reports_missing_parquet(tmp_path):
    result = runner.invoke(app, ["quality", "check", "issues", "--out-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "No Parquet files found" in result.stdout


def test_quality_check_rejects_unknown_table(tmp_path):
    result = runner.invoke(app, ["quality", "check", "bogus", "--out-dir", str(tmp_path)])
    assert result.exit_code != 0


def test_half_configured_client_cert_is_a_usage_error_before_any_jira_call(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setenv("FLOWBI_JIRA_BASE_URL", "https://jira.corp.example")
    monkeypatch.setenv("FLOWBI_JIRA_CLIENT_CERT_B64", "LS0tLS1CRUdJTg==")
    monkeypatch.delenv("FLOWBI_JIRA_CLIENT_KEY_B64", raising=False)
    with patch("openflowbi.cli.deployment.detect") as detect:
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 2
    assert "must be set together" in result.output
    detect.assert_not_called()


def test_blank_mtls_and_deployment_settings_mean_unset(monkeypatch):
    # `FLOWBI_X=` in .env arrives as "" - it must not become an invalid deployment value
    # or a half-configured certificate pair.
    from openflowbi.config import Settings

    monkeypatch.setenv("FLOWBI_JIRA_BASE_URL", "https://jira.corp.example")
    for name in ("DEPLOYMENT", "CLIENT_CERT_B64", "CLIENT_KEY_B64"):
        monkeypatch.setenv(f"FLOWBI_JIRA_{name}", "")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.jira_deployment is None
    assert settings.jira_client_cert_b64 is None
    assert settings.jira_client_key_b64 is None


def _cloud_profile(client_cert=None):
    from dlt.sources.helpers.rest_client.auth import HttpBasicAuth

    from openflowbi.jira.deployment import DeploymentProfile

    return DeploymentProfile(
        is_cloud=True,
        base_url="https://example.atlassian.net",
        version="1001.0.0",
        auth=HttpBasicAuth("svc@example.com", "wrong-token"),
        instance_id="example-atlassian-net",
        client_cert=client_cert,
    )


def test_cloud_credentials_failure_is_one_clear_line_not_a_dlt_traceback(monkeypatch):
    from unittest.mock import patch

    import requests

    response = requests.Response()
    response.status_code = 401
    monkeypatch.setenv("FLOWBI_JIRA_BASE_URL", "https://example.atlassian.net")
    with (
        patch("openflowbi.cli.deployment.detect", return_value=_cloud_profile()),
        patch(
            "openflowbi.pipeline.source.deployment_mod.account_timezone",
            side_effect=requests.exceptions.HTTPError("401", response=response),
        ),
    ):
        result = runner.invoke(app, ["extract", "issues", "--limit", "1"])

    assert result.exit_code == 1
    assert "Jira Cloud rejected the credentials" in result.output
    assert "FLOWBI_JIRA_API_TOKEN" in result.output
    assert "Traceback" not in result.output


def test_resolve_profile_logs_connection_state_without_secrets(monkeypatch, capsys):
    from unittest.mock import patch

    from openflowbi import cli

    cert = ("/tmp/flowbi-mtls-x/client-cert.pem", "/tmp/flowbi-mtls-x/client-key.pem")
    monkeypatch.setenv("FLOWBI_JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("FLOWBI_JIRA_DEPLOYMENT", "cloud")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/certs/combined-ca.pem")
    with (
        patch("openflowbi.cli.deployment.detect", return_value=_cloud_profile(cert)),
        patch("openflowbi.cli.tls.client_cert_files", return_value=cert),
    ):
        cli._resolve_profile()

    line = capsys.readouterr().out
    assert "event='jira_connection'" in line
    assert "deployment='cloud'" in line
    assert "detection='declared'" in line
    assert "mtls='configured'" in line
    assert "ca_bundle='/certs/combined-ca.pem'" in line
    assert "client-key.pem" not in line  # paths to key material are never logged
