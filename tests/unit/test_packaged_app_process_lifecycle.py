from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.e2e._packaged_app import assert_process_survives_startup


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX launcher process groups")


@pytest.fixture
def process_root():
    # AF_UNIX paths must fit sun_path even on runners with long work directories.
    with tempfile.TemporaryDirectory(prefix="solin-proc-", dir="/tmp") as directory:
        yield Path(directory)


def _launcher(tmp_path: Path, *, early_exit: bool = False, ignore_term: bool = False) -> Path:
    child = tmp_path / "child.py"
    child.write_text(
        "import os, signal, socket, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "open(sys.argv[1] + '.pid', 'w').write(str(os.getpid()))\n"
        "server = socket.socket(socket.AF_UNIX)\n"
        "server.bind(sys.argv[1])\n"
        "server.listen()\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "launcher"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, {str(child)!r}, sys.argv[1]], stdout=subprocess.PIPE)\n"
        "assert child.stdout.readline() == b'ready\\n'\n"
        + ("sys.exit(0)\n" if early_exit else "child.wait()\n"),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def _assert_no_listener(path: Path) -> None:
    assert Path(str(path) + ".pid").is_file(), "The fixture must have started its IPC child."
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(0.1)
            try:
                client.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                return
            except (TimeoutError, BlockingIOError):
                pass
        time.sleep(0.01)
    pytest.fail("The packaged launcher's child retained its IPC listener.")


def _rescue_child(path: Path) -> None:
    import signal

    pid_file = Path(str(path) + ".pid")
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("early_exit", [False, True])
def test_startup_cleanup_stops_children_even_if_launcher_exits(process_root, monkeypatch, early_exit):
    monkeypatch.setenv("SOLIN_E2E_STARTUP_SECONDS", "1.5")
    launcher = _launcher(process_root, early_exit=early_exit)
    listener = process_root / "ipc.sock"
    try:
        if early_exit:
            with pytest.raises(pytest.fail.Exception, match="exited during startup with 0"):
                assert_process_survives_startup(launcher, args=(str(listener),))
        else:
            assert_process_survives_startup(launcher, args=(str(listener),))
        _assert_no_listener(listener)
    finally:
        # Rescue descendants when this regression is exercised on the old helper.
        _rescue_child(listener)


def test_startup_cleanup_escalates_for_unresponsive_descendants(process_root, monkeypatch):
    monkeypatch.setenv("SOLIN_E2E_STARTUP_SECONDS", "1.5")
    monkeypatch.setenv("SOLIN_E2E_SHUTDOWN_TIMEOUT", "0.2")
    launcher = _launcher(process_root, ignore_term=True)
    listener = process_root / "ipc.sock"
    try:
        assert_process_survives_startup(launcher, args=(str(listener),))
        _assert_no_listener(listener)
    finally:
        _rescue_child(listener)


def test_sequential_launches_can_reuse_the_same_ipc_endpoint(process_root, monkeypatch):
    monkeypatch.setenv("SOLIN_E2E_STARTUP_SECONDS", "1.5")
    launcher = _launcher(process_root)
    listener = process_root / "ipc.sock"
    try:
        for _ in range(2):
            assert_process_survives_startup(launcher, args=(str(listener),))
            _assert_no_listener(listener)
            listener.unlink()
    finally:
        _rescue_child(listener)


def test_cleanup_keeps_unrelated_ipc_listeners_alive(process_root, monkeypatch):
    monkeypatch.setenv("SOLIN_E2E_STARTUP_SECONDS", "1.5")
    launcher = _launcher(process_root)
    listener = process_root / "ipc.sock"
    unrelated_path = process_root / "unrelated.sock"
    with socket.socket(socket.AF_UNIX) as unrelated:
        unrelated.bind(str(unrelated_path))
        unrelated.listen()
        try:
            assert_process_survives_startup(launcher, args=(str(listener),))
            _assert_no_listener(listener)
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1)
                client.connect(str(unrelated_path))
        finally:
            _rescue_child(listener)
