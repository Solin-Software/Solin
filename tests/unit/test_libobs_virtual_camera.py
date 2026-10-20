"""Output progress and bounded recovery without native graphics or clocks."""

from types import SimpleNamespace

import pytest

from solin.core.scenes import libobs_virtual_camera as camera_module
from solin.core.scenes.output_health import OutputHealth, OutputHealthStatus


@pytest.fixture
def output(monkeypatch):
    from solin.core.scenes import libobs_windows_virtual_camera as windows

    clock = SimpleNamespace(now=10.0)
    producers = []
    settings = SimpleNamespace(start_ok=True)

    class Producer:
        def __init__(self, runtime):
            self.active = False
            self.health = OutputHealth(OutputHealthStatus.STARTING)
            self.stops = 0
            producers.append(self)

        def start(self):
            self.active = settings.start_ok
            return settings.start_ok

        def stop(self):
            self.active = False
            self.stops += 1

    monkeypatch.setattr(camera_module.sys, "platform", "win32")
    monkeypatch.setattr(camera_module.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(windows, "LibobsWindowsVirtualCamera", Producer)
    camera = camera_module.LibobsVirtualCamera(object())
    try:
        yield SimpleNamespace(camera=camera, clock=clock, producers=producers, settings=settings)
    finally:
        camera.stop()


def test_output_is_ready_only_after_successful_frame_observation(output):
    assert output.camera.start()
    assert output.camera.health.status is OutputHealthStatus.STARTING
    output.producers[-1].health = OutputHealth(OutputHealthStatus.READY)
    assert output.camera.poll().status is OutputHealthStatus.READY
    assert output.camera.start()  # full-state sync is idempotent
    assert len(output.producers) == 1


def test_stalled_output_releases_old_generation_and_recovers(output):
    assert output.camera.start()
    previous = output.producers[-1]
    previous.health = OutputHealth(OutputHealthStatus.FAILED, "output_no_frames")
    state = output.camera.poll()
    assert state.status is OutputHealthStatus.RECOVERING
    assert state.error_code == "output_no_frames"
    assert previous.stops == 1
    assert not output.camera.active
    for _ in range(5):
        output.camera.poll()
    assert len(output.producers) == 1  # no tight retry loop
    output.clock.now += 1
    assert output.camera.poll().status is OutputHealthStatus.STARTING
    assert output.camera.health.generation > state.generation
    output.producers[-1].health = OutputHealth(OutputHealthStatus.READY)
    assert output.camera.poll().status is OutputHealthStatus.READY


def test_failed_start_retries_without_hydrate_resetting_the_budget(output):
    output.settings.start_ok = False
    assert output.camera.start() is False
    assert output.camera.health.status is OutputHealthStatus.RECOVERING
    for _ in range(3):
        assert output.camera.start() is False
        output.clock.now += 10
        output.camera.poll()
    assert len(output.producers) == 4  # initial launch plus three recovery attempts
    assert output.camera.health.status is OutputHealthStatus.FAILED
    output.clock.now += 1000
    output.camera.poll()
    assert output.camera.start() is False
    assert len(output.producers) == 4
    output.camera.stop()
    output.settings.start_ok = True
    assert output.camera.start()  # explicit disable/enable starts a new attempt budget


def test_disable_cancels_scheduled_recovery(output):
    output.settings.start_ok = False
    assert not output.camera.start()
    output.camera.stop()
    output.clock.now += 100
    assert output.camera.poll() == OutputHealth()
    assert len(output.producers) == 1


def test_brief_progress_does_not_reset_recovery_budget(output):
    assert output.camera.start()
    for _ in range(3):
        output.producers[-1].health = OutputHealth(OutputHealthStatus.FAILED, "output_no_frames")
        output.camera.poll()
        output.clock.now += 10
        output.camera.poll()
        output.producers[-1].health = OutputHealth(OutputHealthStatus.READY)
        output.camera.poll()
        output.clock.now += 1
    output.producers[-1].health = OutputHealth(OutputHealthStatus.FAILED, "output_no_frames")
    assert output.camera.poll().status is OutputHealthStatus.FAILED
    assert len(output.producers) == 4


def test_sustained_progress_restores_recovery_budget(output):
    assert output.camera.start()
    output.producers[-1].health = OutputHealth(OutputHealthStatus.FAILED, "output_no_frames")
    output.camera.poll()
    output.clock.now += 10
    output.camera.poll()
    output.producers[-1].health = OutputHealth(OutputHealthStatus.READY)
    output.camera.poll()
    output.clock.now += 11
    output.camera.poll()
    output.producers[-1].health = OutputHealth(OutputHealthStatus.FAILED, "output_no_frames")
    output.camera.poll()
    assert output.camera._recovery_attempts == 0


def test_native_output_tracks_frames_and_stops_before_release(monkeypatch):
    monkeypatch.setattr(camera_module.sys, "platform", "linux")
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(camera_module.time, "monotonic", lambda: clock.now)
    calls = []
    native = SimpleNamespace(active=True, total_frames=0, set_media=lambda *_: None,
                             start=lambda: True, stop=lambda **kw: calls.append(("stop", kw)),
                             release=lambda: calls.append("release"))
    runtime = SimpleNamespace(
        ob=SimpleNamespace(enum_output_types=lambda: ["virtualcam_output"],
                           Output=SimpleNamespace(create=lambda *_: native)),
        context=SimpleNamespace(get_video=lambda: None, get_audio=lambda: None),
    )
    camera = camera_module.LibobsVirtualCamera(runtime)
    try:
        assert camera.start()
        assert camera.poll().status is OutputHealthStatus.STARTING
        native.total_frames = 1
        assert camera.poll().status is OutputHealthStatus.READY
        clock.now += 3
        assert camera.poll().status is OutputHealthStatus.RECOVERING
        assert calls == [("stop", {"wait": False}), "release"]
    finally:
        camera.stop()
