"""Disposable, bounded native camera metadata queries, independent of scene runtime."""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from typing import Any, BinaryIO, cast
from uuid import uuid4

from solin.core.foundation.constants import CAMERA_DISCOVERY_ARGUMENT, IS_DEV

from .engine import LocalCameraDevice, LocalCameraModeQuery
from .ipc_protocol import (
    MAXIMUM_CONTROL_FRAME_BYTES,
    SceneIpcEnvelope,
    SceneIpcMessageError,
    encode_envelope,
    read_envelope,
    require_payload_fields,
    require_non_negative_int,
    require_text,
    reserve_protocol_stream,
    write_envelope,
)

log = logging.getLogger(__name__)

# Includes interpreter startup, metadata and native teardown. Forced cleanup has
# a separate reserve below; callers' deadline also covers scene command routing.
NATIVE_CAMERA_QUERY_TIMEOUT_SECONDS = 15.0
CAMERA_DISCOVERY_DEADLINE_MS = 20_000
_REAP_TIMEOUT_SECONDS = 2.0
_POLL_SECONDS = 0.02
_MAX_DIAGNOSTIC_BYTES = 1024 * 1024
_DIAGNOSTIC_TAIL_BYTES = 16 * 1024
_FAILURE_CODE = "discovery_request_failed"
_PARENT_PID_ENV = "SOLIN_CAMERA_DISCOVERY_PARENT_PID"
_JOB_NAME_ENV = "SOLIN_CAMERA_DISCOVERY_JOB_NAME"


def _windows_kernel() -> Any:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


@contextmanager
def _child_ownership():
    environment = os.environ.copy()
    environment[_PARENT_PID_ENV] = str(os.getpid())
    environment.pop(_JOB_NAME_ENV, None)
    if sys.platform != "win32":
        yield environment
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = _windows_kernel()
    name = f"Local\\SolinCameraMetadata-{uuid4().hex}"
    job = kernel.CreateJobObjectW(None, name)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        environment[_JOB_NAME_ENV] = name
        # Never inherit the job handle: a Windows venv launcher can retain an
        # inherited copy while it waits for the real interpreter to exit.
        # The child opens this unique job, assigns itself, then closes its copy.
        yield environment
    finally:
        kernel.CloseHandle(job)


@contextmanager
def _parent_guard():
    raw_parent = os.environ.get(_PARENT_PID_ENV)
    if raw_parent is None:
        # Direct protocol/launcher invocation has no owning helper instance.
        yield
        return
    parent_pid = int(raw_parent)
    if sys.platform == "win32":
        import ctypes

        kernel = _windows_kernel()
        job = kernel.OpenJobObjectW(0x0001, False, os.environ.pop(_JOB_NAME_ENV))
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(job)
        yield
        return
    if os.getppid() != parent_pid:
        raise RuntimeError("Camera metadata parent exited before helper initialization")
    if sys.platform.startswith("linux"):
        import ctypes
        import signal

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        libc.prctl.restype = ctypes.c_int
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0):  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), "Camera metadata parent-death guard failed")
        # Close the race where the parent died before prctl installed the signal.
        if os.getppid() != parent_pid:
            raise RuntimeError("Camera metadata parent exited during helper initialization")
        yield
        return
    stopped = threading.Event()

    def watch_parent():
        while not stopped.wait(0.1):
            if os.getppid() != parent_pid:
                os._exit(1)  # Native callbacks/teardown may never return.

    watcher = threading.Thread(target=watch_parent, name="camera-metadata-parent", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stopped.set()
        watcher.join(timeout=1)


class CameraDiscoveryError(RuntimeError):
    """A failed isolated query; detailed native reasons belong in diagnostics."""

    def __init__(self, error_code: str = _FAILURE_CODE) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _command() -> list[str]:
    if IS_DEV:
        return [sys.executable, "-m", "solin.core.scenes.camera_discovery_process"]
    return [sys.executable, CAMERA_DISCOVERY_ARGUMENT]


def _kill_and_reap(child: subprocess.Popen) -> None:
    if child.poll() is None:
        try:
            child.kill()
        except ProcessLookupError:
            log.debug("Camera metadata helper already exited during cancellation")
    child.wait(timeout=_REAP_TIMEOUT_SECONDS)


class CameraDiscoveryProcess:
    """Serialize disposable children; close cancels active and queued queries.

    No native code runs in this process. No locks are held while waiting for a
    child, except the query gate, which close deliberately does not acquire.
    A closed instance cannot be reused; a catalog may lazily create a new one.
    """

    def __init__(self, *, timeout_seconds: float = NATIVE_CAMERA_QUERY_TIMEOUT_SECONDS) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Camera metadata timeout must be finite and positive")
        self._timeout_seconds = timeout_seconds
        self._closed = threading.Event()
        self._query_gate = threading.Lock()
        self._state_lock = threading.Lock()
        self._active: subprocess.Popen | None = None

    def discover(self) -> list[LocalCameraDevice]:
        return self._request(None)

    def modes(self, query: LocalCameraModeQuery) -> LocalCameraDevice:
        if not isinstance(query, LocalCameraModeQuery):
            raise TypeError("Camera mode query must be a LocalCameraModeQuery")
        return self._request(query)[0]

    def close(self) -> None:
        """Cancel without acquiring the request gate or any scene/runtime lock."""
        with self._state_lock:
            self._closed.set()
            child = self._active
        if child is not None:
            try:
                _kill_and_reap(child)
            except (OSError, subprocess.TimeoutExpired):
                log.error("Camera metadata cancellation failed to reap child", exc_info=True)

    def _check_budget(self, deadline: float) -> None:
        if self._closed.is_set():
            raise RuntimeError("Camera metadata query cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Camera metadata query budget expired")

    def _request(self, query: LocalCameraModeQuery | None) -> list[LocalCameraDevice]:
        started = time.monotonic()
        deadline = started + self._timeout_seconds
        acquired = False
        try:
            while not acquired:
                self._check_budget(deadline)
                acquired = self._query_gate.acquire(timeout=min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
            self._check_budget(deadline)
            return self._run_child(query, deadline)
        except Exception as error:  # noqa: BLE001 - isolated process and protocol boundary
            log.warning("Camera metadata request failed: %s (elapsed=%.3fs)",
                        error, time.monotonic() - started, exc_info=True)
            raise CameraDiscoveryError() from error
        finally:
            if acquired:
                self._query_gate.release()
            log.info("Camera metadata request finished (kind=%s elapsed=%.3fs)",
                     "modes" if query else "inventory", time.monotonic() - started)

    def _run_child(self, query: LocalCameraModeQuery | None, deadline: float) -> list[LocalCameraDevice]:
        identity = uuid4().hex
        request = SceneIpcEnvelope(
            "list_local_cameras" if query is None else "query_local_camera_modes",
            identity, identity, identity, 1, 0, int(deadline * 1000),
            {} if query is None else asdict(query),
        )
        # Regular files avoid pipe-reader/writer threads, pipe backpressure and
        # inherited handles keeping EOF open. Only bounded output is read into RAM.
        with (cast(BinaryIO, tempfile.TemporaryFile()) as command_input,
              cast(BinaryIO, tempfile.TemporaryFile()) as protocol_output,
              cast(BinaryIO, tempfile.TemporaryFile()) as diagnostics,
              _child_ownership() as ownership):
            command_input.write(encode_envelope(request))
            command_input.seek(0)
            child = None
            try:
                self._check_budget(deadline)
                log.info("Camera metadata helper spawning")
                child = subprocess.Popen(
                    _command(), stdin=command_input, stdout=protocol_output, stderr=diagnostics,
                    close_fds=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    env=ownership,
                )
                with self._state_lock:
                    self._active = child
                # Cancellation may have raced Popen before registration.
                while True:
                    self._check_budget(deadline)
                    self._check_output(protocol_output, diagnostics)
                    if child.poll() is not None:
                        break
                    self._closed.wait(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
                child.wait(timeout=_REAP_TIMEOUT_SECONDS)
                self._check_output(protocol_output, diagnostics)
                if child.returncode != 0:
                    raise RuntimeError(f"Camera metadata helper exited with status {child.returncode}")
                protocol_output.seek(0)
                response = read_envelope(protocol_output)
                if response is None or protocol_output.read(1):
                    raise SceneIpcMessageError("Camera metadata helper returned no frame or trailing output")
                if response != replace(request, message_type=response.message_type, payload=response.payload):
                    raise SceneIpcMessageError("Camera metadata response identity mismatch")
                expected_type = "local_camera_list" if query is None else "local_camera_modes"
                if response.message_type != expected_type:
                    raise SceneIpcMessageError("Camera metadata helper returned a failed or unexpected response")
                from .process_engine import (
                    _local_camera_discovery_from_envelope, _local_camera_modes_from_envelope,
                )

                result = (_local_camera_discovery_from_envelope(response) if query is None
                          else _local_camera_modes_from_envelope(response, query))
                if not result.supported or not result.ready or result.error_code:
                    raise SceneIpcMessageError("Camera metadata helper returned unavailable metadata")
                self._check_budget(deadline)
                return list(result.devices)
            finally:
                try:
                    if child is not None:
                        _kill_and_reap(child)
                finally:
                    with self._state_lock:
                        if self._active is child:
                            self._active = None
                    diagnostics.seek(max(0, os.fstat(diagnostics.fileno()).st_size - _DIAGNOSTIC_TAIL_BYTES))
                    tail = diagnostics.read(_DIAGNOSTIC_TAIL_BYTES).decode("utf-8", errors="replace")
                    if tail:
                        # Retain the last stage even when the driver never returns.
                        logger = log.warning if sys.exc_info()[0] is not None else log.debug
                        logger("Camera metadata helper diagnostics (tail):\n%s", tail)

    @staticmethod
    def _check_output(protocol_output: BinaryIO, diagnostics: BinaryIO) -> None:
        if os.fstat(protocol_output.fileno()).st_size > MAXIMUM_CONTROL_FRAME_BYTES + 4:
            raise RuntimeError("Camera metadata protocol output exceeded limit")
        if os.fstat(diagnostics.fileno()).st_size > _MAX_DIAGNOSTIC_BYTES:
            raise RuntimeError("Camera metadata diagnostics exceeded limit")


@contextmanager
def _stage(name: str):
    started = time.monotonic()
    log.info("Camera metadata stage=%s started", name)
    try:
        yield
    finally:
        log.info("Camera metadata stage=%s finished elapsed=%.3fs", name, time.monotonic() - started)


@contextmanager
def _com_apartment():
    if sys.platform != "win32":
        yield
        return
    import ctypes

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    # DirectShow property enumeration runs on this thread, outside capture's
    # own DShowThread. Match that thread's STA and release S_OK and S_FALSE.
    result = ole32.CoInitializeEx(None, 2)
    if result not in (0, 1):
        raise OSError(f"Camera metadata COM initialization failed: 0x{result & 0xffffffff:08x}")
    try:
        yield
    finally:
        ole32.CoUninitialize()


def _load_camera_module() -> None:
    from pylibobs._ffi import ffi, get_lib
    from pylibobs._lib import get_bundled_modules

    name = "win-dshow" if sys.platform == "win32" else "mac-avcapture"
    module = next((item for item in get_bundled_modules() if item[0] == name), None)
    if module is None:
        raise RuntimeError(f"Bundled camera metadata module unavailable: {name}")
    _name, binary, data = module
    lib: Any = get_lib()
    pointer = ffi.new("obs_module_t **")
    status = lib.obs_open_module(pointer, binary.encode(), data.encode())
    if status != 0 or pointer[0] == ffi.NULL or not lib.obs_init_module(pointer[0]):
        raise RuntimeError(f"Camera metadata module failed to load: {name} ({status})")
    lib.obs_post_load_modules()


def _native_metadata(query: LocalCameraModeQuery | None) -> list[LocalCameraDevice]:
    from .v4l2_camera_discovery import discover_local_cameras, query_local_camera_modes

    def collect():
        with _stage("inventory" if query is None else "modes"):
            return discover_local_cameras() if query is None else [query_local_camera_modes(query)]

    if sys.platform.startswith("linux"):
        return collect()
    with _stage("com"), _com_apartment():
        from pylibobs import OBSContext

        context = OBSContext()
        try:
            with _stage("obs_startup"):
                context.startup()
            with _stage("camera_module"):
                _load_camera_module()
            return collect()
        finally:
            with _stage("obs_shutdown"):
                context.shutdown()


def serve(stream_in: BinaryIO, stream_out: BinaryIO) -> int:
    """Handle exactly one versioned request, with no GUI or capture ownership."""
    request = read_envelope(stream_in)
    if request is None:
        return 0
    try:
        if time.monotonic() * 1000 >= request.deadline_monotonic_ms:
            raise TimeoutError("Camera metadata request expired before native initialization")
        query = None
        if request.message_type == "list_local_cameras":
            require_payload_fields(request.payload, frozenset(), message_type=request.message_type)
        elif request.message_type == "query_local_camera_modes":
            fields = require_payload_fields(request.payload, frozenset({"device_id", "width", "height"}),
                                            message_type=request.message_type)
            query = LocalCameraModeQuery(
                require_text(fields["device_id"], "camera device id", maximum=1024),
                require_non_negative_int(fields["width"], "camera width"),
                require_non_negative_int(fields["height"], "camera height"),
            )
        else:
            raise SceneIpcMessageError("Unsupported camera metadata request")
        devices = _native_metadata(query)
        from .v4l2_camera_discovery import serialize_camera_device

        payload = {"supported": True, "ready": True, "generation": 1,
                   "devices": [serialize_camera_device(device) for device in devices], "error_code": ""}
        response = replace(request, message_type="local_camera_list" if query is None else "local_camera_modes",
                           payload=payload)
    except Exception:  # noqa: BLE001 - child process reports contextual metadata failure
        log.exception("Camera metadata child request failed")
        response = replace(request, message_type="error", payload={"error_code": _FAILURE_CODE})
    write_envelope(stream_out, response)
    return 0


def main() -> int:
    # Reserve stdout BEFORE loading libobs: C-level diagnostics also use fd 1.
    with reserve_protocol_stream() as protocol_out:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        with _parent_guard():
            return serve(sys.stdin.buffer, protocol_out)


if __name__ == "__main__":
    raise SystemExit(main())
