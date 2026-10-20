"""Fresh-frame verification for shared camera reconfiguration."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solin.core.media.obs_camera_capture import CameraCaptureState, ObsCameraCaptureProbe


def _probe():
    now = [10.0]
    source = SimpleNamespace(showing=True, update=Mock())
    runtime = SimpleNamespace(
        ob=SimpleNamespace(add_main_render_callback=Mock(return_value="render"),
                           remove_main_render_callback=Mock()),
        observe_source_updates=Mock(return_value=Mock()),
        observe_source_video=Mock(return_value=Mock()),
    )
    probe = ObsCameraCaptureProbe(runtime, source, width=1920, height=1080,
                                 monotonic=lambda: now[0])
    tick = runtime.ob.add_main_render_callback.call_args.args[0]
    updated = runtime.observe_source_updates.call_args.args[1]
    return probe, source, runtime, now, tick, updated


def test_creation_requires_a_frame_and_reports_negotiated_dimensions():
    probe, _source, runtime, _now, tick, _updated = _probe()
    try:
        assert probe.state.status == "starting"
        tick(1920, 1080)
        assert probe.state.status == "starting"
        runtime.observe_source_video.call_args.args[1](1920, 1080, 100)
        tick(1920, 1080)
        assert probe.state.status == "ready"
        assert (probe.state.width, probe.state.height) == (1920, 1080)
    finally:
        probe.close()


def test_frame_sequence_distinguishes_fresh_capture_from_cached_ready_and_survives_reconnect():
    probe, _source, runtime, now, tick, updated = _probe()
    receive = runtime.observe_source_video.call_args.args[1]
    try:
        receive(1920, 1080, 100)
        tick(1920, 1080)
        first = probe.state.frame_sequence
        assert first > 0
        tick(1920, 1080)
        assert probe.state.frame_sequence == first
        receive(1920, 1080, 101)
        tick(1920, 1080)
        assert probe.state.frame_sequence > first
        last_ready = probe.state.frame_sequence
        now[0] += 6
        tick(1920, 1080)
        assert probe.state.error_code == "camera_no_frames"
        probe.update({}, width=1920, height=1080)
        updated()
        tick(1920, 1080)
        receive(1920, 1080, 102)
        tick(1920, 1080)
        assert probe.state.status == "ready" and probe.state.frame_sequence > last_ready
    finally:
        probe.close()


def test_update_does_not_accept_a_frame_from_the_previous_configuration():
    probe, source, runtime, _now, tick, updated = _probe()
    try:
        runtime.observe_source_video.call_args.args[1](1920, 1080, 100)
        tick(1920, 1080)
        probe.update({"resolution": "1280x720"}, width=1280, height=720)
        generation = probe.state.generation
        source.update.assert_called_once_with({"resolution": "1280x720"})
        runtime.observe_source_video.call_args.args[1](1280, 720, 101)
        tick(1920, 1080)
        assert probe.state.status == "starting"
        updated()
        tick(1920, 1080)  # The update tick can still select the preceding decoder.
        assert probe.state.status == "starting"
        runtime.observe_source_video.call_args.args[1](1280, 720, 102)
        tick(1920, 1080)
        assert probe.state.status == "ready"
        assert probe.state.generation == generation
    finally:
        probe.close()


def test_mismatch_is_not_ready_and_timeout_is_counted_only_while_showing():
    probe, source, runtime, now, tick, _updated = _probe()
    try:
        source.showing = False
        now[0] += 100
        tick(1920, 1080)
        assert probe.state.status == "starting"
        source.showing = True
        runtime.observe_source_video.call_args.args[1](640, 480, 100)
        tick(1920, 1080)
        now[0] += 6
        tick(1920, 1080)
        assert probe.state.status == "failed"
        assert probe.state.error_code == "camera_mode_mismatch"
    finally:
        probe.close()


def test_missing_frame_and_update_failure_are_reported_and_close_drains_callbacks():
    probe, source, runtime, now, tick, _updated = _probe()
    tick(1920, 1080)
    now[0] += 6
    tick(1920, 1080)
    assert probe.state.error_code == "camera_no_frames"
    source.update.side_effect = RuntimeError("device busy")
    try:
        probe.update({}, width=0, height=0)
    except RuntimeError:
        pass
    assert probe.state.error_code == "camera_update_failed"
    runtime.ob.remove_main_render_callback.side_effect = lambda _handle: tick(1920, 1080)
    probe.close()
    probe.close()
    runtime.ob.remove_main_render_callback.assert_called_once_with("render")
    runtime.observe_source_updates.return_value.assert_called_once_with()
    runtime.observe_source_video.return_value.assert_called_once_with()


def test_rejected_update_cannot_be_confirmed_by_frames_from_the_previous_stream():
    probe, source, runtime, _now, tick, updated = _probe()
    try:
        receive = runtime.observe_source_video.call_args.args[1]
        receive(1920, 1080, 100)
        tick(1920, 1080)
        source.update.side_effect = RuntimeError("device rejected rate")
        try:
            probe.update({"frame_interval": 666667}, width=1920, height=1080)
        except RuntimeError:
            pass
        receive(1920, 1080, 101)
        tick(1920, 1080)
        assert probe.state.status == "failed"
        assert probe.state.error_code == "camera_update_failed"
        source.update.side_effect = None
        probe.update({"frame_interval": 333333}, width=1920, height=1080)
        updated()
        tick(1920, 1080)
        receive(1920, 1080, 102)
        tick(1920, 1080)
        assert probe.state.status == "ready"
    finally:
        probe.close()


@pytest.mark.parametrize("status, error_code, accepted", [
    ("starting", "", False),
    ("ready", "", False),
    ("starting", "camera_no_frames", False),
    ("ready", "camera_update_failed", False),
    ("failed", "camera_no_frames", True),
    ("failed", "camera_update_failed", True),
    ("failed", "", False),
    ("failed", "camera_mode_mismatch", False),
    ("failed", "camera_observation_failed", False),
])
def test_reconnect_reserves_only_confirmed_connectivity_failures(status, error_code, accepted):
    probe, source, _runtime, _now, _tick, _updated = _probe()
    try:
        previous = CameraCaptureState(3, status, error_code, frame_sequence=7)
        with probe._lock:
            probe._state = previous

        applied = probe.reconnect({"resolution": "1280x720"}, expected_generation=3,
                                  width=1280, height=720)

        assert applied is accepted
        if accepted:
            assert probe.state == CameraCaptureState(4, "starting", frame_sequence=7)
            source.update.assert_called_once_with({"resolution": "1280x720"})
            assert not probe.reconnect({}, expected_generation=4, width=1280, height=720)
        else:
            assert probe.state == previous
            source.update.assert_not_called()
    finally:
        probe.close()


@pytest.mark.parametrize("closed, generation_offset", [(False, -1), (False, 1), (True, 0)])
def test_reconnect_rejects_stale_or_closed_capture_without_native_update(closed, generation_offset):
    probe, source, _runtime, now, tick, _updated = _probe()
    try:
        tick(1920, 1080)
        now[0] += 6
        tick(1920, 1080)
        previous = probe.state
        assert previous.error_code == "camera_no_frames"
        if closed:
            probe.close()

        assert not probe.reconnect({}, expected_generation=previous.generation + generation_offset,
                                   width=1920, height=1080)
        assert probe.state == previous
        source.update.assert_not_called()
    finally:
        probe.close()


def test_reconnect_publishes_starting_generation_before_releasing_reservation_lock():
    probe, source, _runtime, now, tick, _updated = _probe()
    try:
        tick(1920, 1080)
        now[0] += 6
        tick(1920, 1080)
        previous = probe.state
        assert previous.error_code == "camera_no_frames"
        lock = probe._lock
        publications = []

        class ObservedLock:
            def __enter__(self):
                lock.acquire()

            def __exit__(self, *_exception):
                publications.append(probe._state)
                lock.release()

        probe._lock = ObservedLock()
        assert probe.reconnect({}, expected_generation=previous.generation,
                               width=1920, height=1080)
        assert publications[0].status == "starting"
        assert publications[0].generation == previous.generation + 1
        source.update.assert_called_once_with({})
    finally:
        probe.close()


@pytest.mark.parametrize("reconnect", [False, True])
def test_native_update_can_invoke_probe_callbacks_without_holding_probe_lock(reconnect):
    probe, source, runtime, now, tick, updated = _probe()
    try:
        tick(1920, 1080)
        now[0] += 6
        tick(1920, 1080)
        generation = probe.state.generation
        receive = runtime.observe_source_video.call_args.args[1]
        unlocked = []

        def update(_settings):
            acquired = probe._lock.acquire(blocking=False)
            unlocked.append(acquired)
            if not acquired:
                return  # Report a held mutex without deadlocking the native callbacks below.
            probe._lock.release()
            assert probe.state.status == "starting" and probe.state.generation == generation + 1
            receive(1920, 1080, 100)
            tick(1920, 1080)
            assert probe.state.status == "starting"
            updated()

        source.update.side_effect = update
        if reconnect:
            assert probe.reconnect({}, expected_generation=generation, width=1920, height=1080)
        else:
            probe.update({}, width=1920, height=1080)

        assert unlocked == [True], "Native updates must not hold the probe callback mutex"
        tick(1920, 1080)  # Drain the frame selected before update acknowledgement.
        receive(1920, 1080, 101)
        tick(1920, 1080)
        assert probe.state.status == "ready"
        source.update.assert_called_once_with({})
    finally:
        probe.close()


def test_reconnect_native_failure_propagates_and_allows_retry_only_from_new_failed_generation():
    probe, source, _runtime, now, tick, _updated = _probe()
    try:
        tick(1920, 1080)
        now[0] += 6
        tick(1920, 1080)
        generation = probe.state.generation
        failure = RuntimeError("device busy")
        source.update.side_effect = failure

        with pytest.raises(RuntimeError) as error:
            probe.reconnect({}, expected_generation=generation, width=1920, height=1080)

        assert error.value is failure
        assert probe.state.generation == generation + 1
        assert probe.state.status == "failed" and probe.state.error_code == "camera_update_failed"
        source.update.side_effect = None
        assert not probe.reconnect({}, expected_generation=generation, width=1920, height=1080)
        assert probe.reconnect({}, expected_generation=generation + 1, width=1920, height=1080)
        assert source.update.call_count == 2
    finally:
        probe.close()
