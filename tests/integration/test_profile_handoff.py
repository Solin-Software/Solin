from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from solin.bootstrap.single_instance import SingleInstanceServer
from solin.controllers.ipc_controller import IpcController, MAX_RECENT_REQUESTS


_PARENT = r"""
import json, sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QTimer
from solin.bootstrap.profile_handoff import ProfileHandoff
from solin.bootstrap.single_instance import SingleInstanceServer

folder, mode, public_name = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
app = QCoreApplication([])
ipc = SingleInstanceServer(app, [None], [], server_name=public_name)
assert ipc.start()
gate = ProfileHandoff(app)
name = gate.start()
print(name, flush=True)

def ready():
    (folder / "waiting").touch()
    if mode == "abort":
        gate.close()
        app.quit()
gate.ready.connect(ready)

def prepared():
    (folder / "prepared").touch()
    if mode in ("prepared_abort", "partial_commit"):
        if mode == "partial_commit":
            gate._socket.write(b"rea")
            gate._socket.waitForBytesWritten(2000)
        gate.close()
        app.quit()
        return
    ipc.reserve_until_exit()
    ipc.close()
    assert gate.release()
    (folder / "released").touch()
gate.prepared.connect(prepared)

released = False
def poll():
    global released
    if (folder / "release").exists() and not released:
        released = True
        state = json.loads((folder / "handoff.json").read_text(encoding="utf-8"))
        gate.prepare(state["files"], state["accepted"])
    if (folder / "exit").exists():
        app.quit()
timer = QTimer(app)
timer.timeout.connect(poll)
timer.start(10)
QTimer.singleShot(10000, app.quit)
app.exec()
gate.close()
ipc.close()
"""

_CHILD = r"""
import json, sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from solin.bootstrap import profile_handoff
from solin.bootstrap.single_instance import SingleInstanceServer

app = QCoreApplication([])
if sys.argv[4] == "timeout":
    profile_handoff._SHUTDOWN_TIMEOUT_MS = 100
state = profile_handoff.wait_for_profile_handoff(sys.argv[1])
files = state.pending_files if state is not None else None
accepted = state.accepted_request_ids if state is not None else []
started = False
if files is not None:
    ipc = SingleInstanceServer(app, [None], [], server_name=sys.argv[3])
    ipc.restore_request_ids(accepted)
    started = ipc.start()
    if started:
        Path(sys.argv[2]).touch()
    ipc.close()
print(json.dumps({"files": files, "accepted": accepted, "started": started}), flush=True)
"""


def _wait_for(path: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Process exited before {path.name}: {process.stderr.read()}")
        time.sleep(0.01)
    assert path.exists(), f"Timed out waiting for {path.name}"


def _stop(process: subprocess.Popen | None) -> None:
    if process is not None:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("mode", ["success", "large", "abort", "timeout", "prepared_abort", "partial_commit"])
def test_real_process_handoff_requires_release_and_departing_process_exit(tmp_path, mode):
    from solin.bootstrap.profile_handoff import _MAX_MESSAGE_BYTES

    public_name = f"solin-test-{uuid4().hex}"
    files = ["https://example.test/late.mp4"]
    accepted = ["a" * 32]
    if mode == "large":
        files = [f"https://example.test/{index}/" + '\u4f8b"\\' * 60_000 for index in range(3)]
        accepted = [f"{index:032x}" for index in range(MAX_RECENT_REQUESTS)]
        assert len(json.dumps({"files": files, "accepted": accepted})) > _MAX_MESSAGE_BYTES
    (tmp_path / "handoff.json").write_text(
        json.dumps({"files": files, "accepted": accepted}), encoding="utf-8",
    )
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    parent = subprocess.Popen(
        [sys.executable, "-u", "-c", _PARENT, str(tmp_path), mode, public_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    child = None
    try:
        name = parent.stdout.readline().strip()
        assert name.startswith("solin-profile-handoff-"), parent.stderr.read()
        marker = tmp_path / "child-started"
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", _CHILD, name, str(marker), public_name, mode],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        _wait_for(tmp_path / "waiting", parent) if mode != "abort" else None
        if mode in ("success", "large", "prepared_abort", "partial_commit"):
            assert not marker.exists()
            (tmp_path / "release").touch()
        if mode in ("success", "large"):
            _wait_for(tmp_path / "released", parent)
            assert not marker.exists(), "A release token alone must not expose profile mutations"
            assert child.poll() is None
            # Even after closing IPC, startup admission belongs to the live parent.
            duplicate = SingleInstanceServer(
                QApplication.instance(),
                [None],
                [],
                server_name=public_name,
            )
            try:
                assert not duplicate.start()
            finally:
                duplicate.close()
            (tmp_path / "exit").touch()
            parent.communicate(timeout=5)
        output, errors = child.communicate(timeout=5)
        assert child.returncode == 0, errors
        result = json.loads(output)
        if mode in ("success", "large"):
            assert result == {
                "files": files,
                "accepted": accepted,
                "started": True,
            }
            assert marker.exists()
        else:
            assert result == {"files": None, "accepted": [], "started": False}
            assert not marker.exists()
    finally:
        _stop(child)
        _stop(parent)


@pytest.mark.parametrize("failure", ["disconnect", "invalid_ack", "timeout", "oversized_item"])
def test_handoff_rejects_failed_preparation_without_reporting_success(monkeypatch, failure):
    from solin.bootstrap import profile_handoff

    gate = profile_handoff.ProfileHandoff(QApplication.instance())
    socket = QLocalSocket()
    ready = []
    prepared = []
    failures = []
    gate.ready.connect(lambda: ready.append(True))
    gate.prepared.connect(lambda: prepared.append(True))
    gate.failed.connect(failures.append)

    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.005)
        assert predicate()

    try:
        socket.connectToServer(gate.start())
        assert socket.waitForConnected(1000)
        socket.write(b"waiting\n")
        socket.flush()
        wait_for(lambda: ready)
        monkeypatch.setattr(profile_handoff, "_CONNECT_TIMEOUT_MS", 50)
        files = ["x" * profile_handoff._MAX_MESSAGE_BYTES] if failure == "oversized_item" else ["accepted.mp4"]
        gate.prepare(files, [])
        if failure == "disconnect":
            socket.abort()
        elif failure == "invalid_ack":
            socket.write(b"invalid\n")
            socket.flush()
        wait_for(lambda: failures)
        assert prepared == []
        assert not gate.release()
    finally:
        socket.abort()
        gate.close()
        gate.deleteLater()


def test_live_ipc_cannot_be_taken_over_and_repeated_cleanup_preserves_replacement():
    app = QApplication.instance()
    name = f"solin-test-{uuid4().hex}"
    first = SingleInstanceServer(app, [None], [], server_name=name)
    second = SingleInstanceServer(app, [None], [], server_name=name)
    socket = QLocalSocket()
    try:
        assert first.start()
        assert not second.start()
        first.close()
        assert second.start()
        first.close()
        first.close()
        socket.connectToServer(name)
        assert socket.waitForConnected(1000)
        assert app._solin_single_instance_server is second
        assert app._solin_app_ipc_server_active
    finally:
        socket.abort()
        first.close()
        second.close()


def test_runtime_ipc_cleanup_cannot_remove_an_endpoint_it_never_owned(monkeypatch):
    removed = []
    monkeypatch.setattr(
        "solin.controllers.ipc_controller.QLocalServer.removeServer",
        lambda name: removed.append(name),
    )
    controller = IpcController(
        QApplication.instance(),
        bring_to_front=lambda: None,
        open_media_files=lambda _files: None,
    )
    controller.close()
    controller.close()
    assert removed == []


_PAUSING_SERVER = r"""
import json, sys, time
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QTimer
from solin.bootstrap.single_instance import SingleInstanceServer
folder, name = Path(sys.argv[1]), sys.argv[2]
app = QCoreApplication([])
pending = []
server = SingleInstanceServer(app, [None], pending, server_name=name)
assert server.start()
print("ready", flush=True)
paused = False
def poll():
    global paused
    if (folder / "pause").exists() and not paused:
        paused = True
        server.pause()
        (folder / "paused").touch()
        deadline = time.monotonic() + 5
        while not (folder / "resume").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.start()
    if pending:
        print(json.dumps(pending), flush=True)
        app.quit()
timer = QTimer(app)
timer.timeout.connect(poll)
timer.start(10)
QTimer.singleShot(10000, app.quit)
app.exec()
server.close()
"""


_FORWARDING_CLIENT = r"""
import json, sys
from pathlib import Path
from solin.bootstrap import single_instance
single_instance._FORWARD_TIMEOUT_SECONDS = float(sys.argv[3])
Path(sys.argv[2]).touch()
print(json.dumps(single_instance.try_forward_to_running(
    ["https://example.test/during-shutdown.mp4"], server_name=sys.argv[1],
    wait_for_owner=True,
)), flush=True)
"""


def test_file_open_waits_through_synchronous_shutdown_without_false_success(tmp_path):
    name = f"solin-test-{uuid4().hex}"
    server = subprocess.Popen(
        [sys.executable, "-u", "-c", _PAUSING_SERVER, str(tmp_path), name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    client = None
    try:
        assert server.stdout.readline().strip() == "ready"
        (tmp_path / "pause").touch()
        _wait_for(tmp_path / "paused", server)
        client = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                _FORWARDING_CLIENT,
                name,
                str(tmp_path / "client-started"),
                "5",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_for(tmp_path / "client-started", client)
        assert client.poll() is None
        (tmp_path / "resume").touch()
        output, errors = client.communicate(timeout=7)
        assert client.returncode == 0, errors
        assert json.loads(output) is True
        delivered, errors = server.communicate(timeout=5)
        assert server.returncode == 0, errors
        assert json.loads(delivered) == ["https://example.test/during-shutdown.mp4"]
    finally:
        _stop(client)
        _stop(server)


def test_transport_connection_without_application_ack_cannot_report_success(tmp_path):
    from PySide6.QtNetwork import QLocalServer

    name = f"solin-test-{uuid4().hex}"
    server = QLocalServer()
    assert server.listen(name)
    client = None
    try:

        def reject():
            connection = server.nextPendingConnection()
            if connection is not None:
                connection.abort()
                connection.deleteLater()

        server.newConnection.connect(reject)
        client = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                _FORWARDING_CLIENT,
                name,
                str(tmp_path / "client-started"),
                "0.2",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while client.poll() is None and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        output, errors = client.communicate(timeout=1)
        assert client.returncode == 0, errors
        assert json.loads(output) is False
    finally:
        _stop(client)
        server.close()


def test_retry_during_modal_file_delivery_is_accepted_once_without_dead_socket_access(
    tmp_path,
    monkeypatch,
):
    name = f"solin-test-{uuid4().hex}"
    deliveries = []
    callback_errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: callback_errors.append(error))
    server = SingleInstanceServer(QApplication.instance(), [None], [], server_name=name)

    def deliver(paths):
        deliveries.append(paths)
        if len(deliveries) == 1:
            # Outlast the client's ACK wait while processing nested Qt events,
            # as a modal dialog does. The original socket is deleted on retry.
            loop = QEventLoop()
            QTimer.singleShot(2500, loop.quit)
            loop.exec()

    server._receiver._open_media_files = deliver
    server._receiver._bring_to_front = lambda: None
    client = None
    try:
        assert server.start()
        client = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                _FORWARDING_CLIENT,
                name,
                str(tmp_path / "client-started"),
                "5",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 7
        while client.poll() is None and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        output, errors = client.communicate(timeout=1)
        assert client.returncode == 0, errors
        assert json.loads(output) is True
        assert deliveries == [["https://example.test/during-shutdown.mp4"]]
        assert callback_errors == []
    finally:
        _stop(client)
        server.close()
