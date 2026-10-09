from __future__ import annotations

from types import SimpleNamespace
import sys
import weakref

import pytest

from solin.core.media import obs_runtime


@pytest.mark.parametrize("upload_fails", [False, True])
def test_native_frame_priming_releases_the_frame_after_upload(monkeypatch, upload_fails):
    events = []
    frame = object()
    source = SimpleNamespace(_ptr=object())

    def get_frame(pointer):
        assert pointer is source._ptr
        return frame

    def upload(pointer, selected):
        assert pointer is source._ptr and selected is frame
        events.append("upload")
        if upload_fails:
            raise RuntimeError("upload failed")

    def release(pointer, selected):
        assert pointer is source._ptr and selected is frame
        events.append("release")

    def cast(signature, function):
        assert signature == "struct obs_source_frame *(*)(obs_source_t *)"
        return function

    lib = SimpleNamespace(
        obs_source_get_frame=get_frame, obs_source_set_video_frame=upload,
        obs_source_release_frame=release,
    )
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        ffi=SimpleNamespace(NULL=None, cast=cast), get_lib=lambda: lib,
    ))
    runtime = obs_runtime.ObsRuntime()
    if upload_fails:
        with pytest.raises(RuntimeError, match="upload failed"):
            runtime.prime_source_video(source, allow_preloaded=False)
    else:
        assert runtime.prime_source_video(source, allow_preloaded=False)
    assert events == ["upload", "release"]


@pytest.mark.parametrize("preloaded", [False, True])
def test_native_preload_priming_does_not_mistake_cached_dimensions_for_a_texture(monkeypatch, preloaded):
    source = SimpleNamespace(_ptr=object(), width=160, height=90)
    events = []

    def clear(pointer, frame):
        assert pointer is source._ptr and frame is None
        events.append("clear")
        source.width = source.height = 0

    def show(pointer):
        assert pointer is source._ptr
        events.append("show")
        if preloaded:
            source.width, source.height = 160, 90

    lib = SimpleNamespace(
        obs_source_get_frame=lambda _: None,
        obs_source_output_video=clear, obs_source_show_preloaded_video=show,
    )
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", SimpleNamespace(
        ffi=SimpleNamespace(NULL=None, cast=lambda _, function: function), get_lib=lambda: lib,
    ))
    runtime = obs_runtime.ObsRuntime()
    assert not runtime.prime_source_video(source, allow_preloaded=False)
    assert events == []
    assert runtime.prime_source_video(source, allow_preloaded=True) is preloaded
    assert events == ["clear", "show"]


@pytest.mark.parametrize("audio_failure", [False, True])
def test_graphics_buffer_is_retained_through_context_shutdown(monkeypatch, audio_failure):
    events = []
    runtime = _runtime_with_fake_libobs(monkeypatch, events)
    context = runtime.ob.OBSContext()
    owner_ref = None

    class GraphicsBuffer:
        pass

    def initialize_video(*args, **kwargs):
        nonlocal owner_ref
        owner = GraphicsBuffer()
        owner_ref = weakref.ref(owner)
        return owner

    def audio():
        assert owner_ref is not None and owner_ref() is not None
        if audio_failure:
            raise RuntimeError("audio failed")

    def shutdown():
        assert owner_ref is not None and owner_ref() is not None
        events.append("shutdown-with-buffer")

    monkeypatch.setattr(obs_runtime, "set_video_compat", initialize_video)
    monkeypatch.setattr(runtime, "_configure_nix_platform", lambda: False)
    monkeypatch.setattr(context, "set_audio", audio)
    monkeypatch.setattr(context, "shutdown", shutdown)
    if audio_failure:
        with pytest.raises(obs_runtime.ObsRuntimeError, match="audio failed"):
            runtime.ensure_started(width=320, height=180, fps=30)
    else:
        runtime.ensure_started(width=320, height=180, fps=30)
        assert runtime._graphics_module_owner is owner_ref()
        runtime.shutdown()

    assert events[-1] == "shutdown-with-buffer"
    assert runtime._graphics_module_owner is None
    assert owner_ref is not None and owner_ref() is None


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
