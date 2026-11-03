"""Usage (this text is the CLI help):

  python gateway/mint.py init                         # new dev-key.pem + jwks.json; restart agentgateway
  python gateway/mint.py viewer|operator|admin [sub] [--ttl SECONDS]   # print an ES256 JWT (max 1 day)
"""
import json
import os
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
MAX_TTL = 86400   # = MCP_JWT_MAX_LIFETIME_SECONDS


def init() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    # Created 0600: never readable by others, even briefly.
    KEY.unlink(missing_ok=True)
    with os.fdopen(os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh:
        fh.write(pem)
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=KID, alg="ES256", use="sig")
    JWKS.write_text(json.dumps({"keys": [jwk]}, indent=2) + "\n")


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
    ttl = 3600
    if "--ttl" in argv:
        i = argv.index("--ttl")
        ttl = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    print(token(argv[0], argv[1] if len(argv) > 1 else f"local-{argv[0]}", ttl))


if __name__ == "__main__":
    main(sys.argv[1:])
