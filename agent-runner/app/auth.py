"""Caller authentication: the JWT on every request, verified against the gateway's keys.

There is no proxy in this install, so the panel sends the token gateway/mint.py made (audience
agent-runner). The runner forwards the same token to agentgateway for every agent in the run, so
the gateway and the MCP server enforce the caller's role on every tool call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import jwt
import structlog
from fastapi import Header, HTTPException

from app.config import settings

log = structlog.get_logger()


@dataclass(frozen=True)
class Identity:
    sub: str
    role: str | None
    token: str = field(repr=False)


class _FileJWKS:
    """Keys from the mounted JWKS file. Re-read on an unknown kid, so a new key needs no restart."""

    def __init__(self, path: str):
        self.path = path
        self._keys: dict = {}

    def key(self, token: str):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self._keys:
            with open(self.path, encoding="utf-8") as fh:
                self._keys = {k.key_id: k for k in jwt.PyJWKSet.from_dict(json.load(fh)).keys}
        if kid not in self._keys:
            raise jwt.InvalidTokenError(f"no key with kid {kid!r}")
        return self._keys[kid].key


_jwks = _FileJWKS(settings.jwks_file)


def _role(claims: dict) -> str | None:
    roles = claims.get("roles")
    value = roles.get("aistudio") if isinstance(roles, dict) else None
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value else None


def verify(token: str) -> Identity:
    """The caller, or HTTPException 401. The reason is logged, never returned."""
    try:
        claims = jwt.decode(
            token, _jwks.key(token), algorithms=["ES256"], audience=settings.jwt_audience,
            issuer=settings.jwt_issuer, leeway=settings.jwt_leeway_seconds,
            options={"require": ["exp", "sub", "iss", "aud", "jti"]})
    except (jwt.InvalidTokenError, OSError, ValueError) as exc:
        log.info("auth.rejected", reason=str(exc))
        raise HTTPException(401, "invalid or expired token",
                            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}) from None
    sub = str(claims["sub"])
    if not sub.strip() or not sub.isprintable() or len(sub) > 256:
        raise HTTPException(401, "invalid or expired token")
    return Identity(sub=sub, role=_role(claims), token=token)


def caller(authorization: str = Header(default="")) -> Identity:
    """FastAPI dependency: the verified caller."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(401, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    return verify(token.strip())
