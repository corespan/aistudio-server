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


def test_a_new_address_is_added_and_the_old_names_kept(tls_dir):
    mint.tls(["mcp.example.com", "10.0.0.5"])
    ca_before = (tls_dir / "ca.pem").read_bytes()
    assert mint.tls(["10.0.0.9"], if_needed=True)
    assert mint._cert_hosts(_cert(tls_dir)) == ["mcp.example.com", "10.0.0.5", "10.0.0.9"]
    assert (tls_dir / "ca.pem").read_bytes() == ca_before, "a new CA would make every client re-trust"


def test_a_cert_near_expiry_is_renewed(tls_dir, monkeypatch):
    monkeypatch.setattr(mint, "CERT_DAYS", 10)
    mint.tls(["10.0.0.5"])
    assert mint.tls(["10.0.0.5"], if_needed=True)
    left = _cert(tls_dir).not_valid_after_utc - datetime.now(timezone.utc)
    assert left > timedelta(days=9)


def test_a_forced_reissue_uses_exactly_the_hosts_given(tls_dir):
    mint.tls(["old.example", "10.0.0.5"])
    mint.tls(["new.example"])
    assert mint._cert_hosts(_cert(tls_dir)) == ["new.example"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_ca_key_is_owner_only(tls_dir):
    mint.tls(["localhost"])
    assert (tls_dir / "ca.key").stat().st_mode & 0o077 == 0
