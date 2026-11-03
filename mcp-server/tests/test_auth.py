"""Caller authentication: what a valid JWT is, and what is refused."""
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.auth import AuthError, JWTVerifier, bearer_token, identity_from_claims


@pytest.fixture
def verifier(jwks_file):
    return JWTVerifier(None, "aistudio", "mcp:aistudio", jwks_file=jwks_file)


def test_a_valid_token_yields_the_aistudio_role(verifier, mint):
    who = identity_from_claims(verifier.verify(mint(role="operator", client_id="claude-code")))
    assert (who.sub, who.role, who.client_id) == ("alice", "operator", "claude-code")


def test_only_the_aistudio_role_counts(verifier, mint):
    who = identity_from_claims(verifier.verify(mint(roles={"composer": "admin"})))
    assert who.role is None


@pytest.mark.parametrize("bad", [
    dict(ttl=-120),                                   # expired
    dict(aud="mcp:composer"),                         # a token for another server
    dict(iss="someone-else"),                         # wrong issuer
    dict(kid="unknown-key"),                          # key not in the JWKS
    dict(jti=None),                                   # every token carries a jti
    dict(ttl=86400 + 60),                             # longer than MCP_JWT_MAX_LIFETIME_SECONDS
])
def test_refused(verifier, mint, bad):
    with pytest.raises(AuthError):
        verifier.verify(mint(**bad))


def test_a_token_signed_by_another_key_is_refused(verifier, mint):
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(AuthError):
        verifier.verify(mint(key=other))


def test_hs256_is_refused(verifier):
    forged = jwt.encode({"iss": "aistudio", "sub": "alice", "aud": "mcp:aistudio",
                         "exp": 4102444800}, "secret-of-at-least-32-bytes-long-xxxx",
                        algorithm="HS256", headers={"kid": "test-key-1"})
    with pytest.raises(AuthError):
        verifier.verify(forged)


def test_bearer_parsing():
    assert bearer_token([(b"authorization", b"Bearer abc")]) == "abc"
    assert bearer_token([(b"authorization", b"Basic abc")]) is None
    assert bearer_token([]) is None


def test_verifier_needs_a_key_source():
    with pytest.raises(ValueError):
        JWTVerifier(None, "aistudio", "mcp:aistudio")


def test_a_client_id_that_cannot_be_logged_safely_is_dropped():
    assert identity_from_claims({"sub": "a", "client_id": "x\ny"}).client_id is None


@pytest.mark.parametrize("sub", ["a\tb", "", " alice", "x" * 300])
def test_a_sub_that_cannot_be_logged_safely_is_refused(verifier, mint, sub):
    with pytest.raises(AuthError):
        verifier.verify(mint(sub=sub))


def test_the_mint_script_makes_tokens_both_checks_accept(tmp_path, monkeypatch):
    """gateway/mint.py's keys and claims are the ones agentgateway and this server expect."""
    import importlib.util
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "gateway" / "mint.py"
    spec = importlib.util.spec_from_file_location("mint", src)
    mint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mint)
    monkeypatch.setattr(mint, "KEY", tmp_path / "dev-key.pem")
    monkeypatch.setattr(mint, "JWKS", tmp_path / "jwks.json")
    mint.init()
    token = mint.token("operator", "bob", ttl=60)
    claims = JWTVerifier(None, "aistudio", "mcp:aistudio", jwks_file=str(tmp_path / "jwks.json")).verify(token)
    assert "agentgateway" in claims["aud"] and identity_from_claims(claims).role == "operator"


def test_made_up_kids_do_not_reread_the_jwks_every_time(verifier, mint, monkeypatch):
    loads = []
    real = verifier._jwks._load
    monkeypatch.setattr(verifier._jwks, "_load", lambda: loads.append(1) or real())
    verifier._jwks._loaded_at = 0.0          # long ago: the first unknown kid may re-read
    for i in range(5):
        with pytest.raises(AuthError):
            verifier.verify(mint(kid=f"made-up-{i}"))
    assert len(loads) == 1
