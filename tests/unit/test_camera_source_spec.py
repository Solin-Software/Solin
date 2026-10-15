"""OBS 32.1.2 capture plugin contracts, without hardware."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from solin.core.media.camera_source import camera_source_spec, libobs_cameras, v4l2_fourcc


def test_v4l2_fourcc_packs_little_endian():
    assert v4l2_fourcc("MJPG") == 1196444237
    assert v4l2_fourcc("YUYV") == 1448695129
    assert v4l2_fourcc("Y16") == v4l2_fourcc("Y16 ")


@pytest.mark.parametrize(("platform", "pixel_format", "media_type", "kind", "expected"), [
    ("win32", "400", "image/jpeg", "dshow_input", {
        "res_type": 1, "resolution": "1920x1080", "frame_interval": 333667, "video_format": 400,
    }),
    ("darwin", "420v", "video/x-raw", "av_capture_input", {
        "use_preset": False, "resolution": '{"width":1920,"height":1080}',
        "input_format": 0x34323076, "frame_rate": {"numerator": 30000, "denominator": 1001},
    }),
    ("linux", "MJPG", "image/jpeg", "v4l2_input", {
        "pixelformat": 1196444237, "resolution": (1920 << 32) | 1080,
        "framerate": (1001 << 32) | 30000,
    }),
])
def test_exact_mode_preserves_native_format_and_rational_rate(
    monkeypatch, platform, pixel_format, media_type, kind, expected,
):
    monkeypatch.setattr(sys, "platform", platform)
    actual_kind, settings = camera_source_spec(
        "device", "Camera", width=1920, height=1080,
        fps_numerator=30000, fps_denominator=1001,
        media_type=media_type, pixel_format=pixel_format,
    )
    assert actual_kind == kind
    assert {key: settings[key] for key in expected} == expected


@pytest.mark.parametrize(("platform", "expected"), [
    ("win32", {"res_type": 0, "resolution": "", "frame_interval": -1, "video_format": 0}),
    ("darwin", {"use_preset": True, "resolution": "", "input_format": -1,
                "frame_rate": {"numerator": 0, "denominator": 1}}),
    ("linux", {"pixelformat": -1, "resolution": -1, "framerate": -1}),
])
def test_automatic_explicitly_resets_every_controlled_mode_key(monkeypatch, platform, expected):
    monkeypatch.setattr(sys, "platform", platform)
    _, automatic = camera_source_spec("device", "Camera")
    assert {key: automatic[key] for key in expected} == expected


def test_windows_device_identifier_is_not_prefixed_twice(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    _, settings = camera_source_spec("Camera:device", "Camera")
    assert settings["video_device_id"] == settings["last_video_device_id"] == "Camera:device"


def test_mac_resolution_is_json_and_fourcc_is_big_endian(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _, settings = camera_source_spec(
        "device", width=1280, height=720, fps_numerator=30,
        media_type="video/x-raw", pixel_format="2vuy",
    )
    assert json.loads(settings["resolution"]) == {"width": 1280, "height": 720}
    assert settings["input_format"] == 0x32767579


@pytest.mark.parametrize("partial", [
    {"width": 1280}, {"pixel_format": "MJPG"}, {"fps_numerator": 30},
    {"fps_denominator": 0}, {"fps_denominator": 1001}, {"height": -1},
])
def test_partial_or_invalid_mode_is_rejected_instead_of_silently_falling_back(monkeypatch, partial):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ValueError):
        camera_source_spec("device", **partial)


@pytest.mark.parametrize(("platform", "token"), [("win32", "MJPEG"), ("darwin", "NV12-long")])
def test_native_format_tokens_are_not_guessed_from_display_names(monkeypatch, platform, token):
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(ValueError):
        camera_source_spec(
            "device", width=1920, height=1080, fps_numerator=30,
            media_type="video/x-raw", pixel_format=token,
        )


@pytest.mark.parametrize("failure", [False, True])
def test_enumeration_releases_properties_on_success_and_failure(monkeypatch, failure):
    monkeypatch.setattr(sys, "platform", "win32")
    released = []

    class Properties:
        def __iter__(self):
            if failure:
                raise RuntimeError("property read failed")
            return iter([SimpleNamespace(name="video_device_id", items=[
                SimpleNamespace(name="Camera", value="device", disabled=False),
            ])])

        def release(self):
            released.append(True)

    monkeypatch.setitem(sys.modules, "pylibobs", SimpleNamespace(
        Properties=SimpleNamespace(from_source_id=lambda _kind: Properties()),
    ))
    from solin.core.media import obs_runtime
    monkeypatch.setattr(obs_runtime, "obs_runtime", lambda: SimpleNamespace(ensure_started=lambda: None))
    if failure:
        with pytest.raises(RuntimeError, match="enumerat"):
            libobs_cameras()
    else:
        assert libobs_cameras() == [("Camera", "device")]
    assert released == [True]


def test_unavailable_enumeration_is_distinct_from_empty_inventory(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    from solin.core.media import obs_runtime

    def unavailable():
        raise RuntimeError("missing plugin")

    monkeypatch.setattr(obs_runtime, "obs_runtime", unavailable)
    with pytest.raises(RuntimeError, match="enumerat"):
        libobs_cameras()
