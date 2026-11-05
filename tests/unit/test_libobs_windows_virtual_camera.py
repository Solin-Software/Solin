"""Tests for the Windows vcam orchestrator that are safe on any OS.

The producer's file mapping + broker are Windows-only; this covers the ring
filename contract, the non-Windows construction guard, and that ``LibobsVirtualCamera``
routes to the Windows path on win32 without disturbing the Linux v4l2 path.
"""
from __future__ import annotations

import sys
import struct
import threading
from types import SimpleNamespace

import pytest

from solin.core.scenes.libobs_windows_virtual_camera import temporary_frame_file_name


@pytest.fixture
def producer(monkeypatch):
    from solin.core.scenes import libobs_windows_virtual_camera as camera

    clock = SimpleNamespace(now=10.0)
    bindings = SimpleNamespace(callback=None, removals=[], fail_registration=False)
    rings = []
    brokers = []

    class Ring:
        def __init__(self, size, sid):
            self.buffer = bytearray(size)
            self.mapping_size = size
            self.path = "C:/Temp/Solin.VirtualCamera.{test}.frames"
            self.closed = False
            rings.append(self)

        def close(self):
            self.closed = True

    class Broker:
        def __init__(self, *args):
            self.healthy = False
            brokers.append(self)

        def start(self):
            self.healthy = True

        def stop(self):
            self.healthy = False

    def register(callback, **kwargs):
        if bindings.fail_registration:
            raise RuntimeError("registration failed")
        bindings.callback = callback
        return callback

    monkeypatch.setattr(camera, "require_windows", lambda: None)
    monkeypatch.setattr(camera, "current_user_sid", lambda: "test-sid")
    monkeypatch.setattr(camera, "current_user_broker_pipe_name", lambda: "test-pipe")
    monkeypatch.setattr(camera, "_WindowsSharedRingFile", Ring)
    monkeypatch.setattr(camera, "WindowsVcamBroker", Broker)
    monkeypatch.setattr(camera.time, "monotonic", lambda: clock.now)
    runtime = SimpleNamespace(
        video=SimpleNamespace(width=4, height=2, fps=30),
        ob=SimpleNamespace(
            VideoFormat=SimpleNamespace(NV12=2), add_raw_video_callback=register,
            remove_raw_video_callback=bindings.removals.append,
        ),
    )
    active = camera.LibobsWindowsVirtualCamera(runtime)
    try:
        yield SimpleNamespace(camera=active, clock=clock, bindings=bindings,
                              rings=rings, brokers=brokers, runtime=runtime)
    finally:
        active.stop()


def _publish(producer, **overrides):
    frame = dict(planes=[bytes([90]) * 8, bytes([128]) * 4], linesizes=[4, 4],
                 width=4, height=2, _fmt=2, timestamp=1)
    frame.update(overrides)
    producer.bindings.callback(**frame)


def test_producer_requires_first_frame_and_detects_stalled_static_video(producer):
    assert producer.camera.start()
    assert producer.camera.health.status == "starting"
    _publish(producer)
    assert producer.camera.health.status == "ready"
    producer.clock.now += 1
    _publish(producer)  # identical pixels still count as a new frame
    producer.clock.now += 1
    assert producer.camera.health.status == "ready"
    producer.clock.now += 10
    assert producer.camera.health.status == "failed"
    assert producer.camera.health.error_code == "output_no_frames"
    assert struct.unpack_from("<QQ", producer.rings[0].buffer, 80) == (2, 3)


def test_producer_rejects_invalid_frames_without_publishing(producer):
    assert producer.camera.start()
    for invalid in (
        {"width": 2}, {"_fmt": 1}, {"planes": [b"a", b"b"]},
        {"linesizes": [2, 2]}, {"timestamp": -1},
    ):
        _publish(producer, **invalid)
    assert struct.unpack_from("<Q", producer.rings[0].buffer, 80)[0] == 0
    producer.clock.now += 10
    assert producer.camera.health.status == "failed"
    _publish(producer)
    assert producer.camera.health.status == "ready"


def test_producer_observes_broker_failure_even_when_frames_arrive(producer):
    assert producer.camera.start()
    _publish(producer)
    producer.brokers[0].healthy = False
    assert producer.camera.health.status == "failed"
    assert producer.camera.health.error_code == "output_broker_unavailable"


def test_failed_callback_registration_releases_all_resources(producer):
    producer.bindings.fail_registration = True
    assert producer.camera.start() is False
    assert not producer.camera.active
    assert producer.rings[0].closed
    assert not producer.brokers[0].healthy


def test_first_callback_can_run_before_registration_returns(producer, monkeypatch):
    def register(callback, **kwargs):
        producer.bindings.callback = callback
        _publish(producer)
        return callback

    monkeypatch.setattr(producer.runtime.ob, "add_raw_video_callback", register)
    assert producer.camera.start()
    assert producer.camera.health.status == "ready"


def test_stop_drains_inflight_publication_before_closing_ring(producer, monkeypatch):
    assert producer.camera.start()
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    writer = producer.camera._writer
    write = writer.write

    def blocked_write(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        assert not producer.rings[0].closed
        write(*args, **kwargs)

    monkeypatch.setattr(writer, "write", blocked_write)
    callback = threading.Thread(target=_publish, args=(producer,))

    def stop():
        producer.camera.stop()
        stopped.set()

    stopping = threading.Thread(target=stop)
    callback.start()
    try:
        assert entered.wait(3)
        stopping.start()
        assert not stopped.wait(0.05)
    finally:
        release.set()
        callback.join(timeout=3)
        if stopping.ident is not None:
            stopping.join(timeout=3)
    assert not callback.is_alive() and not stopping.is_alive()
    assert stopped.is_set() and producer.rings[0].closed


def test_stop_discards_late_callback_and_restart_has_a_new_generation(producer):
    assert producer.camera.start()
    _publish(producer)
    first_generation = struct.unpack_from("<Q", producer.rings[0].buffer, 48)[0]
    callback = producer.bindings.callback
    producer.camera.stop()
    before = bytes(producer.rings[0].buffer)
    callback([b"a" * 8, b"b" * 4], [4, 4], 4, 2, 2, 1)
    assert bytes(producer.rings[0].buffer) == before
    assert producer.rings[0].closed
    assert producer.camera.start()
    assert struct.unpack_from("<Q", producer.rings[1].buffer, 48)[0] != first_generation
    callback([b"a" * 8, b"b" * 4], [4, 4], 4, 2, 2, 1)
    assert struct.unpack_from("<Q", producer.rings[1].buffer, 80)[0] == 0


def test_frame_file_name_matches_the_native_pattern():
    name = temporary_frame_file_name("{ABCD}")
    assert name == "Solin.VirtualCamera.{ABCD}.frames"


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows file mapping and ACLs")
def test_native_ring_mapping_is_writable_and_removed_on_close(tmp_path, monkeypatch):
    from pathlib import Path

    from solin.core.scenes import libobs_windows_virtual_camera as camera
    from solin.core.scenes.windows_vcam_identity import current_user_sid

    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    # Restrict this lifecycle check to its own ring, without sweeping other producers.
    monkeypatch.setattr(camera, "_sweep_orphaned_rings", lambda: 0)
    ring = camera._WindowsSharedRingFile(4096, current_user_sid())
    path = Path(ring.path)
    try:
        assert path.parent == tmp_path
        assert path.stat().st_size == 4096
        ring.buffer[:4] = b"test"
        assert path.read_bytes()[:4] == b"test"
    finally:
        ring.close()
    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="guard is for non-Windows hosts")
def test_orchestrator_construction_refused_off_windows():
    from solin.core.scenes.libobs_windows_virtual_camera import LibobsWindowsVirtualCamera

    with pytest.raises(RuntimeError):
        LibobsWindowsVirtualCamera(object())


def test_virtual_camera_uses_the_v4l2_path_on_linux(monkeypatch):
    from solin.core.scenes.libobs_virtual_camera import LibobsVirtualCamera

    monkeypatch.setattr("solin.core.scenes.libobs_virtual_camera.sys.platform", "linux")

    class _Ctx:
        def get_video(self):
            return "video"

        def get_audio(self):
            return "audio"

    class _Output:
        def __init__(self):
            self.started = False
            self.active = False

        def set_media(self, video, audio):
            self.video, self.audio = video, audio

        def start(self):
            self.started = True
            self.active = True
            return True

        def stop(self, *, wait=True):
            self.started = False
            self.active = False

        def release(self):
            pass

    created = {}

    class _Ob:
        def enum_output_types(self):
            return ["virtualcam_output"]

        class Output:
            @staticmethod
            def create(kind, name, settings):
                created["kind"] = kind
                return _Output()

    class _Runtime:
        ob = _Ob()
        context = _Ctx()

    camera = LibobsVirtualCamera(_Runtime())
    assert camera.start() is True
    assert camera.active is True
    assert created["kind"] == "virtualcam_output"
    camera.stop()
    assert camera.active is False
