import re
from collections.abc import MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEY = re.compile(r"(token|password|secret)", re.IGNORECASE)
_REDACTED = "***REDACTED***"


def redact_sensitive(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact Authorization and any key matching token|password|secret."""
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
