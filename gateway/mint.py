"""Signing key and tokens for the MCP door (agentgateway + aistudio-mcp both verify them).

  python gateway/mint.py init                         # make dev-key.pem + jwks.json (both gitignored)
  python gateway/mint.py viewer|operator|admin [sub] [--ttl SECONDS]   # print an ES256 JWT

`make mcp-init` / `make mcp-token ROLE=admin` run this inside the aistudio-mcp image, so the host
needs no Python packages. Whoever holds dev-key.pem can mint any role: keep it on this host.
After `init` again (a new key), restart agentgateway: it reads jwks.json only at start.
"""
import json
import sys
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

HERE = Path(__file__).resolve().parent
KEY = HERE / "dev-key.pem"
JWKS = HERE / "jwks.json"
KID = "aistudio-1"
ISSUER = "aistudio"
ROLES = ("viewer", "operator", "admin")


def init() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    KEY.chmod(0o600)
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=KID, alg="ES256", use="sig")
    JWKS.write_text(json.dumps({"keys": [jwk]}, indent=2) + "\n")


def token(role: str, sub: str, ttl: int) -> str:
    if role not in ROLES:
        raise SystemExit(f"role must be one of {', '.join(ROLES)}")
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
    ttl = 3600
    if "--ttl" in argv:
        i = argv.index("--ttl")
        ttl = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    print(token(argv[0], argv[1] if len(argv) > 1 else f"local-{argv[0]}", ttl))


if __name__ == "__main__":
    main(sys.argv[1:])
