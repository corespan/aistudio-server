"""gateway/mint.py tls: the local CA and the gateway's certificate. Offline."""
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509

_spec = importlib.util.spec_from_file_location(
    "mint", Path(__file__).resolve().parents[2] / "gateway" / "mint.py")
mint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mint)


@pytest.fixture(autouse=True)
def tls_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mint, "TLS", tmp_path / "tls")
    monkeypatch.delenv("MCP_TLS_HOSTS", raising=False)
    return tmp_path / "tls"


def _cert(tls_dir) -> x509.Certificate:
    return x509.load_pem_x509_certificate((tls_dir / "cert.pem").read_bytes())


def _ca(tls_dir) -> x509.Certificate:
    return x509.load_pem_x509_certificate((tls_dir / "ca.pem").read_bytes())


def test_the_cert_names_every_host_and_is_signed_by_the_ca(tls_dir):
    assert mint.tls(["mcp.example.com", "10.0.0.5", "::1"])
    cert = _cert(tls_dir)
    assert mint._cert_hosts(cert) == ["mcp.example.com", "10.0.0.5", "::1"]
    cert.verify_directly_issued_by(_ca(tls_dir))
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert _ca(tls_dir).extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_hosts_come_from_the_environment_space_or_comma_separated(monkeypatch):
    monkeypatch.setenv("MCP_TLS_HOSTS", "a.example, 10.0.0.1  a.example")
    assert mint._hosts([]) == ["a.example", "10.0.0.1"]
    assert mint._hosts(["b.example"]) == ["b.example"]


def test_if_needed_keeps_a_fresh_cert_that_covers_the_hosts(tls_dir):
    mint.tls(["10.0.0.5", "localhost"])
    before = (tls_dir / "cert.pem").read_bytes()
    assert not mint.tls(["10.0.0.5"], if_needed=True)
    assert (tls_dir / "cert.pem").read_bytes() == before


def test_if_needed_reissues_a_cert_from_an_earlier_ca(tls_dir):
    mint.tls(["10.0.0.5"])
    (tls_dir.parent / "ca.key").unlink()      # lost: the next run makes a new CA
    assert mint.tls(["10.0.0.5"], if_needed=True)
    _cert(tls_dir).verify_directly_issued_by(_ca(tls_dir))


def test_the_ca_can_sign_only_the_names_it_was_made_for(tls_dir):
    mint.tls(["mcp.example.com", "10.0.0.5", "S53"])
    ca = _ca(tls_dir)
    assert mint._permits(ca, "mcp.example.com") and mint._permits(ca, "a.mcp.example.com")
    assert mint._permits(ca, "S53") and "S53" in mint._cert_hosts(_cert(tls_dir))   # DNS is case-blind
    assert mint._permits(ca, "10.0.0.5")
    for other in ["google.com", "example.com", "10.0.0.6", "evilmcp.example.com"]:
        assert not mint._permits(ca, other), other


def test_a_name_outside_the_ca_is_left_out_and_the_old_names_kept(tls_dir):
    mint.tls(["mcp.example.com", "10.0.0.5"])
    ca_before = (tls_dir / "ca.pem").read_bytes()
    assert not mint.tls(["mcp.example.com", "172.18.0.1"], if_needed=True)   # a new docker bridge IP
    assert mint._cert_hosts(_cert(tls_dir)) == ["mcp.example.com", "10.0.0.5"]
    assert (tls_dir / "ca.pem").read_bytes() == ca_before, "a new CA would make every client re-trust"


def test_a_public_host_outside_the_ca_asks_for_a_new_ca(tls_dir):
    mint.tls(["10.0.0.5"])
    with pytest.raises(SystemExit, match="not made for 10.0.0.9"):
        mint.tls(["10.0.0.9"], if_needed=True)


def _handshake(tls_dir, name):
    import socket
    import ssl
    import threading
    srv = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv.load_cert_chain(tls_dir / "cert.pem", tls_dir / "key.pem")
    a, b = socket.socketpair()

    def serve():
        try:
            srv.wrap_socket(a, server_side=True).close()
        except OSError:
            pass          # the client hangs up right after the handshake
    t = threading.Thread(target=serve)
    t.start()
    try:
        ssl.create_default_context(cafile=str(tls_dir / "ca.pem")).wrap_socket(b, server_hostname=name).close()
    finally:
        b.close()
        t.join()


@pytest.mark.parametrize("name", ["10.0.0.5", "mcp.example.com"])
def test_openssl_verifies_the_cert_under_the_constraints(tls_dir, name):
    mint.tls(["10.0.0.5", "mcp.example.com"])
    _handshake(tls_dir, name)


@pytest.mark.parametrize("made_for,forged", [(["10.0.0.5"], "google.com"), (["mcp.example.com"], "10.0.0.9")])
def test_openssl_refuses_a_cert_the_ca_was_not_made_for(tls_dir, monkeypatch, made_for, forged):
    import ssl
    mint.tls(made_for)
    monkeypatch.setattr(mint, "_permits", lambda ca, h: True)   # as if someone stole ca.key
    mint.tls([forged])
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(tls_dir, forged)


def test_a_cert_near_expiry_is_renewed(tls_dir, monkeypatch):
    monkeypatch.setattr(mint, "CERT_DAYS", 10)
    mint.tls(["10.0.0.5"])
    assert mint.tls(["10.0.0.5"], if_needed=True)
    left = _cert(tls_dir).not_valid_after_utc - datetime.now(timezone.utc)
    assert left > timedelta(days=9)


def test_a_forced_reissue_uses_exactly_the_hosts_given(tls_dir):
    mint.tls(["old.example", "10.0.0.5"])
    mint.tls(["10.0.0.5"])
    assert mint._cert_hosts(_cert(tls_dir)) == ["10.0.0.5"]


def test_the_ca_key_is_not_in_the_directory_the_gateway_mounts(tls_dir):
    mint.tls(["localhost"])
    assert not (tls_dir / "ca.key").exists() and (tls_dir.parent / "ca.key").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_ca_key_is_owner_only(tls_dir):
    mint.tls(["localhost"])
    assert (tls_dir.parent / "ca.key").stat().st_mode & 0o077 == 0
