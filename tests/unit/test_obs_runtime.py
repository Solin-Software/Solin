from __future__ import annotations

from types import SimpleNamespace
import sys
import base64

import pytest

from solin.core.media import obs_runtime


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("devices", [
    [],
    [SimpleNamespace(name="Notebook speakers", id="speakers")],
    [SimpleNamespace(name="Default", id="physical-device-named-default")],
])
def test_monitoring_follows_system_default_without_pinning_an_endpoint(
    monkeypatch, platform, devices,
):
    monkeypatch.setattr(obs_runtime.sys, "platform", platform)
    selected = []
    runtime = obs_runtime.ObsRuntime()
    runtime._pylibobs = SimpleNamespace(
        audio_monitoring_available=lambda: True,
        enum_audio_monitoring_devices=lambda: devices,
        set_audio_monitoring_device=lambda name, ident: selected.append((name, ident)) or True,
    )

    runtime._configure_monitoring()

    # Keep the logical default even with an empty catalog. Resolving it to an
    # endpoint disables the Windows backend's default-device notifications.
    assert len(selected) == 1
    assert selected[0][1] == "default"
    assert runtime.monitoring_device == selected[0]


@pytest.mark.parametrize("failure", ["unavailable", "probe", "rejected", "selection"])
def test_monitoring_configuration_failure_is_nonfatal_and_does_not_cache_success(failure, caplog):
    selected = []

    def available():
        if failure == "probe":
            raise RuntimeError("audio backend unavailable")
        return failure != "unavailable"

    def select(name, ident):
        selected.append((name, ident))
        if failure == "selection":
            raise RuntimeError("device selection failed")
        return False

    runtime = obs_runtime.ObsRuntime()
    runtime._pylibobs = SimpleNamespace(
        audio_monitoring_available=available,
        enum_audio_monitoring_devices=lambda: [],
        set_audio_monitoring_device=select,
    )

    runtime._configure_monitoring()

    assert runtime.monitoring_device is None
    assert len(selected) == (0 if failure in {"unavailable", "probe"} else 1)
    if failure != "unavailable":
        assert any(record.levelname == "WARNING" for record in caplog.records)


def test_monitoring_mutations_run_on_the_native_lifetime_lane(monkeypatch):
    executing = False
    changes = []

    def run(operation):
        nonlocal executing
        executing = True
        try:
            return operation()
        finally:
            executing = False

    def change_source(pointer, mode):
        assert executing
        changes.append((pointer, mode))

    def change_mixers(pointer, mixers):
        assert executing
        changes.append((pointer, "mixers", mixers))

    def select(name, ident):
        assert executing
        changes.append((name, ident))
        return True

    runtime = obs_runtime.ObsRuntime()
    runtime._native_tasks = SimpleNamespace(run=run)
    runtime._pylibobs = SimpleNamespace(set_audio_monitoring_device=select)
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        get_lib=lambda: SimpleNamespace(
            obs_source_set_monitoring_type=change_source,
            obs_source_set_audio_mixers=change_mixers,
        ),
    ))
    source = SimpleNamespace(_ptr=object())

    runtime.set_source_monitoring(source, obs_runtime.MONITORING_MONITOR_ONLY)
    assert runtime.set_monitoring_device("Default", "default")

    assert changes == [
        (source._ptr, "mixers", 0),
        (source._ptr, obs_runtime.MONITORING_MONITOR_ONLY),
        ("Default", "default"),
    ]
    assert runtime.monitoring_device == ("Default", "default")


@pytest.mark.parametrize(
    ("monitoring", "mixer_changes"),
    [
        # libobs builds that deprecated monitoring types treat monitor-only as
        # "monitor and output": only clearing the tracks keeps the source out of
        # the recording mix, where the speaker loopback already carries it.
        (obs_runtime.MONITORING_MONITOR_ONLY, [0]),
        # Sources that are meant to reach a mix keep the tracks they were given.
        (obs_runtime.MONITORING_MONITOR_AND_OUTPUT, []),
        (obs_runtime.MONITORING_NONE, []),
    ],
)
def test_monitor_only_keeps_a_source_out_of_every_mix(monkeypatch, monitoring, mixer_changes):
    mixers, modes = [], []
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        get_lib=lambda: SimpleNamespace(
            obs_source_set_monitoring_type=lambda _pointer, mode: modes.append(mode),
            obs_source_set_audio_mixers=lambda _pointer, value: mixers.append(value),
        ),
    ))

    obs_runtime.ObsRuntime().set_source_monitoring(SimpleNamespace(_ptr=object()), monitoring)

    assert mixers == mixer_changes
    assert modes == [monitoring]


@pytest.mark.parametrize("matching", [False, True])
def test_active_tree_inspection_borrows_children_without_removing_sources(monkeypatch, matching):
    root, wanted, other = object(), object(), object()
    visits = []

    def inspect(pointer, callback, data):
        assert pointer is root
        for child in (other, wanted if matching else other, None):
            visits.append(child)
            callback(root, child, data)

    # Expose only the borrowed traversal. An owning reference or removal would
    # fail this boundary instead of silently blanking the live composition.
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        ffi=SimpleNamespace(NULL=None, callback=lambda _signature: lambda function: function),
        get_lib=lambda: SimpleNamespace(obs_source_enum_active_tree=inspect),
    ))
    runtime = obs_runtime.ObsRuntime()
    assert runtime.active_source_tree_contains(
        SimpleNamespace(_ptr=root), SimpleNamespace(_ptr=wanted),
    ) is matching
    assert len(visits) == 3


def test_active_tree_null_roots_do_not_enter_native_traversal(monkeypatch):
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        ffi=SimpleNamespace(NULL=None),
        get_lib=lambda: pytest.fail("No native traversal for a null root"),
    ))
    runtime = obs_runtime.ObsRuntime()
    assert not runtime.active_source_tree_contains(None, object())
    assert not runtime.active_source_tree_contains(SimpleNamespace(_ptr=None), SimpleNamespace(_ptr=1))
    source = SimpleNamespace(_ptr=object())
    assert runtime.active_source_tree_contains(source, source)


@pytest.mark.parametrize("progress", [0.0, 0.5, 1.0])
@pytest.mark.parametrize("destination_contains_target", [False, True])
def test_transition_demand_excludes_retained_origin_only_after_video_completes(
    monkeypatch, progress, destination_contains_target,
):
    root, wanted, destination = object(), object(), object()
    released, visited = [], []

    def inspect(pointer, callback, data):
        visited.append(pointer)
        # OBS still enumerates the origin at 1.0 when projection audio is not
        # consumed. Only the destination contributes to completed video.
        children = (wanted, destination) if pointer is root else (
            (wanted,) if destination_contains_target else ()
        )
        for child in children:
            callback(pointer, child, data)

    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        ffi=SimpleNamespace(NULL=None, callback=lambda _signature: lambda function: function),
        get_lib=lambda: SimpleNamespace(
            obs_transition_get_time=lambda _pointer: progress,
            obs_transition_get_active_source=lambda _pointer: destination,
            obs_source_enum_active_tree=inspect,
            obs_source_release=released.append,
        ),
    ))
    runtime = obs_runtime.ObsRuntime()
    assert runtime.transition_source_tree_contains(
        SimpleNamespace(_ptr=root), SimpleNamespace(_ptr=wanted),
    ) is (progress < 1.0 or destination_contains_target)
    assert visited == [root if progress < 1.0 else destination]
    assert released == ([] if progress < 1.0 else [destination])


@pytest.mark.parametrize("destination", [None, "idle", "scene"])
def test_completed_transition_releases_temporary_reference_without_removing_source(
    monkeypatch, destination,
):
    releases = []

    def fail_traversal(*_args):
        raise RuntimeError("traversal failed")

    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        ffi=SimpleNamespace(NULL=None, callback=lambda _signature: lambda function: function),
        get_lib=lambda: SimpleNamespace(
            obs_transition_get_time=lambda _pointer: 1.0,
            obs_transition_get_active_source=lambda _pointer: destination,
            obs_source_release=releases.append,
            obs_source_enum_active_tree=fail_traversal,
        ),
    ))
    runtime = obs_runtime.ObsRuntime()
    transition, target = SimpleNamespace(_ptr="transition"), SimpleNamespace(_ptr="idle")
    if destination == "scene":
        with pytest.raises(RuntimeError, match="traversal failed"):
            runtime.transition_source_tree_contains(transition, target)
    else:
        assert runtime.transition_source_tree_contains(transition, target) is (destination == "idle")
    assert releases == ([] if destination is None else [destination])


@pytest.mark.parametrize("failure", [None, "upload", "show"])
@pytest.mark.parametrize("timestamp", [0, 123])
def test_native_frame_priming_releases_the_frame_after_upload(monkeypatch, failure, timestamp):
    events = []
    frame = SimpleNamespace(timestamp=timestamp)
    source = SimpleNamespace(_ptr=object())

    def get_frame(pointer):
        assert pointer is source._ptr
        return frame

    def upload(pointer, selected):
        assert pointer is source._ptr and selected is frame
        events.append("upload")
        if failure == "upload":
            raise RuntimeError("upload failed")

    def show(pointer):
        assert pointer is source._ptr
        events.append("show")
        if failure == "show":
            raise RuntimeError("show failed")

    def release(pointer, selected):
        assert pointer is source._ptr and selected is frame
        events.append("release")

    lib = SimpleNamespace(
        obs_source_get_frame=get_frame, obs_source_set_video_frame=upload,
        obs_source_release_frame=release, obs_source_show_preloaded_video=show,
    )
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        ffi=SimpleNamespace(NULL=None), get_lib=lambda: lib,
    ))
    runtime = obs_runtime.ObsRuntime()
    if failure:
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            runtime.prime_source_video(source)
    else:
        assert runtime.prime_source_video(source) is True
    assert events == (["upload", "release"] if failure == "upload" else ["upload", "show", "release"])


@pytest.mark.parametrize("preloaded", [False, True])
def test_native_preload_priming_preserves_a_frame_queued_before_its_tick(monkeypatch, preloaded):
    source = SimpleNamespace(_ptr=object(), width=0, height=0)
    events = []
    queued_frames = [object()]

    def get_frame(pointer):
        assert pointer is source._ptr
        assert queued_frames  # Published, but not yet selected by the next native tick.
        return None

    def clear(pointer, frame):
        pytest.fail("Readiness must not clear a frame waiting for the next OBS tick")

    def show(pointer):
        assert pointer is source._ptr
        events.append("show")
        if preloaded:
            source.width, source.height = 160, 90

    lib = SimpleNamespace(
        obs_source_get_frame=get_frame,
        obs_source_output_video=clear, obs_source_show_preloaded_video=show,
    )
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        ffi=SimpleNamespace(NULL=None, cast=lambda _, function: function), get_lib=lambda: lib,
    ))
    runtime = obs_runtime.ObsRuntime()
    assert not runtime.prime_source_video(source)
    assert events == []
    assert runtime.prime_source_video(source) is False
    assert events == []
    assert len(queued_frames) == 1


def test_native_probe_without_a_new_frame_rejects_cached_dimensions_after_invalidation(monkeypatch):
    source = SimpleNamespace(_ptr=object(), width=160, height=90)
    lib = SimpleNamespace(obs_source_get_frame=lambda _: None)
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        ffi=SimpleNamespace(NULL=None, cast=lambda _, function: function), get_lib=lambda: lib,
    ))
    assert not obs_runtime.ObsRuntime().prime_source_video(source)


def test_initial_preload_waits_for_upload_before_acknowledging_fresh_inactive_source(monkeypatch):
    source = SimpleNamespace(_ptr=object(), width=0, height=0)
    uploads = []

    def show(pointer):
        uploads.append(pointer)
        if len(uploads) == 2:
            source.width, source.height = 160, 90

    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        get_lib=lambda: SimpleNamespace(obs_source_show_preloaded_video=show),
    ))
    runtime = obs_runtime.ObsRuntime()
    assert not runtime.prime_initial_source_video(source)
    assert runtime.prime_initial_source_video(source)
    assert uploads == [source._ptr, source._ptr]


def test_prepared_decoder_showing_reference_is_balanced_and_released_only_once(monkeypatch):
    events = []
    source = SimpleNamespace(_ptr=object())
    lib = SimpleNamespace(
        obs_source_inc_showing=lambda pointer: events.append(("retain", pointer)),
        obs_source_dec_showing=lambda pointer: events.append(("release", pointer)),
    )
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(get_lib=lambda: lib))
    release = obs_runtime.ObsRuntime().retain_source_showing(source)
    assert events == [("retain", source._ptr)]
    release()
    release()
    assert events == [("retain", source._ptr), ("release", source._ptr)]



class _FakeContext:
    def __init__(self, events: list[str], *, fail_startup: bool = False) -> None:
        self._events = events
        self._fail_startup = fail_startup
        self.version = "test"

    def startup(self) -> None:
        self._events.append("startup")
        if self._fail_startup:
            raise RuntimeError("startup failed")

    def set_video(self, *_args, **_kwargs) -> None:
        self._events.append("video")

    def set_audio(self) -> None:
        self._events.append("audio")

    def load_modules(self) -> None:
        self._events.append("modules")

    def shutdown(self) -> None:
        self._events.append("shutdown")


class _FakeOwnedDisplay:
    pointer = 0x1234

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def close(self) -> None:
        self._events.append("xclose")


def _runtime_with_fake_libobs(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    fail_startup: bool = False,
) -> obs_runtime.ObsRuntime:
    context = _FakeContext(events, fail_startup=fail_startup)
    fake_ob = SimpleNamespace(
        NixPlatform=SimpleNamespace(X11_EGL=1),
        OBSContext=lambda **_kwargs: context,
        set_nix_platform_display=lambda _display: events.append("display"),
        set_nix_platform=lambda _platform: events.append("platform"),
    )
    runtime = obs_runtime.ObsRuntime()
    runtime._pylibobs = fake_ob
    monkeypatch.setattr(obs_runtime.sys, "platform", "linux")
    monkeypatch.setattr(obs_runtime, "libobs_available", lambda: True)
    monkeypatch.setattr(obs_runtime, "_graphics_module_path", lambda: "libobs-opengl.so")
    monkeypatch.setattr(runtime, "_configure_monitoring", lambda: None)
    return runtime


@pytest.mark.parametrize("failure", [None, "install", "video", "audio", "modules", "notifications"])
def test_windows_native_tasks_are_drained_before_shutdown_and_retained_through_module_unload(
    monkeypatch, request, failure,
):
    events = []
    runtime = _runtime_with_fake_libobs(monkeypatch, events)
    request.addfinalizer(runtime.shutdown)
    monkeypatch.setattr(obs_runtime.sys, "platform", "win32")
    context = runtime.ob.OBSContext()

    def step(name):
        events.append(name)
        if failure == name:
            raise RuntimeError(f"{name} failed")

    tasks = SimpleNamespace(
        install=lambda: step("install"),
        quiesce=lambda: step("quiesce"),
        close=lambda: step("close"),
    )
    monkeypatch.setitem(sys.modules, "solin.core.media.obs_tasks", SimpleNamespace(
        ObsTaskDispatcher=lambda: tasks,
    ))
    monkeypatch.setattr(context, "set_video", lambda *_args, **_kwargs: step("video"))
    monkeypatch.setattr(context, "set_audio", lambda: step("audio"))
    monkeypatch.setattr(context, "load_modules", lambda: step("modules"))

    def source_types():
        events.append("notifications")
        return [] if failure == "notifications" else ["wasapi_output_capture"]

    runtime.ob.enum_source_types = source_types
    if failure is None:
        runtime.ensure_started()
        runtime.ensure_started()
        assert runtime._native_tasks is tasks
        runtime.shutdown()
    else:
        with pytest.raises(obs_runtime.ObsRuntimeError):
            runtime.ensure_started()

    assert events.index("startup") < events.index("install")
    if "modules" in events:
        assert events.index("install") < events.index("modules")
    assert events[-3:] == ["quiesce", "shutdown", "close"]
    assert events.count("install") == 1
    assert runtime._native_tasks is None
    assert not runtime.started


def test_sidecar_x11_display_is_configured_before_obs_startup_and_closed_after_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    owned_display = _FakeOwnedDisplay(events)
    runtime = _runtime_with_fake_libobs(monkeypatch, events)
    monkeypatch.setattr(obs_runtime, "qt_x_display", lambda: None)
    monkeypatch.setattr(
        obs_runtime._OwnedX11Display,
        "open",
        classmethod(lambda _cls: owned_display),
    )

    runtime.ensure_started(width=320, height=180, fps=30)

    assert events[:5] == ["display", "platform", "startup", "video", "audio"]
    assert "xclose" not in events

    runtime.shutdown()

    assert events[-2:] == ["shutdown", "xclose"]


def test_qt_x11_display_is_shared_without_taking_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    runtime = _runtime_with_fake_libobs(monkeypatch, events)
    monkeypatch.setattr(obs_runtime, "qt_x_display", lambda: 0xCAFE)

    def unexpected_open(_cls):
        raise AssertionError("Qt X11 sessions must not open a second display")

    monkeypatch.setattr(obs_runtime._OwnedX11Display, "open", classmethod(unexpected_open))

    runtime.ensure_started()
    runtime.shutdown()

    assert events[:3] == ["display", "platform", "startup"]
    assert "xclose" not in events


def test_failed_obs_startup_closes_the_owned_x11_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    owned_display = _FakeOwnedDisplay(events)
    runtime = _runtime_with_fake_libobs(monkeypatch, events, fail_startup=True)
    monkeypatch.setattr(obs_runtime, "qt_x_display", lambda: None)
    monkeypatch.setattr(
        obs_runtime._OwnedX11Display,
        "open",
        classmethod(lambda _cls: owned_display),
    )

    with pytest.raises(obs_runtime.ObsRuntimeError, match="startup failed"):
        runtime.ensure_started()

    assert events == ["display", "platform", "startup", "shutdown", "xclose"]


# Camera capture settings and ownership are exercised without loading libobs.
@pytest.fixture
def camera_runtime(monkeypatch):
    from solin.core.media.obs_camera_capture import CameraCaptureState
    from solin.core.scenes import camera_catalog

    monkeypatch.setattr(sys, "platform", "linux")
    runtime = obs_runtime.ObsRuntime()
    events = []
    created = []
    automatic_modes = []

    class Source:
        def __init__(self, settings):
            self.settings = dict(settings)
            self.updates = []
            self.reject_update = False
            self.releases = 0

        def update(self, settings):
            self.updates.append(dict(settings))
            if self.reject_update:
                raise RuntimeError("device rejected update")
            self.settings.update(settings)

        def release(self):
            self.releases += 1

    def create(kind, name, settings):
        source = Source(settings)
        created.append((kind, name, source))
        return source

    class Probe:
        def __init__(self, source, *, width, height):
            self.source = source
            self.dimensions = (width, height)
            self.state = CameraCaptureState(1, "starting")
            self.updates = []
            self.closed = False

        def update(self, settings, *, width, height):
            assert not self.closed and self.source.releases == 0
            self.dimensions = (width, height)
            self.updates.append((dict(settings), width, height))
            self.state = CameraCaptureState(self.state.generation + 1, "starting")
            try:
                self.source.update(settings)
            except RuntimeError:
                self.state = CameraCaptureState(self.state.generation, "failed",
                                                "camera_update_failed")
                raise

        def fail(self):
            self.state = CameraCaptureState(self.state.generation, "failed", "camera_no_frames")

        def reconnect(self, settings, *, expected_generation, width, height):
            if (self.closed or self.state.generation != expected_generation
                    or self.state.status != "failed"
                    or self.state.error_code not in {"camera_no_frames", "camera_update_failed"}):
                return False
            self.update(settings, width=width, height=height)
            return True

        def close(self):
            assert self.source.releases == 0
            self.closed = True

    def automatic_mode(device, **kwargs):
        # Another thread must be able to start/use the runtime while discovery
        # waits for a catalog scan. Holding runtime._lock here deadlocks it.
        import threading
        unlocked = []

        def acquire():
            acquired = runtime._lock.acquire(timeout=1)
            unlocked.append(acquired)
            if acquired:
                runtime._lock.release()

        worker = threading.Thread(target=acquire)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive() and unlocked == [True]
        assert events and events[-1] == "started"
        events.append((device, kwargs))
        return automatic_modes[-1] if automatic_modes else None

    monkeypatch.setattr(runtime, "ensure_started", lambda: events.append("started"))
    monkeypatch.setattr(runtime, "watch_camera_video", Probe)
    runtime._pylibobs = SimpleNamespace(Source=SimpleNamespace(create=create))
    monkeypatch.setattr(camera_catalog, "camera_catalog",
                        lambda: SimpleNamespace(automatic_mode=automatic_mode))
    return runtime, created, events, automatic_modes


def _exact_camera_mode(**changes):
    return dict(width=1920, height=1080, fps_numerator=30000, fps_denominator=1001,
                media_type="image/jpeg", pixel_format="MJPG", native_format="") | changes


def test_camera_rate_only_change_updates_existing_shared_source(camera_runtime):
    runtime, created, _events, _modes = camera_runtime
    first = runtime.camera_source("/dev/video0", **_exact_camera_mode())
    assert runtime.camera_source("/dev/video0", **_exact_camera_mode()) is first
    assert first.updates == []
    assert runtime.camera_source("/dev/video0", **_exact_camera_mode(
        fps_numerator=25, fps_denominator=1,
    )) is first
    assert len(created) == 1
    assert first.settings["framerate"] == (1 << 32) | 25
    assert len(first.updates) == 1


def test_camera_full_mode_signature_includes_media_type(camera_runtime):
    runtime, _created, _events, _modes = camera_runtime
    first = runtime.camera_source("device", **_exact_camera_mode())
    runtime.camera_source("device", **_exact_camera_mode(media_type="video/x-raw"))
    assert len(first.updates) == 1


def test_camera_automatic_is_resolved_once_after_start_outside_runtime_lock(camera_runtime):
    runtime, created, events, modes = camera_runtime
    modes.append(SimpleNamespace(**_exact_camera_mode()))
    source = runtime.camera_source("/dev/video0")
    assert source.settings["resolution"] == (1920 << 32) | 1080
    assert source.settings["framerate"] == (1001 << 32) | 30000
    assert runtime.camera_source("/dev/video0") is source
    assert len(created) == 1
    assert events == ["started", ("/dev/video0", {
        "canvas_width": runtime.video.width, "canvas_height": runtime.video.height,
    })]


def test_camera_automatic_with_cold_catalog_does_not_discover(camera_runtime, monkeypatch):
    from solin.core.scenes import camera_catalog as catalog_module

    runtime, created, _events, _modes = camera_runtime
    catalog = catalog_module.CameraCatalog(lambda: pytest.fail(
        "Preparing a scene must not start camera discovery",
    ))
    monkeypatch.setitem(sys.modules, "solin.core.scenes.camera_catalog", SimpleNamespace(
        camera_catalog=lambda: catalog,
    ))
    source = runtime.camera_source("device")
    assert len(created) == 1
    assert source.settings["resolution"] == -1


@pytest.mark.parametrize(("platform", "token", "expected"), [
    ("win32", "400", {"res_type": 0, "resolution": "", "frame_interval": -1, "video_format": 0}),
    ("darwin", "420v", {"use_preset": True, "supported_format": "",
                        "frame_rate": {"numerator": 0, "denominator": 1}}),
    ("linux", "MJPG", {"pixelformat": -1, "resolution": -1, "framerate": -1}),
])
def test_camera_manual_to_automatic_clears_pinned_settings(
    monkeypatch, camera_runtime, platform, token, expected,
):
    monkeypatch.setattr(sys, "platform", platform)
    runtime, created, _events, _modes = camera_runtime
    mode = _exact_camera_mode(pixel_format=token)
    if platform == "darwin":
        mode.update(media_type="video/x-raw", native_format=base64.b64encode(
            b"1920x1080 29.970-30.000 2 875704438").decode())
    source = runtime.camera_source("device", **mode)
    assert runtime.camera_source("device") is source
    assert len(created) == 1
    assert {key: source.settings[key] for key in expected} == expected


def test_camera_failed_update_is_reported_and_does_not_cache_rejected_mode(camera_runtime):
    runtime, created, _events, _modes = camera_runtime
    original = _exact_camera_mode()
    source = runtime.camera_source("device", **original)
    source.reject_update = True
    changed = _exact_camera_mode(fps_numerator=25, fps_denominator=1)
    for _ in range(2):
        with pytest.raises(obs_runtime.ObsRuntimeError, match="update") as error:
            runtime.camera_source("device", **changed)
        assert isinstance(error.value.__cause__, RuntimeError)
    assert len(source.updates) == 2
    assert len(created) == 1
    source.reject_update = False
    assert runtime.camera_source("device", **original) is source
    assert len(source.updates) == 3
    assert source.settings["framerate"] == (1001 << 32) | 30000


def test_windows_aliases_share_one_camera_source(monkeypatch, camera_runtime):
    monkeypatch.setattr(sys, "platform", "win32")
    runtime, created, _events, _modes = camera_runtime
    mode = _exact_camera_mode(pixel_format="400")
    source = runtime.camera_source("Camera:USB-DEVICE", **mode)
    assert runtime.camera_source("usb-device", "Renamed", **mode) is source
    assert len(created) == 1
    runtime._release_camera_sources()
    assert source.releases == 1 and not runtime._camera_sources


def test_camera_empty_device_does_not_start_runtime(camera_runtime):
    runtime, created, events, _modes = camera_runtime
    assert runtime.camera_source("") is None
    assert not created and not events


def test_camera_reconnect_does_not_restart_frames_recovered_after_policy_snapshot(
    monkeypatch, camera_runtime,
):
    from unittest.mock import Mock

    from solin.core.media.obs_camera_capture import ObsCameraCaptureProbe

    monkeypatch.setattr(sys, "platform", "win32")
    runtime, _created, _events, _modes = camera_runtime
    now = [10.0]
    runtime.ob.add_main_render_callback = Mock(return_value="render")
    runtime.ob.remove_main_render_callback = Mock()
    monkeypatch.setattr(runtime, "observe_source_updates", Mock(return_value=Mock()))
    monkeypatch.setattr(runtime, "observe_source_video", Mock(return_value=Mock()))
    monkeypatch.setattr(runtime, "watch_camera_video", lambda source, **dimensions:
                        ObsCameraCaptureProbe(runtime, source, monotonic=lambda: now[0], **dimensions))
    source = runtime.camera_source("device", **_exact_camera_mode(pixel_format="400"))
    source.showing = True
    tick = runtime.ob.add_main_render_callback.call_args.args[0]
    receive = runtime.observe_source_video.call_args.args[1]
    try:
        tick(1920, 1080)
        now[0] += 6
        tick(1920, 1080)
        snapshot = runtime.camera_capture_state("device")
        assert snapshot.status == "failed" and snapshot.error_code == "camera_no_frames"

        # The renderer publishes a fresh frame after policy reads failure but
        # before the runtime attempts to reserve the same generation.
        receive(1920, 1080, 100)
        tick(1920, 1080)
        recovered = runtime.camera_capture_state("device")
        assert recovered.status == "ready" and recovered.generation == snapshot.generation

        assert not runtime.reconnect_camera("device", expected_generation=snapshot.generation)
        assert runtime.camera_capture_state("device") == recovered
        assert not source.updates
    finally:
        runtime._release_camera_sources()


def test_camera_reconnect_forces_unchanged_mode_and_keeps_shared_consumers(
    monkeypatch, camera_runtime,
):
    monkeypatch.setattr(sys, "platform", "win32")
    runtime, created, events, _modes = camera_runtime
    mode = _exact_camera_mode(pixel_format="400")
    source = runtime.camera_source("Camera:USB-DEVICE", **mode)
    consumer = runtime.camera_source("usb-device", "Renamed", **mode)
    settings = dict(source.settings)
    generation = runtime.camera_capture_state("usb-device").generation
    previous_events = list(events)

    for _ in range(2):
        runtime._camera_probes["usb-device"].fail()
        assert runtime.reconnect_camera("Renamed:USB-DEVICE", expected_generation=generation)
        generation += 1
        assert runtime.camera_capture_state("usb-device").generation == generation
        assert not runtime.reconnect_camera("Renamed:USB-DEVICE", expected_generation=generation)
        assert runtime.camera_source("usb-device", **mode) is consumer is source

    assert len(created) == 1
    assert source.updates == [settings, settings]
    assert runtime._camera_probes["usb-device"].updates == [(settings, 1920, 1080)] * 2
    assert events == previous_events
    assert source.releases == 0


@pytest.mark.parametrize("cached_mode", [False, True])
@pytest.mark.parametrize("from_manual", [False, True])
def test_camera_reconnect_reuses_effective_automatic_settings_without_resolving(
    monkeypatch, camera_runtime, cached_mode, from_manual,
):
    from solin.core.media import camera_source

    monkeypatch.setattr(sys, "platform", "win32")
    runtime, created, events, modes = camera_runtime
    if from_manual:
        runtime.camera_source("device", **_exact_camera_mode(
            width=640, height=480, pixel_format="401",
        ))
    if cached_mode:
        modes.append(SimpleNamespace(**_exact_camera_mode(pixel_format="400")))
    source = runtime.camera_source("device")
    settings = dict(source.settings)
    previous_updates = list(source.updates)
    generation = runtime.camera_capture_state("device").generation
    previous_events = list(events)
    modes.append(SimpleNamespace(**_exact_camera_mode(width=1280, height=720, pixel_format="401")))
    monkeypatch.setattr(camera_source, "camera_source_spec", lambda *args, **kwargs: pytest.fail(
        "Reconnect must reuse effective settings without resolving a mode",
    ))

    runtime._camera_probes["device"].fail()
    assert runtime.reconnect_camera("device", expected_generation=generation)
    assert runtime.camera_source("device") is source

    dimensions = (1920, 1080) if cached_mode else (0, 0)
    assert runtime._camera_probes["device"].updates == [(settings, *dimensions)] * (
        2 if from_manual else 1
    )
    assert source.updates == previous_updates + [settings]
    assert len(created) == 1 and events == previous_events


def test_camera_reconnect_rejects_generation_observed_before_reconfiguration(
    monkeypatch, camera_runtime,
):
    monkeypatch.setattr(sys, "platform", "win32")
    runtime, created, _events, _modes = camera_runtime
    source = runtime.camera_source("device", **_exact_camera_mode(pixel_format="400"))
    observed = runtime.camera_capture_state("device").generation
    changed = _exact_camera_mode(width=1280, height=720, pixel_format="401")
    assert runtime.camera_source("device", **changed) is source
    current = runtime.camera_capture_state("device").generation
    assert current != observed
    settings = dict(source.settings)

    runtime._camera_probes["device"].fail()
    assert not runtime.reconnect_camera("device", expected_generation=observed)
    assert len(source.updates) == 1
    assert runtime.camera_capture_state("device").generation == current
    assert runtime.reconnect_camera("device", expected_generation=current)
    assert not runtime.reconnect_camera("device", expected_generation=current)
    assert runtime.camera_source("device", **changed) is source

    assert source.updates == [settings, settings]
    assert runtime._camera_probes["device"].updates[-1] == (settings, 1280, 720)
    assert len(created) == 1


def test_failed_camera_reconnect_preserves_configuration_for_explicit_retry_and_release(
    monkeypatch, camera_runtime,
):
    monkeypatch.setattr(sys, "platform", "win32")
    runtime, created, events, _modes = camera_runtime
    mode = _exact_camera_mode(pixel_format="400")
    source = runtime.camera_source("device", **mode)
    settings = dict(source.settings)
    probe = runtime._camera_probes["device"]
    probe.fail()
    generation = probe.state.generation
    previous_events = list(events)
    source.reject_update = True

    with pytest.raises(obs_runtime.ObsRuntimeError, match="reconnect") as error:
        runtime.reconnect_camera("device", expected_generation=generation)

    assert isinstance(error.value.__cause__, RuntimeError)
    assert probe.state.error_code == "camera_update_failed"
    assert probe.state.generation == generation + 1
    assert runtime.camera_source("device", **mode) is source
    assert source.updates == [settings]
    assert source.settings == settings
    assert not probe.closed and source.releases == 0
    assert not runtime.reconnect_camera("device", expected_generation=generation)

    source.reject_update = False
    assert runtime.reconnect_camera("device", expected_generation=probe.state.generation)
    assert runtime.camera_source("device", **mode) is source
    assert probe.updates == [(settings, 1920, 1080)] * 2
    assert len(created) == 1 and events == previous_events
    runtime._release_camera_sources()
    assert probe.closed and source.releases == 1
    assert not runtime._camera_sources and not runtime._camera_probes
    assert not runtime.reconnect_camera("device", expected_generation=probe.state.generation)


def test_camera_reconnect_holds_runtime_lock_through_probe_reservation_and_native_update(
    monkeypatch, camera_runtime,
):
    import threading

    monkeypatch.setattr(sys, "platform", "win32")
    runtime, _created, _events, _modes = camera_runtime
    source = runtime.camera_source("device", **_exact_camera_mode(pixel_format="400"))
    probe = runtime._camera_probes["device"]
    probe.fail()
    generation = probe.state.generation
    checking, updating = threading.Event(), threading.Event()
    allow_check, allow_update = threading.Event(), threading.Event()
    results, errors = [], []

    class BlockingProbe:
        def reconnect(self, settings, **reservation):
            checking.set()
            assert allow_check.wait(5)
            return probe.reconnect(settings, **reservation)

        def close(self):
            probe.close()

    runtime._camera_probes["device"] = BlockingProbe()
    source_update = source.update

    def blocking_update(settings):
        updating.set()
        assert allow_update.wait(5)
        source_update(settings)

    monkeypatch.setattr(source, "update", blocking_update)

    def reconnect():
        try:
            results.append(runtime.reconnect_camera("device", expected_generation=generation))
        except Exception as error:  # noqa: BLE001 - report worker failures on the test thread
            errors.append(error)

    def assert_runtime_locked():
        acquired = runtime._lock.acquire(blocking=False)
        if acquired:
            runtime._lock.release()
        assert not acquired, "Mode changes and shutdown must wait for reconnect"

    worker = threading.Thread(target=reconnect)
    worker.start()
    try:
        assert checking.wait(5)
        assert_runtime_locked()
        allow_check.set()
        assert updating.wait(5)
        assert_runtime_locked()
        assert not probe.closed and source.releases == 0 and not source.updates
    finally:
        allow_check.set()
        allow_update.set()
        worker.join(timeout=5)

    assert not worker.is_alive() and not errors and results == [True]
    assert len(source.updates) == 1


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_camera_reconnect_leaves_native_platform_recovery_passive(
    monkeypatch, camera_runtime, platform,
):
    monkeypatch.setattr(sys, "platform", platform)
    runtime, created, events, _modes = camera_runtime
    source = runtime.camera_source("device")
    generation = runtime.camera_capture_state("device").generation
    previous_events = list(events)

    assert not runtime.reconnect_camera("device", expected_generation=generation)

    assert runtime.camera_capture_state("device").generation == generation
    assert not source.updates and source.releases == 0
    assert len(created) == 1 and events == previous_events


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("device", ["", "missing"])
def test_camera_reconnect_without_capture_does_not_start_or_instantiate(
    monkeypatch, camera_runtime, platform, device,
):
    monkeypatch.setattr(sys, "platform", platform)
    runtime, created, events, _modes = camera_runtime
    runtime._pylibobs = None

    assert not runtime.reconnect_camera(device, expected_generation=1)

    assert runtime._pylibobs is None
    assert not runtime.started and not created and not events
    assert not runtime._camera_sources and not runtime._camera_probes


class _RtspSource:
    def __init__(self, settings: dict) -> None:
        self.settings = dict(settings)
        self.updates: list[dict] = []

    def update(self, settings: dict) -> None:
        self.updates.append(dict(settings))
        self.settings.update(settings)


def _rtsp_runtime(monkeypatch) -> tuple[obs_runtime.ObsRuntime, list[_RtspSource]]:
    created: list[_RtspSource] = []
    runtime = obs_runtime.ObsRuntime()
    monkeypatch.setattr(runtime, "ensure_started", lambda *args, **kwargs: None)
    runtime._pylibobs = SimpleNamespace(Source=SimpleNamespace(
        create=lambda kind, name, settings: created.append(_RtspSource(settings)) or created[-1],
    ))
    return runtime, created


def _rtsp_settings(uri: str = "rtsp://camera/live", options: str = "rtsp_transport=tcp max_delay=200000"):
    return {"is_local_file": False, "input": uri, "ffmpeg_options": options}


def test_shared_rtsp_source_follows_transport_and_buffer_edits(monkeypatch) -> None:
    runtime, created = _rtsp_runtime(monkeypatch)
    source = runtime.rtsp_source("cam", "rtsp://camera/live", _rtsp_settings())

    # Same address, new transport and buffer: the live source must follow.
    edited = _rtsp_settings(options="rtsp_transport=udp max_delay=500000")
    assert runtime.rtsp_source("cam", "rtsp://camera/live", edited) is source

    assert len(created) == 1
    assert source.updates == [edited]
    assert source.settings["ffmpeg_options"] == "rtsp_transport=udp max_delay=500000"


def test_shared_rtsp_source_is_not_rebuilt_for_unchanged_settings(monkeypatch) -> None:
    runtime, created = _rtsp_runtime(monkeypatch)
    source = runtime.rtsp_source("cam", "rtsp://camera/live", _rtsp_settings())

    # Every hydrate asks again; an unchanged camera must keep its connection.
    runtime.rtsp_source("cam", "rtsp://camera/live", _rtsp_settings())
    runtime.rtsp_source("cam", "rtsp://camera/other", _rtsp_settings("rtsp://camera/other"))

    assert len(created) == 1
    assert [update["input"] for update in source.updates] == ["rtsp://camera/other"]


def test_camera_native_identity_change_updates_shared_source(monkeypatch, camera_runtime):
    monkeypatch.setattr(sys, "platform", "darwin")
    runtime, created, _events, _modes = camera_runtime
    token = lambda colorspace: base64.b64encode(
        f"1920x1080 29.970-30.000 {colorspace} 875704438".encode()).decode()
    mode = _exact_camera_mode(media_type="video/x-raw", pixel_format="420v", native_format=token(2))
    source = runtime.camera_source("device", **mode)
    assert runtime.camera_source("device", **(mode | {"native_format": token(1)})) is source
    assert len(created) == 1 and len(source.updates) == 1
    assert source.settings["supported_format"] == token(1)
