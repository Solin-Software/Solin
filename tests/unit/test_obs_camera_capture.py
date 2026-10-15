"""Fresh-frame verification for shared camera reconfiguration."""

from types import SimpleNamespace
from unittest.mock import Mock

from solin.core.media.obs_camera_capture import ObsCameraCaptureProbe


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
