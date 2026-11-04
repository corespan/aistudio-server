"""Verify the caller's JWT on every /mcp request, again after agentgateway did."""
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import anyio
import jwt
from jwt import PyJWKClient

log = logging.getLogger("aistudio-mcp.auth")

STATE_KEY = "aistudio_identity"


class AuthError(Exception):
    """The request carries no valid JWT."""


@dataclass(frozen=True)
class Identity:
    sub: str
    role: Optional[str] = None          # roles.aistudio
    client_id: Optional[str] = None
    claims: dict = field(default_factory=dict, compare=False, repr=False)


def _clean(value, limit: int = 128) -> Optional[str]:
    """An identifier that is safe to log, or None."""
    if value is None:
        return None
    value = str(value).strip()
    if not value or not value.isprintable():
        return None
    return value[:limit]


def _aistudio_role(claims: dict) -> Optional[str]:
    # Nested or flat. A list counts only with one entry: picking one of several would depend on
    # claim order, and agentgateway's `jwt.roles.aistudio in [...]` matches no list at all.
    roles = claims.get("roles")
    value = roles.get("aistudio") if isinstance(roles, dict) else claims.get("roles.aistudio")
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    return str(value) if value else None


def identity_from_claims(claims: dict) -> Identity:
    return Identity(sub=str(claims["sub"]), role=_aistudio_role(claims),
                    client_id=_clean(claims.get("client_id") or claims.get("azp")), claims=claims)


class _FileJWKS:
    """Keys from a local JWKS file. Re-read on an unknown kid, so a rotated key needs no restart."""

    # Throttled: made-up kids must not cost a file read per request.
    RELOAD_EVERY = 30.0

    def __init__(self, path: str):
        self.path = path
        self._keys = self._load()
        self._loaded_at = time.monotonic()

    def _load(self) -> dict:
        with open(self.path, encoding="utf-8") as fh:
            return {k.key_id: k for k in jwt.PyJWKSet.from_dict(json.load(fh)).keys}

    def get_signing_key_from_jwt(self, token: str):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self._keys and time.monotonic() - self._loaded_at >= self.RELOAD_EVERY:
            self._loaded_at = time.monotonic()
            self._keys = self._load()
        if kid not in self._keys:
            raise jwt.PyJWKClientError(f"no key with kid {kid!r} in {self.path}")
        return self._keys[kid]


class JWTVerifier:
    """Verify JWTs against a JWKS URL or a local JWKS file."""

    def __init__(self, jwks_url: Optional[str], issuer: str, audience: str,
                 leeway_seconds: int = 30, jwks_cache_seconds: int = 300,
                 jwks_file: Optional[str] = None, max_lifetime_seconds: int = 86400):
        if not (jwks_url or jwks_file) or not issuer or not audience:
            raise ValueError("JWT verification needs MCP_JWKS_URL (or MCP_JWKS_FILE), "
                             "MCP_JWT_ISSUER and MCP_JWT_AUDIENCE")
        self.issuer = issuer
        self.audience = audience
        self.leeway = leeway_seconds
        self.max_lifetime = max_lifetime_seconds
        self._jwks = (_FileJWKS(jwks_file) if jwks_file else
                      PyJWKClient(jwks_url, cache_keys=True, lifespan=jwks_cache_seconds))

    def verify(self, token: str) -> dict:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["ES256"], audience=self.audience, issuer=self.issuer,
                leeway=self.leeway, options={"require": ["exp", "iat", "sub", "iss", "aud", "jti"]})
        except jwt.PyJWKClientError as exc:
            raise AuthError(f"signing key unavailable: {exc}") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"invalid token: {exc}") from exc
        # No revocation list, so lifetimes stay short.
        if self.max_lifetime > 0 and claims["exp"] - claims["iat"] > self.max_lifetime:
            raise AuthError(f"invalid token: lifetime over {self.max_lifetime} s (MCP_JWT_MAX_LIFETIME_SECONDS)")
        if _clean(claims["sub"], 256) != claims["sub"]:
            raise AuthError("invalid token: sub is empty, too long or has a control character")
        return claims


def bearer_token(headers: list) -> Optional[str]:
    for name, value in headers:
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
            return None
    return None


class JWTAuthMiddleware:
    """ASGI middleware: 401 unless the request carries a valid JWT."""

    def __init__(self, app, verifier: JWTVerifier, open_paths: tuple = ()):
        self.app = app
        self.verifier = verifier
        self.open_paths = frozenset(open_paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in self.open_paths:
            await self.app(scope, receive, send)
            return
        token = bearer_token(scope.get("headers") or [])
        if token is None:
            await _unauthorized(send, "missing bearer token")
            return
        try:
            # Off the event loop: a JWKS URL fetch blocks.
            claims = await anyio.to_thread.run_sync(self.verifier.verify, token)
        except AuthError as exc:
            # The reason goes to the log only.
            log.info("rejected /mcp call: %s", exc)
            await _unauthorized(send, "invalid or expired token")
            return
        scope.setdefault("state", {})[STATE_KEY] = identity_from_claims(claims)
        await self.app(scope, receive, send)


async def _unauthorized(send, detail: str) -> None:
    body = json.dumps({"title": "unauthorized", "status": 401, "detail": detail}).encode("utf-8")
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/problem+json"),
        (b"www-authenticate", b'Bearer error="invalid_token"'),
        (b"content-length", str(len(body)).encode("ascii")),
    ]})
    await send({"type": "http.response.body", "body": body})
