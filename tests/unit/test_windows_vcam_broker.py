"""Tests for the Windows vcam broker's decision core (platform-neutral).

The named-pipe server itself is Windows-only ctypes; this covers the status
selection and response bytes it hands back, plus that the module imports and
guards construction on non-Windows hosts.
"""
from __future__ import annotations

import struct
import sys

import pytest

from solin.core.scenes.windows_vcam_broker import (
    VcamBrokerEndpoint,
    build_response_bytes,
)
from solin.core.scenes.windows_vcam_transport import (
    BrokerStatus,
    encode_request,
    mapping_size_for,
    nv12_layout,
)


def _status(frame: bytes) -> int:
    return struct.unpack_from("<I", frame, 12)[0]


def _nonce(frame: bytes) -> bytes:
    return bytes(frame[16:32])


def _endpoint() -> VcamBrokerEndpoint:
    layout = nv12_layout(1280, 720)
    return VcamBrokerEndpoint(
        mapping_file_path_utf8=r"C:\Temp\Solin.VirtualCamera.{G}.frames",
        mapping_size=mapping_size_for(layout),
        generation=99,
        layout=layout,
        fps_numerator=30,
        fps_denominator=1,
    )


def test_junk_request_yields_invalid_request_with_zeroed_nonce():
    frame = build_response_bytes(b"not a request", _endpoint(), authorized=True)
    assert _status(frame) == int(BrokerStatus.INVALID_REQUEST)
    assert _nonce(frame) == bytes(16)


def test_unauthorized_client_is_denied_but_nonce_echoed():
    nonce = bytes(range(16))
    frame = build_response_bytes(encode_request(nonce), _endpoint(), authorized=False)
    assert _status(frame) == int(BrokerStatus.UNAUTHORIZED)
    assert _nonce(frame) == nonce


def test_missing_endpoint_reports_transport_unavailable():
    nonce = bytes(range(16))
    frame = build_response_bytes(encode_request(nonce), None, authorized=True)
    assert _status(frame) == int(BrokerStatus.TRANSPORT_UNAVAILABLE)
    assert _nonce(frame) == nonce


def test_ok_response_carries_the_ring_coordinates():
    nonce = bytes(range(16))
    endpoint = _endpoint()
    frame = build_response_bytes(encode_request(nonce), endpoint, authorized=True)
    assert _status(frame) == int(BrokerStatus.OK)
    assert _nonce(frame) == nonce
    mapping_size, generation = struct.unpack_from("<QQ", frame, 32)
    assert mapping_size == endpoint.mapping_size
    assert generation == 99
    fps_num, fps_den = struct.unpack_from("<II", frame, 88)
    assert (fps_num, fps_den) == (30, 1)
    path_len = struct.unpack_from("<H", frame, 96)[0]
    assert frame[128:128 + path_len].decode("utf-8") == endpoint.mapping_file_path_utf8


@pytest.mark.skipif(sys.platform == "win32", reason="guard is for non-Windows hosts")
def test_broker_construction_refused_off_windows():
    from solin.core.scenes.windows_vcam_broker import WindowsVcamBroker

    with pytest.raises(RuntimeError):
        WindowsVcamBroker("pipe", "S-1-5", lambda: None)
