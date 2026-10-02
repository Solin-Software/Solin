"""Per-user named-pipe broker for the Windows virtual-camera feed.

Python reimplementation of ``native/media_engine/src/virtual_camera_broker.cpp``.
The registered Solin DirectShow filter connects to a per-user pipe, sends a 32-byte
v3 request, and expects a 640-byte response naming the shared-memory frame ring;
on success it keeps the pipe open and streams ``0xA5`` presence markers while it
consumes frames. This serves that handshake from the libobs sidecar.

Only the frame *producer* differs from the native engine — the wire contract, the
pipe name, the ACL, and the ring format are identical, so the same installed
filter binds transparently.

The status-decision core (:func:`build_response_bytes`) is platform-neutral and
unit-tested. The pipe server (:class:`WindowsVcamBroker`) is Windows-only ctypes
over kernel32/advapi32 and is therefore only exercised on Windows hardware — it
ships **unverified** from non-Windows build hosts.
"""

from __future__ import annotations

import logging
import struct
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from solin.core.scenes.windows_vcam_api import (
    load_windows_library,
    require_windows,
    windows_last_error,
)
from solin.core.scenes.windows_vcam_transport import (
    PRESENCE_MARKER,
    REQUEST_SIZE,
    RESPONSE_SIZE,
    BrokerProtocolError,
    BrokerStatus,
    PackedVideoFrameLayout,
    decode_request,
    encode_response,
)

log = logging.getLogger(__name__)

_PIPE_INSTANCES = 4
_HANDSHAKE_TIMEOUT_MS = 2000


@dataclass(frozen=True)
class VcamBrokerEndpoint:
    """What the broker advertises: where the current frame ring lives + its shape."""

    mapping_file_path_utf8: str
    mapping_size: int
    generation: int
    layout: PackedVideoFrameLayout
    fps_numerator: int
    fps_denominator: int


# Returns the current endpoint, or ``None`` when the ring is not ready — matching
# the C++ ``frame_sink_->endpoint()`` optional.
EndpointProvider = Callable[[], Optional[VcamBrokerEndpoint]]


def build_response_bytes(
    request_frame: bytes,
    endpoint: Optional[VcamBrokerEndpoint],
    *,
    authorized: bool,
) -> bytes:
    """Decode a request and produce the 640-byte response (broker decision core).

    Mirrors ``handle_client``'s status selection: an unparseable request →
    ``invalid_request``; an unauthorized client → ``unauthorized``; no ring yet →
    ``transport_unavailable``; otherwise ``ok`` with the ring coordinates. The
    response always echoes the request nonce (zeroed if the request was junk).
    """
    try:
        nonce = decode_request(request_frame)
    except BrokerProtocolError:
        return encode_response(status=BrokerStatus.INVALID_REQUEST, nonce=bytes(16))
    if not authorized:
        return encode_response(status=BrokerStatus.UNAUTHORIZED, nonce=nonce)
    if endpoint is None:
        return encode_response(status=BrokerStatus.TRANSPORT_UNAVAILABLE, nonce=nonce)
    return encode_response(
        status=BrokerStatus.OK,
        nonce=nonce,
        mapping_file_path_utf8=endpoint.mapping_file_path_utf8,
        mapping_size=endpoint.mapping_size,
        generation=endpoint.generation,
        layout=endpoint.layout,
        fps_numerator=endpoint.fps_numerator,
        fps_denominator=endpoint.fps_denominator,
    )


class WindowsVcamBroker:
    """Serves the v3 broker handshake on the current user's pipe (Windows only).

    Runs :data:`_PIPE_INSTANCES` worker threads, each owning one pipe instance and
    looping accept → handshake → drain-presence → disconnect until :meth:`stop`.
    The frame producer keeps the ring fresh out of band; the broker only points
    the filter at it.
    """

    def __init__(self, pipe_name: str, sid: str, endpoint_provider: EndpointProvider) -> None:
        require_windows()
        self._pipe_name = pipe_name
        self._sid = sid
        self._endpoint_provider = endpoint_provider
        self._threads: list[threading.Thread] = []
        self._stop_event = None  # HANDLE to a Win32 manual-reset event
        self._started = False
        self._win = _Win32()

    def start(self) -> None:
        if self._started:
            return
        self._stop_event = self._win.create_event()
        # Create the first instance synchronously so FILE_FLAG_FIRST_PIPE_INSTANCE
        # cannot lose a race to a sibling worker (which would fail its create).
        try:
            first_pipe = self._win.create_pipe(self._pipe_name, self._sid, True)
        except OSError:
            self._win.close_handle(self._stop_event)
            self._stop_event = None
            raise
        for index in range(_PIPE_INSTANCES):
            thread = threading.Thread(
                target=self._serve,
                args=(first_pipe if index == 0 else None,),
                name=f"solin-vcam-broker-{index}",
                daemon=True,
            )
            self._threads.append(thread)
        self._started = True
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._stop_event is not None:
            self._win.set_event(self._stop_event)
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads.clear()
        if self._stop_event is not None:
            self._win.close_handle(self._stop_event)
            self._stop_event = None

    # ── worker loop ──────────────────────────────────────────────────────────

    def _serve(self, initial_pipe) -> None:
        win = self._win
        pipe = initial_pipe  # worker 0 inherits the pre-created first instance
        try:
            while not win.stop_requested(self._stop_event):
                try:
                    if pipe is None:
                        pipe = win.create_pipe(self._pipe_name, self._sid, False)
                    if not win.connect(pipe, self._stop_event):
                        break
                    self._handle_client(pipe)
                except OSError:
                    log.debug("vcam broker worker errored", exc_info=True)
                    break
                finally:
                    if pipe is not None:
                        win.disconnect_and_close(pipe)
                        pipe = None
        finally:
            if pipe is not None:
                win.disconnect_and_close(pipe)

    def _handle_client(self, pipe) -> None:
        win = self._win
        request = win.read_exact(pipe, REQUEST_SIZE, self._stop_event, _HANDSHAKE_TIMEOUT_MS)
        if request is None:
            return
        authorized = win.client_matches_identity(pipe, self._sid)
        endpoint = None
        try:
            endpoint = self._endpoint_provider()
        except Exception:  # noqa: BLE001 - provider boundary
            log.debug("vcam endpoint provider errored", exc_info=True)
        response = build_response_bytes(request, endpoint, authorized=authorized)
        if not win.write_exact(pipe, response, self._stop_event, _HANDSHAKE_TIMEOUT_MS):
            return
        status = struct.unpack_from("<I", response, 12)[0]  # response status u32
        if status == int(BrokerStatus.OK):
            # A persistent filter holds the pipe open and streams presence markers.
            win.drain_presence(pipe, self._stop_event)


def _Win32():  # noqa: N802 - factory named for the class it stands in for
    """Build the Windows syscall shim, or raise on a non-Windows host."""
    require_windows()
    return _Win32Impl()


class _Win32Impl:
    """Thin ctypes wrapper over the kernel32/advapi32 calls the broker needs.

    Faithful to the C++ pipe: ``CreateNamedPipeA`` (duplex, overlapped, byte mode,
    reject-remote, 4 instances) guarded by SDDL ``D:P(A;;GA;;;SY)(A;;GRGW;;;<SID>)``;
    overlapped connect/read/write gated on a shared stop event; identity confirmed
    by impersonation (same SID) and client-session match.
    """

    # Win32 constants
    PIPE_ACCESS_DUPLEX = 0x00000003
    FILE_FLAG_OVERLAPPED = 0x40000000
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    # CreateNamedPipeA's restype is HANDLE (c_void_p), which ctypes returns as an
    # UNSIGNED int — so a failed call yields 0xFFFF…FFFF, never the signed -1.
    # Compared against a plain -1 the failure guard never fires; use the unsigned
    # sentinel (set per-instance in __init__, matching _WindowsSharedRingFile).
    INVALID_HANDLE_VALUE = -1
    ERROR_IO_PENDING = 997
    ERROR_PIPE_CONNECTED = 535
    ERROR_BROKEN_PIPE = 109
    ERROR_PIPE_NOT_CONNECTED = 233
    ERROR_NO_DATA = 232
    ERROR_INSUFFICIENT_BUFFER = 122
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x00000102
    INFINITE = 0xFFFFFFFF
    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1
    SDDL_REVISION_1 = 1

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._k32 = load_windows_library("kernel32")
        self._a32 = load_windows_library("advapi32")
        self.INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value  # 0xFFFF…FFFF, unsigned

        HANDLE = wintypes.HANDLE
        BOOL = wintypes.BOOL
        DWORD = wintypes.DWORD
        LPVOID = wintypes.LPVOID
        LPCSTR = ctypes.c_char_p
        LPDWORD = ctypes.POINTER(DWORD)

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", DWORD),
                ("lpSecurityDescriptor", LPVOID),
                ("bInheritHandle", BOOL),
            ]

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", DWORD),
                ("OffsetHigh", DWORD),
                ("hEvent", HANDLE),
            ]

        self.SECURITY_ATTRIBUTES = SECURITY_ATTRIBUTES
        self.OVERLAPPED = OVERLAPPED

        k32, a32 = self._k32, self._a32
        k32.CreateNamedPipeA.restype = HANDLE
        k32.CreateNamedPipeA.argtypes = [
            LPCSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD,
            ctypes.POINTER(SECURITY_ATTRIBUTES),
        ]
        k32.CreateEventW.restype = HANDLE
        k32.CreateEventW.argtypes = [LPVOID, BOOL, BOOL, wintypes.LPCWSTR]
        k32.SetEvent.argtypes = [HANDLE]
        k32.SetEvent.restype = BOOL
        k32.CloseHandle.argtypes = [HANDLE]
        k32.CloseHandle.restype = BOOL
        k32.ConnectNamedPipe.argtypes = [HANDLE, ctypes.POINTER(OVERLAPPED)]
        k32.ConnectNamedPipe.restype = BOOL
        k32.DisconnectNamedPipe.argtypes = [HANDLE]
        k32.DisconnectNamedPipe.restype = BOOL
        k32.ReadFile.argtypes = [HANDLE, LPVOID, DWORD, LPDWORD, ctypes.POINTER(OVERLAPPED)]
        k32.ReadFile.restype = BOOL
        k32.WriteFile.argtypes = [HANDLE, LPVOID, DWORD, LPDWORD, ctypes.POINTER(OVERLAPPED)]
        k32.WriteFile.restype = BOOL
        k32.GetOverlappedResult.argtypes = [
            HANDLE, ctypes.POINTER(OVERLAPPED), LPDWORD, BOOL,
        ]
        k32.GetOverlappedResult.restype = BOOL
        k32.CancelIoEx.argtypes = [HANDLE, ctypes.POINTER(OVERLAPPED)]
        k32.CancelIoEx.restype = BOOL
        k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
        k32.WaitForSingleObject.restype = DWORD
        k32.WaitForMultipleObjects.argtypes = [DWORD, ctypes.POINTER(HANDLE), BOOL, DWORD]
        k32.WaitForMultipleObjects.restype = DWORD
        k32.GetNamedPipeClientProcessId.argtypes = [HANDLE, LPDWORD]
        k32.GetNamedPipeClientProcessId.restype = BOOL
        k32.ProcessIdToSessionId.argtypes = [DWORD, LPDWORD]
        k32.ProcessIdToSessionId.restype = BOOL
        k32.GetCurrentProcessId.restype = DWORD
        k32.GetCurrentThread.restype = HANDLE
        k32.LocalFree.argtypes = [HANDLE]
        k32.LocalFree.restype = HANDLE

        a32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, DWORD, ctypes.POINTER(LPVOID), LPVOID,
        ]
        a32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = BOOL
        a32.ImpersonateNamedPipeClient.argtypes = [HANDLE]
        a32.ImpersonateNamedPipeClient.restype = BOOL
        a32.RevertToSelf.restype = BOOL
        a32.OpenThreadToken.argtypes = [HANDLE, DWORD, BOOL, ctypes.POINTER(HANDLE)]
        a32.OpenThreadToken.restype = BOOL
        a32.GetTokenInformation.argtypes = [HANDLE, ctypes.c_int, LPVOID, DWORD, LPDWORD]
        a32.GetTokenInformation.restype = BOOL
        a32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(LPVOID)]
        a32.ConvertStringSidToSidW.restype = BOOL
        a32.EqualSid.argtypes = [LPVOID, LPVOID]
        a32.EqualSid.restype = BOOL

    # ── event + handle helpers ───────────────────────────────────────────────

    def create_event(self):
        handle = self._k32.CreateEventW(None, True, False, None)
        if not handle:
            raise self._last_error("CreateEventW")
        return handle

    def set_event(self, event) -> None:
        self._k32.SetEvent(event)

    def close_handle(self, handle) -> None:
        self._k32.CloseHandle(handle)

    def stop_requested(self, stop_event) -> bool:
        return self._k32.WaitForSingleObject(stop_event, 0) == self.WAIT_OBJECT_0

    # ── pipe lifecycle ───────────────────────────────────────────────────────

    def _security_attributes(self, sid: str):
        ctypes = self._ctypes
        LPVOID = self._wintypes.LPVOID
        sddl = f"D:P(A;;GA;;;SY)(A;;GRGW;;;{sid})"
        descriptor = LPVOID()
        if not self._a32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, self.SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise self._last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        attributes = self.SECURITY_ATTRIBUTES()
        attributes.nLength = ctypes.sizeof(self.SECURITY_ATTRIBUTES)
        attributes.lpSecurityDescriptor = descriptor
        attributes.bInheritHandle = False
        return attributes, descriptor

    def create_pipe(self, pipe_name: str, sid: str, first_instance: bool):
        ctypes = self._ctypes
        attributes, descriptor = self._security_attributes(sid)
        flags = self.PIPE_ACCESS_DUPLEX | self.FILE_FLAG_OVERLAPPED
        if first_instance:
            flags |= self.FILE_FLAG_FIRST_PIPE_INSTANCE
        mode = (
            self.PIPE_TYPE_BYTE | self.PIPE_READMODE_BYTE
            | self.PIPE_WAIT | self.PIPE_REJECT_REMOTE_CLIENTS
        )
        try:
            handle = self._k32.CreateNamedPipeA(
                pipe_name.encode("ascii"), flags, mode, _PIPE_INSTANCES,
                RESPONSE_SIZE, REQUEST_SIZE, 0, ctypes.byref(attributes),
            )
        finally:
            if descriptor:
                self._k32.LocalFree(descriptor)  # SD is copied by CreateNamedPipe
        if handle == self.INVALID_HANDLE_VALUE or not handle:
            raise self._last_error("CreateNamedPipeA")
        return handle

    def connect(self, pipe, stop_event) -> bool:
        """Wait for a client (or stop). True if a client connected."""
        ctypes = self._ctypes
        event = self.create_event()
        try:
            overlapped = self.OVERLAPPED()
            overlapped.hEvent = event
            if self._k32.ConnectNamedPipe(pipe, ctypes.byref(overlapped)):
                return True
            error = windows_last_error()
            if error == self.ERROR_PIPE_CONNECTED:
                return True
            if error != self.ERROR_IO_PENDING:
                return False
            if not self._wait_overlapped(pipe, overlapped, event, stop_event, self.INFINITE):
                return False
            return True
        finally:
            self.close_handle(event)

    def disconnect_and_close(self, pipe) -> None:
        self._k32.DisconnectNamedPipe(pipe)
        self.close_handle(pipe)

    # ── overlapped I/O ───────────────────────────────────────────────────────

    def _wait_overlapped(self, pipe, overlapped, event, stop_event, timeout_ms) -> bool:
        """Block until the overlapped op completes, stop fires, or timeout. True on complete."""
        ctypes = self._ctypes
        HANDLE = self._wintypes.HANDLE
        handles = (HANDLE * 2)(stop_event, event)
        result = self._k32.WaitForMultipleObjects(2, handles, False, timeout_ms)
        if result == self.WAIT_OBJECT_0:  # stop
            self._k32.CancelIoEx(pipe, ctypes.byref(overlapped))
            transferred = self._wintypes.DWORD(0)
            self._k32.GetOverlappedResult(pipe, ctypes.byref(overlapped),
                                          ctypes.byref(transferred), True)
            return False
        if result != self.WAIT_OBJECT_0 + 1:  # timeout or failure
            self._k32.CancelIoEx(pipe, ctypes.byref(overlapped))
            transferred = self._wintypes.DWORD(0)
            self._k32.GetOverlappedResult(pipe, ctypes.byref(overlapped),
                                          ctypes.byref(transferred), True)
            return False
        transferred = self._wintypes.DWORD(0)
        return bool(self._k32.GetOverlappedResult(
            pipe, ctypes.byref(overlapped), ctypes.byref(transferred), False))

    def read_exact(self, pipe, count, stop_event, timeout_ms) -> Optional[bytes]:
        ctypes = self._ctypes
        buffer = (ctypes.c_char * count)()
        offset = 0
        while offset < count:
            event = self.create_event()
            try:
                overlapped = self.OVERLAPPED()
                overlapped.hEvent = event
                transferred = self._wintypes.DWORD(0)
                view = (ctypes.c_char * (count - offset)).from_buffer(buffer, offset)
                ok = self._k32.ReadFile(pipe, view, count - offset,
                                        ctypes.byref(transferred), ctypes.byref(overlapped))
                if not ok:
                    error = windows_last_error()
                    if error != self.ERROR_IO_PENDING:
                        return None
                    if not self._wait_overlapped(pipe, overlapped, event, stop_event, timeout_ms):
                        return None
                    self._k32.GetOverlappedResult(pipe, ctypes.byref(overlapped),
                                                  ctypes.byref(transferred), False)
                if transferred.value == 0:
                    return None
                offset += transferred.value
            finally:
                self.close_handle(event)
        return bytes(buffer)

    def write_exact(self, pipe, data, stop_event, timeout_ms) -> bool:
        ctypes = self._ctypes
        buffer = (ctypes.c_char * len(data)).from_buffer_copy(data)
        offset = 0
        total = len(data)
        while offset < total:
            event = self.create_event()
            try:
                overlapped = self.OVERLAPPED()
                overlapped.hEvent = event
                transferred = self._wintypes.DWORD(0)
                view = (ctypes.c_char * (total - offset)).from_buffer(buffer, offset)
                ok = self._k32.WriteFile(pipe, view, total - offset,
                                         ctypes.byref(transferred), ctypes.byref(overlapped))
                if not ok:
                    error = windows_last_error()
                    if error != self.ERROR_IO_PENDING:
                        return False
                    if not self._wait_overlapped(pipe, overlapped, event, stop_event, timeout_ms):
                        return False
                    self._k32.GetOverlappedResult(pipe, ctypes.byref(overlapped),
                                                  ctypes.byref(transferred), False)
                if transferred.value == 0:
                    return False
                offset += transferred.value
            finally:
                self.close_handle(event)
        return True

    def drain_presence(self, pipe, stop_event) -> None:
        """After an ok handshake, read presence markers until the client disconnects."""
        ctypes = self._ctypes
        while not self.stop_requested(stop_event):
            event = self.create_event()
            try:
                overlapped = self.OVERLAPPED()
                overlapped.hEvent = event
                sentinel = (ctypes.c_char * 1)()
                transferred = self._wintypes.DWORD(0)
                ok = self._k32.ReadFile(pipe, sentinel, 1,
                                        ctypes.byref(transferred), ctypes.byref(overlapped))
                if not ok:
                    error = windows_last_error()
                    if error in (self.ERROR_BROKEN_PIPE, self.ERROR_PIPE_NOT_CONNECTED,
                                 self.ERROR_NO_DATA):
                        return
                    if error != self.ERROR_IO_PENDING:
                        return
                    if not self._wait_overlapped(pipe, overlapped, event, stop_event, self.INFINITE):
                        return
                    self._k32.GetOverlappedResult(pipe, ctypes.byref(overlapped),
                                                  ctypes.byref(transferred), False)
                if transferred.value == 0:
                    return
                if sentinel[0] != bytes([PRESENCE_MARKER]):
                    return  # a non-presence byte ends the persistent contract
            finally:
                self.close_handle(event)

    # ── identity check ───────────────────────────────────────────────────────

    def client_matches_identity(self, pipe, expected_sid: str) -> bool:
        """True when the connected client is the same SID+session as this process."""
        ctypes = self._ctypes
        wintypes = self._wintypes
        try:
            client_pid = wintypes.DWORD(0)
            if not self._k32.GetNamedPipeClientProcessId(pipe, ctypes.byref(client_pid)):
                return False
            client_session = wintypes.DWORD(0)
            if not self._k32.ProcessIdToSessionId(client_pid, ctypes.byref(client_session)):
                return False
            our_session = wintypes.DWORD(0)
            if not self._k32.ProcessIdToSessionId(
                self._k32.GetCurrentProcessId(), ctypes.byref(our_session)
            ):
                return False
            if client_session.value != our_session.value:
                return False

            expected_psid = wintypes.LPVOID()
            if not self._a32.ConvertStringSidToSidW(expected_sid, ctypes.byref(expected_psid)):
                return False
            try:
                if not self._a32.ImpersonateNamedPipeClient(pipe):
                    return False
                try:
                    return self._impersonated_sid_matches(expected_psid)
                finally:
                    self._a32.RevertToSelf()
            finally:
                if expected_psid:
                    self._k32.LocalFree(expected_psid)
        except OSError:
            return False

    def _impersonated_sid_matches(self, expected_psid) -> bool:
        ctypes = self._ctypes
        wintypes = self._wintypes
        token = wintypes.HANDLE()
        if not self._a32.OpenThreadToken(
            self._k32.GetCurrentThread(), self.TOKEN_QUERY, True, ctypes.byref(token)
        ):
            return False
        try:
            size = wintypes.DWORD(0)
            self._a32.GetTokenInformation(token, self.TOKEN_USER, None, 0, ctypes.byref(size))
            if windows_last_error() != self.ERROR_INSUFFICIENT_BUFFER or size.value == 0:
                return False
            storage = (ctypes.c_byte * size.value)()
            if not self._a32.GetTokenInformation(
                token, self.TOKEN_USER, storage, size, ctypes.byref(size)
            ):
                return False
            # TOKEN_USER begins with SID_AND_ATTRIBUTES whose first member is PSID.
            client_psid = ctypes.cast(storage, ctypes.POINTER(ctypes.c_void_p))[0]
            return bool(self._a32.EqualSid(client_psid, expected_psid))
        finally:
            self.close_handle(token)

    def _last_error(self, where: str) -> OSError:
        return OSError(windows_last_error(), f"{where} failed")
