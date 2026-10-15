from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace

from cffi import FFI
import pytest

from solin.core.media import obs_camera_properties as metadata
from solin.core.media.pylibobs_compat import complete_frame_rate_type_compat
from solin.core.scenes.engine import LocalCameraModeQuery


class DShowProperties:
    def __init__(self):
        self.settings = {}
        self.modified = []

    def set(self, name, value):
        self.settings[name] = value
        self.modified.append(name)

    def choices(self, name):
        assert self.settings["video_device_id"] == "Camera:path"
        assert self.settings["res_type"] == 1
        if name == "resolution":
            return (("VGA", "640x480"), ("HD", "1920x1080"))
        if name == "frame_interval":
            assert self.settings["video_format"] == 0
            return (("matching", -1), ("highest", 0), ("30", 333333), ("5", 2000000))
        if name == "video_format":
            assert self.settings["frame_interval"] > 0
            if self.settings["resolution"] == "1920x1080" and self.settings["frame_interval"] == 333333:
                return (("Any", 0), ("MJPEG", 400))
            return (("Any", 0), ("YUY2", 301), ("MJPEG", 400))
        raise AssertionError(name)


def test_dshow_callbacks_preserve_correlated_modes_and_native_enum():
    properties = DShowProperties()
    formats, ranges = metadata._dshow_modes(properties, "Camera:path")
    assert ranges == ()
    hd30 = [mode for mode in formats if mode.width == 1920 and mode.frames_per_second == 30]
    assert [mode.pixel_format for mode in hd30] == ["400"]
    assert not any(mode.fps_numerator <= 0 for mode in formats)
    assert {mode.pixel_format for mode in formats} == {"301", "400"}
    assert properties.modified.count("resolution") == 2


@pytest.mark.parametrize("size", [(1920, 1080), (640, 480), (1280, 720)])
def test_dshow_exact_size_query_does_not_probe_other_resolutions(size):
    properties = DShowProperties()
    formats, ranges = metadata._dshow_modes(properties, "Camera:path", requested_size=size)
    assert ranges == ()
    assert all((mode.width, mode.height) == size for mode in formats)
    assert properties.modified.count("resolution") == (0 if size == (1280, 720) else 1)


def test_avcapture_exact_size_query_does_not_invent_resolution():
    properties = AVCaptureProperties()
    assert metadata._avcapture_modes(properties, "native-device", requested_size=(1296, 728)) == ((), ())
    assert "frame_rate" not in properties.settings


class AVCaptureProperties:
    def __init__(self):
        self.settings = {}

    def set(self, name, value):
        self.settings[name] = value

    def fps_ranges(self):
        assert self.settings["resolution"] == '{"width":1920,"height":1080}'
        # All underlying ranges are exposed by OBS's enumerate_frame_rates().
        return ((Fraction(15), Fraction(20)), (Fraction(25), Fraction(30)), (Fraction(15), Fraction(30)))

    def choices(self, name):
        if name == "resolution":
            assert not self.settings["use_preset"]
            return (("HD", '{"width":1920,"height":1080}'),)
        if name == "input_format":
            fps = self.settings["frame_rate"]
            formats = [("automatic", -1), ("UYVY", int.from_bytes(b"2vuy", "big"))]
            if 15 <= fps <= 20 or 25 <= fps <= 30:
                formats.append(("NV12", int.from_bytes(b"420v", "big")))
            return formats
        raise AssertionError(name)


def test_avcapture_preserves_gaps_and_validates_formats_at_the_selected_fps():
    formats, ranges = metadata._avcapture_modes(AVCaptureProperties(), "native-device")
    nv12 = [item for item in ranges if item.pixel_format == "420v"]
    assert any(item.supports(30, 1) for item in nv12)
    assert any(item.supports(20, 1) for item in nv12)
    assert not any(item.supports(24, 1) for item in nv12)
    assert not any(mode.pixel_format == "420v" and 20 < mode.frames_per_second < 25 for mode in formats)
    assert any(mode.pixel_format == "420v" and (mode.fps_numerator, mode.fps_denominator) == (30000, 1001)
               for mode in formats)
    assert any(mode.pixel_format == "420v" and mode.frames_per_second == 30 for mode in formats)
    assert all(len(mode.pixel_format) == 4 for mode in formats)


def test_opaque_binding_fps_type_is_completed_idempotently():
    ffi = FFI()
    ffi.cdef("typedef unsigned int uint32_t; struct media_frames_per_second;")
    with pytest.raises(ValueError):
        ffi.sizeof("struct media_frames_per_second")
    complete_frame_rate_type_compat(ffi)
    complete_frame_rate_type_compat(ffi)
    assert ffi.sizeof("struct media_frames_per_second") == 8


def test_raw_properties_are_freed_even_when_default_callbacks_fail(monkeypatch):
    import pylibobs._ffi as binding

    freed = []
    ffi = SimpleNamespace(NULL=None, sizeof=lambda _type: 8)
    properties, settings = object(), object()
    def fail(*_args):
        raise RuntimeError("driver callback failed")
    lib = SimpleNamespace(
        obs_get_source_properties=lambda _id: properties,
        obs_get_source_defaults=lambda _id: settings,
        obs_properties_apply_settings=fail,
        obs_properties_destroy=lambda value: freed.append(("properties", value)),
        obs_data_release=lambda value: freed.append(("settings", value)),
    )
    monkeypatch.setattr(binding, "ffi", ffi)
    monkeypatch.setattr(binding, "get_lib", lambda: lib)
    with pytest.raises(RuntimeError, match="driver callback failed"):
        metadata._NativeProperties("dshow_input")
    assert freed == [("properties", properties), ("settings", settings)]


def test_raw_properties_requery_lists_and_filter_disabled_entries(monkeypatch):
    import pylibobs._ffi as binding

    state = {"modified": False}
    freed = []
    ffi = SimpleNamespace(NULL=None, sizeof=lambda _type: 8, string=lambda value: value)
    lib = SimpleNamespace(
        obs_get_source_properties=lambda _id: "properties",
        obs_get_source_defaults=lambda _id: "settings",
        obs_properties_apply_settings=lambda *_args: None,
        obs_properties_destroy=lambda value: freed.append(value),
        obs_data_release=lambda value: freed.append(value),
        obs_properties_get=lambda _props, _key: "property",
        obs_property_get_type=lambda _prop: 6,
        obs_property_list_item_count=lambda _prop: 2,
        obs_property_list_format=lambda _prop: 1,
        obs_property_list_item_disabled=lambda _prop, index: index == 1,
        obs_property_list_item_name=lambda _prop, _index: b"format",
        obs_property_list_item_int=lambda _prop, _index: 400 if state["modified"] else 301,
        obs_data_set_int=lambda *_args: None,
        obs_property_modified=lambda *_args: state.update(modified=True),
    )
    monkeypatch.setattr(binding, "ffi", ffi)
    monkeypatch.setattr(binding, "get_lib", lambda: lib)
    with metadata._NativeProperties("dshow_input") as props:
        assert props.choices("video_format") == (("format", 301),)
        props.set("video_format", 400)
        assert props.choices("video_format") == (("format", 400),)
    assert freed == ["properties", "settings"]


def test_capability_overflow_is_not_silent(monkeypatch):
    monkeypatch.setattr(metadata, "_LIMIT", 1)
    with pytest.raises(metadata.CameraPropertiesError, match="capability_overflow"):
        metadata._dshow_modes(DShowProperties(), "Camera:path")


def test_discovery_retains_device_failure_but_propagates_inventory_failure(monkeypatch):
    from solin.core.media import obs_runtime

    monkeypatch.setattr(metadata.sys, "platform", "win32")
    monkeypatch.setattr(obs_runtime, "obs_runtime", lambda: SimpleNamespace(ensure_started=lambda: None))
    class Session:
        def __init__(self, _source_id):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def choices(self, key):
            if key == "video_device_id":
                return (("Camera", "device"),)
            raise RuntimeError("broken inventory")
        def set(self, *_args):
            raise RuntimeError("device busy")
    monkeypatch.setattr(metadata, "_NativeProperties", Session)
    devices = metadata.discover_obs_cameras()
    assert devices[0].probe.error_code == "capability_query_failed"
    assert devices[0].formats == ()
    def fail(_source_id):
        raise RuntimeError("plugin missing")
    monkeypatch.setattr(metadata, "_NativeProperties", fail)
    with pytest.raises(RuntimeError, match="plugin missing"):
        metadata.discover_obs_cameras()


def test_native_exact_size_adapter_uses_fresh_properties_and_releases_both_sessions(monkeypatch):
    from solin.core.media import obs_runtime

    sessions = []
    closed = []
    class Session(DShowProperties):
        def __init__(self, source_id):
            super().__init__()
            assert source_id == "dshow_input"
            sessions.append(self)
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            closed.append(self)
        def choices(self, name):
            if name == "video_device_id":
                return (("Camera", "Camera:path"),)
            return super().choices(name)
    monkeypatch.setattr(metadata.sys, "platform", "win32")
    monkeypatch.setattr(obs_runtime, "obs_runtime", lambda: SimpleNamespace(ensure_started=lambda: None))
    monkeypatch.setattr(metadata, "_NativeProperties", Session)
    result = metadata.query_obs_camera_modes(LocalCameraModeQuery("Camera:path", 1920, 1080))
    assert result.device_id == "Camera:path"
    assert result.resolution_ranges == ()
    assert result.formats and all(mode.width == 1920 for mode in result.formats)
    assert len(sessions) == 2
    assert closed == sessions
    assert sessions[1].modified.count("resolution") == 1
    with pytest.raises(OSError, match="inventory"):
        metadata.query_obs_camera_modes(LocalCameraModeQuery("missing", 1920, 1080))
    assert len(sessions) == 3  # A missing device never gets a per-device query.
    assert closed == sessions
