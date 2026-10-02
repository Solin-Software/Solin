r"""Per-user identity for the Windows virtual-camera broker pipe.

Mirrors ``native/media_engine/src/windows_virtual_camera_contract.cpp`` so the
Python broker binds the exact pipe name the registered DirectShow filter probes:

    \\.\pipe\Solin.VirtualCamera.FrameBroker.v3.<hash>

where ``<hash>`` is the lowercase-hex SHA-256 of the UTF-16LE bytes (no BOM, no
terminator) of ``"{sid}:{session_id}"`` — ``sid`` the current user's SID string
(``ConvertSidToStringSidW``) and ``session_id`` the process' Terminal-Services
session. Scoping to SID+session keeps one user's feed off another's pipe on a
shared machine.

:func:`broker_identity_hash` / :func:`broker_pipe_name` are platform-neutral and
unit-tested; the SID/session queries are Windows-only (ctypes over advapi32 /
kernel32) and therefore only exercised on Windows hardware.
"""

from __future__ import annotations

import hashlib

from solin.core.scenes.windows_vcam_api import (
    load_windows_library,
    require_windows,
    windows_last_error,
)

BROKER_PIPE_PREFIX = r"\\.\pipe\Solin.VirtualCamera.FrameBroker.v3."


def identity_digest_input(sid: str, session_id: int) -> bytes:
    """The exact bytes hashed by the contract: UTF-16LE of ``sid:session``.

    Matches ``identity.size() * sizeof(wchar_t)`` bytes of the C++ ``wstring``
    (UTF-16LE, no byte-order mark, no null terminator).
    """
    return f"{sid}:{int(session_id)}".encode("utf-16-le")


def broker_identity_hash(sid: str, session_id: int) -> str:
    return hashlib.sha256(identity_digest_input(sid, session_id)).hexdigest()


def broker_pipe_name(sid: str, session_id: int) -> str:
    return BROKER_PIPE_PREFIX + broker_identity_hash(sid, session_id)


# ── Windows-only identity queries (ctypes) ───────────────────────────────────


def current_user_sid() -> str:
    """The current process user's SID string (``S-1-5-…``). Windows only."""
    import ctypes
    from ctypes import wintypes

    advapi32 = load_windows_library("advapi32")
    kernel32 = load_windows_library("kernel32")

    # argtypes/restype are REQUIRED, not cosmetic: without them ctypes marshals
    # every pointer/HANDLE as a 32-bit C int, truncating 64-bit heap addresses
    # (the PSID, the token) on Win64 — corrupting the SID pointer and breaking
    # the whole vcam. Declare them for every call that moves a pointer.
    LPVOID = wintypes.LPVOID
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [LPVOID]
    kernel32.LocalFree.restype = LPVOID
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1  # TOKEN_INFORMATION_CLASS::TokenUser
    ERROR_INSUFFICIENT_BUFFER = 122

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise OSError(windows_last_error(), "OpenProcessToken failed")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        if windows_last_error() != ERROR_INSUFFICIENT_BUFFER or size.value == 0:
            raise OSError(windows_last_error(), "GetTokenInformation sizing failed")
        buffer = (ctypes.c_byte * size.value)()
        if not advapi32.GetTokenInformation(
            token, TOKEN_USER, buffer, size, ctypes.byref(size)
        ):
            raise OSError(windows_last_error(), "GetTokenInformation failed")
        # TOKEN_USER begins with SID_AND_ATTRIBUTES whose first member is the PSID.
        # Indexing POINTER(c_void_p) yields a bare Python int; wrap it in c_void_p
        # (with the LPVOID argtype above) so the full 64-bit pointer is passed.
        psid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        string_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            ctypes.c_void_p(psid), ctypes.byref(string_sid)
        ):
            raise OSError(windows_last_error(), "ConvertSidToStringSidW failed")
        try:
            value = string_sid.value or ""
        finally:
            kernel32.LocalFree(ctypes.cast(string_sid, LPVOID))
        if not value:
            raise OSError("empty SID string")
        return value
    finally:
        kernel32.CloseHandle(token)


def current_session_id() -> int:
    """The current process' Terminal-Services session id. Windows only."""
    import ctypes
    from ctypes import wintypes

    kernel32 = load_windows_library("kernel32")
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.GetCurrentProcessId.argtypes = []
    kernel32.ProcessIdToSessionId.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    session = wintypes.DWORD(0)
    if not kernel32.ProcessIdToSessionId(
        kernel32.GetCurrentProcessId(), ctypes.byref(session)
    ):
        raise OSError(windows_last_error(), "ProcessIdToSessionId failed")
    return int(session.value)


def current_user_broker_pipe_name() -> str:
    """The pipe name for this user+session. Windows only."""
    require_windows()
    return broker_pipe_name(current_user_sid(), current_session_id())
