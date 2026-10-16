from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from solin.core.scenes import camera_discovery_process as discovery
from solin.core.scenes.engine import (
    LocalCameraDevice, LocalCameraModeQuery, LocalCameraProbe, LocalCameraProbeStatus,
    LocalVideoFormat,
)
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope, encode_envelope, read_envelope
from solin.core.scenes.model import CameraMediaType
from solin.core.scenes.v4l2_camera_discovery import serialize_camera_device


def camera() -> LocalCameraDevice:
    return LocalCameraDevice("camera-1", "PC Camera", False,
        (LocalVideoFormat(CameraMediaType.RAW, "YUY2", 640, 480, 30, 1),),
        LocalCameraProbe(LocalCameraProbeStatus.READY, "test"))


def request(kind="list_local_cameras", payload=None) -> SceneIpcEnvelope:
    return SceneIpcEnvelope(kind, "request", "session", "process", 1, 0,
        int(time.monotonic() * 1000) + 5000, payload or {})


def _publish_pid_code(marker) -> str:
    """Expose readiness only after the complete PID is closed on disk."""
    staging = marker.with_suffix(".pid.pending")
    return (
        f"(Path({str(staging)!r}).write_text(str(os.getpid())), "
        f"Path({str(staging)!r}).replace(Path({str(marker)!r})))"
    )


@pytest.fixture
def launch(monkeypatch, tmp_path):
    children = []
    handles = []
    actual_processes = []
    original = subprocess.Popen
    kernel = None
    guarded = True
    if os.name == "nt":
        from ctypes import wintypes

        kernel = discovery._windows_kernel()
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes, function.restype = arguments, result

    def popen(*args, **kwargs):
        handles.extend(kwargs[key] for key in ("stdin", "stdout", "stderr"))
        marker = tmp_path / f"actual-{len(children)}.pid"
        release = marker.with_suffix(".release")
        command = args[0]
        code = command[-1]
        # Retain the actual interpreter before it can crash or finish. The
        # Windows venv redirector has a different PID and can exit independently.
        ready = (
            f"{_publish_pid_code(marker)}\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        )
        body = ready + f"exec({code!r})\n"
        wrapper = "import os,time\nfrom pathlib import Path\n"
        if guarded:
            wrapper += (
                "from solin.core.scenes.camera_discovery_process import _parent_guard\n"
                "with _parent_guard():\n"
                + "".join(f"    {line}\n" for line in body.splitlines())
            )
        else:
            wrapper += body
        child = original([*command[:-1], wrapper], *args[1:], **kwargs)
        children.append(child)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if marker.exists():
                    break
                assert child.poll() is None, "fixture child exited before reporting its PID"
                time.sleep(0.01)
            actual_pid = int(marker.read_text())
            if kernel is not None:
                # SYNCHRONIZE | PROCESS_TERMINATE: retain identity across PID
                # recycling, and permit emergency cleanup if the test fails.
                process = kernel.OpenProcess(0x00100001, False, actual_pid)
                assert process
                actual_processes.append((actual_pid, process))
            else:
                assert actual_pid == child.pid
        finally:
            release.touch()
        return child

    monkeypatch.setattr(discovery.subprocess, "Popen", popen)

    def configure(code, *, guard=True):
        nonlocal guarded
        guarded = guard
        monkeypatch.setattr(discovery, "_command", lambda: [sys.executable, "-c", code])
        return children, handles

    try:
        yield configure
    finally:
        failures = []
        for child in children:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as error:
                failures.append(str(error))
        if kernel is not None:
            for actual_pid, process in actual_processes:
                try:
                    if kernel.WaitForSingleObject(process, 5000) != 0:
                        failures.append(f"Fixture interpreter {actual_pid} survived launcher cleanup")
                        kernel.TerminateProcess(process, 1)
                        if kernel.WaitForSingleObject(process, 5000) != 0:
                            failures.append(f"Fixture interpreter {actual_pid} resisted emergency cleanup")
                finally:
                    kernel.CloseHandle(process)
        assert not failures, "\n".join(failures)


def test_hung_child_is_killed_reaped_and_streams_closed(launch):
    children, handles = launch("import time; time.sleep(60)")
    helper = discovery.CameraDiscoveryProcess(timeout_seconds=0.2)
    started = time.monotonic()
    with pytest.raises(discovery.CameraDiscoveryError) as error:
        helper.discover()
    assert error.value.error_code == "discovery_request_failed"
    assert time.monotonic() - started < 3
    assert children[0].returncode is not None
    assert all(handle.closed for handle in handles)


def test_crash_is_reaped_and_diagnostics_are_bounded(launch, caplog):
    children, handles = launch("import os; os.write(2,b'camera driver crash'); os._exit(7)")
    with pytest.raises(discovery.CameraDiscoveryError):
        discovery.CameraDiscoveryProcess().discover()
    assert children[0].returncode == 7
    assert all(handle.closed for handle in handles)
    assert "camera driver crash" in caplog.text


def test_close_cancels_active_child_and_is_idempotent(launch):
    children, handles = launch("import time; time.sleep(60)")
    helper = discovery.CameraDiscoveryProcess()
    failures = []

    def run():
        try:
            helper.discover()
        except discovery.CameraDiscoveryError as error:
            failures.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not children and time.monotonic() < deadline:
            time.sleep(0.01)
        assert children
        started = time.monotonic()
        helper.close()
        helper.close()
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert time.monotonic() - started < 3
        assert children[0].returncode is not None
        assert failures
        assert all(handle.closed for handle in handles)
        with pytest.raises(discovery.CameraDiscoveryError):
            helper.discover()
        assert len(children) == 1
    finally:
        helper.close()
        worker.join(timeout=5)


def test_success_roundtrip_and_modes_are_reaped(launch):
    code = (
        "import sys; from solin.core.scenes import camera_discovery_process as d; "
        "from solin.core.scenes.process_engine import _local_camera_discovery_from_envelope as parse; "
        f"device=parse(d.SceneIpcEnvelope('local_camera_list','r','s','p',1,0,0,"
        f"{dict(supported=True, ready=True, generation=1, devices=[serialize_camera_device(camera())], error_code='')!r})).devices[0]; "
        "d._native_metadata=lambda query: [device]; "
        "raise SystemExit(d.main())"
    )
    children, handles = launch(code, guard=False)
    helper = discovery.CameraDiscoveryProcess()
    try:
        assert helper.discover() == [camera()]
        assert helper.modes(LocalCameraModeQuery("camera-1", 640, 480)) == camera()
        assert all(child.returncode == 0 for child in children)
        assert len(children) == 2
        assert all(handle.closed for handle in handles)
    finally:
        helper.close()


@pytest.mark.parametrize("channel", [1, 2])
def test_output_flood_is_bounded_and_killed(launch, monkeypatch, caplog, channel):
    monkeypatch.setattr(discovery, "_MAX_DIAGNOSTIC_BYTES", 8192)
    monkeypatch.setattr(discovery, "MAXIMUM_CONTROL_FRAME_BYTES", 8192)
    children, _ = launch(
        f"import os,time; os.write({channel},b'x'*100000); time.sleep(60)"
    )
    started = time.monotonic()
    with pytest.raises(discovery.CameraDiscoveryError):
        discovery.CameraDiscoveryProcess(timeout_seconds=2).discover()
    assert time.monotonic() - started < 3
    assert children[0].returncode is not None
    assert "exceeded limit" in caplog.text


@pytest.mark.parametrize("kind,payload", [
    ("list_local_cameras", {"unexpected": True}),
    ("query_local_camera_modes", {"device_id": "camera-1", "width": True, "height": 480}),
    ("hello", {}),
])
def test_invalid_request_does_not_initialize_native(monkeypatch, kind, payload):
    monkeypatch.setattr(discovery, "_native_metadata", lambda _: pytest.fail("native initialized"))
    output = io.BytesIO()
    assert discovery.serve(io.BytesIO(encode_envelope(request(kind, payload))), output) == 0
    response = read_envelope(io.BytesIO(output.getvalue()))
    assert response is not None and response.message_type == "error"


def test_expired_request_does_not_initialize_native(monkeypatch):
    monkeypatch.setattr(discovery, "_native_metadata", lambda _: pytest.fail("native initialized"))
    output = io.BytesIO()
    expired = replace(request(), deadline_monotonic_ms=1)
    discovery.serve(io.BytesIO(encode_envelope(expired)), output)
    response = read_envelope(io.BytesIO(output.getvalue()))
    assert response is not None and response.message_type == "error"


def test_main_preserves_protocol_against_native_stdout_without_gui():
    code = (
        "import os,sys; sys.modules['PySide6']=None; "
        "sys.modules['solin.core.media.obs_runtime']=None; "
        "from solin.core.scenes import camera_discovery_process as d; "
        "d._native_metadata=lambda query: (os.write(1,b'native chatter\\n'),[])[1]; "
        "raise SystemExit(d.main())"
    )
    result = subprocess.run([sys.executable, "-c", code], input=encode_envelope(request()),
        capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    response = read_envelope(io.BytesIO(result.stdout))
    assert response is not None and response.message_type == "local_camera_list"
    assert b"native chatter" in result.stderr


@pytest.mark.parametrize(
    ("compiled", "arguments"),
    [
        (False, ["-m", "solin.core.scenes.camera_discovery_process"]),
        (True, ["--camera-discovery"]),
    ],
)
def test_helper_command_follows_compilation_mode(monkeypatch, compiled, arguments):
    monkeypatch.setattr(discovery, "IS_COMPILED", compiled)
    assert discovery._command() == [sys.executable, *arguments]


def test_close_during_popen_registration_reaps_late_child(launch, monkeypatch):
    children, handles = launch("import time; time.sleep(60)")
    creating = threading.Event()
    release = threading.Event()
    original = discovery.subprocess.Popen

    def delayed_popen(*args, **kwargs):
        child = original(*args, **kwargs)
        creating.set()
        assert release.wait(5)
        return child

    monkeypatch.setattr(discovery.subprocess, "Popen", delayed_popen)
    helper = discovery.CameraDiscoveryProcess()
    errors = []

    def run():
        try:
            helper.discover()
        except discovery.CameraDiscoveryError as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert creating.wait(3)
        started = time.monotonic()
        helper.close()
        assert time.monotonic() - started < 1
        release.set()
        worker.join(timeout=5)
        assert not worker.is_alive() and errors
        assert children[0].returncode is not None
        assert all(handle.closed for handle in handles)
    finally:
        release.set()
        helper.close()
        worker.join(timeout=5)


@pytest.mark.parametrize("failure_stage", [None, "startup", "module", "inventory", "shutdown"])
def test_native_lifecycle_stays_on_caller_thread_and_always_releases(monkeypatch, failure_stage):
    from solin.core.scenes import v4l2_camera_discovery

    events = []
    owner = threading.get_ident()

    def stage(name):
        assert threading.get_ident() == owner
        events.append(name)
        if failure_stage == name:
            raise RuntimeError(name)

    @contextmanager
    def com():
        stage("com")
        try:
            yield
        finally:
            stage("release_com")

    class Context:
        def startup(self):
            stage("startup")

        def shutdown(self):
            stage("shutdown")

    def inventory():
        stage("inventory")
        return [camera()]

    monkeypatch.setattr(discovery.sys, "platform", "win32")
    monkeypatch.setattr(discovery, "_com_apartment", com)
    monkeypatch.setattr(discovery, "_load_camera_module", lambda: stage("module"))
    monkeypatch.setitem(sys.modules, "pylibobs", SimpleNamespace(OBSContext=Context))
    monkeypatch.setattr(v4l2_camera_discovery, "discover_local_cameras", inventory)
    if failure_stage:
        with pytest.raises(RuntimeError, match=failure_stage):
            discovery._native_metadata(None)
    else:
        assert discovery._native_metadata(None) == [camera()]
        assert events == ["com", "startup", "module", "inventory", "shutdown", "release_com"]
    assert events[-2:] == ["shutdown", "release_com"]


@pytest.mark.parametrize("hresult", [0, 1, -2147417850])  # S_OK, S_FALSE, RPC_E_CHANGED_MODE
def test_com_balances_only_successful_sta_initialization(monkeypatch, hresult):
    import ctypes

    events = []

    def initialize(reserved, apartment):
        events.append((reserved, apartment))
        return hresult

    def uninitialize():
        events.append("released")

    monkeypatch.setattr(discovery.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: SimpleNamespace(
        CoInitializeEx=initialize, CoUninitialize=uninitialize), raising=False)
    if hresult in (0, 1):
        with pytest.raises(RuntimeError), discovery._com_apartment():
            raise RuntimeError("query failed")
        assert events == [(None, 2), "released"]
    else:
        with pytest.raises(OSError, match="80010106"), discovery._com_apartment():
            pytest.fail("failed COM initialization must stop metadata")
        assert events == [(None, 2)]


@pytest.mark.parametrize("change", ["request_id='other'", "message_type='hello_ack'",
    "payload={}", "payload={'supported':True,'ready':True,'generation':1,'devices':[],'error_code':'driver_failed'}"])
def test_invalid_reply_is_reaped_without_fallback(launch, change):
    children, handles = launch(
        "import sys; from dataclasses import replace; "
        "from solin.core.scenes.ipc_protocol import read_envelope,write_envelope; "
        "r=read_envelope(sys.stdin.buffer); "
        "r=replace(r,message_type='local_camera_list',payload="
        "{'supported':True,'ready':True,'generation':1,'devices':[],'error_code':''}); "
        f"r=replace(r,{change}); write_envelope(sys.stdout.buffer,r)"
    )
    with pytest.raises(discovery.CameraDiscoveryError):
        discovery.CameraDiscoveryProcess().discover()
    assert children[0].returncode == 0
    assert all(handle.closed for handle in handles)


@pytest.mark.parametrize("before_guard", [False, True])
@pytest.mark.parametrize("death", ["crash", "kill"])
def test_parent_death_does_not_leave_hung_metadata_orphan(tmp_path, before_guard, death):
    marker = tmp_path / "child.pid"
    release = tmp_path / "release-child"
    owner_marker = tmp_path / "owner.pid"
    owner_exit = tmp_path / "exit-owner"
    child_code = (
        "import os,time; from pathlib import Path; "
        "from solin.core.scenes import camera_discovery_process as d; "
        f"marker=Path({str(marker)!r}); release=Path({str(release)!r}); "
    )
    if before_guard:
        child_code += (
            f"{_publish_pid_code(marker)}; "
            "exec('while not release.exists(): time.sleep(0.01)'); "
        )
    child_code += (
        "d._native_metadata=lambda query: "
        f"(None if marker.exists() else {_publish_pid_code(marker)},time.sleep(60),[])[2]; "
        "raise SystemExit(d.main())"
    )
    owner_code = (
        "import os,sys,time,threading; from pathlib import Path; "
        "from solin.core.scenes import camera_discovery_process as d; "
        f"{_publish_pid_code(owner_marker)}; "
        f"exit_marker=Path({str(owner_exit)!r}); "
        "exec('def crash():\\n while not exit_marker.exists(): time.sleep(0.01)\\n os._exit(11)'); "
        "threading.Thread(target=crash,daemon=True).start(); "
        f"d._command=lambda: [sys.executable,'-c',{child_code!r}]; "
        "d.CameraDiscoveryProcess().discover()"
    )
    owner = subprocess.Popen([sys.executable, "-c", owner_code],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = None
    kernel = process_handle = None
    try:
        deadline = time.monotonic() + 8
        while not marker.exists() and time.monotonic() < deadline:
            assert owner.poll() is None
            time.sleep(0.01)
        assert marker.exists()
        child_pid = int(marker.read_text())
        if os.name == "nt":
            from ctypes import wintypes

            kernel = discovery._windows_kernel()
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            process_handle = kernel.OpenProcess(0x00100000, False, child_pid)
            assert process_handle
        if death == "kill":
            # Windows venv Popen.pid can identify the redirector, whereas the
            # ownership job is held by its real interpreter.
            os.kill(int(owner_marker.read_text()), 9)
        else:
            owner_exit.touch()
        owner.wait(timeout=5)
        release.touch()
        if kernel is not None:
            assert kernel.WaitForSingleObject(process_handle, 5000) == 0
        else:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                # Container PID 1 may retain a zombie after its original parent
                # died; a zombie has exited and cannot own cameras or run code.
                stat = __import__("pathlib").Path(f"/proc/{child_pid}/stat")
                if stat.exists() and stat.read_text().split()[2] == "Z":
                    break
                time.sleep(0.01)
            else:
                pytest.fail("metadata child survived its owner")
        child_pid = None
    finally:
        release.touch()
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        if kernel is not None and process_handle:
            kernel.CloseHandle(process_handle)
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass  # The asserted parent-death cleanup already completed.
