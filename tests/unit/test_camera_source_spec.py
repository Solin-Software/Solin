"""Tests for the platform camera-source spec (v4l2 format pinning).

Pure functions — no libobs/Qt. The Linux branch is what fixes the sluggish
default (uncompressed YUYV) by pinning a compressed format at a resolution.
"""
from __future__ import annotations

import sys

import pytest

from solin.core.media.camera_source import camera_source_spec, v4l2_fourcc


def test_v4l2_fourcc_packs_little_endian():
    # 'MJPG' → 0x47504A4D per V4L2's fourcc packing.
    assert v4l2_fourcc("MJPG") == 1196444237
    assert v4l2_fourcc("YUYV") == (ord("Y") | ord("U") << 8 | ord("Y") << 16 | ord("V") << 24)


def test_v4l2_fourcc_pads_short_codes_with_spaces():
    # V4L2 pads short fourccs with spaces (e.g. "Y16 ").
    assert v4l2_fourcc("Y16") == v4l2_fourcc("Y16 ")


@pytest.mark.skipif(sys.platform != "linux", reason="v4l2 spec is Linux-only")
def test_linux_automatic_sets_only_device_id():
    kind, settings = camera_source_spec("/dev/video1")
    assert kind == "v4l2_input"
    assert settings == {"device_id": "/dev/video1"}


@pytest.mark.skipif(sys.platform != "linux", reason="v4l2 spec is Linux-only")
def test_linux_selected_format_pins_pixelformat_and_resolution():
    kind, settings = camera_source_spec(
        "/dev/video1", "Brio", pixel_format="MJPG", width=1280, height=720)
    assert kind == "v4l2_input"
    assert settings["device_id"] == "/dev/video1"
    assert settings["pixelformat"] == 1196444237
    assert settings["resolution"] == (1280 << 16) | 720
    # framerate must NOT be pinned — the plugin rejects the mode if it is.
    assert "framerate" not in settings


@pytest.mark.skipif(sys.platform != "linux", reason="v4l2 spec is Linux-only")
def test_linux_partial_format_is_ignored():
    # Missing resolution → cannot pin; fall back to device default (no crash).
    _kind, settings = camera_source_spec("/dev/video1", pixel_format="MJPG")
    assert "pixelformat" not in settings and "resolution" not in settings
