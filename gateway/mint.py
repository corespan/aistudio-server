"""Usage (this text is the CLI help):

  python gateway/mint.py tls [--if-needed] [host ...]  # gateway TLS cert from the local CA (MCP_TLS_HOSTS)
"""
import ipaddress
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Outside the repo (api and worker mount it read-write): MCP_SECRETS_DIR, mounted at /secrets by make.
SECRETS = Path(os.environ.get("MCP_SECRETS") or Path.home() / ".aistudio-mcp")
TLS = SECRETS / "tls"          # mounted into agentgateway; the CA key sits one level up, never mounted
CA_DAYS, CERT_DAYS, RENEW_DAYS = 3650, 365, 30


def _write_key(path: Path, key, mode: int) -> None:
    # Created with its final mode: never more readable than `mode`, even briefly.
    path.unlink(missing_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))


def _hosts(values: list) -> list:
    """Hosts from `values`, else MCP_TLS_HOSTS; space or comma separated, deduplicated."""
    raw = " ".join(values) or os.environ.get("MCP_TLS_HOSTS", "")
    return list(dict.fromkeys(h for h in raw.replace(",", " ").split() if h))


def _san(host: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def _cert_hosts(cert: x509.Certificate) -> list:
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return [str(n.value) for n in sans]


def _subtree(host: str) -> x509.GeneralName:
    try:
        ip = ipaddress.ip_address(host)
        return x509.IPAddress(ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}"))
    except ValueError:
        return x509.DNSName(host)


def _permits(ca: x509.Certificate, host: str) -> bool:
    """Whether the CA's name constraints let it sign `host`."""
    try:
        subtrees = ca.extensions.get_extension_for_class(x509.NameConstraints).value.permitted_subtrees
    except x509.ExtensionNotFound:
        raise SystemExit(f"{TLS / 'ca.pem'} can sign any name: delete {TLS.parent / 'ca.key'} and {TLS}, "
                         "then make mcp-tls; clients must trust the new ca.pem") from None
    try:
        ip = ipaddress.ip_address(host)
        return any(isinstance(n, x509.IPAddress) and ip in n.value for n in subtrees)
    except ValueError:
        h = host.lower().rstrip(".")
        return any(isinstance(n, x509.DNSName) and (h == n.value.lower() or h.endswith("." + n.value.lower()))
                   for n in subtrees)


def _ca(hosts: list):
    """The local CA, created on first use and limited to `hosts`: trusting it trusts nothing else."""
    ca_key, ca_pem = TLS.parent / "ca.key", TLS / "ca.pem"
    if ca_key.exists() and ca_pem.exists():
        return (serialization.load_pem_private_key(ca_key.read_bytes(), None),
                x509.load_pem_x509_certificate(ca_pem.read_bytes()))
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"AIStudio MCP CA ({hosts[0]})")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                           critical=True)
            # A name type with no subtree is unconstrained, so both types always get one (localhost).
            .add_extension(x509.NameConstraints(
                [_subtree(h) for h in dict.fromkeys(hosts + ["localhost", "127.0.0.1"])], None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write_key(ca_key, key, 0o600)  # only mint.py reads it; never mounted into a service
    ca_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


def tls(hosts: list, if_needed: bool = False) -> bool:
    """Issue tls/cert.pem for `hosts`; if_needed keeps a fresh cert that covers them, else adds to its names."""
    TLS.mkdir(parents=True, exist_ok=True)
    cert_pem = TLS / "cert.pem"
    current = x509.load_pem_x509_certificate(cert_pem.read_bytes()) if cert_pem.exists() else None
    have = _cert_hosts(current) if current else []
    ca_key, ca_cert = _ca(hosts or have or ["localhost", "127.0.0.1"])
    # Names the CA was not made for (a new docker bridge IP, say) are left out; the first is the public host.
    outside = [h for h in hosts if not _permits(ca_cert, h)]
    if hosts and hosts[0] in outside:
        raise SystemExit(f"the CA was not made for {hosts[0]}: delete {TLS.parent / 'ca.key'} and {TLS}, "
                         "then make mcp-tls; clients must trust the new ca.pem")
    if outside:
        print(f"left out (not in the CA's names): {', '.join(outside)}", file=sys.stderr)
    hosts = [h for h in hosts if h not in outside]
    if if_needed and current:
        fresh = current.not_valid_after_utc - datetime.now(timezone.utc) > timedelta(days=RENEW_DAYS)
        # A cert from an earlier CA (ca.key lost and remade) fails every client that trusts ca.pem.
        try:
            current.verify_directly_issued_by(ca_cert)
        except (ValueError, TypeError, InvalidSignature):
            fresh = False
        if fresh and set(hosts) <= set(have):
            return False
        hosts = list(dict.fromkeys(have + hosts))
    hosts = hosts or have or ["localhost", "127.0.0.1"]
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            # Not a host name: OpenSSL checks a host-like CN against the CA's name constraints.
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AIStudio MCP gateway")]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=CERT_DAYS))
            .add_extension(x509.SubjectAlternativeName([_san(h) for h in hosts]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False),
                           critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    # ponytail: 0644 so agentgateway (uid 65532) can read it; chown 65532 + 0600 if the host is shared.
    _write_key(TLS / "key.pem", key, 0o644)
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return True


def main(argv: list) -> None:
    if not argv:
        raise SystemExit(__doc__)
    if argv[0] == "tls":
        if_needed = "--if-needed" in argv
        issued = tls(_hosts([a for a in argv[1:] if a != "--if-needed"]), if_needed)
        cert = x509.load_pem_x509_certificate((TLS / "cert.pem").read_bytes())
        state = "issued" if issued else "current"
        print(f"{state}: tls/cert.pem for {', '.join(_cert_hosts(cert))}, "
              f"expires {cert.not_valid_after_utc:%Y-%m-%d}; clients trust tls/ca.pem in MCP_SECRETS_DIR")
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
