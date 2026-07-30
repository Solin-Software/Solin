"""Unit tests for the shared libobs runtime (ObsRuntime).

pylibobs and its cffi layer are mocked, so these tests verify the runtime's own
orchestration (startup sequence, monitoring-device selection, channel
bookkeeping, fail-closed teardown and the ffi wrappers) with no real OBS context.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

import solin.core.media.obs_runtime as obs_runtime_mod
from solin.core.media.obs_runtime import (
    MONITORING_MONITOR_ONLY,
    ObsRuntime,
    ObsRuntimeError,
)


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeContext:
    def __init__(self, *, version: str = "32.1.2", fail_set_video: bool = False) -> None:
        self.version = version
        self.calls: list = []
        self._fail_set_video = fail_set_video
        self.shutdown_count = 0

    def startup(self) -> None:
        self.calls.append("startup")

    def set_video(self, width, height, fps_num=None, graphics_module=None) -> None:
        self.calls.append(("set_video", width, height, fps_num, graphics_module))
        if self._fail_set_video:
            raise RuntimeError("obs_reset_video failed")

    def set_audio(self) -> None:
        self.calls.append("set_audio")

    def load_modules(self) -> None:
        self.calls.append("load_modules")

    def shutdown(self) -> None:
        self.shutdown_count += 1


class _Device:
    def __init__(self, name: str, dev_id: str) -> None:
        self.name = name
        self.id = dev_id


class _NixPlatform:
    X11_EGL = 1
    WAYLAND = 2


class _FakeOb:
    NixPlatform = _NixPlatform

    def __init__(self, context, *, devices=None, monitoring=True) -> None:
        self._context = context
        self._devices = devices if devices is not None else [_Device("Speakers", "dev0")]
        self._monitoring = monitoring
        self.set_device_calls: list[tuple[str, str]] = []
        self.nix_platform_calls: list[int] = []
        self.nix_display_calls: list[int] = []

    def OBSContext(self, locale="en-US"):  # noqa: N802 - mirrors pylibobs API
        self._context.locale = locale
        return self._context

    def set_nix_platform(self, platform) -> None:
        self.nix_platform_calls.append(int(platform))

    def set_nix_platform_display(self, display) -> None:
        self.nix_display_calls.append(int(display))

    def audio_monitoring_available(self) -> bool:
        return self._monitoring

    def enum_audio_monitoring_devices(self):
        return list(self._devices)

    def set_audio_monitoring_device(self, name: str, dev_id: str) -> bool:
        self.set_device_calls.append((name, dev_id))
        return True


def _prime(monkeypatch, *, ob, graphics_module="/fake/libobs-opengl.so", available=True):
    monkeypatch.setattr(obs_runtime_mod, "libobs_available", lambda: available)
    monkeypatch.setattr(obs_runtime_mod, "_graphics_module_path", lambda: graphics_module)
    # Deterministic: no Qt display unless a test opts in (avoids platform coupling).
    monkeypatch.setattr(obs_runtime_mod, "qt_x_display", lambda: None)
    rt = ObsRuntime()
    rt._pylibobs = ob  # inject fake pylibobs module
    return rt


def _mock_ffi(monkeypatch):
    """Install a fake pylibobs._ffi and return its recording lib."""
    null = object()

    class _Lib:
        def __init__(self) -> None:
            self.calls: list = []

        def obs_set_output_source(self, channel, ptr) -> None:
            self.calls.append(("output", channel, ptr))

        def obs_source_set_monitoring_type(self, ptr, mtype) -> None:
            self.calls.append(("monitoring", ptr, mtype))

    lib = _Lib()
    module = types.ModuleType("pylibobs._ffi")
    module.ffi = types.SimpleNamespace(NULL=null)
    module.get_lib = lambda: lib
    monkeypatch.setitem(sys.modules, "pylibobs._ffi", module)
    return lib, null


# ── startup orchestration ────────────────────────────────────────────────────


def test_ensure_started_runs_full_sequence_and_selects_monitoring(monkeypatch):
    context = _FakeContext()
    ob = _FakeOb(context)
    rt = _prime(monkeypatch, ob=ob)

    rt.ensure_started(width=1280, height=720, fps=30)

    assert rt.started is True
    assert rt.context is context
    assert context.calls == [
        "startup",
        ("set_video", 1280, 720, 30, "/fake/libobs-opengl.so"),
        "set_audio",
        "load_modules",
    ]
    assert rt.monitoring_device == ("Speakers", "dev0")
    assert ob.set_device_calls == [("Speakers", "dev0")]
    assert rt.video.width == 1280 and rt.video.height == 720 and rt.video.fps == 30


def test_ensure_started_shares_qt_display_on_linux(monkeypatch):
    context = _FakeContext()
    ob = _FakeOb(context)
    rt = _prime(monkeypatch, ob=ob)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(obs_runtime_mod, "qt_x_display", lambda: 0xABCDEF)

    rt.ensure_started()

    # libobs is told to use X11-EGL and share Qt's display before set_video
    assert ob.nix_platform_calls == [_NixPlatform.X11_EGL]
    assert ob.nix_display_calls == [0xABCDEF]
    assert rt.started is True


def test_ensure_started_without_qt_display_skips_sharing(monkeypatch):
    context = _FakeContext()
    ob = _FakeOb(context)
    rt = _prime(monkeypatch, ob=ob)  # qt_x_display returns None by default
    monkeypatch.setattr(sys, "platform", "linux")

    rt.ensure_started()

    assert ob.nix_platform_calls == []  # no sharing → GL-release fallback used
    assert rt.started is True


def test_configure_nix_platform_noop_off_linux(monkeypatch):
    ob = _FakeOb(_FakeContext())
    rt = _prime(monkeypatch, ob=ob)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(obs_runtime_mod, "qt_x_display", lambda: 0x1234)

    assert rt._configure_nix_platform() is False
    assert ob.nix_platform_calls == []


def test_ensure_started_is_idempotent(monkeypatch):
    context = _FakeContext()
    rt = _prime(monkeypatch, ob=_FakeOb(context))

    rt.ensure_started()
    first = list(context.calls)
    rt.ensure_started()

    assert context.calls == first  # no second startup


def test_ensure_started_without_graphics_module_uses_default(monkeypatch):
    context = _FakeContext()
    rt = _prime(monkeypatch, ob=_FakeOb(context), graphics_module=None)

    rt.ensure_started(width=800, height=600, fps=25)

    set_video = [c for c in context.calls if isinstance(c, tuple) and c[0] == "set_video"][0]
    assert set_video == ("set_video", 800, 600, 25, None)


def test_ensure_started_raises_when_libobs_unavailable(monkeypatch):
    rt = _prime(monkeypatch, ob=_FakeOb(_FakeContext()), available=False)

    with pytest.raises(ObsRuntimeError):
        rt.ensure_started()
    assert rt.started is False


def test_ensure_started_tears_down_context_on_failure(monkeypatch):
    context = _FakeContext(fail_set_video=True)
    rt = _prime(monkeypatch, ob=_FakeOb(context))

    with pytest.raises(ObsRuntimeError):
        rt.ensure_started()

    assert context.shutdown_count == 1  # half-initialised context is torn down
    assert rt.started is False


def test_monitoring_skipped_when_unavailable(monkeypatch):
    ob = _FakeOb(_FakeContext(), monitoring=False)
    rt = _prime(monkeypatch, ob=ob)

    rt.ensure_started()

    assert rt.monitoring_device is None
    assert ob.set_device_calls == []


def test_monitoring_skipped_when_no_devices(monkeypatch):
    ob = _FakeOb(_FakeContext(), devices=[])
    rt = _prime(monkeypatch, ob=ob)

    rt.ensure_started()

    assert rt.monitoring_device is None


def test_monitoring_prefers_os_default_output(monkeypatch):
    ob = _FakeOb(
        _FakeContext(),
        devices=[_Device("Headphones", "dev0"), _Device("Speakers", "dev1")],
    )
    rt = _prime(monkeypatch, ob=ob)
    monkeypatch.setattr(rt, "_os_default_output_name", lambda: "Speakers")

    rt.ensure_started()

    assert rt.monitoring_device == ("Speakers", "dev1")  # OS default, not devices[0]


def test_monitoring_prefers_explicit_default_entry(monkeypatch):
    ob = _FakeOb(
        _FakeContext(),
        devices=[_Device("Default", "default"), _Device("Speakers", "dev1")],
    )
    rt = _prime(monkeypatch, ob=ob)

    rt.ensure_started()

    assert rt.monitoring_device == ("Default", "default")


def test_monitoring_falls_back_to_first_device_without_os_default(monkeypatch):
    ob = _FakeOb(
        _FakeContext(),
        devices=[_Device("Headphones", "dev0"), _Device("Speakers", "dev1")],
    )
    rt = _prime(monkeypatch, ob=ob)
    monkeypatch.setattr(rt, "_os_default_output_name", lambda: "")

    rt.ensure_started()

    assert rt.monitoring_device == ("Headphones", "dev0")  # first enumerated


# ── channel bookkeeping ──────────────────────────────────────────────────────


def test_acquire_channel_returns_lowest_free_and_tracks_use():
    rt = ObsRuntime()

    assert rt.acquire_channel() == 0
    assert rt.acquire_channel() == 1
    assert rt.acquire_channel() == 2


def test_acquire_channel_exhaustion_raises():
    rt = ObsRuntime()
    for _ in range(64):
        rt.acquire_channel()

    with pytest.raises(ObsRuntimeError):
        rt.acquire_channel()


def test_release_channel_frees_the_slot(monkeypatch):
    lib, null = _mock_ffi(monkeypatch)
    rt = ObsRuntime()
    first = rt.acquire_channel()
    second = rt.acquire_channel()

    rt.release_channel(first)

    # freed slot is handed out again on the next acquire
    assert rt.acquire_channel() == first
    assert second == 1
    # release clears the channel source via ffi
    assert ("output", first, null) in lib.calls


# ── ffi wrappers ─────────────────────────────────────────────────────────────


def test_set_channel_source_routes_pointer(monkeypatch):
    lib, null = _mock_ffi(monkeypatch)
    rt = ObsRuntime()
    source = types.SimpleNamespace(_ptr="SRC_PTR")

    rt.set_channel_source(3, source)
    rt.set_channel_source(3, None)

    assert lib.calls == [("output", 3, "SRC_PTR"), ("output", 3, null)]


def test_set_source_monitoring_routes_type(monkeypatch):
    lib, _null = _mock_ffi(monkeypatch)
    rt = ObsRuntime()
    source = types.SimpleNamespace(_ptr="SRC_PTR")

    rt.set_source_monitoring(source, MONITORING_MONITOR_ONLY)

    assert lib.calls == [("monitoring", "SRC_PTR", MONITORING_MONITOR_ONLY)]


# ── shutdown ─────────────────────────────────────────────────────────────────


def test_shutdown_clears_channels_and_context(monkeypatch):
    lib, null = _mock_ffi(monkeypatch)
    context = _FakeContext()
    rt = _prime(monkeypatch, ob=_FakeOb(context))
    rt.ensure_started()
    channel = rt.acquire_channel()

    rt.shutdown()

    assert context.shutdown_count == 1
    assert rt.started is False
    # each used channel was cleared to NULL during shutdown
    assert ("output", channel, null) in lib.calls


def test_shutdown_without_start_is_noop():
    rt = ObsRuntime()
    rt.shutdown()  # must not raise
    assert rt.started is False


# ── monitoring device helpers ────────────────────────────────────────────────


def test_set_monitoring_device_updates_only_on_success(monkeypatch):
    class _FailDevice(_FakeOb):
        def set_audio_monitoring_device(self, name, dev_id):
            self.set_device_calls.append((name, dev_id))
            return False

    ok_ob = _FakeOb(_FakeContext())
    rt_ok = _prime(monkeypatch, ob=ok_ob)
    assert rt_ok.set_monitoring_device("Headset", "hs0") is True
    assert rt_ok.monitoring_device == ("Headset", "hs0")

    fail_ob = _FailDevice(_FakeContext())
    rt_fail = ObsRuntime()
    rt_fail._pylibobs = fail_ob
    assert rt_fail.set_monitoring_device("Headset", "hs0") is False
    assert rt_fail.monitoring_device is None


def test_available_monitoring_devices_returns_enumeration(monkeypatch):
    devices = [_Device("A", "a0"), _Device("B", "b0")]
    ob = _FakeOb(_FakeContext(), devices=devices)
    rt = _prime(monkeypatch, ob=ob)

    assert rt.available_monitoring_devices() == devices


def test_monitoring_device_stays_none_when_selection_returns_false(monkeypatch):
    class _FailDevice(_FakeOb):
        def set_audio_monitoring_device(self, name, dev_id):
            self.set_device_calls.append((name, dev_id))
            return False

    ob = _FailDevice(_FakeContext())
    rt = _prime(monkeypatch, ob=ob)

    rt.ensure_started()

    assert ob.set_device_calls == [("Speakers", "dev0")]  # attempted
    assert rt.monitoring_device is None  # but not recorded on failure


def test_monitoring_enumeration_errors_do_not_break_startup(monkeypatch):
    class _RaisingOb(_FakeOb):
        def enum_audio_monitoring_devices(self):
            raise RuntimeError("enumeration failed")

    context = _FakeContext()
    rt = _prime(monkeypatch, ob=_RaisingOb(context))

    rt.ensure_started()  # must not raise

    assert rt.started is True  # startup still succeeds
    assert rt.monitoring_device is None


# ── OBSContext construction failure ──────────────────────────────────────────


def test_context_construction_failure_raises_without_shutdown(monkeypatch):
    class _NoContextOb(_FakeOb):
        def OBSContext(self, locale="en-US"):  # noqa: N802 - mirrors pylibobs API
            raise RuntimeError("cannot create context")

    rt = _prime(monkeypatch, ob=_NoContextOb(_FakeContext()))

    with pytest.raises(ObsRuntimeError):
        rt.ensure_started()
    assert rt.started is False


# ── singleton accessor ───────────────────────────────────────────────────────


def test_obs_runtime_accessor_returns_process_singleton(monkeypatch):
    monkeypatch.setattr(obs_runtime_mod, "_runtime", None)

    first = obs_runtime_mod.obs_runtime()
    second = obs_runtime_mod.obs_runtime()

    assert first is second
    assert isinstance(first, ObsRuntime)


# ── graphics module path (real helper) ───────────────────────────────────────


def test_graphics_module_path_is_none_or_absolute_opengl():
    path = obs_runtime_mod._graphics_module_path()
    if path is not None:
        assert os.path.isabs(path)  # the whole point: absolute, not LD_LIBRARY_PATH-dependent
        base = os.path.basename(path)
        assert base.startswith("libobs-opengl")


def test_graphics_module_path_returns_bundled_absolute_path(monkeypatch, tmp_path):
    """When a co-located graphics module exists, an absolute path to it is returned."""
    pylibobs = pytest.importorskip("pylibobs")  # needs the real package; skip in CI

    pkg_dir = tmp_path / "pylibobs"
    lib_dir = pkg_dir / "_libs" / "linux" / "x86_64"
    lib_dir.mkdir(parents=True)
    (lib_dir / "libobs-opengl.so").write_bytes(b"")
    monkeypatch.setattr(pylibobs, "__file__", str(pkg_dir / "__init__.py"))
    monkeypatch.setattr(sys, "platform", "linux")

    path = obs_runtime_mod._graphics_module_path()

    assert path is not None
    assert os.path.isabs(path)
    assert path.endswith(os.path.join("linux", "x86_64", "libobs-opengl.so"))
