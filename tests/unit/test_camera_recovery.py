"""Physical recovery policy with controlled futures and a deterministic clock."""

from concurrent.futures import Executor, Future
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solin.core.media.obs_camera_capture import CameraCaptureState
from solin.core.scenes.camera_recovery import LocalCameraRecovery


class _Executor(Executor):
    def __init__(self):
        self.pending = []
        self.closed = False

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        self.pending.append(future)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.closed = True


@pytest.fixture
def recovery():
    clock = [10.0]
    executor = _Executor()
    state = {"camera": CameraCaptureState(1, "failed", "camera_no_frames")}
    calls = []

    def reconnect(device, generation):
        calls.append((device, generation))
        state[device] = CameraCaptureState(generation + 1, "starting")
        return True

    cancel = Mock()
    owner = LocalCameraRecovery(
        discover=lambda: (), state=state.get, reconnect=reconnect, cancel_discovery=cancel,
        monotonic=lambda: clock[0], executor=executor,
    )
    owner.set_targets({"camera": "config"})
    fixture = SimpleNamespace(owner=owner, clock=clock, executor=executor, state=state,
                              calls=calls, cancel=cancel)
    try:
        yield fixture
    finally:
        owner.close()


def _inventory(recovery, devices=("camera",), *, revision=1):
    assert recovery.owner.changed(revision)
    recovery.owner.poll()
    recovery.executor.pending[-1].set_result(devices)
    recovery.owner.poll()


def test_topology_triggers_recovery_without_waiting_for_discovery(recovery):
    owner = recovery.owner
    owner.poll()
    assert recovery.executor.pending == []
    owner.changed(1)
    owner.poll()
    owner.poll()
    assert len(recovery.executor.pending) == 1 and not recovery.calls
    recovery.executor.pending[0].set_result(("camera",))
    owner.poll()
    assert recovery.calls == [("camera", 1)]
    assert owner.recovering("camera")
    # An update is not proof that the device is delivering video.
    owner.poll()
    assert len(recovery.calls) == 1
    recovery.state["camera"] = CameraCaptureState(2, "ready", frame_sequence=1)
    owner.poll()
    assert not owner.recovering("camera")


def test_healthy_capture_is_not_interrupted_by_another_device(recovery):
    recovery.state["camera"] = CameraCaptureState(1, "ready", frame_sequence=12)
    _inventory(recovery)
    recovery.state["camera"] = CameraCaptureState(1, "ready", frame_sequence=13)
    recovery.owner.poll()
    recovery.clock[0] += 6
    recovery.owner.poll()
    assert recovery.calls == []


def test_quick_replug_waits_for_missing_frame_confirmation(recovery):
    recovery.state["camera"] = CameraCaptureState(1, "ready", frame_sequence=12)
    _inventory(recovery)
    assert not recovery.calls
    recovery.state["camera"] = CameraCaptureState(1, "failed", "camera_no_frames")
    recovery.owner.poll()
    assert recovery.calls == [("camera", 1)]


def test_normally_starting_capture_is_not_restarted_before_frame_timeout(recovery):
    recovery.state["camera"] = CameraCaptureState(1, "starting")
    _inventory(recovery)
    assert not recovery.calls
    recovery.state["camera"] = CameraCaptureState(1, "ready", frame_sequence=1)
    recovery.owner.poll()
    assert not recovery.calls and not recovery.owner.recovering("camera")


def test_driver_enumeration_may_lag_the_arrival_notification(recovery):
    _inventory(recovery, ())
    assert not recovery.calls
    recovery.clock[0] += 1
    recovery.owner.poll()
    assert len(recovery.executor.pending) == 2
    recovery.executor.pending[-1].set_result(("camera",))
    recovery.owner.poll()
    assert recovery.calls == [("camera", 1)]


def test_metadata_failure_is_not_a_missing_device_and_retries_are_bounded(recovery):
    recovery.owner.changed(1)
    for delay in (0, 1, 3):
        recovery.clock[0] += delay
        recovery.owner.poll()
        recovery.executor.pending[-1].set_exception(OSError("metadata unavailable"))
        recovery.owner.poll()
    recovery.clock[0] += 5
    recovery.owner.poll()
    assert len(recovery.executor.pending) == 3 and not recovery.calls


def test_capture_failures_retry_after_backoff_at_most_three_times(recovery):
    _inventory(recovery)
    for generation, delay in ((2, 1), (3, 3)):
        recovery.state["camera"] = CameraCaptureState(generation, "failed", "camera_no_frames")
        recovery.owner.poll()
        assert len(recovery.calls) == generation - 1
        recovery.clock[0] += delay
        recovery.owner.poll()
        assert len(recovery.calls) == generation
    recovery.state["camera"] = CameraCaptureState(4, "failed", "camera_no_frames")
    recovery.owner.poll()
    recovery.clock[0] += 10
    recovery.owner.poll()
    assert len(recovery.calls) == 3


def test_native_update_exception_retries_from_the_advanced_failed_generation(recovery):
    reconnect = recovery.owner._reconnect

    def failed(device, generation):
        reconnect(device, generation)
        recovery.state[device] = CameraCaptureState(generation + 1, "failed", "camera_update_failed")
        raise RuntimeError("native update failed")

    recovery.owner._reconnect = failed
    _inventory(recovery)
    assert recovery.owner.recovering("camera")
    recovery.owner.poll()
    recovery.clock[0] += 1
    recovery.owner.poll()
    assert recovery.calls == [("camera", 1), ("camera", 2)]


@pytest.mark.parametrize("change", ["topology", "generation", "configuration"])
def test_new_topology_retains_only_its_own_failed_reactivation(recovery, change):
    reconnect = recovery.owner._reconnect

    def failed(device, generation):
        reconnect(device, generation)
        recovery.state[device] = CameraCaptureState(generation + 1, "failed", "camera_update_failed")
        raise RuntimeError("native update failed")

    recovery.owner._reconnect = failed
    _inventory(recovery)
    assert recovery.calls == [("camera", 1)]
    if change == "generation":
        recovery.state["camera"] = replace(recovery.state["camera"], generation=3)
    elif change == "configuration":
        recovery.owner.set_targets({"camera": "new-config"})
    _inventory(recovery, revision=2)
    expected = [("camera", 1), ("camera", 2)] if change == "topology" else [("camera", 1)]
    assert recovery.calls == expected


@pytest.mark.parametrize("change", ["generation", "configuration", "removed"])
def test_obsolete_inventory_cannot_reconfigure_a_newer_capture(recovery, change):
    recovery.owner.changed(1)
    recovery.owner.poll()
    if change == "generation":
        recovery.state["camera"] = replace(recovery.state["camera"], generation=2)
    else:
        recovery.owner.set_targets({"camera": "new-config"} if change == "configuration" else {})
    recovery.executor.pending[-1].set_result(("camera",))
    recovery.owner.poll()
    assert not recovery.calls


def test_change_during_discovery_discards_result_and_coalesces_follow_up(recovery):
    recovery.owner.changed(1)
    recovery.owner.poll()
    for revision in (2, 3, 4):
        recovery.owner.changed(revision)
        recovery.owner.poll()
    assert len(recovery.executor.pending) == 1
    recovery.executor.pending[0].set_result(("camera",))
    recovery.owner.poll()
    assert not recovery.calls and len(recovery.executor.pending) == 2
    recovery.executor.pending[-1].set_result(())
    recovery.owner.poll()
    assert not recovery.calls


@pytest.mark.parametrize("code", ["camera_mode_mismatch", "camera_update_failed", "camera_observation_failed"])
def test_non_connectivity_failures_are_not_retried(recovery, code):
    recovery.state["camera"] = CameraCaptureState(1, "failed", code)
    _inventory(recovery)
    assert not recovery.calls


def test_unused_camera_is_not_instantiated(recovery):
    recovery.state.clear()
    recovery.owner.changed(1)
    recovery.owner.poll()
    assert not recovery.executor.pending and not recovery.calls


def test_topology_event_before_first_hydration_covers_late_capture_creation(recovery):
    recovery.owner.set_targets({})
    recovery.owner.changed(1)
    recovery.owner.poll()
    assert not recovery.executor.pending
    recovery.owner.set_targets({"camera": "config"})
    recovery.owner.poll()
    recovery.executor.pending[-1].set_result(("camera",))
    recovery.owner.poll()
    assert recovery.calls == [("camera", 1)]


def test_native_managed_capture_does_not_repeat_reactivation(recovery):
    recovery.owner._reconnect = Mock(return_value=False)
    _inventory(recovery)
    for _ in range(5):
        recovery.owner.poll()
    recovery.owner._reconnect.assert_called_once_with("camera", 1)
    assert not recovery.owner.recovering("camera")


def test_cycle_expires_without_clearing_unconfirmed_capture_warning(recovery):
    _inventory(recovery)
    recovery.clock[0] += 30
    recovery.owner.poll()
    assert recovery.owner.recovering("camera")
    assert recovery.calls == [("camera", 1)]
    assert not recovery.owner.changed(1)
    assert recovery.owner.changed(2)


def test_close_cancels_native_work_and_ignores_late_completion(recovery):
    recovery.owner.changed(1)
    recovery.owner.poll()
    recovery.owner.close()
    recovery.owner.close()
    recovery.cancel.assert_called_once_with()
    assert recovery.executor.closed
    recovery.executor.pending[0].set_result(("camera",))
    assert not recovery.owner.changed(2)
    recovery.owner.poll()
    assert not recovery.calls
