"""Process-wide libobs runtime shared by every media source and projection surface.

libobs is a **singleton engine**: there is exactly one ``obs_startup``/video/audio
context per process.  Solin, by contrast, owns several playback surfaces — two
:class:`~solin.core.media.obs_playback.ObsMediaController` instances (foreground video +
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
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .pylibobs_compat import set_video_compat
from .camera_identity import local_camera_device_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from .obs_tasks import ObsTaskDispatcher

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


@dataclass(slots=True)
class _OwnedX11Display:
    """X11 connection owned by the libobs runtime sidecar."""

    library: Any
    pointer: int

    @classmethod
    def open(cls) -> "_OwnedX11Display | None":
        import ctypes
        import ctypes.util

        path = ctypes.util.find_library("X11")
        if not path:
            return None
        try:
            x11 = ctypes.CDLL(path)
        except OSError:
            return None
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.restype = ctypes.c_int
        pointer = x11.XOpenDisplay(None)
        if not pointer:
            return None
        return cls(library=x11, pointer=int(pointer))

    def close(self) -> None:
        if not self.pointer:
            return
        import ctypes

        self.library.XCloseDisplay(ctypes.c_void_p(self.pointer))
        self.pointer = 0


def libobs_available() -> bool:
    """Return ``True`` when the ``pylibobs`` package can be imported."""
    try:
        import pylibobs  # type: ignore[import-not-found]  # noqa: F401
    except Exception:  # noqa: BLE001 - optional dependency probe
        return False
    return True


def _graphics_module_path() -> str | None:
    """Absolute path to the bundled graphics module for this platform, if present.

    Passing this to ``set_video`` avoids depending on ``LD_LIBRARY_PATH`` for the
    graphics-module dlopen on Linux.  Returns ``None`` on platforms/layouts where
    the co-located module is not found (libobs then uses its default name).

    The module differs per platform: Linux/macOS render through OpenGL, but
    Windows must use **D3D11**. OBS's Windows OpenGL module requires the
    ``ARB_pixel_format`` extension, which is absent under the Basic Render Driver
    (and on plenty of real headless/RDP sessions), so forcing OpenGL there makes
    ``obs_reset_video`` fail outright with "not supported".
    """
    try:
        import pylibobs  # type: ignore[import-not-found]

        module_file = pylibobs.__file__
        if not module_file:
            return None
        base = Path(module_file).resolve().parent / "_libs"
    except Exception:  # noqa: BLE001 - optional dependency probe
        return None

    if sys.platform.startswith("linux"):
        candidates = ["linux/x86_64/libobs-opengl.so", "linux/x86_64/libobs-opengl.so.0"]
    elif sys.platform == "darwin":
        # pylibobs keeps the macOS bundle in a .app-style layout —
        # macos/<arch>/Frameworks/ — so the module is NOT at the arch root. Without
        # the Frameworks/ component this never matched and the absolute-path
        # guarantee silently degraded to libobs' bare default name.
        candidates = [
            f"macos/{arch}/Frameworks/libobs-opengl.dylib"
            for arch in ("arm64", "universal", "x86_64")
        ]
    elif sys.platform == "win32":
        # D3D11 first: it is OBS's default Windows renderer. OpenGL stays as a
        # last resort for the rare box where D3D11 is unavailable.
        candidates = [
            "windows/x86_64/libobs-d3d11.dll",
            "windows/x86_64/libobs-opengl.dll",
        ]
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
        self._graphics_module_owner: Any | None = None
        self._native_tasks: ObsTaskDispatcher | None = None
        self._pylibobs: Any | None = None
        self._video = ObsVideoConfig()
        self._used_channels: set[int] = set()
        self._monitoring_device: tuple[str, str] | None = None
        self._owned_x11_display: _OwnedX11Display | None = None
        # ONE shared capture source per camera device — a V4L2 device can't be
        # opened twice, so the projector and the virtual camera reference the same
        # source (see :meth:`camera_source`).
        self._camera_sources: dict[str, tuple[Any, tuple[object, ...] | None]] = {}
        self._camera_probes: dict[str, Any] = {}
        self._camera_failures: dict[str, Any] = {}
        self._video_observers: Any | None = None
        self._rtsp_sources: dict[str, Any] = {}
        self._rtsp_source_seq = 0
        self._camera_source_seq = 0

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

    def camera_source(
        self,
        device_path: str,
        device_name: str = "",
        *,
        pixel_format: str = "",
        width: int = 0,
        height: int = 0,
        fps_numerator: int = 0,
        fps_denominator: int = 1,
        media_type: str = "",
    ) -> Any:
        """Return the runtime-owned capture shared by every device consumer.

        The complete requested mode identifies the cache entry. Automatic is
        selected from completed metadata when entering that mode or creating a
        source. An absent snapshot uses the plugin's native preference; scene
        preparation never starts or waits on driver enumeration. Updating a rejected mode
        raises and invalidates the cached request so a subsequent selection is
        explicitly reapplied, including a return to the previous working mode.
        """
        if not device_path:
            return None
        from .camera_source import camera_source_spec

        # DShow names may change, but the device path identifies the capture.
        # AVFoundation IDs and Linux paths retain their native case sensitivity.
        device_key = local_camera_device_key(device_path)
        signature = (media_type, pixel_format, width, height, fps_numerator, fps_denominator)
        with self._lock:
            existing = self._camera_sources.get(device_key)
            if existing is not None and existing[1] == signature:
                return existing[0]

        # Validate the whole request before resolving Automatic or mutating OBS.
        kind, settings = camera_source_spec(
            device_path, device_name, pixel_format=pixel_format, width=width, height=height,
            fps_numerator=fps_numerator, fps_denominator=fps_denominator, media_type=media_type,
        )
        self.ensure_started()
        capture_width, capture_height = width, height
        if width == 0:
            from solin.core.scenes.camera_catalog import camera_catalog

            try:
                selected = camera_catalog().automatic_mode(
                    device_path, canvas_width=self.video.width, canvas_height=self.video.height,
                )
            except Exception:  # noqa: BLE001 - native preference remains an explicit fallback
                log.warning("Camera capabilities unavailable; trying the native preferred mode",
                            exc_info=True)
                selected = None
            if selected is not None:
                capture_width, capture_height = selected.width, selected.height
                kind, settings = camera_source_spec(
                    device_path, device_name,
                    pixel_format=selected.pixel_format, width=selected.width, height=selected.height,
                    fps_numerator=selected.fps_numerator, fps_denominator=selected.fps_denominator,
                    media_type=selected.media_type,
                )
            else:
                log.info("No cached automatic camera mode; using the native preference")

        with self._lock:
            # Another consumer may have created the device while preparing settings.
            existing = self._camera_sources.get(device_key)
            if existing is not None:
                source, cached_signature = existing
                if cached_signature != signature:
                    try:
                        self._camera_probes[device_key].update(
                            settings, width=capture_width, height=capture_height,
                        )
                    except Exception as error:  # noqa: BLE001 - native update boundary
                        self._camera_sources[device_key] = (source, None)
                        raise ObsRuntimeError(f"Could not update camera mode for {device_path}") from error
                    self._camera_sources[device_key] = (source, signature)
                return source
            self._camera_source_seq += 1
            try:
                source = self.ob.Source.create(
                    kind, f"solin-shared-cam-{self._camera_source_seq}", settings,
                )
            except Exception:  # noqa: BLE001 - source-creation / plugin boundary
                log.warning("Could not create %s for %s", kind, device_path, exc_info=True)
                self._camera_creation_failed(device_key)
                return None
            if source is None:
                self._camera_creation_failed(device_key)
                return None
            try:
                probe = self.watch_camera_video(source, width=capture_width, height=capture_height)
            except Exception:  # noqa: BLE001 - release unobserved source before propagating
                source.release()
                raise
            self._camera_probes[device_key] = probe
            self._camera_failures.pop(device_key, None)
            self._camera_sources[device_key] = (source, signature)
            return source

    def _camera_creation_failed(self, device_key: str) -> None:
        from .obs_camera_capture import CameraCaptureState

        previous = self._camera_failures.get(device_key)
        self._camera_failures[device_key] = CameraCaptureState(
            previous.generation + 1 if previous is not None else 1,
            "failed", "camera_create_failed",
        )

    def watch_camera_video(self, source: Any, *, width: int, height: int):
        from .obs_camera_capture import ObsCameraCaptureProbe

        return ObsCameraCaptureProbe(self, source, width=width, height=height)

    def camera_capture_state(self, device_path: str):
        key = local_camera_device_key(device_path)
        with self._lock:
            probe = self._camera_probes.get(key)
            return probe.state if probe is not None else self._camera_failures.get(key)

    def observe_source_video(self, source: Any, callback: Any):
        from .obs_video_observer import ObsVideoObserverRegistration

        with self._lock:
            if self._video_observers is None:
                self._video_observers = ObsVideoObserverRegistration(self.ob)
            return self._video_observers.observe(source, callback)

    def rtsp_source(self, camera_id: str, uri: str, settings: dict) -> Any:
        """The ONE shared ``ffmpeg_source`` for ``camera_id``, created on first use.

        The same reason local capture devices are shared: an IP camera usually caps
        how many concurrent streams it will serve — often one for the main stream —
        so a source per scene means the first scene connects and the rest are refused
        and render black. libobs happily shows one source in several scenes at once
        and opens the connection once while it is active in any of them.

        Editing the address updates the existing source in place, so the connection
        follows the new URI without the operator rebuilding their scenes. The runtime
        owns these and frees them on shutdown; callers add them ``owned=False``.
        """
        if not camera_id or not uri:
            return None
        with self._lock:
            existing = self._rtsp_sources.get(camera_id)
            if existing is not None:
                source, cached_uri = existing
                if cached_uri != uri:
                    try:
                        source.update(settings)  # follow the edited address live
                        self._rtsp_sources[camera_id] = (source, uri)
                    except Exception:  # noqa: BLE001 - update/plugin boundary
                        log.warning("Could not update the IP camera address", exc_info=True)
                return source
            self.ensure_started()
            self._rtsp_source_seq += 1
            try:
                source = self.ob.Source.create(
                    "ffmpeg_source", f"solin-shared-rtsp-{self._rtsp_source_seq}", settings
                )
            except Exception:  # noqa: BLE001 - source-creation boundary
                log.warning("Could not create the IP camera source", exc_info=True)
                return None
            if source is None:
                return None
            self._rtsp_sources[camera_id] = (source, uri)
            return source

    def _release_rtsp_sources(self) -> None:
        for source, _uri in self._rtsp_sources.values():
            try:
                source.release()
            except Exception:  # noqa: BLE001 - shutdown must be total
                log.debug("Error releasing shared IP camera source", exc_info=True)
        self._rtsp_sources.clear()

    def _release_camera_sources(self) -> None:
        for probe in self._camera_probes.values():
            probe.close()
        self._camera_probes.clear()
        self._camera_failures.clear()
        for source, _signature in self._camera_sources.values():
            try:
                source.release()
            except Exception:  # noqa: BLE001 - shutdown must be total
                log.debug("Error releasing shared camera source", exc_info=True)
        self._camera_sources.clear()

    def prime_source_video(self, source: Any) -> bool:
        """Upload a decoder frame before exposing its source to a transition.

        Accept only a selected native frame after its synchronous upload.
        Dimensions and cached preloads cannot acknowledge a frame.
        """
        from pylibobs._ffi import ffi, get_lib

        lib: Any = get_lib()
        # The generated binding declares this pointer-returning API as void(void).
        # Use the public libobs signature at this native boundary.
        get_frame = ffi.cast(
            "struct obs_source_frame *(*)(obs_source_t *)", lib.obs_source_get_frame,
        )
        frame = get_frame(source._ptr)
        if frame != ffi.NULL:
            try:
                lib.obs_source_set_video_frame(source._ptr, frame)
                lib.obs_source_show_preloaded_video(source._ptr)
                return True
            finally:
                lib.obs_source_release_frame(source._ptr, frame)
        return False

    def prime_initial_source_video(self, source: Any) -> bool:
        """Upload the initial preload of a newly opened, inactive decoder.

        Only call for a fresh source that has never played. Its dimensions stay
        zero until libobs uploads the decoded preload; no previous texture can
        satisfy this acknowledgement. This does not acknowledge seeks.
        """
        from pylibobs._ffi import get_lib

        lib: Any = get_lib()
        lib.obs_source_show_preloaded_video(source._ptr)
        return source.width > 0 and source.height > 0

    def retain_source_showing(self, source: Any):
        """Keep a prepared decoder eligible for transport, independently of demand."""
        from pylibobs._ffi import get_lib

        lib: Any = get_lib()
        lib.obs_source_inc_showing(source._ptr)
        pointer = source._ptr

        def release():
            nonlocal pointer
            if pointer is not None:
                lib.obs_source_dec_showing(pointer)
                pointer = None

        return release

    @staticmethod
    def atomic_scene_update(scene: Any, update: Any) -> None:
        """Publish scene mutations under the public native scene lock."""
        from pylibobs._ffi import ffi, get_lib

        errors: list[BaseException] = []

        @ffi.callback("void(void *, obs_scene_t *)")
        def apply(_data, _scene):
            try:
                update()
            except BaseException as exc:  # noqa: BLE001 - re-raised after native callback
                errors.append(exc)

        lib: Any = get_lib()
        atomic = ffi.cast(
            "void(*)(obs_scene_t *, void(*)(void *, obs_scene_t *), void *)",
            lib.obs_scene_atomic_update,
        )
        atomic(scene._ptr, apply, ffi.NULL)
        if errors:
            raise errors[0]

    def discard_selected_source_video(self, source: Any) -> None:
        """Release the tick's selected frame without clearing its async queue."""
        from pylibobs._ffi import ffi, get_lib

        lib: Any = get_lib()
        get_frame = ffi.cast(
            "struct obs_source_frame *(*)(obs_source_t *)", lib.obs_source_get_frame,
        )
        frame = get_frame(source._ptr)
        if frame != ffi.NULL:
            lib.obs_source_release_frame(source._ptr, frame)

    def observe_source_updates(self, source: Any, callback: Any):
        """Subscribe to the public signal emitted after the native deferred update."""
        from pylibobs._ffi import ffi, get_lib

        lib: Any = get_lib()
        handler = lib.obs_source_get_signal_handler(source._ptr)

        @ffi.callback("void(void *, calldata_t *)")
        def updated(_data, _calldata):
            callback()

        lib.signal_handler_connect(handler, b"update", updated, ffi.NULL)

        def close():
            lib.signal_handler_disconnect(handler, b"update", updated, ffi.NULL)

        return close

    def watch_source_video(self, source: Any):
        """Prepare a source's video before its decoder/input is opened."""
        from .obs_video_readiness import ObsSourceVideoReadiness

        return ObsSourceVideoReadiness(self, source)

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
                # libobs requires the Unix platform display to be configured
                # before obs_startup(). OBS Studio does the same in OBSInit().
                # The sidecar has no QGuiApplication, so it owns an X11
                # connection when there is no Qt display to share.
                nix_display_configured = self._configure_nix_platform()
                context.startup()
                if sys.platform == "win32":
                    from .obs_tasks import ObsTaskDispatcher

                    self._native_tasks = ObsTaskDispatcher()
                    self._native_tasks.install()
                graphics_module = _graphics_module_path()

                def _init_video(ctx=context, gm=graphics_module):
                    self._graphics_module_owner = set_video_compat(
                        ctx,
                        binding=ob,
                        width=width,
                        height=height,
                        fps_num=fps,
                        graphics_module=gm,
                    )

                if nix_display_configured:
                    _init_video()
                else:
                    # Fallback (no shared display): release any current Qt GL
                    # context so libobs' eglMakeCurrent does not hit EGL_BAD_ACCESS.
                    _run_with_gl_context_released(_init_video)
                context.set_audio()
                context.load_modules()
                if sys.platform == "win32" and "wasapi_output_capture" not in ob.enum_source_types():
                    raise ObsRuntimeError("The Windows audio device notification module is unavailable")
            except Exception as exc:  # noqa: BLE001 - startup boundary, re-raised typed
                # Best-effort teardown so a half-initialised context does not linger.
                try:
                    if self._native_tasks is not None:
                        self._native_tasks.quiesce()
                    ctx = locals().get("context")
                    if ctx is not None:
                        ctx.shutdown()
                except Exception:  # noqa: BLE001 - teardown must not mask the root cause
                    log.warning("libobs teardown after failed startup errored", exc_info=True)
                self._graphics_module_owner = None
                self._close_native_tasks()
                self._release_owned_x11_display()
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
            if self._native_tasks is not None:
                self._native_tasks.quiesce()
            # Under the libobs-sidecar architecture the sidecar owns and tears
            # down its own program graph, recorder, media, window output, and
            # virtual camera before it calls this — so this only releases the
            # runtime's own channels/sources and shuts the context down.
            for channel in sorted(self._used_channels):
                try:
                    self.set_channel_source(channel, None)
                except Exception:  # noqa: BLE001 - shutdown must be total
                    log.warning("Could not clear libobs channel %d on shutdown", channel,
                                exc_info=True)
            self._used_channels.clear()
            # Release the shared camera sources now that no scene references them.
            self._release_camera_sources()
            self._release_rtsp_sources()
            try:
                self._context.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.warning("libobs context shutdown errored", exc_info=True)
            finally:
                self._context = None
                self._video_observers = None
                self._graphics_module_owner = None
                self._close_native_tasks()
                self._release_owned_x11_display()

    def _close_native_tasks(self) -> None:
        if self._native_tasks is not None:
            self._native_tasks.close()
            self._native_tasks = None

    def active_source_tree_contains(self, source: Any, target: Any) -> bool:
        """Inspect active children using borrowed pointers, without owning them.

        pylibobs enumeration wraps children as owned Sources, whose release also
        marks them removed. A read-only traversal must never remove a producer.
        The native walker protects each borrowed child for the callback's lifetime.
        Callers keep the root source alive throughout this synchronous traversal.
        """
        if source is None or target is None:
            return False
        from pylibobs._ffi import ffi, get_lib

        root, wanted = source._ptr, target._ptr
        if root == ffi.NULL or wanted == ffi.NULL:
            return False
        if root == wanted:
            return True
        return self._active_tree_contains(root, wanted, ffi, get_lib())

    def transition_source_tree_contains(self, transition: Any, target: Any) -> bool:
        """Inspect visible transition content, excluding completed audio origins.

        An offscreen projection transition may retain A until its audio is mixed,
        even after video reaches B. Before video completion both branches count;
        afterwards the native active source is the destination. Its temporary
        reference is released directly, without marking the source removed.
        """
        if transition is None or target is None:
            return False
        from pylibobs._ffi import ffi, get_lib

        root, wanted = transition._ptr, target._ptr
        if root == ffi.NULL or wanted == ffi.NULL:
            return False
        lib: Any = get_lib()
        if lib.obs_transition_get_time(root) < 1.0:
            return self._active_tree_contains(root, wanted, ffi, lib)
        active = lib.obs_transition_get_active_source(root)
        try:
            return self._active_tree_contains(active, wanted, ffi, lib)
        finally:
            if active != ffi.NULL:
                lib.obs_source_release(active)

    @staticmethod
    def _active_tree_contains(root: Any, wanted: Any, ffi: Any, lib: Any) -> bool:
        if root == ffi.NULL or wanted == ffi.NULL:
            return False
        if root == wanted:
            return True
        found = False

        @ffi.callback("void(obs_source_t *, obs_source_t *, void *)")
        def inspect(_parent, child, _data):
            nonlocal found
            found = found or child == wanted

        lib.obs_source_enum_active_tree(root, inspect, ffi.NULL)
        return found

    # ── Linux X display sharing ───────────────────────────────────────────

    def _configure_nix_platform(self) -> bool:
        """Configure libobs' Linux/X11 display before ``obs_startup``.

        The app process shares Qt's native X display. The supervised sidecar has
        no Qt application, so it opens and owns a connection to ``$DISPLAY``.
        A False return (non-Linux/BSD or no X display) leaves the caller on the
        GL-context-release fallback.
        """
        if not (sys.platform.startswith("linux") or "bsd" in sys.platform):
            return False
        display = qt_x_display()
        display_owner = "Qt"
        if not display:
            owned_display = _OwnedX11Display.open()
            if owned_display is None:
                return False
            self._owned_x11_display = owned_display
            display = owned_display.pointer
            display_owner = "sidecar"
        ob = self.ob
        try:
            ob.set_nix_platform_display(display)
            ob.set_nix_platform(ob.NixPlatform.X11_EGL)
        except Exception:  # noqa: BLE001 - optional libobs API boundary
            self._release_owned_x11_display()
            log.debug("Could not configure libobs nix platform", exc_info=True)
            return False
        log.info("libobs using %s X display (0x%x) via EGL", display_owner, display)
        return True

    def _release_owned_x11_display(self) -> None:
        display = self._owned_x11_display
        if display is None:
            return
        self._owned_x11_display = None
        try:
            display.close()
        except Exception:  # noqa: BLE001 - native teardown must not escape
            log.debug("Could not close the libobs X11 display", exc_info=True)

    # ── Audio monitoring (speakers) ───────────────────────────────────────

    def _configure_monitoring(self) -> None:
        """Select libobs' logical system-default output instead of a fixed endpoint."""
        try:
            if not self.ob.audio_monitoring_available():
                log.info("Audio monitoring not available on this platform")
                return
        except Exception:  # noqa: BLE001 - native availability boundary
            log.warning("Could not query audio monitoring support", exc_info=True)
            return
        try:
            # "default" is a logical output, even when no physical device is
            # currently present. Resolving it to an endpoint pins playback there
            # and disables Windows' native default-device change handling.
            if self.set_monitoring_device("Default", "default"):
                log.info("libobs audio monitoring uses the system default output")
            else:
                log.warning("Could not select the system default audio monitoring device")
        except Exception:  # noqa: BLE001 - device selection boundary
            log.warning("Could not select audio monitoring device", exc_info=True)

    @property
    def monitoring_device(self) -> tuple[str, str] | None:
        return self._monitoring_device

    def set_monitoring_device(self, name: str, dev_id: str) -> bool:
        def select() -> bool:
            return bool(self.ob.set_audio_monitoring_device(name, dev_id))

        tasks = self._native_tasks
        ok = select() if tasks is None else tasks.run(select)
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
        lib: Any = get_lib()
        lib.obs_set_output_source(channel, ptr)

    # ── Source helpers ────────────────────────────────────────────────────

    def set_source_monitoring(self, source: Any, monitoring_type: int) -> None:
        """Set a source's monitoring type (route its audio to the speakers)."""
        from pylibobs._ffi import get_lib  # type: ignore[import-not-found]

        lib: Any = get_lib()

        def apply() -> None:
            lib.obs_source_set_monitoring_type(source._ptr, int(monitoring_type))

        # Windows resets must share the native destruction queue with monitor
        # creation and teardown; its monitoring mutex alone does not protect
        # audio_monitor_create/init_final or audio_monitor_destroy/free.
        tasks = self._native_tasks
        if tasks is None:
            apply()
        else:
            tasks.run(apply)


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
