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
        names = ["Processing.NDI.Lib.x64.dll", "Processing.NDI.Lib.x86.dll"]
        candidates: list[str] = list(names)

        env_dirs = [
            os.environ.get("NDI_RUNTIME_DIR_V6"),
            os.environ.get("NDI_RUNTIME_DIR_V5"),
            os.environ.get("NDI_RUNTIME_DIR"),
        ]
        for base in [p for p in env_dirs if p]:
            for name in names:
                candidates.append(str(Path(base) / name))
                candidates.append(str(Path(base) / "Bin" / "x64" / name))

        program_files = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ]
        relative_dirs = [
            Path("obs-studio") / "obs-plugins" / "64bit",
            Path("obs-studio") / "bin" / "64bit",
            Path("NDI") / "NDI 6 Runtime" / "v6" / "Bin" / "x64",
            Path("NDI") / "NDI 6 Runtime" / "v6",
            Path("NDI") / "NDI 5 Runtime" / "v5" / "Bin" / "x64",
            Path("NDI") / "NDI 5 Runtime" / "v5",
            Path("NewTek") / "NDI 6 Runtime" / "v6" / "Bin" / "x64",
            Path("NewTek") / "NDI 5 Runtime" / "v5" / "Bin" / "x64",
        ]
        for root in [p for p in program_files if p]:
            for rel in relative_dirs:
                for name in names:
                    candidates.append(str(Path(root) / rel / name))
        return candidates

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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lib: _NDILib | None = None
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._active_source = ""
        self._source_name_bytes: bytes | None = None
        self._source_url_bytes: bytes | None = None
        self._known_sources: dict[str, str] = {}
        self._warm_stop_timer = QTimer(self)
        self._warm_stop_timer.setSingleShot(True)
        self._warm_stop_timer.timeout.connect(self.stop)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def active_source(self) -> str:
        return self._active_source

    def refresh_sources(self, timeout_ms: int = 1500) -> None:
        def _run():
            try:
                lib = self._ensure_lib()
                records = lib.find_source_records(timeout_ms)
                self._known_sources = {name: url for name, url in records}
                self.sources_ready.emit([name for name, _url in records])
            except Exception as exc:
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
        self.stop()
        self._stop_evt.clear()
        self._active_source = source_name
        self._thread = threading.Thread(
            target=self._worker,
            args=(source_name, max(1, int(max_fps or 30))),
            daemon=True,
            name="ndi-receiver",
        )
        self._thread.start()

    def stop(self) -> None:
        self._warm_stop_timer.stop()
        self._stop_evt.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None
        self._active_source = ""

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

    def _worker(self, source_name: str, max_fps: int) -> None:
        recv = None
        try:
            lib = self._ensure_lib()
            recv = self._create_receiver(lib, self._source_from_name(source_name))

            self.started.emit(source_name)
            min_interval = 1.0 / max_fps
            last_emit = 0.0
            saw_frame = False
            tried_discovery_fallback = False
            fallback_deadline = time.monotonic() + 0.18

            while not self._stop_evt.is_set():
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
                                self.frame_ready.emit(image)
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
                        resolved = self._find_source(lib, source_name)
                        if resolved is not None and not self._stop_evt.is_set():
                            lib.dll.NDIlib_recv_destroy(recv)
                            recv = self._create_receiver(lib, resolved)
                    continue
                elif frame_type == _NDI_FRAME_ERROR:
                    raise NDIRuntimeError("NDI receiver lost the stream.")
        except Exception as exc:
            if not self._stop_evt.is_set():
                self.error.emit(str(exc))
        finally:
            if recv and self._lib:
                try:
                    self._lib.dll.NDIlib_recv_destroy(recv)
                except Exception:
                    log.debug("Failed to destroy NDI receiver", exc_info=True)
            self.stopped.emit()

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

    def _find_source(self, lib: _NDILib, source_name: str) -> _NDISource | None:
        deadline = time.monotonic() + 3.0
        wanted = source_name.casefold()
        while time.monotonic() < deadline and not self._stop_evt.is_set():
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
