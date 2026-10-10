from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from solin.core.scenes.engine import (
    EngineHealthEvent,
    ProgramRecordingEvent,
    SceneEngineSnapshot,
    SceneEngineStatus,
)
from solin.core.scenes.ipc_protocol import PROTOCOL_VERSION
from solin.core.scenes.idle import IdleScreenState
from solin.core.scenes.model import (
    DELIVERY_BUSES,
    BusId,
    SceneDocument,
    TransitionKind,
    TransitionSpec,
)
from solin.core.scenes.presets import SceneSeedNames, create_default_scene_document
from solin.core.scenes.process_engine import (
    SceneEngineCommandRejectedError,
    SceneEngineProcessConfig,
    SceneEngineProtocolError,
    SceneEngineRequestTimeoutError,
    SubprocessSceneEngine,
    _ack_from_envelope,
)
from solin.core.scenes.recording import (
    AudioDeviceSelection,
    AudioSelectionMode,
    ProgramRecordingRequest,
    ProgramRecordingStatus,
)


FAKE_ENGINE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_scene_engine.py"


def _document():
    return create_default_scene_document(
        SceneSeedNames(
            content_source="Current content",
            default_camera_source="Default camera",
            no_signal_source="No signal background",
            content_scene="Content",
            camera_scene="Camera",
            content_camera_pip_scene="Content + camera",
            no_signal_scene="No signal",
            content_layer="Content",
            camera_layer="Camera",
            background_layer="Background",
        ),
        document_id="process-engine-document",
        created_at="2026-08-02T12:00:00+00:00",
    )


def _engine(mode: str = "normal", *arguments: str) -> SubprocessSceneEngine:
    return SubprocessSceneEngine(
        SceneEngineProcessConfig(
            executable=Path(sys.executable),
            arguments=("-u", str(FAKE_ENGINE), mode, *arguments),
            hello_timeout_ms=1500,
            heartbeat_interval_ms=50,
            heartbeat_timeout_ms=500,
            stop_timeout_ms=500,
            maximum_restarts=2,
            restart_initial_delay_ms=20,
            restart_maximum_delay_ms=50,
            restart_jitter_ratio=0,
        )
    )


def _snapshot(
    sequence: int = 1,
    *,
    document: SceneDocument | None = None,
    enabled: bool = True,
) -> SceneEngineSnapshot:
    document = document or _document()
    active_scenes = tuple((route.bus_id, route.default_scene_id) for route in document.outputs)
    return SceneEngineSnapshot(
        session_id="integration-session",
        sequence=sequence,
        document=document,
        active_scenes=active_scenes + ((BusId.EDITOR, active_scenes[0][1]),),
        render_enabled=tuple((bus_id, enabled) for bus_id in BusId),
        output_enabled=tuple((bus_id, enabled and bus_id in DELIVERY_BUSES) for bus_id in BusId),
    )


@pytest.mark.parametrize("epoch", [0, 2**64 - 1])
def test_empty_content_presentation_roundtrips_without_an_output_take(epoch):
    engine = _engine()
    try:
        engine.start(session_id="clear-content", deadline_ms=2000).result(3)
        response = engine.clear_content_presentation(
            epoch, request_id="clear-1", sequence=7, document_revision=11, deadline_ms=1000,
        ).result(2)
        assert response.applied
        assert (response.request_id, response.sequence, response.document_revision) == ("clear-1", 7, 11)
    finally:
        engine.stop()


@pytest.mark.parametrize("epoch", [None, True, -1, 2**64, "1"])
def test_empty_content_presentation_rejects_invalid_epoch_before_ipc(epoch):
    engine = _engine()
    with pytest.raises(ValueError, match="epoch"):
        engine.clear_content_presentation(
            epoch, request_id="clear-1", sequence=1, document_revision=0, deadline_ms=1000,
        ).result()


def test_idle_state_roundtrip_and_profile_switch_keep_document_revision_independent(tmp_path):
    transcript = tmp_path / "idle-ipc.jsonl"
    engine = _engine("idle_contract", str(transcript))
    initial = IdleScreenState(41, "selected idle.mp4", "annual text.png", 11)
    updated = replace(initial, revision=42, yeartext_revision=12)
    removed = replace(updated, revision=43, media_path="")
    older = _document().with_revision(7)
    newer = replace(_document(), document_id="other-profile", revision=0)
    try:
        engine.start(session_id="integration-session", deadline_ms=2000).result(3)
        for sequence, (message, document, state) in enumerate((
            ("hydrate", older, initial),
            ("set_idle_screen", older, updated),
            ("hydrate", newer, updated),
            ("set_idle_screen", newer, removed),
        ), start=1):
            if message == "hydrate":
                response = engine.hydrate(
                    replace(_snapshot(sequence, document=document), idle_screen=state),
                    request_id=f"idle-hydrate-{sequence}", deadline_ms=1000,
                ).result(2)
            else:
                response = engine.set_idle_screen(
                    state, request_id=f"idle-update-{sequence}", sequence=sequence,
                    deadline_ms=1000,
                ).result(2)
            assert response.applied
            assert response.document_revision == document.revision
            assert response.sequence == sequence
        captured = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
        assert [item["idle_screen"] for item in captured] == [
            state.to_record() for state in (initial, updated, updated, removed)
        ]
        assert [item["document_revision"] for item in captured] == [7, 7, 0, 0]
        assert engine.health.restart_count == engine.metrics.protocol_error_count == 0
    finally:
        engine.stop()


@pytest.mark.parametrize("invalid", [
    {}, {"idle_screen": None},
    {"idle_screen": IdleScreenState().to_record() | {"revision": True}},
    {"idle_screen": IdleScreenState().to_record() | {"yeartext_revision": -1}},
    {"idle_screen": IdleScreenState().to_record() | {"media_path": "https://invalid/video.mp4"}},
    {"idle_screen": IdleScreenState().to_record(), "extra": 1},
])
def test_fake_sidecar_rejects_malformed_idle_wire_payload_without_losing_revision(invalid):
    engine = _engine()
    try:
        engine.start(session_id="integration-session", deadline_ms=2000).result(3)
        snapshot = _snapshot(document=_document().with_revision(9))
        assert engine.hydrate(snapshot, request_id="initial", deadline_ms=1000).result(2).applied
        # Exercise malformed wire records beyond the public typed API's own validation.
        rejected = engine._request(
            message_type="set_idle_screen", expected_message_type="ack", request_id="invalid-idle",
            sequence=2, document_revision=9, deadline_ms=1000, payload=invalid,
            converter=_ack_from_envelope,
        ).result(2)
        assert not rejected.applied
        assert rejected.error_code == "invalid_idle_screen"
        accepted = engine.set_idle_screen(
            IdleScreenState(80, "idle.mp4", "annual.png", 7),
            request_id="valid-idle", sequence=3, deadline_ms=1000,
        ).result(2)
        assert accepted.applied and accepted.document_revision == 9
        assert engine.health.restart_count == engine.metrics.protocol_error_count == 0
    finally:
        engine.stop()


def test_subprocess_camera_discovery_preserves_high_precision_fps() -> None:
    engine = _engine("camera_exact_fps")
    try:
        engine.start(session_id="integration-session", deadline_ms=2000).result(3)
        discovery = engine.list_local_cameras(request_id="cameras", deadline_ms=1000).result(2)
        assert len(discovery.devices) == 2
        assert discovery.devices[0].formats[0].fps_numerator == 10_000_000
        assert discovery.devices[0].formats[0].fps_denominator == 333_333
        assert engine.metrics.protocol_error_count == 0
        assert engine.health.restart_count == 0
    finally:
        engine.stop()


def test_subprocess_camera_failure_reports_the_actual_validation_rule(caplog) -> None:
    engine = _engine("camera_invalid_fps")
    try:
        engine.start(session_id="integration-session", deadline_ms=2000).result(3)
        with pytest.raises(SceneEngineProtocolError) as failure:
            engine.list_local_cameras(request_id="cameras", deadline_ms=1000).result(2)
        assert failure.value.__cause__ is not None
        assert "FPS denominator" in caplog.text
        assert '"device_index":0' in caplog.text
        assert '"format_index":0' in caplog.text
        assert "camera://device-1" not in caplog.text
    finally:
        engine.stop()


def test_subprocess_engine_executes_the_scene_command_lifecycle() -> None:
    engine = _engine()
    recording_events: list[ProgramRecordingEvent] = []
    engine.subscribe(
        lambda event: (
            recording_events.append(event) if isinstance(event, ProgramRecordingEvent) else None
        )
    )
    capabilities = engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    assert capabilities.protocol_version == PROTOCOL_VERSION
    assert not capabilities.hardware_compositing

    discovery = engine.list_local_cameras(
        request_id="list-cameras-1",
        deadline_ms=1000,
    ).result(2)
    assert discovery.supported
    assert discovery.ready
    assert discovery.devices[0].device_id == "camera://device-1"
    assert discovery.devices[0].formats[0].frames_per_second == pytest.approx(29.97, 0.01)
    assert discovery.devices[1].device_id == "camera://inventory-only"
    assert discovery.devices[1].probe.status.value == "unverified"
    assert discovery.devices[1].probe.error_code == "capture_provider_not_reported"
    assert discovery.devices[1].formats == ()

    audio_devices = engine.list_audio_devices(
        request_id="list-audio-1",
        deadline_ms=1000,
    ).result(2)
    assert audio_devices.ready
    assert {device.direction.value for device in audio_devices.devices} == {"input", "output"}

    snapshot = _snapshot()
    hydrated = engine.hydrate(snapshot, request_id="hydrate-1", deadline_ms=1000).result(2)
    assert hydrated.applied
    recording_path = Path.cwd() / "Program recording.mp4"
    recording = engine.start_program_recording(
        ProgramRecordingRequest(
            path=recording_path,
            width=snapshot.document.output(BusId.VIRTUAL_CAMERA).video_format.width,
            height=snapshot.document.output(BusId.VIRTUAL_CAMERA).video_format.height,
            fps_numerator=60,
            fps_denominator=1,
            microphone=AudioDeviceSelection(),
            system_audio=AudioDeviceSelection(AudioSelectionMode.NONE),
        ),
        request_id="recording-start-1",
        deadline_ms=1000,
    ).result(2)
    assert recording.applied
    assert recording_events[-1].state.status is ProgramRecordingStatus.RECORDING
    changed_audio = engine.set_program_recording_audio(
        AudioDeviceSelection(AudioSelectionMode.NONE),
        AudioDeviceSelection(),
        request_id="recording-audio-1",
        deadline_ms=1000,
    ).result(2)
    assert changed_audio.applied
    preparation = engine.prepare_scene(
        BusId.MEDIA_WINDOWS,
        snapshot.active_scenes[0][1],
        transition=TransitionSpec(TransitionKind.CUT, 0),
        document_revision=snapshot.document.revision,
        request_id="prepare-1",
        sequence=2,
        deadline_ms=1000,
    ).result(2)
    taken = engine.take_prepared(
        preparation,
        request_id="take-1",
        sequence=3,
        deadline_ms=1000,
    ).result(2)
    scene = snapshot.document.scene(snapshot.active_scenes[0][1])
    preview = engine.preview_layer_geometry(
        BusId.MEDIA_WINDOWS,
        scene.id,
        scene.layers[0],
        document_revision=snapshot.document.revision,
        request_id="preview-1",
        sequence=4,
        deadline_ms=1000,
    ).result(2)
    output = engine.set_output_enabled(
        BusId.VIRTUAL_CAMERA,
        True,
        request_id="output-1",
        sequence=5,
        deadline_ms=1000,
    ).result(2)
    window_targets = engine.set_window_targets(
        (),
        request_id="window-targets-1",
        sequence=6,
        deadline_ms=1000,
    ).result(2)

    assert taken.applied
    assert preview.applied
    assert output.document_revision == snapshot.document.revision
    assert window_targets.applied
    assert window_targets.document_revision == snapshot.document.revision
    stopped_recording = engine.stop_program_recording(
        request_id="recording-stop-1",
        deadline_ms=1000,
    ).result(2)
    assert stopped_recording.applied
    assert recording_events[-1].state.status is ProgramRecordingStatus.IDLE
    metrics = engine.metrics
    assert metrics.request_count >= 12
    assert metrics.latency_sample_count >= 12
    assert 0 <= metrics.latency_p50_ms <= metrics.latency_p95_ms <= metrics.latency_maximum_ms
    engine.stop()
    _wait_until(lambda: engine.health.status is SceneEngineStatus.STOPPED)


def test_subprocess_engine_does_not_kill_a_healthy_long_running_command() -> None:
    engine = _engine("slow_hydrate")
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)

    hydrated = engine.hydrate(
        _snapshot(),
        request_id="slow-hydrate",
        deadline_ms=1500,
    ).result(2)

    assert hydrated.applied
    assert engine.health.status is SceneEngineStatus.READY
    assert engine.health.restart_count == 0
    engine.stop()


def test_subprocess_engine_restarts_after_a_child_crash(tmp_path: Path) -> None:
    marker = tmp_path / "first-generation-crashed"
    engine = _engine("crash_first", str(marker))
    ready_generations: list[str] = []

    def on_event(event: object) -> None:
        if isinstance(event, EngineHealthEvent) and event.health.status is SceneEngineStatus.READY:
            ready_generations.append(event.health.process_generation)

    engine.subscribe(on_event)
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    _wait_until(lambda: len(set(ready_generations)) >= 2, timeout=4)

    snapshot = _snapshot()
    assert engine.hydrate(snapshot, request_id="hydrate-after-restart", deadline_ms=1000).result(2)
    assert engine.health.restart_count == 1
    engine.stop()


def test_subprocess_engine_stops_automatic_crash_loop_at_restart_limit() -> None:
    engine = _engine("crash_after_hello")
    ready_generations: list[str] = []

    def on_event(event: object) -> None:
        if isinstance(event, EngineHealthEvent) and event.health.status is SceneEngineStatus.READY:
            ready_generations.append(event.health.process_generation)

    engine.subscribe(on_event)
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    _wait_until(
        lambda: (
            engine.health.status is SceneEngineStatus.FAILED and engine.health.restart_count == 2
        ),
        timeout=4,
    )

    assert len(set(ready_generations)) == 3


def test_subprocess_engine_counts_malformed_unsolicited_events() -> None:
    engine = _engine("malformed_event")

    engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    _wait_until(lambda: engine.health.status is SceneEngineStatus.FAILED, timeout=4)

    assert engine.metrics.protocol_error_count >= 1
    engine.stop()


def test_subprocess_engine_expires_unanswered_requests() -> None:
    engine = _engine("ignore_prepare")
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    snapshot = _snapshot()
    engine.hydrate(snapshot, request_id="hydrate-1", deadline_ms=1000).result(2)

    future = engine.prepare_scene(
        BusId.MEDIA_WINDOWS,
        snapshot.active_scenes[0][1],
        transition=TransitionSpec(TransitionKind.CUT, 0),
        document_revision=snapshot.document.revision,
        request_id="prepare-timeout",
        sequence=2,
        deadline_ms=100,
    )
    with pytest.raises(SceneEngineRequestTimeoutError):
        future.result(2)
    assert engine.health.status is SceneEngineStatus.READY
    assert engine.metrics.timeout_count == 1
    engine.stop()


def test_subprocess_engine_surfaces_command_rejection_without_restarting() -> None:
    engine = _engine("reject_prepare")
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    snapshot = _snapshot()
    engine.hydrate(snapshot, request_id="hydrate-1", deadline_ms=1000).result(2)

    future = engine.prepare_scene(
        BusId.MEDIA_WINDOWS,
        snapshot.active_scenes[0][1],
        transition=TransitionSpec(TransitionKind.CUT, 0),
        document_revision=snapshot.document.revision,
        request_id="prepare-rejected",
        sequence=2,
        deadline_ms=1000,
    )
    with pytest.raises(SceneEngineCommandRejectedError) as error:
        future.result(2)
    assert error.value.error_code == "source_unavailable"
    assert engine.health.status is SceneEngineStatus.READY
    assert engine.health.restart_count == 0
    assert engine.metrics.rejected_count == 1
    engine.stop()


def test_rejected_hydration_does_not_advance_the_applied_document_revision() -> None:
    engine = _engine("reject_hydrate")
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    document = _document().with_revision(7)
    snapshot = _snapshot(document=document, enabled=False)

    rejected = engine.hydrate(
        snapshot,
        request_id="hydrate-rejected",
        deadline_ms=1000,
    ).result(2)
    output = engine.set_output_enabled(
        BusId.MEDIA_WINDOWS,
        True,
        request_id="output-after-rejection",
        sequence=2,
        deadline_ms=1000,
    ).result(2)

    assert not rejected.applied
    assert rejected.document_revision == 7
    assert output.document_revision == 0
    engine.stop()


def test_subprocess_engine_returns_a_typed_transition_fallback() -> None:
    engine = _engine("transition_fallback")
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    snapshot = _snapshot()
    engine.hydrate(snapshot, request_id="hydrate-1", deadline_ms=1000).result(2)

    preparation = engine.prepare_scene(
        BusId.VIRTUAL_CAMERA,
        snapshot.active_scenes[1][1],
        transition=TransitionSpec(TransitionKind.DISSOLVE, 350),
        document_revision=snapshot.document.revision,
        request_id="prepare-1",
        sequence=2,
        deadline_ms=1000,
    ).result(2)

    assert preparation.transition == TransitionSpec(TransitionKind.CUT, 0)
    assert preparation.fallback_applied
    assert preparation.fallback_reason == "transition_pipeline_unavailable"
    assert (
        engine.take_prepared(
            preparation,
            request_id="take-1",
            sequence=3,
            deadline_ms=1000,
        )
        .result(2)
        .applied
    )
    engine.stop()


def test_hydrating_another_document_replaces_the_command_revision() -> None:
    engine = _engine()
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    older_profile = replace(_document(), document_id="older-profile", revision=7)
    newer_profile = replace(_document(), document_id="newer-profile", revision=0)

    for sequence, document in enumerate((older_profile, newer_profile), start=1):
        snapshot = _snapshot(sequence, document=document)
        assert (
            engine.hydrate(
                snapshot,
                request_id=f"hydrate-profile-{sequence}",
                deadline_ms=1000,
            )
            .result(2)
            .applied
        )

    output = engine.set_output_enabled(
        BusId.VIRTUAL_CAMERA,
        True,
        request_id="output-after-profile-switch",
        sequence=3,
        deadline_ms=1000,
    ).result(2)

    assert output.document_revision == newer_profile.revision
    engine.stop()


def test_subprocess_engine_locks_gstreamer_to_its_bundled_runtime(tmp_path: Path) -> None:
    runtime_root = tmp_path / "gstreamer"
    (runtime_root / "bin").mkdir(parents=True)
    (runtime_root / "lib" / "gstreamer-1.0").mkdir(parents=True)
    engine = SubprocessSceneEngine(
        SceneEngineProcessConfig(
            executable=Path(sys.executable),
            arguments=("-u", str(FAKE_ENGINE), "expect_locked_plugins"),
            gstreamer_runtime_root=runtime_root,
            gstreamer_registry_path=tmp_path / "cache" / "registry.bin",
            hello_timeout_ms=1500,
            heartbeat_interval_ms=50,
            heartbeat_timeout_ms=500,
            maximum_restarts=0,
        )
    )

    capabilities = engine.start(session_id="integration-session", deadline_ms=2000).result(3)

    assert capabilities.protocol_version == PROTOCOL_VERSION
    engine.stop()


def test_subprocess_engine_force_terminates_a_sidecar_that_hangs_on_stop() -> None:
    engine = _engine("ignore_stop")
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)

    engine.stop()

    assert engine.health.status is SceneEngineStatus.STOPPED
    assert engine._supervisor is None


def test_subprocess_engine_allows_bounded_clean_sidecar_shutdown(tmp_path: Path) -> None:
    marker = tmp_path / "clean-stop.marker"
    engine = _engine("slow_clean_stop", str(marker))
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)

    engine.stop()

    assert engine.health.status is SceneEngineStatus.STOPPED
    assert engine._supervisor is None
    assert marker.read_text(encoding="utf-8") == "clean"


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")
