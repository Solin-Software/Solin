"""Windows virtual-camera feed for the libobs sidecar (broker + NV12 producer).

On Linux libobs' ``virtualcam_output`` writes straight to a v4l2loopback device.
Windows has no such output; instead a registered Solin DirectShow filter consumes
frames from a per-user shared-memory ring whose location it learns from a per-user
named-pipe broker. The native GStreamer engine normally plays broker + producer,
but it does not run under the libobs engine — so this reimplements both in Python,
feeding the ring from libobs' main-mix NV12 frames.

Pieces:
* :mod:`windows_vcam_transport` — byte-exact wire + ring formats (tested).
* :mod:`windows_vcam_identity` — the per-user pipe name.
* :mod:`windows_vcam_broker` — the named-pipe server (Windows-only ctypes).
* here — the shared ring *file* and the ``add_raw_video_callback`` → ``pack_nv12``
  → ring writer producer, wired to the broker's endpoint.

The frame contract is identical to the native engine's, so the same installed
filter binds transparently. The Windows-only glue (file mapping, ctypes) ships
**unverified** from non-Windows build hosts; only the byte formats are unit-tested.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from solin.core.scenes.windows_vcam_broker import VcamBrokerEndpoint, WindowsVcamBroker
from solin.core.scenes.windows_vcam_identity import (
    current_user_broker_pipe_name,
    current_user_sid,
)
from solin.core.scenes.windows_vcam_transport import (
    PackedVideoFrameLayout,
    SharedFrameRingWriter,
    mapping_size_for,
    nv12_layout,
    pack_nv12,
)

log = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_S = 0.5


def temporary_frame_file_name(guid: str) -> str:
    """The ring filename the filter expects: ``Solin.VirtualCamera.{GUID}.frames``."""
    return f"Solin.VirtualCamera.{guid}.frames"


def _new_generation() -> int:
    # Non-zero, < INT64_MAX/2; changes per channel so a reader re-reads the header.
    return (time.time_ns() & 0x3FFF_FFFF_FFFF_FFFF) or 1


class LibobsWindowsVirtualCamera:
    """Broker + NV12 producer feeding the Solin DirectShow filter (Windows only)."""

    def __init__(self, runtime: Any) -> None:
        if sys.platform != "win32":
            raise RuntimeError("the Windows virtual camera is Windows-only")
        self._runtime = runtime
        self._lock = threading.Lock()
        self._ring_file: Optional[_WindowsSharedRingFile] = None
        self._writer: Optional[SharedFrameRingWriter] = None
        self._broker: Optional[WindowsVcamBroker] = None
        self._callback: Any = None
        self._layout: Optional[PackedVideoFrameLayout] = None
        self._endpoint: Optional[VcamBrokerEndpoint] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()

    @property
    def active(self) -> bool:
        return self._broker is not None

    def start(self) -> bool:
        with self._lock:
            if self._broker is not None:
                return True
            video = self._runtime.video
            width, height, fps = int(video.width), int(video.height), int(video.fps)
            if width % 2 or height % 2 or width <= 0 or height <= 0:
                log.warning("virtual camera needs even canvas dimensions, got %dx%d",
                            width, height)
                return False
            layout = nv12_layout(width, height)
            generation = _new_generation()
            try:
                ring_file = _WindowsSharedRingFile(mapping_size_for(layout), current_user_sid())
            except Exception:  # noqa: BLE001 - Win32 file boundary
                log.warning("could not create the virtual camera frame ring", exc_info=True)
                return False
            try:
                writer = SharedFrameRingWriter(ring_file.buffer, layout, generation=generation)
                endpoint = VcamBrokerEndpoint(
                    mapping_file_path_utf8=ring_file.path,
                    mapping_size=ring_file.mapping_size,
                    generation=generation,
                    layout=layout,
                    fps_numerator=fps,
                    fps_denominator=1,
                )
                broker = WindowsVcamBroker(
                    current_user_broker_pipe_name(), current_user_sid(),
                    lambda: self._endpoint,
                )
                callback = self._runtime.ob.add_raw_video_callback(
                    self._on_frame, format=int(self._runtime.ob.VideoFormat.NV12),
                    width=width, height=height,
                )
            except Exception:  # noqa: BLE001 - libobs / broker boundary
                log.warning("could not start the Windows virtual camera producer", exc_info=True)
                ring_file.close()
                return False
            self._ring_file = ring_file
            self._writer = writer
            self._layout = layout
            self._endpoint = endpoint
            self._callback = callback
            self._broker = broker
        try:
            broker.start()
        except Exception:  # noqa: BLE001 - broker boundary
            log.warning("could not start the virtual camera broker", exc_info=True)
            self.stop()
            return False
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="solin-vcam-heartbeat", daemon=True)
        self._heartbeat_thread.start()
        log.info("Windows virtual camera producer serving %dx%d@%d on %s",
                 width, height, fps, self._endpoint.mapping_file_path_utf8)
        return True

    def _on_frame(self, planes, linesizes, width, height, _fmt, timestamp) -> None:
        # Runs on the obs video thread for every rendered main-mix frame.
        with self._lock:
            writer = self._writer
            layout = self._layout
        if writer is None or layout is None or len(planes) < 2 or len(linesizes) < 2:
            return
        y_plane, uv_plane = planes[0], planes[1]
        y_stride, uv_stride = int(linesizes[0]), int(linesizes[1])
        if not y_plane or not uv_plane or y_stride <= 0 or uv_stride <= 0:
            return
        try:
            payload = pack_nv12(y_plane, y_stride, uv_plane, uv_stride,
                                layout.width, layout.height)
            writer.write(payload, presentation_timestamp_ns=int(timestamp or 0))
        except Exception:  # noqa: BLE001 - shared-memory boundary
            log.debug("virtual camera frame write errored", exc_info=True)

    def _heartbeat_loop(self) -> None:
        # Keeps the ring's liveness counter advancing between frames so the filter
        # can tell a paused-but-live producer from a dead one.
        while not self._heartbeat_stop.wait(_HEARTBEAT_INTERVAL_S):
            with self._lock:
                writer = self._writer
            if writer is None:
                return
            try:
                writer.heartbeat()
            except Exception:  # noqa: BLE001 - shared-memory boundary
                return

    def stop(self) -> None:
        self._heartbeat_stop.set()
        heartbeat, self._heartbeat_thread = self._heartbeat_thread, None
        if heartbeat is not None:
            heartbeat.join(timeout=2.0)
        with self._lock:
            broker, self._broker = self._broker, None
            callback, self._callback = self._callback, None
            ring_file, self._ring_file = self._ring_file, None
            self._writer = None
            self._layout = None
            self._endpoint = None
        if callback is not None:
            try:
                self._runtime.ob.remove_raw_video_callback(callback)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("virtual camera callback removal errored", exc_info=True)
        if broker is not None:
            try:
                broker.stop()
            except Exception:  # noqa: BLE001 - broker boundary
                log.debug("virtual camera broker stop errored", exc_info=True)
        if ring_file is not None:
            ring_file.close()


def _sweep_orphaned_rings() -> int:
    """Delete ring files no process still holds; returns how many went.

    The producer unlinks its ring on close, but a killed or crashed sidecar never gets
    there — and each ring is the full mapping size (~8.9 MB at 1080p), so a restart loop
    can leave hundreds behind. Exclusive-open is the liveness test: if the file opens with
    no sharing, nothing has it mapped and it is safe to remove.
    """
    removed = 0
    directory = Path(tempfile.gettempdir())
    try:
        candidates = list(directory.glob("Solin.VirtualCamera.*.frames"))
    except OSError:  # pragma: no cover - directory listing boundary
        return 0
    for candidate in candidates:
        try:
            # The unlink IS the liveness test: Windows refuses to delete a file that
            # still has an active memory-mapped section, so a running producer's ring
            # survives while an abandoned one goes. (Opening the file is no use here —
            # the hardened DACL grants read and delete but not write.)
            candidate.unlink()
            removed += 1
        except OSError:
            continue  # still mapped by a live producer, or not ours to delete
    if removed:
        log.info("removed %d orphaned virtual camera ring file(s)", removed)
    return removed


class _WindowsSharedRingFile:
    """The memory-mapped ``.frames`` file backing the ring (Windows only).

    Creates ``%TEMP%\\Solin.VirtualCamera.{GUID}.frames`` with a duplex-shared
    handle so the filter can map it read-only while this process writes, sizes it
    to the mapping size, and exposes a writable ``mmap`` for the ring writer.
    Follows the native channel's DACL lifecycle: created ``D:P(A;;GA;;;SY)(A;;GA;;;SID)``
    then tightened to ``…(A;;GRSD;;;SID)`` once our writable mapping exists, so no
    later same-user opener can write the ring the filter trusts. DELETE stays granted
    so the producer can still unlink the ring on close.
    """

    def __init__(self, mapping_size: int, sid: str) -> None:
        import ctypes
        import mmap
        import msvcrt
        from ctypes import wintypes

        self._mapping_size = mapping_size
        self._path = ""
        self._fd = -1
        self._buffer: Any = None

        # A killed or crashed producer cannot unlink its own ring, so clear any
        # abandoned ones before adding another.
        _sweep_orphaned_rings()

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        a32 = ctypes.WinDLL("advapi32", use_last_error=True)

        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        CREATE_NEW = 1
        FILE_ATTRIBUTE_TEMPORARY = 0x00000100
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        ERROR_FILE_EXISTS = 80
        ERROR_ALREADY_EXISTS = 183
        FILE_BEGIN = 0

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL),
            ]

        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE,
        ]
        k32.SetFilePointerEx.argtypes = [
            wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong),
            wintypes.DWORD,
        ]
        k32.SetFilePointerEx.restype = wintypes.BOOL
        k32.SetEndOfFile.argtypes = [wintypes.HANDLE]
        k32.SetEndOfFile.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.LocalFree.argtypes = [wintypes.HANDLE]
        k32.LocalFree.restype = wintypes.HANDLE
        a32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
            wintypes.LPVOID,
        ]
        a32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        a32.SetFileSecurityW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID,
        ]
        a32.SetFileSecurityW.restype = wintypes.BOOL
        DACL_SECURITY_INFORMATION = 0x00000004

        descriptor = wintypes.LPVOID()
        sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{sid})"
        if not a32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise OSError(ctypes.get_last_error(), "frame ring security descriptor failed")
        attributes = SECURITY_ATTRIBUTES()
        attributes.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        attributes.lpSecurityDescriptor = descriptor
        attributes.bInheritHandle = False

        temp_dir = os.environ.get("TEMP") or os.environ.get("TMP") or "."
        handle = None
        try:
            for _ in range(8):
                path = os.path.join(
                    temp_dir, temporary_frame_file_name("{" + str(uuid.uuid4()).upper() + "}"))
                raw = k32.CreateFileW(
                    path, GENERIC_READ | GENERIC_WRITE,
                    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                    ctypes.byref(attributes), CREATE_NEW, FILE_ATTRIBUTE_TEMPORARY, None,
                )
                if raw and raw != INVALID_HANDLE_VALUE:
                    handle = raw
                    self._path = path
                    break
                error = ctypes.get_last_error()
                if error not in (ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS):
                    raise OSError(error, "CreateFileW failed")
            if handle is None:
                raise OSError("could not create a unique frame ring file")

            if not k32.SetFilePointerEx(handle, mapping_size, None, FILE_BEGIN):
                raise OSError(ctypes.get_last_error(), "SetFilePointerEx failed")
            if not k32.SetEndOfFile(handle):
                raise OSError(ctypes.get_last_error(), "SetEndOfFile failed")

            # Hand the Win32 handle to the CRT; the fd (and its mmap) now own it.
            self._fd = msvcrt.open_osfhandle(handle, os.O_RDWR)
            handle = None  # ownership transferred to the fd
            self._buffer = mmap.mmap(self._fd, mapping_size)
            # Tighten the on-disk DACL to read-only for the user now that our
            # writable mapping is established (the filter only reads it) — matching
            # the native channel and denying same-user tampering with the ring.
            # Best-effort: the mapping already works, so a failure only forfeits
            # this hardening rather than the camera.
            self._tighten_dacl_to_read_only(k32, a32, ctypes, wintypes, sid,
                                            DACL_SECURITY_INFORMATION)
        except Exception:  # noqa: BLE001 - cleaned up and re-raised below
            if handle is not None:
                k32.CloseHandle(handle)
            self._cleanup_partial()
            raise
        finally:
            if descriptor:
                k32.LocalFree(descriptor)

    def _tighten_dacl_to_read_only(self, k32, a32, ctypes, wintypes, sid, dacl_info) -> None:
        read_only = wintypes.LPVOID()
        # GR grants read; SD grants DELETE. Without SD the tightening below also
        # locks out _cleanup_partial()'s os.remove(), so every ring file survives
        # its producer and %TEMP% grows by the mapping size on each run. DELETE is
        # safe to grant: the point of the hardening is that a same-user process
        # cannot WRITE frames the filter trusts, and withholding GW still ensures that.
        sddl = f"D:P(A;;GA;;;SY)(A;;GRSD;;;{sid})"
        if not a32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(read_only), None
        ):
            log.warning("virtual camera ring: read-only DACL build failed (err %d)",
                        ctypes.get_last_error())
            return
        try:
            if not a32.SetFileSecurityW(self._path, dacl_info, read_only):
                log.warning("virtual camera ring: DACL tighten failed (err %d)",
                            ctypes.get_last_error())
        finally:
            k32.LocalFree(read_only)

    @property
    def buffer(self):
        return self._buffer

    @property
    def path(self) -> str:
        return self._path

    @property
    def mapping_size(self) -> int:
        return self._mapping_size

    def _cleanup_partial(self) -> None:
        if self._buffer is not None:
            try:
                self._buffer.close()
            except Exception:  # noqa: BLE001 - buffer close is best-effort
                log.debug("virtual camera buffer close errored", exc_info=True)
            self._buffer = None
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
        if self._path:
            try:
                os.remove(self._path)
            except OSError:
                # Leaves an 8-9 MB ring behind, so make it visible rather than silent.
                log.warning("could not remove the virtual camera ring %s",
                            self._path, exc_info=True)

    def close(self) -> None:
        self._cleanup_partial()
