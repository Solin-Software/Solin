"""
NDI receiver service for OBS/DistroAV program output.

The service intentionally uses the NDI runtime directly instead of routing the
feed through QMediaPlayer. NDI is a live video transport, not a seekable media
URL, so the public surface is frame-oriented: start a named source, emit QImage
frames, and stop cleanly.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from ctypes import (
    POINTER,
    Structure,
    Union,
    byref,
    c_bool,
    c_char_p,
    c_float,
    c_int,
    c_int64,
    c_uint32,
    c_void_p,
)
from pathlib import Path

from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtGui import QImage

from solin.core.integrations.ndi_runtime_paths import candidate_ndi_library_paths

log = logging.getLogger(__name__)

_NDI_FRAME_NONE = 0
_NDI_FRAME_VIDEO = 1
_NDI_FRAME_ERROR = 4
_NDI_FRAME_STATUS_CHANGE = 100

_NDI_COLOR_BGRX_BGRA = 0
_NDI_BANDWIDTH_HIGHEST = 100

_FOURCC_BGRA = 1_095_911_234
_FOURCC_BGRX = 1_481_787_202


class _NDISource(Structure):
    _fields_ = [
        ("p_ndi_name", c_char_p),
        ("p_url_address", c_char_p),
    ]


class _NDIFindCreate(Structure):
    _fields_ = [
        ("show_local_sources", c_bool),
        ("p_groups", c_char_p),
        ("p_extra_ips", c_char_p),
    ]


class _NDIRecvCreateV3(Structure):
    _fields_ = [
        ("source_to_connect_to", _NDISource),
        ("color_format", c_int),
        ("bandwidth", c_int),
        ("allow_video_fields", c_bool),
        ("p_ndi_recv_name", c_char_p),
    ]


class _NDIVideoStride(Union):
    _fields_ = [
        ("line_stride_in_bytes", c_int),
        ("data_size_in_bytes", c_int),
    ]


class _NDIVideoFrameV2(Structure):
    _fields_ = [
        ("xres", c_int),
        ("yres", c_int),
        ("FourCC", c_int),
        ("frame_rate_N", c_int),
        ("frame_rate_D", c_int),
        ("picture_aspect_ratio", c_float),
        ("frame_format_type", c_int),
        ("timecode", c_int64),
        ("p_data", c_void_p),
        ("stride", _NDIVideoStride),
        ("p_metadata", c_char_p),
        ("timestamp", c_int64),
    ]


class NDIRuntimeError(RuntimeError):
    pass


class _NDILib:
    def __init__(self):
        self.dll = self._load_library()
        self._bind()
        if hasattr(self.dll, "NDIlib_initialize") and not self.dll.NDIlib_initialize():
            raise NDIRuntimeError("NDI runtime could not be initialized.")

    @staticmethod
    def _candidate_paths() -> list[str]:
        return candidate_ndi_library_paths(os.environ, sys.platform)

    @classmethod
    def _load_library(cls):
        errors: list[str] = []
        for candidate in cls._candidate_paths():
            try:
                path = Path(candidate)
                if sys.platform == "win32" and path.is_file() and hasattr(os, "add_dll_directory"):
                    with os.add_dll_directory(str(path.parent)):
                        return ctypes.WinDLL(candidate)
                return ctypes.WinDLL(candidate) if sys.platform == "win32" else ctypes.CDLL(candidate)
            except OSError as exc:
                errors.append(str(exc))
        raise NDIRuntimeError(
            "NDI runtime not found. Install NDI Runtime/DistroAV and restart Solin."
        )

    def _bind(self) -> None:
        d = self.dll
        d.NDIlib_initialize.restype = c_bool
        d.NDIlib_initialize.argtypes = []
        d.NDIlib_find_create_v2.restype = c_void_p
        d.NDIlib_find_create_v2.argtypes = [POINTER(_NDIFindCreate)]
        d.NDIlib_find_wait_for_sources.restype = c_bool
        d.NDIlib_find_wait_for_sources.argtypes = [c_void_p, c_uint32]
        d.NDIlib_find_get_current_sources.restype = POINTER(_NDISource)
        d.NDIlib_find_get_current_sources.argtypes = [c_void_p, POINTER(c_uint32)]
        d.NDIlib_find_destroy.restype = None
        d.NDIlib_find_destroy.argtypes = [c_void_p]
        d.NDIlib_recv_create_v3.restype = c_void_p
        d.NDIlib_recv_create_v3.argtypes = [POINTER(_NDIRecvCreateV3)]
        d.NDIlib_recv_capture_v3.restype = c_int
        d.NDIlib_recv_capture_v3.argtypes = [
            c_void_p,
            POINTER(_NDIVideoFrameV2),
            c_void_p,
            c_void_p,
            c_uint32,
        ]
        d.NDIlib_recv_free_video_v2.restype = None
        d.NDIlib_recv_free_video_v2.argtypes = [c_void_p, POINTER(_NDIVideoFrameV2)]
        d.NDIlib_recv_destroy.restype = None
        d.NDIlib_recv_destroy.argtypes = [c_void_p]

    def find_sources(self, timeout_ms: int = 1500) -> list[str]:
        return [name for name, _url in self.find_source_records(timeout_ms)]

    def find_source_records(self, timeout_ms: int = 1500) -> list[tuple[str, str]]:
        settings = _NDIFindCreate(True, None, None)
        finder = self.dll.NDIlib_find_create_v2(byref(settings))
        if not finder:
            raise NDIRuntimeError("Could not create NDI source finder.")
        try:
            deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
            while True:
                records = self._current_source_records(finder)
                if records or time.monotonic() >= deadline:
                    return records
                remaining = max(1, int((deadline - time.monotonic()) * 1000))
                self.dll.NDIlib_find_wait_for_sources(
                    finder,
                    c_uint32(min(remaining, 120)),
                )
        finally:
            self.dll.NDIlib_find_destroy(finder)

    def _current_sources(self, finder) -> list[str]:
        return [name for name, _url in self._current_source_records(finder)]

    def _current_source_records(self, finder) -> list[tuple[str, str]]:
        count = c_uint32(0)
        ptr = self.dll.NDIlib_find_get_current_sources(finder, byref(count))
        records: list[tuple[str, str]] = []
        for i in range(int(count.value)):
            raw_name = ptr[i].p_ndi_name
            if raw_name:
                name = raw_name.decode("utf-8", errors="replace")
                raw_url = ptr[i].p_url_address
                url = raw_url.decode("utf-8", errors="replace") if raw_url else ""
                records.append((name, url))
        return records


class NDIReceiverService(QObject):
    frame_ready = Signal(QImage)
    started = Signal(str)
    stopped = Signal()
    error = Signal(str)
    sources_ready = Signal(list)

    _worker_frame = Signal(int, QImage)
    _worker_started = Signal(int, str)
    _worker_stopped = Signal(int)
    _worker_error = Signal(int, str)
    _sources_discovered = Signal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lib: _NDILib | None = None
        self._thread: threading.Thread | None = None
        self._run_stop_evt: threading.Event | None = None
        self._generation = 0
        self._pending_start: tuple[str, int] | None = None
        self._source_refresh_generation = 0
        self._lock = threading.Lock()
        self._active_source = ""
        self._source_name_bytes: bytes | None = None
        self._source_url_bytes: bytes | None = None
        self._known_sources: dict[str, str] = {}
        self._warm_stop_timer = QTimer(self)
        self._warm_stop_timer.setSingleShot(True)
        self._warm_stop_timer.timeout.connect(self.stop)
        self._worker_frame.connect(self._on_worker_frame)
        self._worker_started.connect(self._on_worker_started)
        self._worker_stopped.connect(self._on_worker_stopped)
        self._worker_error.connect(self._on_worker_error)
        self._sources_discovered.connect(self._on_sources_discovered)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def active_source(self) -> str:
        return self._active_source

    def refresh_sources(self, timeout_ms: int = 1500) -> None:
        self._source_refresh_generation += 1
        generation = self._source_refresh_generation

        def _run():
            try:
                lib = self._ensure_lib()
                records = lib.find_source_records(timeout_ms)
                self._sources_discovered.emit(generation, records)
            except Exception as exc:  # noqa: BLE001 - NDI SDK worker-thread boundary
                if generation == self._source_refresh_generation:
                    self.error.emit(str(exc))
        threading.Thread(target=_run, daemon=True, name="ndi-source-refresh").start()

    def start(self, source_name: str, *, max_fps: int = 30) -> None:
        source_name = (source_name or "").strip()
        if not source_name:
            self.error.emit("NDI source is not configured.")
            return
        self._warm_stop_timer.stop()
        if self.is_running and self._active_source == source_name:
            self.started.emit(source_name)
            return
        max_fps = max(1, int(max_fps or 30))
        if self.is_running:
            self._pending_start = (source_name, max_fps)
            self._request_stop()
            return
        self._begin_start(source_name, max_fps)

    def _begin_start(self, source_name: str, max_fps: int) -> None:
        self._generation += 1
        generation = self._generation
        stop_event = threading.Event()
        self._run_stop_evt = stop_event
        self._active_source = source_name
        self._thread = threading.Thread(
            target=self._worker,
            args=(generation, stop_event, source_name, max_fps),
            daemon=True,
            name="ndi-receiver",
        )
        self._thread.start()

    def stop(self, *, wait: bool = False, timeout: float = 4.0) -> None:
        self._warm_stop_timer.stop()
        self._pending_start = None
        self._request_stop()
        thread = self._thread
        self._active_source = ""
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
            if thread.is_alive():
                log.warning("NDI receiver did not stop within %.1f seconds", timeout)
            else:
                self._finish_run(self._generation)

    def _request_stop(self) -> None:
        stop_event = self._run_stop_evt
        if stop_event is not None:
            stop_event.set()

    def stop_later(self, delay_ms: int = 15_000) -> None:
        if delay_ms <= 0 or not self.is_running:
            self.stop()
            return
        self._warm_stop_timer.start(int(delay_ms))

    def _ensure_lib(self) -> _NDILib:
        with self._lock:
            if self._lib is None:
                self._lib = _NDILib()
            return self._lib

    def _worker(
        self,
        generation: int,
        stop_event: threading.Event,
        source_name: str,
        max_fps: int,
    ) -> None:
        recv = None
        try:
            lib = self._ensure_lib()
            recv = self._create_receiver(lib, self._source_from_name(source_name))

            self._worker_started.emit(generation, source_name)
            min_interval = 1.0 / max_fps
            last_emit = 0.0
            saw_frame = False
            tried_discovery_fallback = False
            fallback_deadline = time.monotonic() + 0.18

            while not stop_event.is_set():
                frame = _NDIVideoFrameV2()
                frame_type = lib.dll.NDIlib_recv_capture_v3(
                    recv,
                    byref(frame),
                    None,
                    None,
                    c_uint32(50 if not saw_frame else 250),
                )
                if frame_type == _NDI_FRAME_VIDEO:
                    try:
                        saw_frame = True
                        now = time.monotonic()
                        if now - last_emit >= min_interval:
                            image = self._frame_to_image(frame)
                            if image is not None:
                                self._worker_frame.emit(generation, image)
                                last_emit = now
                    finally:
                        lib.dll.NDIlib_recv_free_video_v2(recv, byref(frame))
                elif frame_type in (_NDI_FRAME_NONE, _NDI_FRAME_STATUS_CHANGE):
                    if (
                        not saw_frame
                        and not tried_discovery_fallback
                        and time.monotonic() >= fallback_deadline
                    ):
                        tried_discovery_fallback = True
                        resolved = self._find_source(lib, source_name, stop_event)
                        if resolved is not None and not stop_event.is_set():
                            lib.dll.NDIlib_recv_destroy(recv)
                            recv = self._create_receiver(lib, resolved)
                    continue
                elif frame_type == _NDI_FRAME_ERROR:
                    raise NDIRuntimeError("NDI receiver lost the stream.")
        except Exception as exc:  # noqa: BLE001 - NDI native receiver boundary
            if not stop_event.is_set():
                self._worker_error.emit(generation, str(exc))
        finally:
            if recv and self._lib:
                try:
                    self._lib.dll.NDIlib_recv_destroy(recv)
                except Exception:  # noqa: BLE001 - NDI native cleanup boundary
                    log.debug("Failed to destroy NDI receiver", exc_info=True)
            try:
                self._worker_stopped.emit(generation)
            except RuntimeError:
                return

    def _source_from_name(self, source_name: str) -> _NDISource:
        self._source_name_bytes = source_name.encode("utf-8")
        cached_url = self._known_sources.get(source_name, "")
        self._source_url_bytes = cached_url.encode("utf-8") if cached_url else None
        return _NDISource(self._source_name_bytes, self._source_url_bytes)

    def _create_receiver(self, lib: _NDILib, source: _NDISource):
        settings = _NDIRecvCreateV3(
            source,
            _NDI_COLOR_BGRX_BGRA,
            _NDI_BANDWIDTH_HIGHEST,
            False,
            b"Solin OBS Program Receiver",
        )
        recv = lib.dll.NDIlib_recv_create_v3(byref(settings))
        if not recv:
            raise NDIRuntimeError("Could not create NDI receiver.")
        return recv

    def _find_source(
        self,
        lib: _NDILib,
        source_name: str,
        stop_event: threading.Event,
    ) -> _NDISource | None:
        deadline = time.monotonic() + 3.0
        wanted = source_name.casefold()
        while time.monotonic() < deadline and not stop_event.is_set():
            records = lib.find_source_records(250)
            exact = next((name for name, _url in records if name.casefold() == wanted), "")
            fuzzy = next((name for name, _url in records if wanted in name.casefold()), "")
            chosen = exact or fuzzy
            if chosen:
                self._known_sources[chosen] = next(
                    (url for name, url in records if name == chosen),
                    "",
                )
                return self._source_from_name(chosen)
        return None

    def _is_current_run(self, generation: int) -> bool:
        return (
            generation == self._generation
            and self._run_stop_evt is not None
            and not self._run_stop_evt.is_set()
        )

    def _on_worker_frame(self, generation: int, image: QImage) -> None:
        if self._is_current_run(generation):
            self.frame_ready.emit(image)

    def _on_worker_started(self, generation: int, source_name: str) -> None:
        if self._is_current_run(generation):
            self.started.emit(source_name)

    def _on_worker_error(self, generation: int, message: str) -> None:
        if self._is_current_run(generation):
            self.error.emit(message)

    def _on_worker_stopped(self, generation: int) -> None:
        self._finish_run(generation)

    def _finish_run(self, generation: int) -> None:
        if (
            generation != self._generation
            or (self._thread is None and self._run_stop_evt is None)
        ):
            return
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.25)
            if thread.is_alive():
                QTimer.singleShot(10, lambda: self._finish_run(generation))
                return
        self._thread = None
        self._run_stop_evt = None
        self._active_source = ""
        self.stopped.emit()
        pending, self._pending_start = self._pending_start, None
        if pending is not None:
            self._begin_start(*pending)

    def _on_sources_discovered(
        self,
        generation: int,
        records: list[tuple[str, str]],
    ) -> None:
        if generation != self._source_refresh_generation:
            return
        self._known_sources = {name: url for name, url in records}
        self.sources_ready.emit([name for name, _url in records])

    @staticmethod
    def _frame_to_image(frame: _NDIVideoFrameV2) -> QImage | None:
        if not frame.p_data or frame.xres <= 0 or frame.yres <= 0:
            return None
        stride = int(frame.stride.line_stride_in_bytes)
        if stride <= 0:
            stride = int(frame.xres) * 4
        if frame.FourCC == _FOURCC_BGRA:
            fmt = QImage.Format.Format_ARGB32
        elif frame.FourCC == _FOURCC_BGRX:
            fmt = QImage.Format.Format_RGB32
        else:
            return None
        byte_count = stride * int(frame.yres)
        if byte_count <= 0:
            return None
        raw = (ctypes.c_ubyte * byte_count).from_address(int(frame.p_data))
        image = QImage(
            memoryview(raw),
            int(frame.xres),
            int(frame.yres),
            stride,
            fmt,
        )
        return image.copy() if not image.isNull() else None
