import base64
import os
import stat
from pathlib import Path

import pytest

from openflowbi.cloud import tls

CERT = b"-----BEGIN CERTIFICATE-----\nMIIBcert\n-----END CERTIFICATE-----\n"
KEY = b"-----BEGIN PRIVATE KEY-----\nMIIBkey\n-----END PRIVATE KEY-----\n"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_neither_set_means_no_mtls():
    assert tls.client_cert_files(None, None) is None
    assert tls.client_cert_files("", "") is None


@pytest.mark.parametrize(("cert", "key"), [(_b64(CERT), None), (None, _b64(KEY))])
def test_only_one_half_set_fails_loudly(cert, key):
    # A half-configured pair must not silently fall back to no client cert: the failure
    # would otherwise surface much later as an opaque connection reset from Jira.
    with pytest.raises(ValueError, match="must be set together"):
        tls.client_cert_files(cert, key)


def test_invalid_base64_names_the_offending_variable():
    with pytest.raises(ValueError, match="FLOWBI_JIRA_CLIENT_KEY_B64 is not valid base64"):
        tls.client_cert_files(_b64(CERT), "not base64 !!")


def test_decodes_pem_pair_to_files():
    cert_path, key_path = tls.client_cert_files(_b64(CERT), _b64(KEY))  # type: ignore[misc]

    assert Path(cert_path).read_bytes() == CERT
    assert Path(key_path).read_bytes() == KEY
    assert Path(cert_path).parent == Path(key_path).parent


def test_each_call_gets_its_own_directory():
    first = tls.client_cert_files(_b64(CERT), _b64(KEY))
    second = tls.client_cert_files(_b64(CERT), _b64(KEY))
    assert first is not None and second is not None
    assert Path(first[0]).parent != Path(second[0]).parent


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits; target is a Linux container")
def test_key_file_is_private():
    _, key_path = tls.client_cert_files(_b64(CERT), _b64(KEY))  # type: ignore[misc]
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(Path(key_path).parent).st_mode) == 0o700
