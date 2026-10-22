from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from solin.core.conference.tokens import AccessTokenRequest, RoomGrants, mint_access_token


def _decode(token: str) -> tuple[dict, dict, bytes, bytes]:
    def part(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    header, payload, signature = token.split(".")
    return (json.loads(part(header)), json.loads(part(payload)), part(signature),
            f"{header}.{payload}".encode())


def test_token_carries_livekit_claims_and_a_valid_signature():
    token = mint_access_token("SOLkey", "secret", AccessTokenRequest(
        "hall", RoomGrants("meeting", hidden=True, can_publish_sources=("camera",)),
        name="Kingdom Hall", attributes={"role": "hall"}, ttl_seconds=60,
    ), now=1000)
    header, claims, signature, signed = _decode(token)
    assert header == {"alg": "HS256", "typ": "JWT"}
    assert claims == {
        "iss": "SOLkey", "sub": "hall", "nbf": 1000, "exp": 1060, "name": "Kingdom Hall",
        "attributes": {"role": "hall"},
        "video": {"roomJoin": True, "room": "meeting", "canPublish": True,
                  "canSubscribe": True, "canPublishData": True,
                  "canUpdateOwnMetadata": False, "hidden": True,
                  "canPublishSources": ["camera"]},
    }
    assert hmac.compare_digest(signature, hmac.new(b"secret", signed, hashlib.sha256).digest())


@pytest.mark.parametrize(("key", "secret", "request_"), [
    ("", "s", AccessTokenRequest("hall", RoomGrants("r"))),
    ("k", "", AccessTokenRequest("hall", RoomGrants("r"))),
    ("k", "s", AccessTokenRequest("", RoomGrants("r"))),
    ("k", "s", AccessTokenRequest("hall", RoomGrants(""))),
    ("k", "s", AccessTokenRequest("hall", RoomGrants("r"), ttl_seconds=0)),
])
def test_token_rejects_missing_inputs(key, secret, request_):
    with pytest.raises(ValueError):
        mint_access_token(key, secret, request_)
