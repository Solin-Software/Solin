"""Native filter qualification and libobs transport, without COM registration."""

from __future__ import annotations

from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace
import uuid

import pytest


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows virtual camera"),
]
_ROOT = Path(__file__).resolve().parents[2]
_REPORT = "VIRTUAL_CAMERA_REPORT="


@pytest.mark.parametrize("architecture", ["x64", "x86"])
@pytest.mark.skipif(
    os.environ.get("SOLIN_TEST_PRODUCTION_CAMERA") != "1",
    reason="Production broker qualification requires an explicitly opted-in test session",
)
def test_filter_qualification_preserves_broker_ownership(architecture):
    import ctypes
    from ctypes import wintypes

    from solin.core.scenes.windows_vcam_identity import current_user_broker_pipe_name

    directory = _ROOT / "build" / "native" / f"virtual-camera-{architecture}"
    harness = directory / "Release" / "solin-virtual-camera-filter-tests.exe"
    production = directory / "Release" / "solin-virtual-camera.dll"
    isolated = directory / "test" / "Release" / "solin-virtual-camera.dll"
    if not all(path.is_file() for path in (harness, production, isolated)):
        pytest.skip("Build the Release virtual-camera qualification targets first")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    ]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    # Reserve the endpoint as a producer would. Never replace an existing owner.
    pipe = kernel32.CreateNamedPipeW(
        current_user_broker_pipe_name(), 0x00000003 | 0x00080000 | 0x40000000,
        0x00000008, 1, 4096, 4096, 0, None,
    )
    assert pipe != ctypes.c_void_p(-1).value, (
        f"Cannot reserve production broker: Windows error {ctypes.get_last_error()}; "
        "close Solin and other production-filter tests first"
    )

    def qualify(dll, *options):
        return subprocess.run(
            [str(harness), str(dll), *options],
            cwd=harness.parent, capture_output=True, text=True, timeout=15, check=False,
        )

    try:
        occupied = qualify(production, "--production-broker")
        assert occupied.returncode == 1, occupied.stdout + occupied.stderr
        assert "Could not reserve broker endpoint" in occupied.stderr
        assert "graph validation" not in occupied.stderr
        independent = qualify(isolated)
        assert independent.returncode == 0, independent.stdout + independent.stderr
    finally:
        assert kernel32.CloseHandle(pipe)
    recovered = qualify(production, "--production-broker")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr


@pytest.mark.parametrize("architecture", ["x64", "x86"])
@pytest.mark.skipif(importlib.util.find_spec("pylibobs") is None, reason="libobs required")
def test_native_output_recovers_after_publication_stalls(architecture):
    probe = (_ROOT / "build" / "native" / f"virtual-camera-{architecture}" / "Release"
             / "solin-virtual-camera-broker-probe.exe")
    if not probe.is_file():
        pytest.skip("Build the Release virtual-camera qualification targets first")
    result = subprocess.run(
        [sys.executable, "-B", "-X", "faulthandler", str(Path(__file__).resolve()), str(probe)],
        cwd=_ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-16000:]
    assert "Exception ignored from cffi callback" not in output, output[-16000:]
    reports = [json.loads(line.removeprefix(_REPORT)) for line in result.stdout.splitlines()
               if line.startswith(_REPORT)]
    assert len(reports) == 1, output[-16000:]
    assert reports[0]["registrations"] == 2
    assert reports[0]["generations"][0] != reports[0]["generations"][1]


def _scenario(probe: Path):
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.scenes import libobs_windows_virtual_camera as producer_module
    from solin.core.scenes.libobs_virtual_camera import LibobsVirtualCamera

    pipe = rf"\\.\pipe\Solin.Tests.VirtualCamera.{uuid.uuid4().hex}"
    samples: Queue = Queue()
    delivering = Event()
    delivering.set()
    registrations = []
    with ExitStack() as stack:
        patch = stack.enter_context(pytest.MonkeyPatch.context())
        patch.setattr(producer_module, "current_user_broker_pipe_name", lambda: pipe)
        # This fixture must not sweep rings belonging to another application's session.
        patch.setattr(producer_module, "_sweep_orphaned_rings", lambda: 0)
        runtime = ObsRuntime()
        stack.callback(runtime.shutdown)
        runtime.ensure_started(width=640, height=360, fps=30)
        source = runtime.ob.Source.create("color_source_v3", "vcam-fixture", {
            "color": 0xFFFFFFFF, "width": 640, "height": 360,
        })
        stack.callback(source.release)
        channel = runtime.acquire_channel()
        stack.callback(runtime.release_channel, channel)
        runtime.set_channel_source(channel, source)

        def register(callback, **options):
            generation = len(registrations)

            def forward(*frame):
                if generation or delivering.is_set():
                    callback(*frame)

            token = runtime.ob.add_raw_video_callback(forward, **options)
            registrations.append(token)
            return token

        bindings = SimpleNamespace(
            VideoFormat=runtime.ob.VideoFormat, add_raw_video_callback=register,
            remove_raw_video_callback=runtime.ob.remove_raw_video_callback,
        )
        camera = LibobsVirtualCamera(SimpleNamespace(ob=bindings, video=runtime.video))
        stack.callback(camera.stop)
        assert camera.start()
        process = subprocess.Popen(
            [str(probe), pipe, "20000"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )

        def stop_probe():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

        def collect():
            for line in process.stdout:
                samples.put(json.loads(line))

        reader = Thread(target=collect, name="vcam-probe-reader", daemon=True)
        reader.start()
        stack.callback(reader.join, timeout=3)
        stack.callback(stop_probe)
        latest = None

        def wait(predicate, *, reconcile=True):
            nonlocal latest
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if reconcile:
                    camera.poll()
                try:
                    latest = samples.get(timeout=0.02)
                except Empty:
                    pass
                if latest is not None and predicate(latest):
                    return latest
                assert process.poll() is None, process.stderr.read()
            raise AssertionError(f"Output did not converge: {camera.health}, native={latest}")

        first = wait(lambda sample: sample["live"] and camera.health.status == "ready")
        assert first["bytes"][0] > 200  # real bright video, not an empty/black ring
        next_frame = wait(lambda sample: sample["sequence"] > first["sequence"] + 2)
        assert next_frame["bytes"] == first["bytes"]  # a static scene remains live
        delivering.clear()  # inject an actual publication stall at the OBS callback boundary
        wait(lambda sample: not sample["live"], reconcile=False)
        wait(lambda _: camera.health.status == "recovering")
        recovered = wait(lambda sample: (
            sample["live"] and sample["generation"] != first["generation"]
            and camera.health.status == "ready"
        ))
        assert recovered["bytes"] == first["bytes"]
        camera.stop()
        wait(lambda sample: not sample["live"])
        assert camera.health.status == "stopped"
        print(_REPORT + json.dumps({
            "registrations": len(registrations),
            "generations": [first["generation"], recovered["generation"]],
        }), flush=True)


if __name__ == "__main__":
    _scenario(Path(sys.argv[1]))
