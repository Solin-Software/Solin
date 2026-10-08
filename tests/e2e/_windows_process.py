from __future__ import annotations

import ctypes
import math
import os
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "read_count",
            "write_count",
            "other_count",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_longlong),
        ("kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong),
        ("period_kernel_time", ctypes.c_longlong),
        ("page_faults", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("terminated_processes", wintypes.DWORD),
    ]


class _PortAssociation(ctypes.Structure):
    _fields_ = [("key", ctypes.c_void_p), ("port", wintypes.HANDLE)]


def _kernel() -> Any:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
            wintypes.BOOL,
        ),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
            wintypes.BOOL,
        ),
        "CreateIoCompletionPort": (
            [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD],
            wintypes.HANDLE,
        ),
        "GetQueuedCompletionStatus": (
            [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_void_p),
                wintypes.DWORD,
            ],
            wintypes.BOOL,
        ),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "IsProcessInJob": (
            [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
            wintypes.BOOL,
        ),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    return kernel


def _check(result: object) -> None:
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())


def _remember_process(kernel: Any, job: int, identifier: int, watched: dict[int, int]) -> None:
    if identifier in watched:
        return
    # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION. A PID may be recycled
    # between enumeration/notification and OpenProcess; validate membership on
    # the opened handle before retaining it.
    handle = kernel.OpenProcess(0x00101000, False, identifier)
    if not handle:
        if ctypes.get_last_error() != 87:
            raise ctypes.WinError(ctypes.get_last_error())
        return
    retained = False
    try:
        member = wintypes.BOOL()
        _check(kernel.IsProcessInJob(handle, job, ctypes.byref(member)))
        if member.value:
            watched[identifier] = handle
            retained = True
    finally:
        if not retained:
            kernel.CloseHandle(handle)


def _remember_processes(kernel: Any, job: int, watched: dict[int, int]) -> None:
    capacity = 32
    while True:
        buffer = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(ctypes.c_size_t))
        if kernel.QueryInformationJobObject(job, 3, buffer, len(buffer), None):
            break
        error = ctypes.get_last_error()
        if error != 234:  # ERROR_MORE_DATA
            raise ctypes.WinError(error)
        capacity = max(capacity * 2, wintypes.DWORD.from_buffer(buffer).value)
    count = wintypes.DWORD.from_buffer(buffer, 4).value
    identifiers = (ctypes.c_size_t * count).from_buffer(buffer, 8)
    for identifier in identifiers:
        _remember_process(kernel, job, identifier, watched)


def _wait_for_empty_job(
    kernel: Any, job: int, port: int, deadline: float, watched: dict[int, int]
) -> None:
    while True:
        _remember_processes(kernel, job, watched)
        accounting = _Accounting()
        _check(
            kernel.QueryInformationJobObject(
                job,
                1,
                ctypes.byref(accounting),
                ctypes.sizeof(accounting),
                None,
            )
        )
        if accounting.active_processes == 0:
            # Job accounting can reach zero before a terminating process handle
            # becomes signaled. Wait for the retained handles as well, including
            # descendants whose launcher has already exited.
            for handle in watched.values():
                milliseconds = max(0, math.ceil((deadline - time.monotonic()) * 1000))
                result = kernel.WaitForSingleObject(handle, min(milliseconds, 0xFFFFFFFE))
                if result == 258:
                    raise TimeoutError("An owned Windows process did not terminate")
                if result != 0:
                    raise ctypes.WinError(ctypes.get_last_error())
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The owned Windows process tree did not complete")
        message = wintypes.DWORD()
        key = ctypes.c_size_t()
        process_id = ctypes.c_void_p()
        # Completion notifications wake the wait promptly. They are not the
        # completion oracle: periodically recheck the authoritative job count
        # because Windows does not guarantee every job notification is delivered.
        completed = kernel.GetQueuedCompletionStatus(
            port,
            ctypes.byref(message),
            ctypes.byref(key),
            ctypes.byref(process_id),
            min(1000, max(1, math.ceil(remaining * 1000))),
        )
        if not completed and ctypes.get_last_error() != 258:  # WAIT_TIMEOUT
            raise ctypes.WinError(ctypes.get_last_error())
        if completed and message.value == 6 and key.value == 1 and process_id.value is not None:
            _remember_process(kernel, job, process_id.value, watched)  # JOB_OBJECT_MSG_NEW_PROCESS


class _CapturedFile(Protocol):
    def seek(self, offset: int, whence: int = 0, /) -> int: ...
    def read(self, size: int = -1, /) -> bytes: ...


def _output(file: _CapturedFile) -> str:
    file.seek(0)
    return file.read().decode("utf-8", errors="replace")


def run_windows_process_tree(
    args: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Capture UTF-8 output and wait for the launcher and all owned descendants.

    The initial thread remains suspended until job assignment succeeds. Children
    inherit that job without breakaway permission. On failure or timeout, kill
    and reap the owned tree before closing its handles. This helper is Windows
    only; it does not elevate or shell-expand its command.
    """
    if os.name != "nt":
        raise OSError("Windows process-tree execution requires Windows")
    if not args or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Provide a command and a positive finite timeout")
    import _winapi
    import msvcrt

    command = [os.fspath(argument) for argument in args]
    kernel = _kernel()
    port: int | None = None
    process: int | None = None
    thread: int | None = None
    assigned = False
    watched: dict[int, int] = {}
    deadline = time.monotonic() + timeout
    with (
        tempfile.TemporaryFile() as stdout,
        tempfile.TemporaryFile() as stderr,
        open(os.devnull, "rb") as stdin,
    ):
        job = kernel.CreateJobObjectW(None, None)
        _check(job)
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(
                kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            )
            port = kernel.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
            _check(port)
            association = _PortAssociation(1, port)
            _check(
                kernel.SetInformationJobObject(
                    job,
                    7,
                    ctypes.byref(association),
                    ctypes.sizeof(association),
                )
            )
            handles = [msvcrt.get_osfhandle(file.fileno()) for file in (stdin, stdout, stderr)]
            startup = subprocess.STARTUPINFO()
            startup.dwFlags = subprocess.STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
            startup.lpAttributeList = {"handle_list": handles}
            try:
                for handle in handles:
                    os.set_handle_inheritable(handle, True)
                process, thread, identifier, _ = _winapi.CreateProcess(
                    None,
                    subprocess.list2cmdline(command),
                    None,
                    None,
                    True,
                    0x4 | 0x400 | 0x80000,  # SUSPENDED | UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO
                    dict(os.environ if env is None else env),
                    os.fspath(cwd) if cwd is not None else None,
                    startup,
                )
            finally:
                for handle in handles:
                    os.set_handle_inheritable(handle, False)
            _check(kernel.AssignProcessToJobObject(job, process))
            assigned = True
            watched[identifier] = process
            if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            assert port is not None
            _wait_for_empty_job(kernel, job, port, deadline, watched)
            return subprocess.CompletedProcess(
                command,
                _winapi.GetExitCodeProcess(process),
                _output(stdout),
                _output(stderr),
            )
        except BaseException as error:
            if process is not None:
                if assigned:
                    _remember_processes(kernel, job, watched)
                    _check(kernel.TerminateJobObject(job, 1))
                    assert port is not None
                    _wait_for_empty_job(kernel, job, port, time.monotonic() + 5, watched)
                else:
                    _winapi.TerminateProcess(process, 1)
                    if _winapi.WaitForSingleObject(process, 5000) != _winapi.WAIT_OBJECT_0:
                        raise RuntimeError(
                            "The suspended Windows process did not terminate"
                        ) from error
            if isinstance(error, TimeoutError):
                raise subprocess.TimeoutExpired(
                    command,
                    timeout,
                    output=_output(stdout),
                    stderr=_output(stderr),
                ) from error
            raise
        finally:
            for handle in watched.values():
                if handle != process:
                    kernel.CloseHandle(handle)
            for handle in (thread, process, job, port):
                if handle is not None:
                    kernel.CloseHandle(handle)
