"""Write the CA bundle the Docker stack uses (.build-ca.pem): certifi's public
CAs plus every root this machine's operating system trusts.

Anything that re-signs HTTPS (a corporate proxy, Zscaler, antivirus TLS
scanning such as Norton's) installs its root into the OS trust store, so this
picks it up without anyone having to know which product it is or where its
certificate file lives. Containers can't see the host's trust store; this
file is how they get it (see README "Running with Docker", step 1).

    uv run python scripts/make_ca_bundle.py            # writes .build-ca.pem, then checks it
    uv run python scripts/make_ca_bundle.py --out x.pem --check-host pypi.org

Sources: Windows "ROOT" and "CA" stores (stdlib ssl.enum_certificates, roots
trusted for server auth only); macOS system and login keychains (`security`);
Linux the distribution's bundle, which already includes admin-added roots.

Rejected: `truststore` (makes Python *use* the OS store, but can't export it
to a file a container can read), `python-certifi-win32` and `wincertstore`
(unmaintained, Windows-only).
"""

import argparse
import re
import socket
import ssl
import subprocess
import sys
from pathlib import Path
from typing import Any

import certifi

SERVER_AUTH = "1.3.6.1.5.5.7.3.1"
PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.DOTALL)
LINUX_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora
    "/etc/ssl/cert.pem",  # others
)


def _windows_roots() -> list[str]:
    pems = []
    for store in ("ROOT", "CA"):
        for der, encoding, trust in ssl.enum_certificates(store):  # type: ignore[attr-defined]
            # trust is True (all purposes), or the set of purposes it is trusted for.
            if encoding == "x509_asn" and (trust is True or (trust and SERVER_AUTH in trust)):
                pems.append(ssl.DER_cert_to_PEM_cert(der))
    return pems


def _macos_roots() -> list[str]:
    keychains = [
        "/System/Library/Keychains/SystemRootCertificates.keychain",
        "/Library/Keychains/System.keychain",
        str(Path.home() / "Library/Keychains/login.keychain-db"),
    ]
    found = [k for k in keychains if Path(k).exists()]
    out = subprocess.run(
        ["security", "find-certificate", "-a", "-p", *found],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return PEM_RE.findall(out)


def _linux_roots() -> list[str]:
    for path in LINUX_BUNDLES:
        if Path(path).exists():
            return PEM_RE.findall(Path(path).read_text())
    return []


def os_roots() -> list[str]:
    if sys.platform == "win32":
        return _windows_roots()
    if sys.platform == "darwin":
        return _macos_roots()
    return _linux_roots()


def build_bundle() -> tuple[str, int, int]:
    """certifi first, then OS roots not already in it. Returns (pem, certifi_count, added)."""
    public = PEM_RE.findall(Path(certifi.where()).read_text())
    seen = {_normalise(p) for p in public}
    added = []
    for pem in os_roots():
        key = _normalise(pem)
        if key not in seen:
            seen.add(key)
            added.append(pem.strip())
    return "\n".join(p.strip() for p in public + added) + "\n", len(public), len(added)


def _normalise(pem: str) -> str:
    return "".join(pem.split())


def check(bundle: Path, host: str) -> str:
    """Open a TLS connection to `host` trusting only `bundle`, as a container would."""
    context = ssl.create_default_context(cafile=str(bundle))
    with socket.create_connection((host, 443), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            cert: dict[str, Any] = tls.getpeercert() or {}
    issuer = {key: value for rdn in cert.get("issuer", ()) for key, value in rdn}
    return issuer.get("organizationName") or issuer.get("commonName") or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(".build-ca.pem"))
    parser.add_argument(
        "--check-host",
        action="append",
        help="host to verify the bundle against (repeatable; default pypi.org and the Jira host)",
    )
    args = parser.parse_args()

    pem, public_count, added = build_bundle()
    # Plain LF, ASCII: what OpenSSL, uv and pip inside the containers expect.
    args.out.write_bytes(pem.encode("ascii"))
    print(f"wrote {args.out}: {public_count} public CAs + {added} from this machine's trust store")

    hosts = args.check_host or ["pypi.org", _jira_host() or "issues.apache.org"]
    failed = False
    for host in hosts:
        try:
            print(f"  {host}: OK (certificate issued by {check(args.out, host)})")
        except ssl.SSLCertVerificationError as exc:
            failed = True
            print(f"  {host}: FAILED - {exc.verify_message}. Something re-signs HTTPS with a root")
            print("    this machine doesn't trust either; ask IT for it and append it to the file.")
        except OSError as exc:
            print(f"  {host}: not checked ({exc})")
    return 1 if failed else 0


def _jira_host() -> str | None:
    env = Path(".env")
    if not env.exists():
        return None
    match = re.search(r"^FLOWBI_JIRA_BASE_URL=https?://([^/:\s]+)", env.read_text(), re.MULTILINE)
    return match.group(1) if match else None


if __name__ == "__main__":
    sys.exit(main())
