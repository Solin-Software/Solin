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


def test_libobs_cameras_is_empty_where_qt_ids_already_work(monkeypatch):
    """Only Windows has two disagreeing device namespaces.

    Linux hands v4l2_input a /dev/videoN path and macOS hands av_capture_input an
    AVFoundation uniqueID — both are exactly what Qt reports, so there is nothing
    to reconcile and no reason to spin up libobs to ask.
    """
    for platform in ("linux", "darwin"):
        monkeypatch.setattr(cs.sys, "platform", platform)
        assert cs.libobs_cameras() == []


def test_libobs_cameras_survives_libobs_being_unavailable(monkeypatch):
    """Enumeration is best-effort: losing it must not lose the camera list."""
    import solin.core.media.obs_runtime as rt

    monkeypatch.setattr(cs.sys, "platform", "win32")

    def _boom():
        raise RuntimeError("libobs not started")

    monkeypatch.setattr(rt, "obs_runtime", _boom)

    assert cs.libobs_cameras() == []
