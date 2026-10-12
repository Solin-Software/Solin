from __future__ import annotations

import io
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e import _packaged_app
from tests.unit import test_packaged_app_process_lifecycle as lifecycle


_GATED_STARTUP = """
import socket, sys
from pathlib import Path

control = socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=10)
print('starting', flush=True)
assert control.recv(1) == b'G'
Path(sys.argv[2]).write_text(sys.argv[3])
print('ready ' + sys.argv[3], flush=True)
control.recv(1)
"""


@pytest.fixture
def launched_processes(monkeypatch):
    processes = []
    original_popen = subprocess.Popen

    def record_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(_packaged_app.subprocess, "Popen", record_process)
    return processes


def test_observation_waits_for_each_launch_after_delayed_initialization(
    tmp_path,
    monkeypatch,
    launched_processes,
):
    monkeypatch.setenv("SOLIN_E2E_STARTUP_SECONDS", "1.5")
    marker = tmp_path / "ready"
    marker.write_text("previous-launch")
    clock = [0.0]
    observations = []

    def monotonic():
        observations.append(clock[0])
        return clock[0]

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(
        _packaged_app,
        "time",
        SimpleNamespace(monotonic=monotonic, sleep=advance),
    )

    def run_launch(launch_id):
        observations.clear()
        with socket.socket() as control, ExitStack() as connections:
            control.bind(("127.0.0.1", 0))
            control.listen()
            control.settimeout(10)
            streams = []

            def wait_for_ready(stream):
                streams.append(stream)
                connection = connections.enter_context(control.accept()[0])
                connection.settimeout(10)
                assert stream.readline().strip() == b"starting"
                assert marker.read_text() != launch_id
                assert observations == []
                # Hold initialization beyond the entire observation window
                # without depending on actual scheduler speed or sleeping.
                advance(2.0)
                connection.sendall(b"G")
                assert stream.readline().strip() == f"ready {launch_id}".encode()
                assert marker.read_text() == launch_id
                assert observations == []

            _packaged_app.assert_process_survives_startup(
                Path(sys.executable),
                args=(
                    "-B",
                    "-c",
                    _GATED_STARTUP,
                    str(control.getsockname()[1]),
                    str(marker),
                    launch_id,
                ),
                wait_for_ready=wait_for_ready,
            )
            assert streams[0].closed
            assert observations[-1] - observations[0] >= 1.5
            assert launched_processes[-1].poll() is not None

    for launch_id in ("first-launch", "second-launch"):
        run_launch(launch_id)


def test_failed_readiness_cleans_up_before_observation(tmp_path, monkeypatch, launched_processes):
    marker = tmp_path / "ready"
    marker.write_text("previous-launch")
    streams = []

    def unexpected_observation():
        pytest.fail("The observation window started before readiness.")

    monkeypatch.setattr(_packaged_app, "time", SimpleNamespace(monotonic=unexpected_observation))
    with socket.socket() as control, ExitStack() as connections:
        control.bind(("127.0.0.1", 0))
        control.listen()
        control.settimeout(10)

        def fail_readiness(stream):
            streams.append(stream)
            connections.enter_context(control.accept()[0])
            assert stream.readline().strip() == b"starting"
            raise RuntimeError("Current launch did not acknowledge readiness")

        with pytest.raises(RuntimeError, match="Current launch did not acknowledge readiness"):
            _packaged_app.assert_process_survives_startup(
                Path(sys.executable),
                args=(
                    "-B",
                    "-c",
                    _GATED_STARTUP,
                    str(control.getsockname()[1]),
                    str(marker),
                    "current-launch",
                ),
                wait_for_ready=fail_readiness,
            )

    assert marker.read_text() == "previous-launch"
    assert streams[0].closed
    assert launched_processes[0].poll() is not None


@pytest.mark.parametrize("stale", ["acknowledgement", "marker"])
def test_fixture_rejects_readiness_from_previous_launch(tmp_path, monkeypatch, stale):
    listener = tmp_path / "ipc.sock"
    marker = Path(str(listener) + ".ready")

    class ReadySelector:
        """Inject a readable notification; validate the real fixture callback."""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def register(self, stream, event):
            return None

        def select(self, timeout):
            return [object()]

    def provide_readiness(launcher, *, args, wait_for_ready):
        launch_id = args[1]
        marker.write_text(launch_id if stale == "acknowledgement" else "previous-launch")
        acknowledgement = "previous-launch" if stale == "acknowledgement" else launch_id
        with io.BytesIO(f"ready {acknowledgement}\n".encode()) as stream:
            wait_for_ready(stream)

    monkeypatch.setattr(lifecycle.selectors, "DefaultSelector", ReadySelector)
    monkeypatch.setattr(lifecycle, "assert_process_survives_startup", provide_readiness)
    message = (
        "The fixture acknowledged a different launch."
        if stale == "acknowledgement"
        else "The readiness marker belongs to a different launch."
    )
    with pytest.raises(AssertionError, match=message):
        lifecycle._assert_launcher_survives_startup(tmp_path / "launcher", listener)
