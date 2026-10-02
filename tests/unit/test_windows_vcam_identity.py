"""Byte-exactness tests for the Windows vcam broker pipe identity.

The hash input must match the C++ ``windows_virtual_camera_contract.cpp`` exactly
(UTF-16LE of ``sid:session``, no BOM, no terminator), or the Python broker binds
a different pipe name than the installed filter probes.
"""
from __future__ import annotations

import hashlib

from solin.core.scenes.windows_vcam_identity import (
    BROKER_PIPE_PREFIX,
    broker_identity_hash,
    broker_pipe_name,
    identity_digest_input,
)


def test_identity_digest_is_utf16le_without_bom_or_terminator():
    digest_input = identity_digest_input("S-1-5", 1)
    # "S-1-5:1" is 7 characters → 14 UTF-16LE bytes, low byte first, no BOM/null.
    assert digest_input == b"S\x00-\x001\x00-\x005\x00:\x001\x00"
    assert len(digest_input) == 7 * 2
    assert digest_input[:2] != b"\xff\xfe"  # no byte-order mark


def test_identity_hash_is_lowercase_hex_sha256():
    sid = "S-1-5-21-1111111111-2222222222-3333333333-1001"
    expected = hashlib.sha256(f"{sid}:7".encode("utf-16-le")).hexdigest()
    value = broker_identity_hash(sid, 7)
    assert value == expected
    assert len(value) == 64 and value == value.lower()


def test_pipe_name_uses_the_v3_prefix_and_hash():
    sid = "S-1-5-21-9-8-7-1001"
    name = broker_pipe_name(sid, 2)
    assert name.startswith(BROKER_PIPE_PREFIX)
    assert name == BROKER_PIPE_PREFIX + broker_identity_hash(sid, 2)
    assert BROKER_PIPE_PREFIX == r"\\.\pipe\Solin.VirtualCamera.FrameBroker.v3."


def test_session_id_is_coerced_to_int():
    assert broker_identity_hash("S-1-5", 3) == broker_identity_hash("S-1-5", 3)
    # a str session that names the same integer hashes identically
    assert identity_digest_input("S-1-5", "3") == identity_digest_input("S-1-5", 3)
