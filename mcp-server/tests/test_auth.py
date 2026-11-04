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


def test_a_keycloak_token_names_its_client_in_azp(verifier, mint):
    who = identity_from_claims(verifier.verify(mint(role="viewer", azp="aistudio-mcp-clients")))
    assert who.client_id == "aistudio-mcp-clients"


def test_only_the_aistudio_role_counts(verifier, mint):
    who = identity_from_claims(verifier.verify(mint(roles={"composer": "admin"})))
    assert who.role is None


@pytest.mark.parametrize("value, role", [
    (["operator"], "operator"),
    (["viewer", "admin"], None),      # which one would hang on claim order
    ([], None),
])
def test_a_role_list_counts_only_with_one_entry(value, role):
    assert identity_from_claims({"sub": "alice", "roles": {"aistudio": value}}).role == role


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


def test_the_keycloak_realm_issues_the_claims_both_checks_expect():
    """gateway/keycloak/aistudio-realm.json: ES256, audience aistudio-mcp, role in roles.aistudio."""
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    realm = json.loads((root / "gateway" / "keycloak" / "aistudio-realm.json").read_text(encoding="utf-8"))
    (client,) = realm["clients"]
    mappers = {m["protocolMapper"]: m["config"] for m in client["protocolMappers"]}
    assert realm["defaultSignatureAlgorithm"] == "ES256"
    assert mappers["oidc-audience-mapper"]["included.custom.audience"] == "aistudio-mcp"
    assert mappers["oidc-usermodel-attribute-mapper"]["claim.name"] == "roles.aistudio"
    assert client["publicClient"] and client["attributes"]["pkce.code.challenge.method"] == "S256"
    gateway = (root / "gateway" / "config.yaml").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    # Keycloak itself is the issuer clients check (RFC 9207): no adapter that renames it.
    assert "issuer: ${MCP_PUBLIC_URL}/realms/aistudio" in gateway and "- aistudio-mcp\n" in gateway
    # No OAuth provider adapter in front of Keycloak (agent-runner's /llm has an LLM provider; that's fine).
    import yaml
    mcp_auth = [r["policies"]["mcpAuthentication"] for b in yaml.safe_load(gateway)["binds"]
                for listener in b["listeners"] for r in listener["routes"] if "mcpAuthentication" in r.get("policies", {})]
    assert mcp_auth and all("provider" not in a for a in mcp_auth)
    # A self-registered client must not bring its own claim mappers (roles.aistudio from firstName).
    assert "pathPrefix: /realms/\n" not in gateway and "pathPrefix: /realms/aistudio/clients-registrations" not in gateway
    assert "exact: /realms/aistudio/clients-registrations/openid-connect" in gateway
    kc = (root / "gateway" / "kc.py").read_text(encoding="utf-8")
    assert "allowed-protocol-mappers" in kc and "usermodel" not in kc.split("SAFE_MAPPERS =")[1].split("\n")[0]
    assert 'MCP_JWT_AUDIENCE: "aistudio-mcp"' in compose


def test_a_keycloak_shaped_token_is_accepted(jwks_file, mint):
    iss = "https://10.0.0.5:8050/realms/aistudio"
    claims = JWTVerifier(None, iss, "aistudio-mcp", jwks_file=jwks_file).verify(
        mint(role="operator", iss=iss, aud="aistudio-mcp", azp="aistudio-mcp-clients", ttl=600))
    who = identity_from_claims(claims)
    assert (who.role, who.client_id) == ("operator", "aistudio-mcp-clients")


def test_made_up_kids_do_not_reread_the_jwks_every_time(verifier, mint, monkeypatch):
    loads = []
    real = verifier._jwks._load
    monkeypatch.setattr(verifier._jwks, "_load", lambda: loads.append(1) or real())
    verifier._jwks._loaded_at = 0.0          # long ago: the first unknown kid may re-read
    for i in range(5):
        with pytest.raises(AuthError):
            verifier.verify(mint(kid=f"made-up-{i}"))
    assert len(loads) == 1
