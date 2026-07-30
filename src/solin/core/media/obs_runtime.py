"""Process-wide libobs runtime shared by every media source and projection surface.

libobs is a **singleton engine**: there is exactly one ``obs_startup``/video/audio
context per process.  Solin, by contrast, owns several playback surfaces — two
:class:`~solin.core.media.playback.MediaController` instances (foreground video +
background audio) plus N projection/preview windows.  They cannot each own an OBS
context; instead they all share the one built here.

This module is deliberately framework-free (no PySide6) so it can be unit- and
smoke-tested headlessly.  Qt-side concerns (attaching a ``Display`` to a window
handle) live in the projection layer.

Key libobs facts encoded here (validated against pylibobs 0.0.1 / libobs 32.1.2):

* ``set_video`` must be given an **absolute** ``graphics_module`` path, otherwise
  libobs' internal loader fails to find ``libobs-opengl.so`` unless the bundled
  ``_libs`` dir happens to be on ``LD_LIBRARY_PATH``.
* A source only ticks/decodes while assigned to an **output channel**
  (``obs_set_output_source``).  Channels are handed out by :meth:`acquire_channel`.
* Audio reaches the OS speakers through **monitoring**, not the program output:
  a monitoring device is selected here and each audible source is set to
  ``MONITOR_ONLY`` by the playback engine.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

log = logging.getLogger(__name__)

# libobs monitoring types (obs_monitoring_type enum).
MONITORING_NONE = 0
MONITORING_MONITOR_ONLY = 1
MONITORING_MONITOR_AND_OUTPUT = 2

# Highest usable global output channel index in libobs (0..63).
_MAX_CHANNELS = 64

_DEFAULT_CANVAS = (1920, 1080)
_DEFAULT_FPS = 30


class ObsRuntimeError(RuntimeError):
    """Raised when the libobs runtime cannot be started or is unavailable."""


def libobs_available() -> bool:
    """Return ``True`` when the ``pylibobs`` package can be imported."""
    try:
        import pylibobs  # type: ignore[import-not-found]  # noqa: F401
    except Exception:  # noqa: BLE001 - optional dependency probe
        return False
    return True


def _graphics_module_path() -> str | None:
    """Absolute path to the bundled ``libobs-opengl`` graphics module, if present.

    Passing this to ``set_video`` avoids depending on ``LD_LIBRARY_PATH`` for the
    graphics-module dlopen on Linux.  Returns ``None`` on platforms/layouts where
    the co-located module is not found (libobs then uses its default name).
    """
    try:
        import pylibobs  # type: ignore[import-not-found]

        module_file = pylibobs.__file__
        if not module_file:
            return None
        base = Path(module_file).resolve().parent / "_libs"
    except Exception:  # noqa: BLE001 - optional dependency probe
        return None

    import sys

    if sys.platform.startswith("linux"):
        candidates = ["linux/x86_64/libobs-opengl.so", "linux/x86_64/libobs-opengl.so.0"]
    elif sys.platform == "darwin":
        candidates = ["macos/universal/libobs-opengl.dylib", "macos/arm64/libobs-opengl.dylib"]
    elif sys.platform == "win32":
        candidates = ["windows/x86_64/libobs-opengl.dll"]
    else:
        candidates = []

    for rel in candidates:
        candidate = base / rel
        if candidate.is_file():
            return str(candidate)
    return None


def qt_x_display() -> int | None:
    """Return Qt's Xlib ``Display*`` as an int pointer, or ``None``.

    Sharing this with libobs (``obs_set_nix_platform_display``) makes libobs'
    EGL reuse Qt's X connection instead of opening a second one — the proper fix
    for the ``EGL_BAD_ACCESS`` conflict between libobs and Qt's OpenGL context.

    PySide6 6.11 exposes no API for this, so it is read via ctypes:
    ``QGuiApplication::platformNativeInterface()`` → ``QXcbNativeInterface::display()``
    (both exported by the bundled Qt). Only valid on the ``xcb`` platform with a
    live ``QGuiApplication``; returns ``None`` otherwise (Wayland, other Qt
    versions, no app) so callers fall back gracefully.
    """
    try:
        from PySide6 import QtGui

        app = QtGui.QGuiApplication.instance()
        if not isinstance(app, QtGui.QGuiApplication) or app.platformName() != "xcb":
            return None
        import ctypes
        from pathlib import Path

        import PySide6

        lib_dir = Path(PySide6.__file__).resolve().parent / "Qt" / "lib"
        gui = ctypes.CDLL(str(lib_dir / "libQt6Gui.so.6"))
        xcb = ctypes.CDLL(str(lib_dir / "libQt6XcbQpa.so.6"))
        gui._ZN15QGuiApplication23platformNativeInterfaceEv.restype = ctypes.c_void_p
        native_iface = gui._ZN15QGuiApplication23platformNativeInterfaceEv()
        if not native_iface:
            return None
        display_fn = xcb._ZNK19QXcbNativeInterface7displayEv
        display_fn.restype = ctypes.c_void_p
        display_fn.argtypes = [ctypes.c_void_p]
        display = display_fn(native_iface)
        return int(display) if display else None
    except Exception:  # noqa: BLE001 - best-effort native-handle probe
        return None


def _run_with_gl_context_released(fn):
    """Run ``fn`` with no Qt OpenGL context current on this thread, then restore.

    libobs creates its OpenGL device on the calling thread and calls
    ``eglMakeCurrent``; if a Qt OpenGL context (Qt Quick, a QOpenGLWidget, …) is
    already current on that thread, Mesa's EGL returns ``EGL_BAD_ACCESS`` and
    device creation fails with ``obs_reset_video: not supported``. Releasing the
    current Qt context around libobs' graphics init avoids the conflict; it is
    restored afterwards so Qt rendering is unaffected. A no-op when PySide6 is
    absent or no context is current (e.g. headless/non-Qt callers).
    """
    previous = None
    surface = None
    try:
        from PySide6.QtGui import QOpenGLContext

        previous = QOpenGLContext.currentContext()
        if previous is not None:
            surface = previous.surface()
            previous.doneCurrent()
    except Exception:  # noqa: BLE001 - optional Qt boundary
        previous = None
    try:
        return fn()
    finally:
        if previous is not None and surface is not None:
            try:
                previous.makeCurrent(surface)
            except Exception:  # noqa: BLE001 - context restore boundary
                log.debug("Could not restore Qt GL context after libobs init", exc_info=True)


@dataclass(slots=True)
class ObsVideoConfig:
    width: int = _DEFAULT_CANVAS[0]
    height: int = _DEFAULT_CANVAS[1]
    fps: int = _DEFAULT_FPS


class ObsRuntime:
    """Lazily-started, process-wide wrapper around a single ``OBSContext``.

    Thread-safety: :meth:`ensure_started` is guarded by a lock and is idempotent.
    All libobs graphics interaction (video reset, displays) must happen on the
    thread that started it — in Solin that is the Qt GUI thread.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._context: Any | None = None
        self._pylibobs: Any | None = None
        self._video = ObsVideoConfig()
        self._used_channels: set[int] = set()
        self._monitoring_device: tuple[str, str] | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @property
    def started(self) -> bool:
        return self._context is not None

    @property
    def context(self) -> Any:
        if self._context is None:
            raise ObsRuntimeError("libobs runtime is not started")
        return self._context

    @property
    def ob(self) -> Any:
        """The imported ``pylibobs`` module (raises if unavailable)."""
        if self._pylibobs is None:
            import pylibobs  # type: ignore[import-not-found]

            self._pylibobs = pylibobs
        return self._pylibobs

    @property
    def video(self) -> ObsVideoConfig:
        return self._video

    def ensure_started(
        self,
        *,
        width: int = _DEFAULT_CANVAS[0],
        height: int = _DEFAULT_CANVAS[1],
        fps: int = _DEFAULT_FPS,
        locale: str = "en-US",
    ) -> None:
        """Start libobs once (video + audio + modules + monitoring).

        Safe to call repeatedly; only the first call does work.  Raises
        :class:`ObsRuntimeError` if pylibobs is missing or startup fails.
        """
        with self._lock:
            if self._context is not None:
                return
            if not libobs_available():
                raise ObsRuntimeError(
                    "pylibobs is not installed; the libobs media engine is unavailable"
                )
            ob = self.ob
            self._video = ObsVideoConfig(width, height, fps)
            try:
                context = ob.OBSContext(locale=locale)
                context.startup()
                graphics_module = _graphics_module_path()

                # Preferred fix: share Qt's X display so libobs' EGL reuses the
                # toolkit connection (one EGLDisplay → no context collision).
                shared_display = self._configure_nix_platform()

                def _init_video(ctx=context, gm=graphics_module):
                    if gm is not None:
                        ctx.set_video(width, height, fps_num=fps, graphics_module=gm)
                    else:
                        ctx.set_video(width, height, fps_num=fps)

                if shared_display:
                    _init_video()
                else:
                    # Fallback (no shared display): release any current Qt GL
                    # context so libobs' eglMakeCurrent does not hit EGL_BAD_ACCESS.
                    _run_with_gl_context_released(_init_video)
                context.set_audio()
                context.load_modules()
            except Exception as exc:  # noqa: BLE001 - startup boundary, re-raised typed
                # Best-effort teardown so a half-initialised context does not linger.
                try:
                    ctx = locals().get("context")
                    if ctx is not None:
                        ctx.shutdown()
                except Exception:  # noqa: BLE001 - teardown must not mask the root cause
                    log.warning("libobs teardown after failed startup errored", exc_info=True)
                raise ObsRuntimeError(f"Failed to start libobs runtime: {exc}") from exc

            self._context = context
            log.info("libobs runtime started (version %s, canvas %dx%d@%d)",
                     getattr(context, "version", "?"), width, height, fps)
            self._configure_monitoring()

    def shutdown(self) -> None:
        """Release every channel and shut the OBS context down."""
        with self._lock:
            if self._context is None:
                return
            # Tear the projection program (channel-0 transition + content
            # scenes) down first: freeing the OBS context while those sources
            # are live crashes libobs. Lazy import breaks the cycle.
            try:
                from .obs_program import projection_program

                projection_program().shutdown()
            except Exception:  # noqa: BLE001 - shutdown must be total
                log.warning("Projection program shutdown errored", exc_info=True)
            for channel in sorted(self._used_channels):
                try:
                    self.set_channel_source(channel, None)
                except Exception:  # noqa: BLE001 - shutdown must be total
                    log.warning("Could not clear libobs channel %d on shutdown", channel,
                                exc_info=True)
            self._used_channels.clear()
            try:
                self._context.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.warning("libobs context shutdown errored", exc_info=True)
            finally:
                self._context = None

    # ── Linux X display sharing ───────────────────────────────────────────

    def _configure_nix_platform(self) -> bool:
        """Share Qt's X display with libobs on Linux/X11; returns True on success.

        For this to actually merge the two EGLDisplays the app must run Qt on
        EGL (``QT_XCB_GL_INTEGRATION=xcb_egl``, set at startup). A False return
        (non-Linux, Wayland, no display) means the caller uses the GL-release
        fallback instead.
        """
        import sys

        if not (sys.platform.startswith("linux") or "bsd" in sys.platform):
            return False
        display = qt_x_display()
        if not display:
            return False
        ob = self.ob
        try:
            ob.set_nix_platform(ob.NixPlatform.X11_EGL)
            ob.set_nix_platform_display(display)
        except Exception:  # noqa: BLE001 - optional libobs API boundary
            log.debug("Could not configure libobs nix platform", exc_info=True)
            return False
        log.info("libobs sharing Qt X display (0x%x) via EGL", display)
        return True

    # ── Audio monitoring (speakers) ───────────────────────────────────────

    def _configure_monitoring(self) -> None:
        """Select an OS monitoring device so source audio can reach the speakers."""
        ob = self.ob
        try:
            if not ob.audio_monitoring_available():
                log.info("Audio monitoring not available on this platform")
                return
            devices = ob.enum_audio_monitoring_devices()
        except Exception:  # noqa: BLE001 - enumeration boundary
            log.warning("Could not enumerate audio monitoring devices", exc_info=True)
            return
        if not devices:
            return
        device = devices[0]
        try:
            if ob.set_audio_monitoring_device(device.name, device.id):
                self._monitoring_device = (device.name, device.id)
                log.info("libobs audio monitoring device: %s", device.name)
        except Exception:  # noqa: BLE001 - device selection boundary
            log.warning("Could not select audio monitoring device", exc_info=True)

    @property
    def monitoring_device(self) -> tuple[str, str] | None:
        return self._monitoring_device

    def set_monitoring_device(self, name: str, dev_id: str) -> bool:
        ok = bool(self.ob.set_audio_monitoring_device(name, dev_id))
        if ok:
            self._monitoring_device = (name, dev_id)
        return ok

    def available_monitoring_devices(self) -> Sequence[Any]:
        return self.ob.enum_audio_monitoring_devices()

    # ── Output channels ───────────────────────────────────────────────────

    def acquire_channel(self) -> int:
        """Reserve and return the lowest free global output channel."""
        with self._lock:
            for channel in range(_MAX_CHANNELS):
                if channel not in self._used_channels:
                    self._used_channels.add(channel)
                    return channel
        raise ObsRuntimeError("No free libobs output channels")

    def release_channel(self, channel: int) -> None:
        with self._lock:
            self.set_channel_source(channel, None)
            self._used_channels.discard(channel)

    def set_channel_source(self, channel: int, source: Any | None) -> None:
        """Route ``source`` (a pylibobs ``Source`` or ``None``) onto ``channel``.

        A source must be on a channel to tick/decode; ``None`` clears it.
        """
        from pylibobs._ffi import ffi, get_lib  # type: ignore[import-not-found]

        ptr = ffi.NULL if source is None else source._ptr
        get_lib().obs_set_output_source(channel, ptr)

    # ── Source helpers ────────────────────────────────────────────────────

    def set_source_monitoring(self, source: Any, monitoring_type: int) -> None:
        """Set a source's monitoring type (route its audio to the speakers)."""
        from pylibobs._ffi import get_lib  # type: ignore[import-not-found]

        get_lib().obs_source_set_monitoring_type(source._ptr, int(monitoring_type))


_runtime: ObsRuntime | None = None
_runtime_lock = threading.Lock()


def obs_runtime() -> ObsRuntime:
    """Return the process-wide :class:`ObsRuntime` singleton."""
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = ObsRuntime()
    return _runtime


__all__ = [
    "ObsRuntime",
    "ObsRuntimeError",
    "ObsVideoConfig",
    "MONITORING_NONE",
    "MONITORING_MONITOR_ONLY",
    "MONITORING_MONITOR_AND_OUTPUT",
    "libobs_available",
    "obs_runtime",
]
