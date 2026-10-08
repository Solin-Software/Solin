from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

import pytest

from tests.e2e import _windows_process
from tests.e2e._windows_process import run_windows_process_tree


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows owned process trees")


def _events() -> Any:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateEventW": (
            [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR],
            wintypes.HANDLE,
        ),
        "SetEvent": ([wintypes.HANDLE], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


@pytest.fixture
def gates():
    kernel = _events()
    names = {
        kind: f"Local\\SolinProcessTest-{kind}-{uuid.uuid4().hex}"
        for kind in (
            "ready",
            "release_child",
            "release_parent",
        )
    }
    handles = {kind: kernel.CreateEventW(None, True, False, name) for kind, name in names.items()}
    assert all(handles.values())
    try:
        yield kernel, names, handles
    finally:
        # Rescue a fixture child if a deliberately regressed helper fails.
        for handle in handles.values():
            kernel.SetEvent(handle)
            kernel.CloseHandle(handle)


def _launcher(root: Path, names: dict[str, str], *, generations: int = 1) -> list[str]:
    fixture = root / "tree fixture.py"
    fixture.write_text(
        "import ctypes, os, subprocess, sys\n"
        "from ctypes import wintypes\n"
        "from pathlib import Path\n"
        "kernel = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]\n"
        "kernel.OpenEventW.restype = wintypes.HANDLE\n"
        "kernel.SetEvent.argtypes = [wintypes.HANDLE]\n"
        "kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]\n"
        "depth = int(sys.argv[1])\n"
        f"names = {names!r}\n"
        f"root = Path({str(root)!r})\n"
        "(root / f'{depth}.pid').write_text(str(os.getpid()))\n"
        "if depth:\n"
        "    subprocess.Popen([sys.executable, __file__, str(depth - 1)], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"    if depth == {generations}:\n"
        "        print('launcher stdout', flush=True)\n"
        "        print('launcher stderr', file=sys.stderr, flush=True)\n"
        "        gate = kernel.OpenEventW(0x00100000, False, names['release_parent'])\n"
        "        assert kernel.WaitForSingleObject(gate, 30000) == 0\n"
        "    sys.exit(7)\n"
        "ready = kernel.OpenEventW(0x0002, False, names['ready'])\n"
        "gate = kernel.OpenEventW(0x00100000, False, names['release_child'])\n"
        "assert kernel.SetEvent(ready)\n"
        "assert kernel.WaitForSingleObject(gate, 30000) == 0\n"
        "(root / 'child-completed.txt').write_text('completed')\n",
        encoding="utf-8",
    )
    return [sys.executable, str(fixture), str(generations)]


def _process_handle(kernel: Any, path: Path) -> int:
    handle = kernel.OpenProcess(0x00100000, False, int(path.read_text()))  # SYNCHRONIZE
    assert handle, "the gated fixture process must be alive"
    return handle


@pytest.mark.parametrize("generations", [1, 2])
@pytest.mark.parametrize("drop_notifications", [False, True])
def test_completion_waits_for_descendants_after_launcher_exit(
    tmp_path: Path, gates, generations: int, drop_notifications: bool, monkeypatch
):
    kernel, names, handles = gates
    if drop_notifications:
        job_kernel = _windows_process._kernel()
        original_wait = job_kernel.GetQueuedCompletionStatus

        def discard_notifications(*arguments):
            original_wait(*arguments)
            ctypes.set_last_error(258)
            return False

        job_kernel.GetQueuedCompletionStatus = discard_notifications
        monkeypatch.setattr(_windows_process, "_kernel", lambda: job_kernel)
    command = _launcher(tmp_path, names, generations=generations)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_windows_process_tree, command, timeout=15)
        try:
            assert kernel.WaitForSingleObject(handles["ready"], 5000) == 0
            parent = _process_handle(kernel, tmp_path / f"{generations}.pid")
            try:
                kernel.SetEvent(handles["release_parent"])
                assert kernel.WaitForSingleObject(parent, 5000) == 0
                assert not future.done(), "launcher exit must not finish the owned tree"
                kernel.SetEvent(handles["release_child"])
                result = future.result(timeout=5)
            finally:
                kernel.CloseHandle(parent)
            assert result.returncode == 7
            assert result.stdout.strip() == "launcher stdout"
            assert result.stderr.strip() == "launcher stderr"
            assert (tmp_path / "child-completed.txt").read_text() == "completed"
        finally:
            kernel.SetEvent(handles["release_child"])
            kernel.SetEvent(handles["release_parent"])


def test_timeout_kills_owned_descendants_after_launcher_exit(tmp_path: Path, gates):
    kernel, names, handles = gates
    command = _launcher(tmp_path, names)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_windows_process_tree, command, timeout=6)
        try:
            assert kernel.WaitForSingleObject(handles["ready"], 5000) == 0
            parent = _process_handle(kernel, tmp_path / "1.pid")
            child = _process_handle(kernel, tmp_path / "0.pid")
            try:
                kernel.SetEvent(handles["release_parent"])
                assert kernel.WaitForSingleObject(parent, 5000) == 0
                with pytest.raises(subprocess.TimeoutExpired) as caught:
                    future.result(timeout=10)
                assert caught.value.timeout == 6
                assert isinstance(caught.value.stdout, str)
                assert isinstance(caught.value.stderr, str)
                assert "launcher stdout" in cast(str, caught.value.stdout)
                assert "launcher stderr" in cast(str, caught.value.stderr)
                assert kernel.WaitForSingleObject(child, 0) == 0, "timeout must reap the child"
                assert not (tmp_path / "child-completed.txt").exists()
            finally:
                kernel.CloseHandle(parent)
                kernel.CloseHandle(child)
        finally:
            kernel.SetEvent(handles["release_child"])
            kernel.SetEvent(handles["release_parent"])


def test_assignment_failure_never_executes_suspended_process(tmp_path: Path, monkeypatch):
    kernel = _windows_process._kernel()
    original = kernel.AssignProcessToJobObject
    marker = tmp_path / "must-not-execute.txt"

    def denied(job, process):
        raise PermissionError("injected job assignment failure")

    kernel.AssignProcessToJobObject = denied
    monkeypatch.setattr(_windows_process, "_kernel", lambda: kernel)
    try:
        with pytest.raises(PermissionError, match="injected job assignment failure"):
            run_windows_process_tree(
                [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                timeout=5,
            )
        assert not marker.exists(), "execution must follow successful job assignment"
    finally:
        kernel.AssignProcessToJobObject = original


def test_output_capture_handles_large_streams_and_invalid_utf8():
    result = run_windows_process_tree(
        [sys.executable, "-c", "import os; os.write(1,b'x'*500000+b'\\xff'); os.write(2,b'\\xff')"],
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == "x" * 500000 + "\ufffd"
    assert result.stderr == "\ufffd"


def test_recycled_pid_outside_the_job_is_not_retained():
    closed = []

    class RecycledPidKernel:
        def OpenProcess(self, access, inherit, identifier):
            return 123

        def IsProcessInJob(self, process, job, membership):
            ctypes.cast(membership, ctypes.POINTER(wintypes.BOOL)).contents.value = False
            return True

        def CloseHandle(self, handle):
            closed.append(handle)

    watched: dict[int, int] = {}
    _windows_process._remember_process(RecycledPidKernel(), 456, 789, watched)
    assert not watched
    assert closed == [123]


def test_command_quoting_cwd_and_environment(tmp_path: Path):
    directory = tmp_path / "working directory with spaces"
    directory.mkdir()
    arguments = ["space value", 'quoted"value', "trailing\\", "á"]
    env = {**os.environ, "SOLIN_PROCESS_FIXTURE": "isolated"}
    result = run_windows_process_tree(
        [
            sys.executable,
            "-c",
            "import json,os,sys; print(json.dumps([sys.argv[1:],os.getcwd(),"
            "os.environ['SOLIN_PROCESS_FIXTURE']]))",
            *arguments,
        ],
        timeout=10,
        cwd=directory,
        env=env,
    )
    assert json.loads(result.stdout) == [arguments, str(directory), "isolated"]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected_before_creation(timeout: float):
    with pytest.raises(ValueError, match="positive finite timeout"):
        run_windows_process_tree([sys.executable], timeout=timeout)
