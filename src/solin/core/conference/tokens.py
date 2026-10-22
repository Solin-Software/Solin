"""LiveKit access tokens, minted locally (an HS256 JWT signed with the server's API secret).

Kept on the standard library so neither the app nor the publisher needs ``livekit-api``.
The claim layout follows LiveKit's ``AccessToken``: ``iss`` is the API key, ``sub`` the
participant identity and ``video`` carries the room grants in camelCase.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field

DEFAULT_TTL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True, slots=True)
class RoomGrants:
    room: str
    can_publish: bool = True
    can_subscribe: bool = True
    can_publish_data: bool = True
    can_update_own_metadata: bool = False
    hidden: bool = False
    room_admin: bool = False
    can_publish_sources: tuple[str, ...] = ()

    def to_claim(self) -> dict[str, object]:
        claim: dict[str, object] = {
            "roomJoin": True,
            "room": self.room,
            "canPublish": self.can_publish,
            "canSubscribe": self.can_subscribe,
            "canPublishData": self.can_publish_data,
            "canUpdateOwnMetadata": self.can_update_own_metadata,
        }
        if self.hidden:
            claim["hidden"] = True
        if self.room_admin:
            claim["roomAdmin"] = True
        if self.can_publish_sources:
            claim["canPublishSources"] = list(self.can_publish_sources)
        return claim


@dataclass(frozen=True, slots=True)
class AccessTokenRequest:
    identity: str
    grants: RoomGrants
    name: str = ""
    metadata: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    ttl_seconds: int = DEFAULT_TTL_SECONDS


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_access_token(api_key: str, api_secret: str, request: AccessTokenRequest,
                      *, now: float | None = None) -> str:
    if not api_key or not api_secret:
        raise ValueError("LiveKit API key and secret are required")
    if not request.identity or not request.grants.room:
        raise ValueError("A participant identity and room are required")
    if request.ttl_seconds <= 0:
        raise ValueError("Token lifetime must be positive")
    issued = int(time.time() if now is None else now)
    claims: dict[str, object] = {
        "iss": api_key,
        "sub": request.identity,
        "nbf": issued,
        "exp": issued + request.ttl_seconds,
        "video": request.grants.to_claim(),
    }
    if request.name:
        claims["name"] = request.name
    if request.metadata:
        claims["metadata"] = request.metadata
    if request.attributes:
        claims["attributes"] = dict(request.attributes)
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(api_secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"
