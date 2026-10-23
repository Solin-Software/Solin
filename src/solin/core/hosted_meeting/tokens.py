"""Galene join tokens: HS256 JWTs signed with a per-meeting group key.

Galene accepts a token when its signature matches a key in the group's ``authKeys``,
``aud`` names the group URL and ``exp`` has not passed; ``sub`` becomes the username
and ``permissions`` the user's permissions (``[]`` = observe). Galene checks tokens
only when a client joins, so Solin issues short-lived *join* tokens and a browser
that reconnects asks the hall for a fresh one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Sequence

GROUP_KEY_BYTES = 32
JOIN_TOKEN_TTL_SECONDS = 120

OBSERVE: tuple[str, ...] = ()
PRESENT: tuple[str, ...] = ("present", "message")
OPERATOR: tuple[str, ...] = ("op", "present", "message")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_group_key() -> bytes:
    """A fresh random HS256 key for one meeting's group."""

    return secrets.token_bytes(GROUP_KEY_BYTES)


def group_jwk(key: bytes) -> dict[str, str]:
    """The key as the JWK Galene expects in a group's ``authKeys``."""

    return {"kty": "oct", "alg": "HS256", "k": _b64url(key)}


def sign_token(
    key: bytes,
    *,
    username: str,
    audience: str,
    permissions: Sequence[str] = OBSERVE,
    ttl_seconds: int = JOIN_TOKEN_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Sign a Galene join token for ``username`` in the group at ``audience``."""

    if not username:
        raise ValueError("a token needs a username")
    issued = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "sub": username,
        "aud": audience,
        "permissions": list(permissions),
        "iat": issued,
        "exp": issued + int(ttl_seconds),
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        for part in (header, claims)
    )
    signature = hmac.new(key, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def decode_claims(token: str) -> dict[str, object]:
    """The unverified claims of a token (for tests and diagnostics)."""

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def verify_token(key: bytes, token: str) -> bool:
    """True when ``token`` carries a valid HS256 signature for ``key``."""

    try:
        signing_input, signature = token.rsplit(".", 1)
    except ValueError:
        return False
    expected = _b64url(hmac.new(key, signing_input.encode("ascii"), hashlib.sha256).digest())
    return hmac.compare_digest(expected, signature)
