from __future__ import annotations

import base64
import json

import pytest

from solin.core.hosted_meeting.tokens import (
    OBSERVE, PRESENT, decode_claims, group_jwk, new_group_key, sign_token, verify_token,
)


def test_token_carries_galene_claims():
    key = new_group_key()
    token = sign_token(key, username="Ana Souza", audience="https://h/group/m1/", permissions=PRESENT,
                       ttl_seconds=60, now=1_000)
    claims = decode_claims(token)
    assert claims == {"sub": "Ana Souza", "aud": "https://h/group/m1/", "permissions": ["present", "message"],
                      "iat": 1_000, "exp": 1_060}
    header = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
    assert header == {"alg": "HS256", "typ": "JWT"}
    assert verify_token(key, token)


def test_observe_token_has_no_permissions():
    token = sign_token(new_group_key(), username="x", audience="a", permissions=OBSERVE)
    assert decode_claims(token)["permissions"] == []


def test_signature_depends_on_key():
    token = sign_token(new_group_key(), username="x", audience="a")
    assert not verify_token(new_group_key(), token)
    assert not verify_token(new_group_key(), "garbage")


def test_username_required():
    with pytest.raises(ValueError):
        sign_token(new_group_key(), username="", audience="a")


def test_group_jwk_round_trips_key():
    key = new_group_key()
    jwk = group_jwk(key)
    assert jwk["kty"] == "oct" and jwk["alg"] == "HS256"
    assert base64.urlsafe_b64decode(jwk["k"] + "=" * (-len(jwk["k"]) % 4)) == key
