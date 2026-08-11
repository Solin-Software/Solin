from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from solin.core.scenes.engine import (
    EngineHealthEvent,
    SceneEngineSnapshot,
    SceneEngineStatus,
)
from solin.core.scenes.model import BusId, TransitionKind
from solin.core.scenes.presets import SceneSeedNames, create_default_scene_document
from solin.core.scenes.process_engine import (
    SceneEngineCommandRejectedError,
    SceneEngineProcessConfig,
    SceneEngineRequestTimeoutError,
    SubprocessSceneEngine,
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


def _snapshot(sequence: int = 1) -> SceneEngineSnapshot:
    document = _document()
    return SceneEngineSnapshot(
        session_id="integration-session",
        sequence=sequence,
        document=document,
        active_scenes=tuple((route.bus_id, route.default_scene_id) for route in document.outputs),
        render_enabled=tuple((bus_id, True) for bus_id in BusId),
        output_enabled=tuple((bus_id, True) for bus_id in BusId),
    )


def test_subprocess_engine_executes_the_scene_command_lifecycle() -> None:
    engine = _engine()
    capabilities = engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    assert capabilities.protocol_version == 2
    assert not capabilities.hardware_compositing

    discovery = engine.list_local_cameras(
        request_id="list-cameras-1",
        deadline_ms=1000,
    ).result(2)
    assert discovery.supported
    assert discovery.ready
    assert discovery.devices[0].device_id == "camera://device-1"
    assert discovery.devices[0].formats[0].frames_per_second == pytest.approx(29.97, 0.01)

    snapshot = _snapshot()
    hydrated = engine.hydrate(snapshot, request_id="hydrate-1", deadline_ms=1000).result(2)
    assert hydrated.applied
    preparation = engine.prepare_scene(
        BusId.MEDIA_WINDOWS,
        snapshot.active_scenes[0][1],
        document_revision=snapshot.document.revision,
        request_id="prepare-1",
        sequence=2,
        deadline_ms=1000,
    ).result(2)
    taken = engine.take_prepared(
        preparation,
        transition=TransitionKind.CUT,
        transition_duration_ms=0,
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

    assert taken.applied
    assert preview.applied
    assert output.document_revision == snapshot.document.revision
    metrics = engine.metrics
    assert metrics.request_count >= 7
    assert metrics.latency_sample_count >= 7
    assert 0 <= metrics.latency_p50_ms <= metrics.latency_p95_ms <= metrics.latency_maximum_ms
    engine.stop()
    _wait_until(lambda: engine.health.status is SceneEngineStatus.STOPPED)


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
    snapshot = SceneEngineSnapshot(
        session_id="integration-session",
        sequence=1,
        document=document,
        active_scenes=tuple((route.bus_id, route.default_scene_id) for route in document.outputs),
        render_enabled=tuple((bus_id, False) for bus_id in BusId),
        output_enabled=tuple((bus_id, False) for bus_id in BusId),
    )

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


def test_hydrating_another_document_replaces_the_command_revision() -> None:
    engine = _engine()
    engine.start(session_id="integration-session", deadline_ms=2000).result(3)
    older_profile = replace(_document(), document_id="older-profile", revision=7)
    newer_profile = replace(_document(), document_id="newer-profile", revision=0)

    for sequence, document in enumerate((older_profile, newer_profile), start=1):
        snapshot = SceneEngineSnapshot(
            session_id="integration-session",
            sequence=sequence,
            document=document,
            active_scenes=tuple(
                (route.bus_id, route.default_scene_id) for route in document.outputs
            ),
            render_enabled=tuple((bus_id, True) for bus_id in BusId),
            output_enabled=tuple((bus_id, True) for bus_id in BusId),
        )
        assert engine.hydrate(
            snapshot,
            request_id=f"hydrate-profile-{sequence}",
            deadline_ms=1000,
        ).result(2).applied

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

    assert capabilities.protocol_version == 2
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
