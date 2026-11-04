"""Shared offline fixtures: a throwaway ES256 key, its JWKS and a JWT minter."""
import json
import os
import time
import types

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

# Set before app.config is imported.
os.environ["MCP_ENV_FILE"] = os.devnull

KID = "test-key-1"


@pytest.fixture(autouse=True)
def writes_on(monkeypatch):
    """The write tools switched on (MCP_WRITES_ENABLED), since most tests exercise them;
    test_guards checks the switch itself."""
    monkeypatch.setattr("app.config.settings.mcp_writes_enabled", True)


@pytest.fixture(scope="session")
def signing_key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def jwks_file(tmp_path, signing_key, monkeypatch):
    """The key's public half as a local JWKS file, wired in as MCP_JWKS_FILE."""
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(signing_key.public_key()))
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps({"keys": [{**jwk, "kid": KID, "alg": "ES256", "use": "sig"}]}))
    monkeypatch.setattr("app.config.settings.mcp_jwks_file", str(path))
    monkeypatch.setattr("app.config.settings.mcp_jwks_url", "")
    return str(path)


@pytest.fixture
def mint(signing_key):
    """mint(role="viewer", sub="alice", ...) -> a signed JWT."""
    def _mint(role="viewer", sub="alice", aud="mcp:aistudio", iss="aistudio", ttl=300,
              kid=KID, roles=None, key=None, algorithm="ES256", jti="jti-1", **claims):
        now = int(time.time())
        body = {"iss": iss, "sub": sub, "aud": ["agentgateway", aud], "iat": now,
                "exp": now + ttl,
                "roles": roles if roles is not None else ({"aistudio": role} if role else {}),
                **({"jti": jti} if jti else {}), **claims}
        return jwt.encode(body, key or signing_key, algorithm=algorithm, headers={"kid": kid})
    return _mint


class _CIHeaders:
    def __init__(self, mapping=None):
        self._d = {str(k).lower(): v for k, v in (mapping or {}).items()}

    def get(self, key, default=None):
        return self._d.get(str(key).lower(), default)


def make_ctx(identity=None):
    """A tool Context whose request carries ``identity`` in request.state."""
    from app.auth import STATE_KEY
    state = types.SimpleNamespace(**({STATE_KEY: identity} if identity is not None else {}))
    request = types.SimpleNamespace(headers=_CIHeaders(), state=state)
    return types.SimpleNamespace(request_context=types.SimpleNamespace(request=request))


def ident(sub="alice", role="viewer", **kw):
    from app.auth import Identity
    return Identity(sub=sub, role=role, **kw)
