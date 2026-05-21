"""JWT validation — port of `src/jwt.validation.ts`.

Fetches Cognito JWKs, verifies the token's signature, expiry, and audience.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import jwt
import requests
from jwt.algorithms import RSAAlgorithm

from . import config as cfg

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _jwks() -> dict:
    url = (f"https://cognito-idp.{cfg.COGNITO_REGION}.amazonaws.com/"
           f"{cfg.COGNITO_USER_POOL_ID}/.well-known/jwks.json")
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    return {k["kid"]: k for k in resp.json()["keys"]}


def validate_token(auth_header: str | None) -> dict:
    """Return decoded claims on success; raises on failure."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise PermissionError("Missing Bearer token")
    token = auth_header.split(" ", 1)[1].strip()

    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    jwk = _jwks().get(kid)
    if not jwk:
        _jwks.cache_clear()
        jwk = _jwks().get(kid)
    if not jwk:
        raise PermissionError(f"Unknown kid {kid}")

    public_key = RSAAlgorithm.from_jwk(jwk)
    claims = jwt.decode(
        token, public_key, algorithms=[header.get("alg", "RS256")],
        audience=cfg.COGNITO_CLIENT_ID if cfg.COGNITO_CLIENT_ID else None,
        options={"verify_aud": bool(cfg.COGNITO_CLIENT_ID)},
    )
    return claims
