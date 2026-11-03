"""Usage (this text is the CLI help):

  python gateway/mint.py init                         # new dev-key.pem + jwks.json; restart agentgateway
  python gateway/mint.py tls [--if-needed] [host ...]  # gateway TLS cert from the local CA (MCP_TLS_HOSTS)
  python gateway/mint.py viewer|operator|admin [sub] [--ttl SECONDS]   # print an ES256 JWT (max 1 day)
"""
import ipaddress
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HERE = Path(__file__).resolve().parent
KEY = HERE / "dev-key.pem"
JWKS = HERE / "jwks.json"
KID = "aistudio-1"
ISSUER = "aistudio"
ROLES = ("viewer", "operator", "admin")
MAX_TTL = 86400   # = MCP_JWT_MAX_LIFETIME_SECONDS

TLS = HERE / "tls"
CA_DAYS, CERT_DAYS, RENEW_DAYS = 3650, 365, 30


def _write_key(path: Path, key, mode: int) -> None:
    # Created with its final mode: never more readable than `mode`, even briefly.
    path.unlink(missing_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))


def init() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    _write_key(KEY, key, 0o600)
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=KID, alg="ES256", use="sig")
    JWKS.write_text(json.dumps({"keys": [jwk]}, indent=2) + "\n")


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


def _ca(label: str):
    """The local CA, created on first use. Clients trust tls/ca.pem once; certs it signs can change freely."""
    ca_key, ca_pem = TLS / "ca.key", TLS / "ca.pem"
    if ca_key.exists() and ca_pem.exists():
        return (serialization.load_pem_private_key(ca_key.read_bytes(), None),
                x509.load_pem_x509_certificate(ca_pem.read_bytes()))
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"AIStudio MCP CA ({label})")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                           critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write_key(ca_key, key, 0o600)  # only mint.py reads it; never mounted into a service
    ca_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


def tls(hosts: list, if_needed: bool = False) -> bool:
    """Issue tls/cert.pem for `hosts`; if_needed keeps a fresh cert that covers them, else adds to its names."""
    TLS.mkdir(exist_ok=True)
    cert_pem = TLS / "cert.pem"
    current = x509.load_pem_x509_certificate(cert_pem.read_bytes()) if cert_pem.exists() else None
    have = _cert_hosts(current) if current else []
    if if_needed and current:
        fresh = current.not_valid_after_utc - datetime.now(timezone.utc) > timedelta(days=RENEW_DAYS)
        if fresh and set(hosts) <= set(have):
            return False
        hosts = list(dict.fromkeys(have + hosts))
    hosts = hosts or have or ["localhost", "127.0.0.1"]
    ca_key, ca_cert = _ca(hosts[0])
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
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


def token(role: str, sub: str, ttl: int) -> str:
    if role not in ROLES:
        raise SystemExit(f"role must be one of {', '.join(ROLES)}")
    if not 0 < ttl <= MAX_TTL:
        raise SystemExit(f"--ttl must be 1..{MAX_TTL} s: aistudio-mcp refuses longer-lived tokens "
                         "(MCP_JWT_MAX_LIFETIME_SECONDS), and none can be revoked before exp")
    now = int(time.time())
    # aud: agentgateway checks "agentgateway", aistudio-mcp "mcp:aistudio".
    return jwt.encode(
        {"iss": ISSUER, "aud": ["agentgateway", "mcp:aistudio"], "sub": sub,
         "jti": str(uuid.uuid4()), "iat": now, "exp": now + ttl, "roles": {"aistudio": role}},
        KEY.read_text(), algorithm="ES256", headers={"kid": KID})


def main(argv: list) -> None:
    if not argv:
        raise SystemExit(__doc__)
    if argv[0] == "init":
        init()
        print(f"wrote {KEY.name} and {JWKS.name}")
        return
    if argv[0] == "tls":
        if_needed = "--if-needed" in argv
        issued = tls(_hosts([a for a in argv[1:] if a != "--if-needed"]), if_needed)
        cert = x509.load_pem_x509_certificate((TLS / "cert.pem").read_bytes())
        state = "issued" if issued else "current"
        print(f"{state}: tls/cert.pem for {', '.join(_cert_hosts(cert))}, "
              f"expires {cert.not_valid_after_utc:%Y-%m-%d}; clients trust tls/ca.pem")
        return
    ttl = 3600
    if "--ttl" in argv:
        i = argv.index("--ttl")
        ttl = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    print(token(argv[0], argv[1] if len(argv) > 1 else f"local-{argv[0]}", ttl))


if __name__ == "__main__":
    main(sys.argv[1:])
