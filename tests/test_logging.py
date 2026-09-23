from openflowbi.logging import redact_sensitive


def test_redacts_authorization_header():
    event = redact_sensitive(None, "info", {"Authorization": "Bearer secret-value", "msg": "ok"})
    assert event["Authorization"] == "***REDACTED***"
    assert event["msg"] == "ok"


def test_redacts_keys_matching_token_password_secret():
    event = redact_sensitive(
        None,
        "info",
        {"api_token": "abc", "user_password": "xyz", "client_secret": "s3cr3t", "other": "keep"},
    )
    assert event["api_token"] == "***REDACTED***"
    assert event["user_password"] == "***REDACTED***"
    assert event["client_secret"] == "***REDACTED***"
    assert event["other"] == "keep"


def test_redacts_mtls_settings_but_not_issue_keys():
    event = redact_sensitive(
        None,
        "info",
        {
            "jira_client_cert_b64": "LS0t...",
            "jira_client_key_b64": "LS0t...",
            "issue_key": "KAFKA-1",
        },
    )
    assert event["jira_client_cert_b64"] == "***REDACTED***"
    assert event["jira_client_key_b64"] == "***REDACTED***"
    # Deliberately not a bare "key" pattern - issue keys must stay readable in logs.
    assert event["issue_key"] == "KAFKA-1"
