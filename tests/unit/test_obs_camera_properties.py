from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace

from cffi import FFI
import pytest

from solin.core.media import obs_camera_properties as metadata
from solin.core.scenes.engine import LocalCameraModeQuery


@pytest.fixture(autouse=True)
def metadata_does_not_start_capture_runtime(monkeypatch):
    from solin.core.media import obs_runtime

    def unexpected_runtime():
        pytest.fail("Metadata queries require the caller's initialized OBS context")

    monkeypatch.setattr(obs_runtime, "obs_runtime", unexpected_runtime)


class DShowProperties:
    def __init__(self):
        self.settings = {}
        self.modified = []

    def reset(self):
        self.settings.clear()

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
    monkeypatch.setattr(metadata.sys, "platform", "win32")
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
        def reset(self):
            pass
    monkeypatch.setattr(metadata, "_NativeProperties", Session)
    devices = metadata.discover_obs_cameras()
    assert devices[0].probe.error_code == "capability_query_failed"
    assert devices[0].formats == ()
    def fail(_source_id):
        raise RuntimeError("plugin missing")
    monkeypatch.setattr(metadata, "_NativeProperties", fail)
    with pytest.raises(RuntimeError, match="plugin missing"):
        metadata.discover_obs_cameras()


def test_native_exact_size_adapter_reuses_inventory_properties_and_releases_session(monkeypatch):
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
    monkeypatch.setattr(metadata, "_NativeProperties", Session)
    result = metadata.query_obs_camera_modes(LocalCameraModeQuery("Camera:path", 1920, 1080))
    assert result.device_id == "Camera:path"
    assert result.resolution_ranges == ()
    assert result.formats and all(mode.width == 1920 for mode in result.formats)
    assert len(sessions) == 1
    assert closed == sessions
    assert sessions[0].modified.count("resolution") == 1
    with pytest.raises(OSError, match="inventory"):
        metadata.query_obs_camera_modes(LocalCameraModeQuery("missing", 1920, 1080))
    assert len(sessions) == 2  # A missing device never gets a per-device query.
    assert closed == sessions


@pytest.mark.parametrize("device_count", [0, 1, metadata.MAXIMUM_LOCAL_CAMERA_DEVICES])
def test_discovery_does_not_reenumerate_inventory_for_each_device(monkeypatch, device_count):
    sessions, closed = [], []

    class Session(DShowProperties):
        def __init__(self, _source_id):
            super().__init__()
            sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.append(self)

        def choices(self, name):
            if name == "video_device_id":
                return tuple((f"Camera {index}", f"Camera:{index}") for index in range(device_count))
            return super().choices(name)

    def probe(properties, device_id, *, requested_size=None):
        properties.settings["selected"] = device_id
        return (), ()

    monkeypatch.setattr(metadata, "_backend", lambda: ("dshow_input", "video_device_id", "dshow", probe))
    monkeypatch.setattr(metadata, "_NativeProperties", Session)
    devices = metadata.discover_obs_cameras()
    assert [device.device_id for device in devices] == [f"Camera:{index}" for index in range(device_count)]
    assert len(sessions) == 1
    assert closed == sessions


@pytest.fixture
def native_properties_library(monkeypatch):
    """Keep native ownership, settings and callback dispatch in the real adapter."""
    import pylibobs._ffi as binding

    ffi = FFI()
    ffi.cdef("typedef unsigned int uint32_t; struct media_frames_per_second;")
    state = SimpleNamespace(queries=[], allocated=[], released=[], destroyed=[], selected=[])
    properties = object()

    def get_properties(source_id):
        state.queries.append(source_id)
        return properties

    def get_defaults(_source_id):
        settings = {}
        state.allocated.append(settings)
        return settings

    def set_value(settings, key, value):
        settings[key.decode()] = value

    def set_fps(settings, key, value, _option):
        set_value(settings, key, Fraction(value.numerator, value.denominator))

    def modified(prop, settings):
        if prop in ("device", "video_device_id"):
            # Native callbacks retain history/autoselect values in obs_data_t.
            # Selecting the next camera must start with a fresh settings object.
            assert set(settings) == {prop}
            state.selected.append(settings[prop].decode())
            settings["last_device"] = settings[prop]
        if prop == "resolution" and settings["last_device"] == b"broken":
            raise metadata.CameraPropertiesError("driver_unavailable")

    lib = SimpleNamespace(
        obs_get_source_properties=get_properties,
        obs_get_source_defaults=get_defaults,
        obs_properties_apply_settings=lambda *_args: None,
        obs_properties_destroy=state.destroyed.append,
        obs_data_release=state.released.append,
        obs_properties_get=lambda _props, key: key.decode(),
        obs_data_set_string=set_value,
        obs_data_set_int=set_value,
        obs_data_set_bool=set_value,
        obs_data_set_frames_per_second=set_fps,
        obs_property_modified=modified,
    )
    monkeypatch.setattr(binding, "ffi", ffi)
    monkeypatch.setattr(binding, "get_lib", lambda: lib)
    return state, lib


@pytest.mark.parametrize("platform", ["win32"])
def test_native_session_isolates_devices_and_recovers_after_callback_failure(
    monkeypatch, native_properties_library, platform,
):
    state, _lib = native_properties_library
    device_key = "video_device_id" if platform == "win32" else "device"
    sizes = {b"first": (640, 480), b"broken": (1280, 720), b"last": (1920, 1080)}
    inventory = (("First", "first"), ("Broken", "broken"), ("Last", "last"))

    def choices(props, name):
        if name == device_key:
            return inventory
        device_id = props.settings[device_key]
        width, height = sizes[device_id]
        if name == "resolution":
            resolution = (f"{width}x{height}" if platform == "win32"
                          else f'{{"width":{width},"height":{height}}}')
            return (("size", resolution),)
        if name == "frame_interval":
            assert props.settings["video_format"] == props.settings["frame_interval"] == 0
            return (("fps", 333333 if device_id == b"first" else 2000000),)
        if name == "video_format":
            return (("format", 301 if device_id == b"first" else 400),)
        if name == "input_format":
            return (("format", int.from_bytes(b"420v" if device_id == b"first" else b"jpeg", "big")),)
        raise AssertionError(name)

    monkeypatch.setattr(metadata.sys, "platform", platform)
    monkeypatch.setattr(metadata._NativeProperties, "choices", choices)
    # Each device fits this budget; a shared cumulative budget would break the
    # final camera after the first camera's successful query and the driver error.
    monkeypatch.setattr(metadata, "_CALLBACK_LIMIT", 7)
    devices = metadata.discover_obs_cameras()

    assert [device.device_id for device in devices] == ["first", "broken", "last"]
    assert [device.probe.error_code for device in devices] == ["", "driver_unavailable", ""]
    assert devices[1].formats == devices[1].frame_rate_ranges == ()
    for device, size, fps in ((devices[0], (640, 480), 30), (devices[2], (1920, 1080), 5)):
        assert device.formats
        assert {(mode.width, mode.height) for mode in device.formats} == {size}
        assert {mode.frames_per_second for mode in device.formats} == {fps}
    assert state.selected == ["first", "broken", "last"]
    assert len(state.queries) == len(state.destroyed) == 1
    assert len(state.allocated) == 4  # Inventory defaults, then fresh defaults per camera.
    assert len(state.released) == len(state.allocated)
    assert {id(settings) for settings in state.released} == {id(settings) for settings in state.allocated}


def test_native_settings_allocation_failure_can_recover_without_double_release(
    monkeypatch, native_properties_library,
):
    state, lib = native_properties_library
    defaults = lib.obs_get_source_defaults
    with metadata._NativeProperties("dshow_input") as props:
        monkeypatch.setattr(metadata, "_CALLBACK_LIMIT", 1)
        props.set("video_format", 0)
        with pytest.raises(metadata.CameraPropertiesError, match="capability_overflow"):
            props.set("video_format", 0)

        monkeypatch.setattr(lib, "obs_get_source_defaults", lambda _source_id: props.ffi.NULL)
        monkeypatch.setattr(lib, "obs_data_create", lambda: props.ffi.NULL, raising=False)
        with pytest.raises(RuntimeError, match="allocate"):
            props.reset()
        assert props.settings == props.ffi.NULL
        assert state.released == state.allocated

        monkeypatch.setattr(lib, "obs_get_source_defaults", defaults)
        props.reset()
        props.set("video_format", 0)  # Recovery also resets the exhausted callback budget.

    assert len(state.queries) == len(state.destroyed) == 1
    assert len(state.released) == 2
    assert [id(settings) for settings in state.released] == [id(settings) for settings in state.allocated]


@pytest.mark.parametrize("failure", ["list", "overflow"])
def test_inventory_failure_releases_native_session_and_is_not_a_device_failure(
    monkeypatch, native_properties_library, failure,
):
    state, _lib = native_properties_library

    def choices(_props, _key):
        if failure == "list":
            raise metadata.CameraPropertiesError("capability_property_missing")
        return tuple(("Camera", str(index)) for index in range(metadata.MAXIMUM_LOCAL_CAMERA_DEVICES + 1))

    monkeypatch.setattr(metadata.sys, "platform", "win32")
    monkeypatch.setattr(metadata._NativeProperties, "choices", choices)
    error = "capability_property_missing" if failure == "list" else "inventory_overflow"
    with pytest.raises(metadata.CameraPropertiesError, match=error):
        metadata.discover_obs_cameras()
    assert len(state.queries) == len(state.destroyed) == len(state.released) == 1
    assert state.selected == []


def test_inventory_is_snapshotted_and_deduplicated_before_callbacks(monkeypatch):
    choices = [("", "device"), ("Duplicate", "device"), ("Empty", ""), ("Wrong type", 1)]
    properties = SimpleNamespace(choices=lambda _key: tuple(choices))
    inventory = metadata._inventory(properties, "video_device_id")
    choices.clear()
    assert inventory == (("device", "device"),)
