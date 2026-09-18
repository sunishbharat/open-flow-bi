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
