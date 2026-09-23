import re
from collections.abc import MutableMapping
from typing import Any

import structlog

# cert|b64 covers the mTLS settings (FLOWBI_JIRA_CLIENT_{CERT,KEY}_B64 hold a private key).
# Deliberately not bare "key": issue_key / field keys are everywhere in normal logs.
_SENSITIVE_KEY = re.compile(r"(token|password|secret|cert|b64|private_key)", re.IGNORECASE)
_REDACTED = "***REDACTED***"


def redact_sensitive(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact Authorization and any key matching the sensitive pattern."""
    for key in list(event_dict):
        if key.lower() == "authorization" or _SENSITIVE_KEY.search(key):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging() -> None:
    structlog.configure(
        processors=[
            redact_sensitive,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.KeyValueRenderer(),
        ],
    )
