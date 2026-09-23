"""Materialise the Jira mTLS client certificate from base64 settings into private temp files.

Lives outside jira/ on purpose (jira/ touches no filesystem) — jira/
only ever receives the resulting paths. Called explicitly by the CLI, never at import time.

Base64 env vars (FLOWBI_JIRA_CLIENT_CERT_B64 / FLOWBI_JIRA_CLIENT_KEY_B64) because that is
how a PEM pair survives `cf set-env` on Cloud Foundry, where the container filesystem is
ephemeral and there is nowhere durable to mount a key file.
"""

import atexit
import base64
import binascii
import os
import shutil
import tempfile
from pathlib import Path

from openflowbi.cloud.http import ClientCert


def _decode(name: str, value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{name} is not valid base64") from exc


def _write_private(path: Path, data: bytes) -> None:
    # O_EXCL + 0600 at creation: the key is never readable by others, even briefly.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def client_cert_files(cert_b64: str | None, key_b64: str | None) -> ClientCert | None:
    """Decode the base64 PEM cert + key into a private temp dir; return (cert_path, key_path).

    Returns None when neither is set (no mTLS). Fails loudly — rather than logging and
    carrying on unauthenticated — when only one is set or either isn't valid base64: a
    misconfigured client cert otherwise surfaces much later as an opaque connection reset.
    The directory is removed at interpreter exit.
    """
    if not cert_b64 and not key_b64:
        return None
    if not cert_b64 or not key_b64:
        raise ValueError(
            "FLOWBI_JIRA_CLIENT_CERT_B64 and FLOWBI_JIRA_CLIENT_KEY_B64 must be set together"
        )
    cert = _decode("FLOWBI_JIRA_CLIENT_CERT_B64", cert_b64)
    key = _decode("FLOWBI_JIRA_CLIENT_KEY_B64", key_b64)

    directory = Path(tempfile.mkdtemp(prefix="flowbi-mtls-"))  # mkdtemp is 0700
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    cert_path, key_path = directory / "client-cert.pem", directory / "client-key.pem"
    _write_private(cert_path, cert)
    _write_private(key_path, key)
    return str(cert_path), str(key_path)
