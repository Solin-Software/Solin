"""Unit tests for the per-platform camera capture-source specification."""

from __future__ import annotations

import types

import solin.core.media.camera_source as cs


def _option(*, device_path="/dev/video0", name="HD Cam", is_virtual=False):
    return types.SimpleNamespace(
        device_path=device_path, name=name, is_virtual=is_virtual
    )


def test_camera_target_none_is_empty():
    assert cs.camera_target(None) == ("", "")


def test_camera_target_real_device_passes_through():
    assert cs.camera_target(_option()) == ("/dev/video0", "HD Cam")


def test_camera_target_virtual_camera_is_rejected():
    # Never feed the vcam a virtual camera (would loop its own output).
    assert cs.camera_target(_option(is_virtual=True)) == ("", "")


def test_camera_target_without_device_path_is_empty():
    assert cs.camera_target(_option(device_path="")) == ("", "")


def test_camera_spec_linux_uses_v4l2(monkeypatch):
    monkeypatch.setattr(cs.sys, "platform", "linux")
    kind, settings = cs.camera_source_spec("/dev/video0", "HD Cam")
    assert kind == "v4l2_input"
    assert settings == {"device_id": "/dev/video0"}


def test_camera_spec_windows_uses_dshow_name_and_path(monkeypatch):
    monkeypatch.setattr(cs.sys, "platform", "win32")
    kind, settings = cs.camera_source_spec(r"\\?\usb#vid_1234", "HD Cam")
    assert kind == "dshow_input"
    # dshow matches "<friendly name>:<device path>".
    assert settings["video_device_id"] == r"HD Cam:\\?\usb#vid_1234"
    assert settings["last_video_device_id"] == r"HD Cam:\\?\usb#vid_1234"


def test_camera_spec_windows_without_name_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(cs.sys, "platform", "win32")
    _kind, settings = cs.camera_source_spec("device-path", "")
    assert settings["video_device_id"] == "device-path"


def test_camera_spec_macos_uses_av_capture(monkeypatch):
    monkeypatch.setattr(cs.sys, "platform", "darwin")
    kind, settings = cs.camera_source_spec("uid-123", "FaceTime")
    assert kind == "av_capture_input"
    assert settings == {"device": "uid-123"}
