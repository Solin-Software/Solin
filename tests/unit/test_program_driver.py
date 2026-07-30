"""Unit tests for pure helpers in the projection program driver."""

from __future__ import annotations

import solin.projection.program_driver as pd


def test_camera_spec_linux_uses_v4l2(monkeypatch):
    monkeypatch.setattr(pd.sys, "platform", "linux")
    kind, settings = pd._camera_source_spec("/dev/video0", "HD Cam")
    assert kind == "v4l2_input"
    assert settings == {"device_id": "/dev/video0"}


def test_camera_spec_windows_uses_dshow_name_and_path(monkeypatch):
    monkeypatch.setattr(pd.sys, "platform", "win32")
    kind, settings = pd._camera_source_spec(r"\\?\usb#vid_1234", "HD Cam")
    assert kind == "dshow_input"
    # dshow matches "<friendly name>:<device path>".
    assert settings["video_device_id"] == r"HD Cam:\\?\usb#vid_1234"
    assert settings["last_video_device_id"] == r"HD Cam:\\?\usb#vid_1234"


def test_camera_spec_windows_without_name_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(pd.sys, "platform", "win32")
    _kind, settings = pd._camera_source_spec("device-path", "")
    assert settings["video_device_id"] == "device-path"


def test_camera_spec_macos_uses_av_capture(monkeypatch):
    monkeypatch.setattr(pd.sys, "platform", "darwin")
    kind, settings = pd._camera_source_spec("uid-123", "FaceTime")
    assert kind == "av_capture_input"
    assert settings == {"device": "uid-123"}
