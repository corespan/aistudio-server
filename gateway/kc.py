"""Keycloak helper for the MCP door (this text is the CLI help):

  python gateway/kc.py env                  # write keycloak.env (admin + DB passwords) if missing
  python gateway/kc.py setup                # client scope, registration policy, first admin (idempotent; make mcp-up runs it)
  python gateway/kc.py prune                # remove self-registered clients with no sessions
  python gateway/kc.py user NAME ROLE       # add or update a user: viewer|operator|admin|none
  python gateway/kc.py token ROLE           # log in a mcp-check-ROLE user as an MCP client would, print its token
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
SECRETS = Path(os.environ.get("MCP_SECRETS") or Path.home() / ".aistudio-mcp")   # as in mint.py
ENV = SECRETS / "keycloak.env"
CA = SECRETS / "tls" / "ca.pem"
FIRST_ADMIN = SECRETS / "first-admin.txt"
REALM_FILE = HERE / "keycloak" / "aistudio-realm.json"
INTERNAL = "http://keycloak:8080"
REALM = "aistudio"
CLIENT = "aistudio-mcp-clients"
SCOPE = "aistudio-mcp"
ROLES = ("viewer", "operator", "admin")
REDIRECT = "http://localhost:6274/oauth/callback"
# Fixed claim names only (name, address, pairwise sub): none can write roles.aistudio.
SAFE_MAPPERS = ["oidc-full-name-mapper", "oidc-address-mapper", "oidc-sha256-pairwise-sub-mapper"]


def env() -> None:
    if ENV.exists():
        print(f"{ENV.name} exists; kept")
        return
    admin, db = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    body = f"KC_BOOTSTRAP_ADMIN_USERNAME=admin\nKC_BOOTSTRAP_ADMIN_PASSWORD={admin}\nKC_DB_PASSWORD={db}\n"
    with os.fdopen(os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as fh:
        fh.write(body)
    print(f"wrote {ENV.name}; Keycloak admin console user: admin (password in that file)")


def _admin():
    import httpx

    conf = dict(line.split("=", 1) for line in ENV.read_text().splitlines() if "=" in line)
    tok = httpx.post(f"{INTERNAL}/realms/master/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "admin-cli",
        "username": conf["KC_BOOTSTRAP_ADMIN_USERNAME"], "password": conf["KC_BOOTSTRAP_ADMIN_PASSWORD"]})
    tok.raise_for_status()
    return httpx.Client(base_url=f"{INTERNAL}/admin/realms/{REALM}", timeout=20,
                        headers={"Authorization": f"Bearer {tok.json()['access_token']}"})


def setup() -> None:
    """Self-registered MCP clients get the audience and role claims, and may only redirect to localhost."""
    c = _admin()
    mappers = json.loads(REALM_FILE.read_text(encoding="utf-8"))["clients"][0]["protocolMappers"]
    scopes = {s["name"]: s for s in c.get("/client-scopes").json()}
    if SCOPE not in scopes:
        c.post("/client-scopes", json={
            "name": SCOPE, "protocol": "openid-connect", "protocolMappers": mappers, "attributes": {
                "include.in.token.scope": "false", "display.on.consent.screen": "true",
                "consent.screen.text": "Use the AIStudio MCP tools with your AIStudio role"}}).raise_for_status()
        scopes = {s["name"]: s for s in c.get("/client-scopes").json()}
    if SCOPE not in {s["name"] for s in c.get("/default-default-client-scopes").json()}:
        c.put(f"/default-default-client-scopes/{scopes[SCOPE]['id']}").raise_for_status()
    kind = "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
    for comp in c.get("/components", params={"type": kind}).json():
        if comp["providerId"] == "trusted-hosts" and comp.get("subType") == "anonymous":
            comp["config"].update({"trusted-hosts": ["localhost", "127.0.0.1"],
                                   "host-sending-registration-request-must-match": ["false"],
                                   "client-uris-must-match": ["true"]})
            c.put(f"/components/{comp['id']}", json=comp).raise_for_status()
        # MCP clients (MCP Inspector) register with scope "openid offline_access"; Keycloak checks each name.
        if comp["providerId"] == "allowed-client-templates" and comp.get("subType") == "anonymous":
            comp["config"].update({"allowed-client-scopes": ["openid", "offline_access", SCOPE],
                                   "allow-default-scopes": ["true"]})
            c.put(f"/components/{comp['id']}", json=comp).raise_for_status()
        # Mappers with a configurable claim name let a user copy a field they can edit into roles.aistudio.
        if comp["providerId"] == "allowed-protocol-mappers" and comp.get("subType") == "anonymous":
            comp["config"]["allowed-protocol-mapper-types"] = SAFE_MAPPERS
            c.put(f"/components/{comp['id']}", json=comp).raise_for_status()
    print(f"Keycloak: scope {SCOPE} on every new client; self-registration limited to localhost redirects, no role mappers")
    first_admin(c)


def needs_first_admin(admins: list) -> bool:
    """No AIStudio admin yet: mcp-check-admin (make mcp-check's own user) does not count."""
    return all(u["username"].startswith("mcp-check-") for u in admins)


def first_admin(c) -> None:
    """A fresh realm has no users: create one admin with a one-time password, kept in FIRST_ADMIN."""
    if not needs_first_admin(c.get("/users", params={"q": "aistudio_role:admin", "max": 1000}).json()):
        return
    name = os.environ.get("MCP_ADMIN_USER") or "aistudio-admin"
    if c.get("/users", params={"username": name, "exact": "true"}).json():
        print(f"no AIStudio admin, and {name} exists without the role: make mcp-user NAME=<user> ROLE=admin")
        return
    password = user(name, "admin")
    FIRST_ADMIN.unlink(missing_ok=True)
    with os.fdopen(os.open(FIRST_ADMIN, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as fh:
        fh.write(f"user: {name}\none-time password: {password}\n"
                 "Keycloak asks for a new password at the first login; delete this file after it.\n")
    print(f"created the first AIStudio admin: {name}, one-time password {password} "
          f"(also in {FIRST_ADMIN.name} in MCP_SECRETS_DIR)")


def user(name: str, role: str, password: str = "", temporary: bool = True) -> str:
    """Create `name` or set its role (`none` clears it); a new user gets a one-time password, returned."""
    if role not in ROLES + ("none",):
        raise SystemExit(f"role must be one of {', '.join(ROLES)} or none")
    c = _admin()
    found = c.get("/users", params={"username": name, "exact": "true"}).json()
    attrs = {"aistudio_role": [role]} if role != "none" else {}
    if not found:
        password = password or secrets.token_urlsafe(12)
        c.post("/users", json={"username": name, "enabled": True, "attributes": attrs, "credentials": [
            {"type": "password", "value": password, "temporary": temporary}]}).raise_for_status()
        return password
    rep = found[0]
    rep["attributes"] = {**{k: v for k, v in (rep.get("attributes") or {}).items() if k != "aistudio_role"},
                         **attrs}
    c.put(f"/users/{rep['id']}", json=rep).raise_for_status()
    if password:
        c.put(f"/users/{rep['id']}/reset-password",
              json={"type": "password", "value": password, "temporary": temporary}).raise_for_status()
    return ""


def idle_registered(clients: list, sessions) -> list:
    """Self-registered clients (Keycloak names them by UUID) that no one is logged in through."""
    uuid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    return [x for x in clients if uuid.fullmatch(x["clientId"]) and not sessions(x["id"])]


def prune() -> None:
    """Remove idle self-registered clients, so registrations never fill Keycloak's 200-client limit."""
    c = _admin()

    def sessions(cid):
        return sum(c.get(f"/clients/{cid}/{k}-count").json()["count"] for k in ("session", "offline-session"))
    gone = idle_registered(c.get("/clients", params={"max": 10000}).json(), sessions)
    for x in gone:
        c.delete(f"/clients/{x['id']}").raise_for_status()
    print(f"removed {len(gone)} idle self-registered client(s)")


def token(role: str) -> str:
    """Discovery from the MCP URL, then a real login (code + PKCE), checking the issuer as RFC 9207 clients do."""
    import httpx

    password = secrets.token_urlsafe(18)
    user(f"mcp-check-{role}", role, password, temporary=False)
    base = os.environ["MCP_PUBLIC_URL"].rstrip("/")
    c = httpx.Client(verify=str(CA), follow_redirects=False, timeout=20)
    issuer = c.get(f"{base}/.well-known/oauth-protected-resource/mcp/aistudio").json()["authorization_servers"][0]
    u = urlparse(issuer)
    meta = c.get(f"{u.scheme}://{u.netloc}/.well-known/oauth-authorization-server{u.path}").json()
    if meta["issuer"] != issuer:
        raise SystemExit(f"metadata issuer {meta['issuer']} is not {issuer}")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    page = c.get(meta["authorization_endpoint"], params={
        "client_id": CLIENT, "redirect_uri": REDIRECT, "response_type": "code", "scope": "openid",
        "state": "check", "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": f"{base}/mcp/aistudio"})
    action = re.search(r'action="([^"]+)"', page.text).group(1).replace("&amp;", "&")
    done = c.post(action, data={"username": f"mcp-check-{role}", "password": password, "credentialId": ""})
    query = parse_qs(urlparse(done.headers["location"]).query)
    if query.get("iss", [issuer])[0] != issuer:
        raise SystemExit(f"authorization response iss {query['iss'][0]} is not {issuer} (RFC 9207)")
    tok = c.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
        "client_id": CLIENT, "code_verifier": verifier})
    tok.raise_for_status()
    return tok.json()["access_token"]


def main(argv: list) -> None:
    if not argv:
        raise SystemExit(__doc__)
    if argv[0] == "env":
        env()
    elif argv[0] == "setup":
        setup()
    elif argv[0] == "prune":
        prune()
    elif argv[0] == "user" and len(argv) == 3:
        pw = user(argv[1], argv[2])
        print(f"created {argv[1]} ({argv[2]}); one-time password: {pw}" if pw else f"{argv[1]}: role {argv[2]}")
    elif argv[0] == "token" and len(argv) == 2 and argv[1] in ROLES:
        print(token(argv[1]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
