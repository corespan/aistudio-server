"""Credential masking (app/fence.py) on the formats tool output actually carries. Offline."""

import pytest

from app.fence import redact
from app.services.platform_api import PlatformApiError, _seg


@pytest.mark.parametrize("text, secret", [
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("X-Api-Key: 3f9a77c1e2b4", "3f9a77c1e2b4"),
    ("mysql --password hunter22 -h db", "hunter22"),
    ("    client-key-data: LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVkt", "LS0tLS1CRUdJTiBSU0Eg"),
    ('{"auths": {"ghcr.io": {"auth": "dXNlcjpnaHBfc2VjcmV0"}}}', "dXNlcjpnaHBfc2VjcmV0"),
    ('{"password": "correct horse battery staple"}', "horse"),
    ("STRIPE=sk_live_4eC39HqLyjWDarjtT1zdp7dc", "4eC39HqLyjWDarjtT1zdp7dc"),
    ("postgresql://amw:p@ss@db:5432/x", "ss@db"),
    # A key's body without its BEGIN line (read_file from_end, a grep_logs hit).
    ("MIIEowIBAAKCAQEAv3Jx9gkGq8sTqZ5wq1Yb0YjL2N0aPqk3h7R+o4mXwLxUq8x/\n"
     "-----END RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEAv3Jx9gk"),
])
def test_masked(text, secret):
    out = redact(text)
    assert secret not in out, out


@pytest.mark.parametrize("text", [
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",   # a SHA-256
    "commit 2f1c8b4e9d0a7f3b6c5e1d2a4b8c9f0e7d6a5b4c",
    "http://10.0.0.5:8002/api/v1/benchmarks?model=a@b",
    "author: Jane Doe",
    "--password-file /run/secrets/pw",
])
def test_ordinary_text_is_left_alone(text):
    assert redact(text) == text


@pytest.mark.parametrize("bad", ["..", ".", "", " .. "])
def test_an_id_cannot_name_a_parent_route(bad):
    with pytest.raises(PlatformApiError):
        _seg(bad)
