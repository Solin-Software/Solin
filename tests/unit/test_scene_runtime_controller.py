from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from concurrent.futures import CancelledError, Future
from dataclasses import replace
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThread

from solin.controllers.scene_runtime_controller import (
    ScenePtzEvent,
    SceneRuntimeController,
    content_category_for_projection,
)
from solin.core.scenes.application import SceneDocumentService
from solin.core.scenes.engine import (
    DEFAULT_ENGINE_STARTUP_DEADLINE_MS,
    EngineHealthEvent,
    FrameChannelDescriptor,
    FrameChannelTransport,
    FrameEgressReadyEvent,
    FrameProducerKind,
    LocalCameraDevice,
    LocalCameraDiscovery,
    LocalCameraProbe,
    LocalCameraProbeStatus,
    LocalVideoFormat,
    MediaPlaybackEvent,
    OutputWindowTarget,
    SceneEngine,
    SceneEngineAck,
    SceneEngineCapabilities,
    SceneEngineEvent,
    SceneEngineHealth,
    SceneEngineSnapshot,
    SceneEngineStatus,
    ScenePreparation,
    SourceHealthEvent,
    SourceHealthStatus,
)
from solin.core.scenes.model import (
    BusId,
    CameraPreset,
    CameraMediaType,
    ContentCategory,
    Crop,
    DEFAULT_CAMERA_SOURCE_ID,
    LocalCameraConfig,
    NormalizedRect,
    OnvifPtzBinding,
    OutputMode,
    PtzTimeoutPolicy,
    RecallPtzPresetAction,
    SceneDocument,
    SceneLayer,
    SceneReferenceConfig,
    SceneValidationError,
    SourceDefinition,
    SourceKind,
    TransitionKind,
    TransitionSpec,
    VideoColorRange,
    VideoColorSpace,
    VideoPixelFormat,
)
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.ptz import (
    PtzControlKind,
    PtzControlResult,
    PtzRecallResult,
    PtzRecallStatus,
)
from solin.core.scenes.ipc_protocol import PROTOCOL_VERSION
from solin.core.scenes.process_engine import (
    SceneEngineCommandRejectedError,
    SceneEngineRequestTimeoutError,
)
from solin.core.scenes.media_control import (
    ContentSourceKind,
    MediaPlaybackNativeState,
    MediaPlaybackState,
)
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_CAMERA_PIP_SCENE_ID,
    CONTENT_SCENE_ID,
    DEFAULT_SCENE_ID,
    NO_SIGNAL_SCENE_ID,
    SceneSeedNames,
    create_default_scene_document,
)
from solin.core.scenes.runtime import (
    SceneRuntimeService,
    create_default_runtime_state,
)


def _document() -> SceneDocument:
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
        document_id="test-scene-document",
        created_at="2026-08-02T12:00:00+00:00",
    )


def test_local_video_formats_enforce_the_native_media_budget() -> None:
    with pytest.raises(ValueError, match="media budget"):
        LocalVideoFormat(
            media_type=CameraMediaType.RAW,
            pixel_format="NV12",
            width=3840,
            height=3840,
            fps_numerator=30,
            fps_denominator=1,
        )
    with pytest.raises(ValueError, match="frame rate"):
        LocalVideoFormat(
            media_type=CameraMediaType.RAW,
            pixel_format="NV12",
            width=1920,
            height=1080,
            fps_numerator=120,
            fps_denominator=1,
        )


@pytest.mark.parametrize(
    ("numerator", "denominator"),
    [
        (10_000_000, 333_333),
        (10_000_000, 166_667),
        (2_147_483_647, 143_165_577),
        (2_147_483_647, 35_791_395),
        (1, 2_147_483_647),
    ],
)
def test_local_video_formats_preserve_exact_native_frame_rates(
    numerator: int, denominator: int
) -> None:
    video_format = LocalVideoFormat(
        media_type=CameraMediaType.JPEG,
        pixel_format="JPEG",
        width=1920,
        height=1080,
        fps_numerator=numerator,
        fps_denominator=denominator,
    )

    assert video_format.fps_numerator == numerator
    assert video_format.fps_denominator == denominator


@pytest.mark.parametrize(
    ("numerator", "denominator"),
    [
        (2_147_483_648, 2_147_483_647),
        (1, 2_147_483_648),
        (20_000_000, 666_666),
        (10_000_000, 166_666),
        (10_000_000, 0),
        (0, 1),
        (True, 1),
        (1, True),
    ],
)
def test_local_video_formats_reject_invalid_native_frame_rates(
    numerator: int, denominator: int
) -> None:
    with pytest.raises(ValueError):
        LocalVideoFormat(
            media_type=CameraMediaType.JPEG,
            pixel_format="JPEG",
            width=1920,
            height=1080,
            fps_numerator=numerator,
            fps_denominator=denominator,
        )


class _Projection:
    def __init__(self) -> None:
        self.state = {"type": "idle"}
        self.session_id = 0
        self._listeners: set[Callable[[], None]] = set()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def set_type(self, state_type: str) -> None:
        self.set_state({"type": state_type})

    def set_state(self, state: dict[str, object]) -> None:
        self.state = state
        self.session_id += 1
        for listener in tuple(self._listeners):
            listener()


class _Workspace:
    def __init__(
        self,
        documents: SceneDocumentService,
        runtime: SceneRuntimeService,
    ) -> None:
        self.documents = documents
        self.runtime = runtime

    def subscribe(self, _listener: Callable) -> Callable[[], None]:
        return lambda: None

    def close(self) -> None:
        self.runtime.close()


class _Engine:
    generation = "engine-generation-1"

    def __init__(self) -> None:
        self.session_id = ""
        self.health = SceneEngineHealth(
            status=SceneEngineStatus.STOPPED,
            session_id="not-started",
            process_generation="not-started",
            process_id=None,
            last_heartbeat_monotonic=None,
            restart_count=0,
        )
        self.snapshots: list[tuple[str, SceneEngineSnapshot]] = []
        self.preparations: list[tuple[str, BusId, str, int, TransitionSpec]] = []
        self.preparation_content_media_epochs: list[int | None] = []
        self.preparation_content_source_kinds: list[ContentSourceKind] = []
        self.takes: list[tuple[str, ScenePreparation]] = []
        self.outputs: list[tuple[str, BusId, bool]] = []
        self.renders: list[tuple[str, BusId, bool]] = []
        self.window_target_updates: list[
            tuple[str, tuple[OutputWindowTarget, ...]]
        ] = []
        self.preview_geometries = []
        self.cancelled: list[str] = []
        self.stopped = False
        self.listener: Callable[[SceneEngineEvent], None] | None = None
        self.start_deadline_ms = 0

    def subscribe(
        self,
        listener: Callable[[SceneEngineEvent], None],
    ) -> Callable[[], None]:
        self.listener = listener
        return lambda: setattr(self, "listener", None)

    def emit_health(self, status: SceneEngineStatus) -> None:
        assert self.listener is not None
        self.listener(
            EngineHealthEvent(
                SceneEngineHealth(
                    status=status,
                    session_id=self.session_id,
                    process_generation=self.generation,
                    process_id=1234,
                    last_heartbeat_monotonic=10.0,
                    restart_count=1,
                )
            )
        )

    def start(
        self,
        *,
        session_id: str,
        deadline_ms: int,
    ) -> Future[SceneEngineCapabilities]:
        assert deadline_ms > 0
        self.start_deadline_ms = deadline_ms
        self.session_id = session_id
        return _completed(
            SceneEngineCapabilities(
                protocol_version=PROTOCOL_VERSION,
                process_generation=self.generation,
                local_cameras=True,
                rtsp_cameras=True,
                hardware_compositing=True,
                virtual_camera=True,
                d3d11_shared_textures=True,
                program_recording=True,
                audio_input_capture=True,
                system_audio_capture=True,
            )
        )

    def hydrate(
        self,
        snapshot: SceneEngineSnapshot,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        assert deadline_ms > 0
        self.snapshots.append((request_id, snapshot))
        return _completed(self._ack(request_id, snapshot.sequence, snapshot.document.revision))

    def list_local_cameras(
        self,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Future[LocalCameraDiscovery]:
        assert request_id
        assert deadline_ms > 0
        return _completed(
            LocalCameraDiscovery(
                supported=True,
                ready=True,
                generation=1,
                devices=(
                    LocalCameraDevice(
                        device_id="camera://one",
                        display_name="Camera one",
                        software_device=False,
                        formats=(
                            LocalVideoFormat(
                                media_type=CameraMediaType.RAW,
                                pixel_format="NV12",
                                width=1920,
                                height=1080,
                                fps_numerator=30,
                                fps_denominator=1,
                            ),
                        ),
                        probe=LocalCameraProbe(
                            status=LocalCameraProbeStatus.READY,
                            backend="media_foundation",
                        ),
                    ),
                ),
            )
        )

    def prepare_scene(
        self,
        bus_id: BusId,
        scene_id: str,
        *,
        transition: TransitionSpec,
        document_revision: int,
        request_id: str,
        sequence: int,
        deadline_ms: int,
        content_media_epoch: int | None = None,
        content_source_kind: ContentSourceKind = ContentSourceKind.FRAMES,
    ) -> Future[ScenePreparation]:
        assert deadline_ms > 0
        self.preparations.append((request_id, bus_id, scene_id, sequence, transition))
        self.preparation_content_media_epochs.append(content_media_epoch)
        self.preparation_content_source_kinds.append(content_source_kind)
        return _completed(
            ScenePreparation(
                request_id=request_id,
                session_id=self.session_id,
                process_generation=self.generation,
                sequence=sequence,
                document_revision=document_revision,
                bus_id=bus_id,
                scene_id=scene_id,
                preparation_token=f"prepared-{sequence}",
                transition=transition,
            )
        )

    def take_prepared(
        self,
        preparation: ScenePreparation,
        *,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        assert deadline_ms > 0
        self.takes.append((request_id, preparation))
        return _completed(self._ack(request_id, sequence, preparation.document_revision))

    def cancel_preparation(self, request_id: str) -> None:
        self.cancelled.append(request_id)

    def reload_yeartext(self) -> None:
        self.yeartext_reloads = getattr(self, "yeartext_reloads", 0) + 1

    def preview_layer_geometry(
        self,
        bus_id,
        scene_id,
        layer,
        *,
        document_revision,
        request_id,
        sequence,
        deadline_ms,
    ):
        assert deadline_ms > 0
        self.preview_geometries.append(
            (request_id, bus_id, scene_id, layer, document_revision, sequence)
        )
        return _completed(self._ack(request_id, sequence, document_revision))

    def set_output_enabled(
        self,
        bus_id: BusId,
        enabled: bool,
        *,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        assert deadline_ms > 0
        self.outputs.append((request_id, bus_id, enabled))
        revision = self.snapshots[-1][1].document.revision
        return _completed(self._ack(request_id, sequence, revision))

    def set_render_enabled(
        self,
        bus_id: BusId,
        enabled: bool,
        *,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        assert deadline_ms > 0
        self.renders.append((request_id, bus_id, enabled))
        revision = self.snapshots[-1][1].document.revision
        return _completed(self._ack(request_id, sequence, revision))

    def set_window_targets(
        self,
        targets: tuple[OutputWindowTarget, ...],
        *,
        request_id: str,
        sequence: int,
        deadline_ms: int,
    ) -> Future[SceneEngineAck]:
        assert deadline_ms > 0
        self.window_target_updates.append((request_id, targets))
        revision = self.snapshots[-1][1].document.revision
        return _completed(self._ack(request_id, sequence, revision))

    def stop(self) -> None:
        self.stopped = True

    def _ack(self, request_id: str, sequence: int, revision: int) -> SceneEngineAck:
        return SceneEngineAck(
            request_id=request_id,
            session_id=self.session_id,
            process_generation=self.generation,
            sequence=sequence,
            document_revision=revision,
            applied=True,
        )


def _completed(value):
    future = Future()
    future.set_result(value)
    return future


def _failed(error: BaseException):
    future = Future()
    future.set_exception(error)
    return future


class _PendingGeometryEngine(_Engine):
    def __init__(self, *, defer_hydration: bool = False) -> None:
        super().__init__()
        self.defer_hydration = defer_hydration
        self.hydration_futures: list[Future[SceneEngineAck]] = []
        self.geometry_futures: list[Future[SceneEngineAck]] = []

    def hydrate(self, snapshot, *, request_id, deadline_ms):
        completed = super().hydrate(snapshot, request_id=request_id, deadline_ms=deadline_ms)
        if not self.defer_hydration:
            return completed
        future: Future[SceneEngineAck] = Future()
        self.hydration_futures.append(future)
        return future

    def finish_hydration(self, index: int) -> None:
        request_id, snapshot = self.snapshots[index]
        self.hydration_futures[index].set_result(
            self._ack(request_id, snapshot.sequence, snapshot.document.revision)
        )

    def preview_layer_geometry(self, *args, **kwargs):
        super().preview_layer_geometry(*args, **kwargs)
        future: Future[SceneEngineAck] = Future()
        self.geometry_futures.append(future)
        return future

    def finish_geometry(self, index: int) -> None:
        request_id, _bus, _scene, _layer, revision, sequence = self.preview_geometries[index]
        self.geometry_futures[index].set_result(self._ack(request_id, sequence, revision))


@pytest.mark.parametrize("geometry", ["rect", "crop"])
@pytest.mark.parametrize(
    ("scene_id", "layer_id"),
    [(scene.id, layer.id) for scene in _document().scenes for layer in scene.layers],
)
def test_committed_geometry_reaches_every_layer_without_preview_or_rehydrate(
    request,
    scene_id: str, layer_id: str, geometry: str,
) -> None:
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate", "committed-geometry"),
    )
    controller.start_engine()
    layer = next(layer for layer in documents.document.scene(scene_id).layers if layer.id == layer_id)
    updated = replace(layer, **{
        geometry: NormalizedRect(x=0.2, y=0.1, width=0.5, height=0.6)
        if geometry == "rect" else Crop(left=0.1, bottom=0.2),
    })

    documents.update_layer(scene_id, layer_id, updated)
    documents.undo()
    documents.redo()

    assert [item[3] for item in engine.preview_geometries] == [updated, layer, updated]
    assert all(item[2] == scene_id for item in engine.preview_geometries)
    assert all(item[4] == engine.snapshots[0][1].document.revision for item in engine.preview_geometries)
    assert len(engine.snapshots) == 1


def test_batch_geometry_commit_undo_redo_sends_all_layers(request) -> None:
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    updated_layers = tuple(
        replace(layer, crop=Crop(left=0.1), rect=NormalizedRect(width=0.7, height=0.8))
        for layer in scene.layers
    )

    documents.update_scene(scene.id, replace(scene, layers=updated_layers))
    documents.undo()
    documents.redo()

    assert [item[3] for item in engine.preview_geometries] == list(
        updated_layers + scene.layers + updated_layers
    )
    assert len(engine.snapshots) == 1


@pytest.mark.parametrize("geometry", ["rect", "crop"])
def test_hidden_geometry_waits_until_visibility_hydrates_the_authored_layout(request, geometry: str) -> None:
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    hidden = replace(scene.layers[0], visible=False)
    documents.update_layer(scene.id, hidden.id, hidden)
    baseline = len(engine.snapshots)
    updated = replace(hidden, **{
        geometry: NormalizedRect(width=0.7, height=0.8)
        if geometry == "rect" else Crop(left=0.1),
    })

    documents.update_layer(scene.id, hidden.id, updated)
    documents.undo()
    documents.redo()
    controller.preview_layer_geometry(scene.id, updated)

    assert engine.preview_geometries == []
    assert len(engine.snapshots) == baseline
    assert errors == []
    revealed = replace(updated, visible=True)
    documents.update_layer(scene.id, hidden.id, revealed)
    assert len(engine.snapshots) == baseline + 1
    assert engine.snapshots[-1][1].document.scene(scene.id).layers[0] == revealed


def test_batch_geometry_history_sends_only_visible_layers(request) -> None:
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    documents.update_layer(scene.id, scene.layers[0].id, replace(scene.layers[0], visible=False))
    scene = documents.document.scene(scene.id)
    baseline = len(engine.snapshots)
    updated = tuple(replace(layer, crop=Crop(left=0.2)) for layer in scene.layers)

    documents.update_scene(scene.id, replace(scene, layers=updated))
    documents.undo()
    documents.redo()

    assert [item[3] for item in engine.preview_geometries] == [
        updated[1], scene.layers[1], updated[1],
    ]
    assert len(engine.snapshots) == baseline


def test_geometry_queue_preserves_other_layer_commits_and_latest_mouse_move(request) -> None:
    engine = _PendingGeometryEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    first, second = scene.layers[:2]
    controller.preview_layer_geometry(scene.id, replace(first, rect=replace(first.rect, x=0.1)))
    committed = replace(second, crop=Crop(left=0.2))
    documents.update_layer(scene.id, second.id, committed)
    controller.preview_layer_geometry(scene.id, replace(first, rect=replace(first.rect, x=0.2)))
    latest = replace(first, rect=replace(first.rect, x=0.3))
    controller.preview_layer_geometry(scene.id, latest)
    documents.update_layer(scene.id, first.id, latest)

    engine.finish_geometry(0)
    assert engine.preview_geometries[1][3] == committed
    engine.finish_geometry(1)
    assert engine.preview_geometries[2][3] == latest
    engine.finish_geometry(2)
    assert len(engine.preview_geometries) == 3
    assert len(engine.snapshots) == 1


def test_batch_undo_during_pending_geometry_keeps_all_layer_restorations(request) -> None:
    engine = _PendingGeometryEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    documents.update_scene(
        scene.id,
        replace(scene, layers=tuple(replace(layer, crop=Crop(left=0.2)) for layer in scene.layers)),
    )

    documents.undo()
    engine.finish_geometry(0)
    engine.finish_geometry(1)
    engine.finish_geometry(2)

    restored = {item[3].id: item[3] for item in engine.preview_geometries}
    assert restored == {layer.id: layer for layer in scene.layers}
    assert len(engine.preview_geometries) == 3
    assert len(engine.snapshots) == 1


@pytest.mark.parametrize("graph_changes", [False, True])
def test_geometry_waits_for_hydration_and_uses_the_applied_revision(request, graph_changes: bool) -> None:
    engine = _PendingGeometryEngine(defer_hydration=True)
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    first, second = scene.layers[:2]
    if graph_changes:
        documents.rename_scene(scene.id, "Renamed scene")
    committed = replace(first, crop=Crop(left=0.2))
    documents.update_layer(scene.id, first.id, committed)
    preview = replace(second, rect=replace(second.rect, x=0.3))
    controller.preview_layer_geometry(scene.id, preview)

    assert engine.preview_geometries == []
    engine.finish_hydration(0)
    if graph_changes:
        assert engine.preview_geometries == []
        assert len(engine.snapshots) == 2
        engine.finish_hydration(1)
    revision = engine.snapshots[-1][1].document.revision
    assert engine.preview_geometries[0][3:5] == (committed, revision)
    engine.finish_geometry(0)
    assert engine.preview_geometries[1][3:5] == (preview, revision)
    engine.finish_geometry(1)
    assert len(engine.snapshots) == (2 if graph_changes else 1)


def test_graph_hydration_supersedes_queued_geometry_for_deleted_layers(request) -> None:
    engine = _PendingGeometryEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    first, second = scene.layers[:2]
    controller.preview_layer_geometry(scene.id, replace(first, crop=Crop(left=0.1)))
    controller.preview_layer_geometry(scene.id, replace(first, crop=Crop(left=0.2)))
    committed = replace(second, crop=Crop(left=0.3))
    documents.update_layer(scene.id, second.id, committed)
    documents.delete_layer(scene.id, first.id)
    latest = replace(committed, crop=Crop(left=0.4))
    documents.update_layer(scene.id, second.id, latest)

    assert len(engine.snapshots) == 1
    engine.finish_geometry(0)

    assert len(engine.snapshots) == 2
    assert engine.snapshots[-1][1].document.scene(scene.id).layers == (latest,)
    assert engine.preview_geometries[1][3] == latest
    assert engine.preview_geometries[1][4] == engine.snapshots[-1][1].document.revision
    engine.finish_geometry(1)
    assert len(engine.preview_geometries) == 2


@pytest.mark.parametrize("emit_failure", [False, True])
def test_restart_hydrates_commits_and_ignores_old_geometry_completion(request, emit_failure: bool) -> None:
    engine = _PendingGeometryEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    first, second = scene.layers[:2]
    committed = replace(first, crop=Crop(left=0.1))
    documents.update_layer(scene.id, first.id, committed)
    other_commit = replace(second, crop=Crop(left=0.2))
    documents.update_layer(scene.id, second.id, other_commit)
    if emit_failure:
        engine.emit_health(SceneEngineStatus.FAILED)
    engine.generation = "engine-generation-2"
    engine.emit_health(SceneEngineStatus.READY)

    assert len(engine.snapshots) == 2
    assert engine.snapshots[-1][1].document.scene(scene.id).layers == (committed, other_commit)
    assert len(engine.preview_geometries) == 1
    latest = replace(other_commit, crop=Crop(left=0.3))
    documents.update_layer(scene.id, second.id, latest)
    engine.geometry_futures[0].set_exception(RuntimeError("stale sensitive error"))
    assert errors == []
    engine.finish_geometry(1)
    assert len(engine.preview_geometries) == 2
    assert engine.preview_geometries[1][3] == latest


@pytest.mark.parametrize("failure", ["rejected", "exception", "invalid", "cancelled", "timeout"])
@pytest.mark.parametrize("committed", [False, True])
def test_geometry_failure_reports_commits_and_continues_other_layers(
    request,
    failure: str, committed: bool,
) -> None:
    engine = _PendingGeometryEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    first, second = scene.layers[:2]
    updated = replace(first, crop=Crop(left=0.1))
    if committed:
        documents.update_layer(scene.id, first.id, updated)
    else:
        controller.preview_layer_geometry(scene.id, updated)
    other_commit = replace(second, crop=Crop(left=0.2))
    documents.update_layer(scene.id, second.id, other_commit)
    def fail_geometry(index: int) -> None:
        request_id, _bus, _scene, _layer, revision, sequence = engine.preview_geometries[index]
        ack = engine._ack(request_id, sequence, revision)
        future = engine.geometry_futures[index]
        if failure == "rejected":
            future.set_result(
                replace(ack, applied=False, error_code="unknown_layer", error_message="sensitive detail")
            )
        elif failure == "invalid":
            future.set_result(replace(ack, document_revision=revision + 1))
        elif failure == "cancelled":
            future.cancel()
        elif failure == "timeout":
            future.set_exception(SceneEngineRequestTimeoutError("sensitive detail"))
        else:
            future.set_exception(RuntimeError("sensitive detail"))

    fail_geometry(0)
    assert errors == []
    assert engine.preview_geometries[1][3] == other_commit
    engine.finish_geometry(1)
    if committed:
        assert engine.preview_geometries[2][3] == updated
        fail_geometry(2)
    assert len(errors) == (1 if committed else 0)
    assert all("sensitive" not in error for error in errors)
    if errors:
        assert controller.last_engine_error_code == (
            "unknown_layer" if failure == "rejected" else
            "engine_request_timed_out" if failure == "timeout" else
            "unexpected_engine_response"
        )
    assert len(engine.preview_geometries) == (3 if committed else 2)
    assert len(engine.snapshots) == 1


def test_synchronous_geometry_dispatch_failure_does_not_wedge_the_queue(request) -> None:
    class _RaisingGeometryEngine(_Engine):
        def preview_layer_geometry(self, *args, **kwargs):
            if not self.preview_geometries:
                super().preview_layer_geometry(*args, **kwargs)
                raise SceneEngineCommandRejectedError("unknown_layer")
            return super().preview_layer_geometry(*args, **kwargs)

    engine = _RaisingGeometryEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    updated = replace(scene, layers=tuple(replace(layer, crop=Crop(left=0.1)) for layer in scene.layers))

    documents.update_scene(scene.id, updated)

    assert [item[3] for item in engine.preview_geometries] == [*updated.layers, updated.layers[0]]
    assert errors == []
    assert len(engine.snapshots) == 1


@pytest.mark.parametrize("recovers", [False, True])
def test_committed_geometry_failure_retries_once_without_starving_other_layers(request, recovers: bool) -> None:
    engine = _PendingGeometryEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()
    scene = documents.document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    first, second = scene.layers[:2]
    committed = replace(first, crop=Crop(left=0.1))
    other = replace(second, crop=Crop(left=0.2))
    documents.update_layer(scene.id, first.id, committed)
    documents.update_layer(scene.id, second.id, other)

    engine.geometry_futures[0].set_exception(SceneEngineRequestTimeoutError("sensitive detail"))
    assert engine.preview_geometries[1][3] == other
    engine.finish_geometry(1)
    assert [item[3] for item in engine.preview_geometries] == [committed, other, committed]
    assert errors == []
    if recovers:
        engine.finish_geometry(2)
        assert controller.last_engine_error_code == ""
    else:
        engine.geometry_futures[2].set_exception(SceneEngineRequestTimeoutError("sensitive detail"))
        assert errors == ["Scene engine layer_geometry failed (engine_request_timed_out)"]
    assert len(engine.preview_geometries) == 3
    assert len(engine.snapshots) == 1


def test_failed_commit_does_not_replay_geometry_superseded_by_a_newer_commit(request) -> None:
    engine = _PendingGeometryEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()
    scene = documents.document.scene(CONTENT_SCENE_ID)
    first = replace(scene.layers[0], crop=Crop(left=0.1))
    latest = replace(first, crop=Crop(left=0.2))
    documents.update_layer(scene.id, first.id, first)
    documents.update_layer(scene.id, first.id, latest)

    engine.geometry_futures[0].set_exception(SceneEngineRequestTimeoutError("timeout"))
    engine.finish_geometry(1)

    assert [item[3] for item in engine.preview_geometries] == [first, latest]
    assert errors == []
    assert len(engine.snapshots) == 1


@pytest.mark.parametrize("outcome", ["applied", "rejected", "failed"])
def test_profile_hydration_holds_geometry_until_the_profile_outcome(
    scene_workspace_factory,
    request,
    tmp_path: Path,
    outcome: str,
) -> None:
    workspace, collection = _real_workspace(scene_workspace_factory, tmp_path)
    engine = _PendingGeometryEngine()
    ids = iter(("hydrate", "profile", "geometry", "new-geometry"))
    controller = SceneRuntimeController(
        workspace, _Projection(), engine=cast(SceneEngine, engine),
        request_id_factory=lambda: next(ids), session_id="test-session",
    )
    request.addfinalizer(controller.close)
    controller.start_engine()
    engine.defer_hydration = True
    controller.activate_scene_profile(collection.id)
    scene = controller.document.scene(CONTENT_SCENE_ID)
    committed = replace(scene.layers[0], crop=Crop(left=0.1))
    controller.documents.update_layer(scene.id, committed.id, committed)
    assert engine.preview_geometries == []
    request_id, snapshot = engine.snapshots[-1]
    ack = engine._ack(request_id, snapshot.sequence, snapshot.document.revision)
    if outcome == "failed":
        engine.hydration_futures[0].set_exception(SceneEngineRequestTimeoutError("timeout"))
    else:
        engine.hydration_futures[0].set_result(
            ack if outcome == "applied" else replace(ack, applied=False, error_code="unknown_layer")
        )

    if outcome == "applied":
        assert engine.preview_geometries == []
        assert workspace.active_collection.id == collection.id
        new_scene = controller.document.scene(CONTENT_SCENE_ID)
        latest = replace(new_scene.layers[0], crop=Crop(left=0.2))
        controller.documents.update_layer(new_scene.id, latest.id, latest)
        assert engine.preview_geometries[0][3] == latest
    else:
        assert engine.preview_geometries[0][3] == committed
        assert engine.preview_geometries[0][4] == engine.snapshots[0][1].document.revision
    engine.finish_geometry(0)
    assert len(engine.snapshots) == 2


def test_preview_geometry_coalesces_mouse_moves_without_hydrating_each_frame(request) -> None:
    class _PendingPreviewEngine(_Engine):
        def __init__(self) -> None:
            super().__init__()
            self.preview_futures: list[Future[SceneEngineAck]] = []

        def preview_layer_geometry(self, *args, **kwargs):
            future: Future[SceneEngineAck] = Future()
            self.preview_futures.append(future)
            super().preview_layer_geometry(*args, **kwargs)
            return future

    engine = _PendingPreviewEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("hydrate", "preview-1", "preview-2"),
    )
    controller.start_engine()
    scene = documents.document.scenes[0]
    layer = scene.layers[0]

    controller.preview_layer_geometry(
        scene.id,
        replace(layer, rect=replace(layer.rect, x=0.1)),
    )
    controller.preview_layer_geometry(
        scene.id,
        replace(layer, rect=replace(layer.rect, x=0.2)),
    )
    controller.preview_layer_geometry(
        scene.id,
        replace(layer, rect=replace(layer.rect, x=0.3)),
    )
    documents.update_layer(
        scene.id,
        layer.id,
        replace(layer, rect=replace(layer.rect, x=0.3)),
    )

    assert len(engine.preview_geometries) == 1
    assert len(engine.snapshots) == 1
    first = engine.preview_geometries[0]
    engine.preview_futures[0].set_result(
        engine._ack(first[0], first[5], first[4])
    )

    assert len(engine.preview_geometries) == 2
    assert engine.preview_geometries[-1][3].rect.x == 0.3
    assert len(engine.snapshots) == 1

    second = engine.preview_geometries[-1]
    engine.preview_futures[-1].set_result(
        engine._ack(second[0], second[5], second[4])
    )
    # Committing the geometry must NOT re-hydrate: the final transform was already
    # applied live by the coalesced preview, so the graph is never rebuilt (which
    # would re-open every source, e.g. cameras).
    assert len(engine.snapshots) == 1


class _PtzExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.futures: list[Future[PtzRecallResult]] = []
        self.cancelled: list[Future[PtzRecallResult]] = []
        self.moves: list[tuple[str, float, float, float]] = []
        self.stops: list[str] = []
        self.stored: list[str] = []
        self.closed = False

    def recall(
        self,
        *,
        camera_source_id,
        binding,
        preset,
        timeout_ms,
    ) -> Future[PtzRecallResult]:
        assert isinstance(binding, OnvifPtzBinding)
        self.calls.append((camera_source_id, preset.id, timeout_ms))
        future: Future[PtzRecallResult] = Future()
        self.futures.append(future)
        return future

    def cancel(self, future: Future[PtzRecallResult]) -> None:
        self.cancelled.append(future)

    def move(
        self,
        *,
        camera_source_id,
        binding,
        pan,
        tilt,
        zoom,
    ):
        del binding
        self.moves.append((camera_source_id, pan, tilt, zoom))
        return _completed(
            PtzControlResult(
                request_id="move",
                camera_source_id=camera_source_id,
                kind=PtzControlKind.MOVE,
                status=PtzRecallStatus.SUCCEEDED,
            )
        )

    def stop(self, *, camera_source_id, binding):
        del binding
        self.stops.append(camera_source_id)
        return _completed(
            PtzControlResult(
                request_id="stop",
                camera_source_id=camera_source_id,
                kind=PtzControlKind.STOP,
                status=PtzRecallStatus.SUCCEEDED,
            )
        )

    def store_preset(self, *, camera_source_id, binding, preset):
        del binding
        self.stored.append(preset.id)
        return _completed(
            PtzControlResult(
                request_id="store",
                camera_source_id=camera_source_id,
                kind=PtzControlKind.STORE_PRESET,
                status=PtzRecallStatus.SUCCEEDED,
            )
        )

    def close(self) -> None:
        self.closed = True


def _document_with_ptz_action(
    policy: PtzTimeoutPolicy,
) -> SceneDocument:
    document = _document()
    source = document.source(DEFAULT_CAMERA_SOURCE_ID)
    assert isinstance(source.configuration, LocalCameraConfig)
    source = replace(
        source,
        configuration=replace(
            source.configuration,
            ptz_binding=OnvifPtzBinding(
                endpoint="https://camera.test/onvif/ptz",
                profile_token="profile-1",
            ),
        ),
    )
    preset = CameraPreset(
        id="preset-wide",
        camera_source_id=source.id,
        name="Wide",
        remote_token="remote-wide",
    )
    scene = document.scene(CONTENT_SCENE_ID)
    scene = replace(
        scene,
        entry_actions=(
            RecallPtzPresetAction(
                preset_id=preset.id,
                timeout_ms=2500,
                on_timeout=policy,
            ),
        ),
    )
    return replace(
        document,
        sources=tuple(source if item.id == source.id else item for item in document.sources),
        scenes=tuple(scene if item.id == scene.id else item for item in document.scenes),
        camera_presets=(preset,),
    )


def _ptz_result(status: PtzRecallStatus, error_code: str = "") -> PtzRecallResult:
    return PtzRecallResult(
        request_id="ptz-request",
        camera_source_id=DEFAULT_CAMERA_SOURCE_ID,
        preset_id="preset-wide",
        status=status,
        error_code=error_code,
    )


def _runtime_controller(
    request,
    engine: _Engine,
    projection: _Projection,
    *,
    request_ids: tuple[str, ...],
    document: SceneDocument | None = None,
    ptz=None,
    cleanup: ExitStack | None = None,
) -> tuple[SceneDocumentService, SceneRuntimeService, SceneRuntimeController]:
    documents = SceneDocumentService(document or _document())
    runtime = SceneRuntimeService(
        documents,
        create_default_runtime_state(documents.document),
    )
    cleanup = ExitStack() if cleanup is None else cleanup
    request.addfinalizer(cleanup.close)
    cleanup.callback(runtime.close)

    def _ids():
        # Named ids first, then anonymous ones. Three buses means more traffic than
        # a fixture can sensibly enumerate, and running out used to abort dispatch
        # silently (SceneRuntimeService._commit swallows listener exceptions),
        # which quietly changed what a test was exercising.
        yield from request_ids
        counter = 0
        while True:
            counter += 1
            yield f"auto-{counter}"

    ids = _ids()
    controller = SceneRuntimeController(
        _Workspace(documents, runtime),
        projection,
        engine=engine,
        ptz=ptz,
        request_id_factory=lambda: next(ids),
        session_id="test-session",
    )
    # Once constructed, the controller owns the runtime through its workspace.
    cleanup.pop_all()
    cleanup.callback(controller.close)
    return documents, runtime, controller


def _deliver_controller_results(controller: SceneRuntimeController) -> None:
    """Deliver this controller's queued callbacks until transactions stop advancing."""
    for _ in range(100):
        previous = dict(controller._pending)
        QCoreApplication.sendPostedEvents(controller, QEvent.Type.MetaCall)
        if controller._pending == previous:
            return
    pytest.fail("Scene controller did not settle after delivering queued results")


def test_runtime_controller_cleanup_releases_subscriptions_after_an_assertion() -> None:
    engine = _Engine()
    projection = _Projection()
    with pytest.raises(AssertionError, match="simulated runtime assertion failure"):
        with ExitStack() as finalizers:
            request = SimpleNamespace(addfinalizer=finalizers.callback)
            _documents, _runtime, controller = _runtime_controller(
                request,
                engine,
                projection,
                request_ids=("hydrate",),
            )
            controller.start_engine()
            assert engine.listener is not None
            assert projection._listeners
            raise AssertionError("simulated runtime assertion failure")

    assert engine.stopped
    assert engine.listener is None
    assert not projection._listeners


def _preview_egress_descriptor() -> FrameChannelDescriptor:
    return FrameChannelDescriptor(
        channel_id="scene-preview",
        generation=1,
        producer_kind=FrameProducerKind.NATIVE_COMPOSITOR,
        transport=FrameChannelTransport.SHARED_MEMORY_BGRA,
        handle_token="scene-preview-mapping",
        width=1920,
        height=1080,
        pixel_format=VideoPixelFormat.BGRA,
        color_space=VideoColorSpace.SRGB,
        color_range=VideoColorRange.FULL,
    )


def _content_ingress_descriptor(
    transport: FrameChannelTransport,
    *,
    generation: int,
) -> FrameChannelDescriptor:
    return FrameChannelDescriptor(
        channel_id="content-ingress",
        generation=generation,
        producer_kind=FrameProducerKind.SOLIN_OFFSCREEN,
        transport=transport,
        handle_token=f"content-ingress-{generation}",
        width=1920,
        height=1080,
        pixel_format=(
            VideoPixelFormat.NV12
            if transport is FrameChannelTransport.D3D11_SHARED_TEXTURE
            else VideoPixelFormat.DYNAMIC
        ),
        color_space=(
            VideoColorSpace.BT709
            if transport is FrameChannelTransport.D3D11_SHARED_TEXTURE
            else VideoColorSpace.SRGB
        ),
        color_range=(
            VideoColorRange.LIMITED
            if transport is FrameChannelTransport.D3D11_SHARED_TEXTURE
            else VideoColorRange.FULL
        ),
    )


def test_content_transport_hydration_preserves_live_program_before_animated_take(request) -> None:
    class _PendingPrepareEngine(_Engine):
        def __init__(self) -> None:
            super().__init__()
            self.prepare_futures: list[Future[ScenePreparation]] = []

        def prepare_scene(self, *args, **kwargs):
            super().prepare_scene(*args, **kwargs)
            future: Future[ScenePreparation] = Future()
            self.prepare_futures.append(future)
            return future

    projection = _Projection()
    engine = _PendingPrepareEngine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(
            "initial-hydrate",
            "prepare-program",
            "transport-hydrate",
            "prepare-program-current-transport",
        ),
    )
    controller.set_content_ingress(
        _content_ingress_descriptor(
            FrameChannelTransport.SHARED_MEMORY_VIDEO,
            generation=1,
        )
    )
    controller.start_engine()
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID

    projection.set_type("video")
    controller.set_content_ingress(
        _content_ingress_descriptor(
            FrameChannelTransport.D3D11_SHARED_TEXTURE,
            generation=2,
        )
    )

    _, handoff = engine.snapshots[-1]
    assert dict(handoff.active_scenes)[BusId.VIRTUAL_CAMERA] == CAMERA_SCENE_ID
    assert engine.cancelled == ["prepare-program"]
    assert engine.preparations[-1][0] == "prepare-program-current-transport"
    assert engine.preparations[-1][1] is BusId.VIRTUAL_CAMERA
    assert engine.preparations[-1][-1].kind is not TransitionKind.CUT


def test_content_transport_retirement_waits_for_the_animated_program_exit(request) -> None:
    class _SwitchablePrepareEngine(_Engine):
        def __init__(self) -> None:
            super().__init__()
            self.defer_preparation = False
            self.prepare_futures: list[Future[ScenePreparation]] = []

        def prepare_scene(self, *args, **kwargs):
            completed = super().prepare_scene(*args, **kwargs)
            if not self.defer_preparation:
                return completed
            future: Future[ScenePreparation] = Future()
            self.prepare_futures.append(future)
            return future

    projection = _Projection()
    engine = _SwitchablePrepareEngine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(
            "initial-hydrate",
            "prepare-program-exit",
            "take-program-exit",
            "transport-hydrate",
            "prepare-media-exit",
            "take-media-exit",
        ),
    )
    projection.set_type("video")
    controller.set_content_ingress(
        _content_ingress_descriptor(
            FrameChannelTransport.D3D11_SHARED_TEXTURE,
            generation=1,
        )
    )
    controller.start_engine()
    _deliver_controller_results(controller)
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    engine.defer_preparation = True

    projection.set_type("idle")
    controller.set_content_ingress(
        _content_ingress_descriptor(
            FrameChannelTransport.SHARED_MEMORY_VIDEO,
            generation=2,
        )
    )

    assert len(engine.snapshots) == 1
    assert engine.cancelled == []
    request_id, bus_id, scene_id, sequence, transition = engine.preparations[-1]
    engine.prepare_futures[-1].set_result(
        ScenePreparation(
            request_id=request_id,
            session_id=engine.session_id,
            process_generation=engine.generation,
            sequence=sequence,
            document_revision=controller.document.revision,
            bus_id=bus_id,
            scene_id=scene_id,
            preparation_token="prepared-program-exit",
            transition=transition,
        )
    )

    assert engine.takes[0][0] == "take-program-exit"
    assert len(engine.snapshots) == 2
    assert dict(engine.snapshots[-1][1].active_scenes)[BusId.VIRTUAL_CAMERA] == (
        CAMERA_SCENE_ID
    )


def test_projection_categories_are_explicit_and_unknown_types_fail_safe() -> None:
    assert content_category_for_projection({"type": "idle"}) is ContentCategory.IDLE
    assert content_category_for_projection(
        {"type": "video", "is_audio": True}
    ) is ContentCategory.IDLE
    assert content_category_for_projection(
        {"type": "video", "is_audio": False}
    ) is ContentCategory.VIDEO
    assert content_category_for_projection({"type": "removed_projection_type"}) is (
        ContentCategory.EXTERNAL_STREAM
    )


@pytest.mark.parametrize("state", list(MediaPlaybackState))
@pytest.mark.parametrize("slot", [0, 1])
def test_playback_events_are_forwarded_without_changing_engine_health(request, state, slot) -> None:
    engine = _Engine()
    cleanup = ExitStack()
    _documents, runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=(), cleanup=cleanup,
    )
    try:
        controller.start_engine()
        assert engine.listener is not None
        source_health = SourceHealthEvent(
            source_id=DEFAULT_CAMERA_SOURCE_ID,
            status=SourceHealthStatus.FAILED,
            error_code="local_camera_stream_failed",
        )
        engine.listener(source_health)
        events: list[SceneEngineEvent] = []
        readiness: list[bool] = []
        controller.engine_event.connect(events.append)
        controller.engine_ready_changed.connect(readiness.append)
        snapshot_count = len(engine.snapshots)
        event = MediaPlaybackEvent(MediaPlaybackNativeState(
            state=state, position_ms=1200, duration_ms=5000,
            error_code="media_open_failed" if state is MediaPlaybackState.ERROR else "",
            slot=slot,
        ))

        engine.listener(event)

        assert controller.last_engine_error_code == ""
        assert events == [event]
        assert controller.engine_ready
        assert readiness == []
        assert len(engine.snapshots) == snapshot_count
        assert controller.source_health(DEFAULT_CAMERA_SOURCE_ID) == source_health
    finally:
        cleanup.close()


def test_source_health_is_retained_until_ready_or_stopped(request) -> None:
    engine = _Engine()
    _documents, runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("hydrate",),
    )
    controller.start_engine()
    assert engine.listener is not None

    failed = SourceHealthEvent(
        source_id=DEFAULT_CAMERA_SOURCE_ID,
        status=SourceHealthStatus.FAILED,
        error_code="local_camera_stream_failed",
    )
    engine.listener(failed)
    assert controller.source_health(DEFAULT_CAMERA_SOURCE_ID) == failed

    ready = SourceHealthEvent(
        source_id=DEFAULT_CAMERA_SOURCE_ID,
        status=SourceHealthStatus.READY,
    )
    engine.listener(ready)
    assert controller.source_health(DEFAULT_CAMERA_SOURCE_ID) == ready

    engine.listener(
        SourceHealthEvent(
            source_id=DEFAULT_CAMERA_SOURCE_ID,
            status=SourceHealthStatus.STOPPED,
        )
    )
    assert controller.source_health(DEFAULT_CAMERA_SOURCE_ID) is None
    assert content_category_for_projection({"type": "future_source"}) is (
        ContentCategory.EXTERNAL_STREAM
    )


def test_scene_profile_is_published_only_after_native_hydration_ack(
    scene_workspace_factory, request, tmp_path: Path
) -> None:
    class _PendingProfileEngine(_Engine):
        def __init__(self) -> None:
            super().__init__()
            self.profile_hydration: Future[SceneEngineAck] = Future()

        def hydrate(self, snapshot, *, request_id, deadline_ms):
            if not self.snapshots:
                return super().hydrate(
                    snapshot,
                    request_id=request_id,
                    deadline_ms=deadline_ms,
                )
            self.snapshots.append((request_id, snapshot))
            return self.profile_hydration

    workspace, collection = _real_workspace(scene_workspace_factory, tmp_path)
    previous_id = workspace.active_collection.id
    engine = _PendingProfileEngine()
    request_ids = iter(("initial-hydrate", "profile-hydrate"))
    controller = SceneRuntimeController(
        workspace,
        _Projection(),
        engine=engine,
        request_id_factory=lambda: next(request_ids),
        session_id="test-session",
    )
    request.addfinalizer(controller.close)
    controller.set_preview_scene(CONTENT_SCENE_ID)
    published_document_ids: list[str] = []
    controller.scene_profiles_changed.connect(
        lambda _change: published_document_ids.append(controller.document.document_id)
    )
    controller.start_engine()

    controller.activate_scene_profile(collection.id)

    assert workspace.active_collection.id == previous_id
    request_id, snapshot = engine.snapshots[-1]
    engine.profile_hydration.set_result(
        SceneEngineAck(
            request_id=request_id,
            session_id=engine.session_id,
            process_generation=engine.generation,
            sequence=snapshot.sequence,
            document_revision=snapshot.document.revision,
            applied=True,
        )
    )

    assert workspace.active_collection.id == collection.id
    assert engine.snapshots[-1][1].document.document_id == "document-b"
    assert dict(engine.snapshots[-1][1].active_scenes)[BusId.EDITOR] == (
        CONTENT_SCENE_ID
    )
    assert controller.preview_scene_id == CONTENT_SCENE_ID
    assert published_document_ids[-1] == "document-b"
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == DEFAULT_SCENE_ID


def test_rejected_scene_profile_hydration_preserves_the_previous_profile(
    scene_workspace_factory,
    request,
    tmp_path: Path,
) -> None:
    class _RejectingProfileEngine(_Engine):
        def hydrate(self, snapshot, *, request_id, deadline_ms):
            if not self.snapshots:
                return super().hydrate(
                    snapshot,
                    request_id=request_id,
                    deadline_ms=deadline_ms,
                )
            self.snapshots.append((request_id, snapshot))
            return _completed(
                SceneEngineAck(
                    request_id=request_id,
                    session_id=self.session_id,
                    process_generation=self.generation,
                    sequence=snapshot.sequence,
                    document_revision=snapshot.document.revision,
                    applied=False,
                    error_code="source_unavailable",
                )
            )

    workspace, collection = _real_workspace(scene_workspace_factory, tmp_path)
    previous_id = workspace.active_collection.id
    engine = _RejectingProfileEngine()
    request_ids = iter(("initial-hydrate", "profile-hydrate"))
    controller = SceneRuntimeController(
        workspace,
        _Projection(),
        engine=engine,
        request_id_factory=lambda: next(request_ids),
        session_id="test-session",
    )
    request.addfinalizer(controller.close)
    controller.start_engine()

    controller.activate_scene_profile(collection.id)

    assert workspace.active_collection.id == previous_id
    assert controller.document.document_id == "default-document"
    assert controller.last_engine_error_code == "source_unavailable"


def _real_workspace(scene_workspace_factory, tmp_path: Path):
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="profile-a",
    )
    paths.ensure_dirs()
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = scene_workspace_factory(
        paths,
        seed_names=SceneSeedNames(
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
        identity_factory=lambda: next(identities),
    )
    collection = workspace.create_collection("Auditorium")
    return workspace, collection


def test_live_scene_deletion_requires_and_takes_the_chosen_replacement(request) -> None:
    documents, runtime, controller = _runtime_controller(
        request,
        _Engine(),
        _Projection(),
        request_ids=(),
    )
    runtime.set_output_enabled(BusId.VIRTUAL_CAMERA, True)

    assert controller.program_scene_is_live(CAMERA_SCENE_ID)
    with pytest.raises(SceneValidationError, match="live scene requires a replacement"):
        controller.delete_scene(CAMERA_SCENE_ID)

    controller.delete_scene(
        CAMERA_SCENE_ID,
        live_replacement_scene_id=CONTENT_CAMERA_PIP_SCENE_ID,
    )

    assert all(scene.id != CAMERA_SCENE_ID for scene in documents.document.scenes)
    assert all(output.mode.value == "manual" for output in runtime.state.outputs)
    assert {
        output.manual_scene_id for output in runtime.state.outputs
    } == {CONTENT_CAMERA_PIP_SCENE_ID}


def test_runtime_refreshes_local_cameras_without_blocking_the_caller(request) -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("hydrate", "list-cameras"),
    )
    controller.start_engine()

    future = controller.refresh_local_cameras()

    assert engine.start_deadline_ms == DEFAULT_ENGINE_STARTUP_DEADLINE_MS
    assert future is not None
    assert controller.local_cameras.supported
    assert controller.local_cameras.devices[0].device_id == "camera://one"


def test_runtime_coalesces_camera_refreshes_while_discovery_is_in_flight(request) -> None:
    class _PendingCameraEngine(_Engine):
        def __init__(self) -> None:
            super().__init__()
            self.camera_requests: list[str] = []
            self.camera_future: Future[LocalCameraDiscovery] = Future()

        def list_local_cameras(
            self,
            *,
            request_id: str,
            deadline_ms: int,
        ) -> Future[LocalCameraDiscovery]:
            assert deadline_ms > 0
            self.camera_requests.append(request_id)
            return self.camera_future

    engine = _PendingCameraEngine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("hydrate", "list-cameras"),
    )
    controller.start_engine()

    first = controller.refresh_local_cameras()
    second = controller.refresh_local_cameras()

    assert first is engine.camera_future
    assert second is first
    assert engine.camera_requests == ["list-cameras"]


def test_reload_yeartext_forwards_a_notification_to_the_engine(request) -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine, projection, request_ids=("hydrate",)
    )

    controller.reload_yeartext()

    assert getattr(engine, "yeartext_reloads", 0) == 1


def test_committing_a_layer_resize_does_not_rehydrate(request) -> None:
    # Resizing/moving a source in the canvas must NOT rebuild the scene graph
    # (which re-opens every source, e.g. the camera). It should be applied live.
    projection = _Projection()
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine, projection,
        request_ids=("hydrate",) + tuple(f"take-{i}" for i in range(20)),
    )
    controller.start_engine()
    assert controller.engine_ready
    baseline = len(engine.snapshots)

    scene = documents.document.scene(CONTENT_SCENE_ID)
    layer = scene.layers[0]
    documents.update_layer(
        CONTENT_SCENE_ID,
        layer.id,
        replace(layer, rect=NormalizedRect(x=0.3, y=0.3, width=0.4, height=0.4)),
    )

    assert len(engine.snapshots) == baseline, (
        "a layer resize must not re-hydrate the engine"
    )


def test_runtime_prepares_and_takes_without_full_snapshot_on_hot_path(request) -> None:
    projection = _Projection()
    engine = _Engine()
    cleanup = ExitStack()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(
            "hydrate",
            "prepare-program",
            "take-program",
            "prepare-media",
            "take-media",
            "prepare-pip-media",
            "take-pip-media",
            "prepare-pip-vcam",
            "take-pip-vcam",
            "output",
        ),
        cleanup=cleanup,
    )

    assert controller.desired_scene(BusId.EDITOR) == CAMERA_SCENE_ID
    assert controller.applied_scene(BusId.EDITOR) is None
    readiness: list[bool] = []
    controller.engine_ready_changed.connect(readiness.append)
    controller.start_engine()
    assert controller.engine_ready
    assert controller.applied_scene(BusId.EDITOR) == CAMERA_SCENE_ID
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert len(engine.snapshots) == 1

    projection.set_type("image")
    assert controller.desired_scene(BusId.EDITOR) == CONTENT_SCENE_ID
    assert controller.applied_scene(BusId.EDITOR) == CONTENT_SCENE_ID
    assert len(engine.snapshots) == 1
    assert engine.preparations[-1][2] == CONTENT_SCENE_ID

    controller.take_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)
    assert controller.applied_scene(BusId.EDITOR) == (
        CONTENT_CAMERA_PIP_SCENE_ID
    )
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == (CONTENT_CAMERA_PIP_SCENE_ID)
    assert len(engine.snapshots) == 1

    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    assert [(bus, on) for _id, bus, on in engine.outputs] == [(BusId.VIRTUAL_CAMERA, True)]
    cleanup.close()
    assert engine.stopped
    assert not controller.engine_ready
    assert readiness == [True, False]


def test_manual_take_reconciles_when_the_saved_auto_return_scene_is_unchanged(request) -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
    )
    runtime.select_program_scene(CAMERA_SCENE_ID)
    projection.set_type("image")
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID

    controller.take_program_scene(CAMERA_SCENE_ID)

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert controller.program_automation_suspended


def test_auto_media_returns_to_the_previous_program_base_scene(request) -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    runtime.select_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)

    projection.set_type("image")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    assert controller.program_return_scene_id == CONTENT_CAMERA_PIP_SCENE_ID

    projection.set_type("idle")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == (CONTENT_CAMERA_PIP_SCENE_ID)


def test_audio_only_playback_does_not_trigger_native_scene_auto_switch(request) -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )

    projection.set_state({"type": "video", "is_audio": True})

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID


def test_return_scene_override_does_not_take_program_until_media_ends(request) -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    projection.set_type("video")
    controller._set_applied_scenes(((BusId.VIRTUAL_CAMERA, CONTENT_SCENE_ID),))
    assert controller.program_return_override_available

    controller.set_program_return_scene(CONTENT_CAMERA_PIP_SCENE_ID)

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    assert controller.program_return_scene_id == CONTENT_CAMERA_PIP_SCENE_ID

    projection.set_type("idle")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == (CONTENT_CAMERA_PIP_SCENE_ID)


@pytest.mark.parametrize("bus_id", [BusId.MEDIA_WINDOWS, BusId.VIRTUAL_CAMERA])
@pytest.mark.parametrize("scene_id", [CAMERA_SCENE_ID, NO_SIGNAL_SCENE_ID, CONTENT_CAMERA_PIP_SCENE_ID])
def test_select_scene_during_media_returns_only_from_content_scenes(
    request,
    bus_id: BusId, scene_id: str,
) -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(request, _Engine(), projection, request_ids=())
    controller.select_scene(bus_id, CONTENT_CAMERA_PIP_SCENE_ID)
    projection.set_type("image")
    assert controller.desired_scene(bus_id) == CONTENT_SCENE_ID
    state_before = runtime.state

    controller.select_scene(bus_id, scene_id)

    assert controller.desired_scene(bus_id) == scene_id
    other_bus = BusId.VIRTUAL_CAMERA if bus_id is BusId.MEDIA_WINDOWS else BusId.MEDIA_WINDOWS
    assert controller.desired_scene(other_bus) == CONTENT_SCENE_ID
    has_content = scene_id == CONTENT_CAMERA_PIP_SCENE_ID
    if has_content:
        assert runtime.state is state_before
    else:
        assert runtime.state.output(bus_id).manual_scene_id == scene_id
    projection.set_type("idle")
    assert controller.desired_scene(bus_id) == (
        CONTENT_CAMERA_PIP_SCENE_ID if has_content else scene_id
    )
    projection.set_type("image")
    assert controller.desired_scene(bus_id) == CONTENT_SCENE_ID


@pytest.mark.parametrize("bus_id", [BusId.MEDIA_WINDOWS, BusId.VIRTUAL_CAMERA])
def test_session_routing_reaches_engine_and_right_override_preserves_live_scene(request, bus_id: BusId) -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, runtime, controller = _runtime_controller(request, engine, projection, request_ids=())
    controller.start_engine()
    projection.set_type("video")
    _deliver_controller_results(controller)
    other_bus = BusId.VIRTUAL_CAMERA if bus_id is BusId.MEDIA_WINDOWS else BusId.MEDIA_WINDOWS
    other_before = runtime.state.output(other_bus)

    controller.select_scene(bus_id, NO_SIGNAL_SCENE_ID)
    _deliver_controller_results(controller)
    assert controller.applied_scene(bus_id) == NO_SIGNAL_SCENE_ID
    assert controller.applied_scene(other_bus) == CONTENT_SCENE_ID
    assert not controller.return_scene_override_available(bus_id)
    with pytest.raises(SceneValidationError):
        controller.set_return_scene(bus_id, CONTENT_CAMERA_PIP_SCENE_ID)
    controller.select_scene(bus_id, CONTENT_CAMERA_PIP_SCENE_ID)
    controller.set_return_scene(bus_id, CONTENT_CAMERA_PIP_SCENE_ID)
    _deliver_controller_results(controller)
    assert controller.applied_scene(bus_id) == CONTENT_CAMERA_PIP_SCENE_ID
    assert runtime.state.output(other_bus) == other_before
    controller.select_scene(bus_id, CAMERA_SCENE_ID)
    _deliver_controller_results(controller)
    assert controller.applied_scene(bus_id) == CAMERA_SCENE_ID
    controller.select_scene(bus_id, CONTENT_SCENE_ID)
    _deliver_controller_results(controller)
    projection.set_type("idle")
    assert controller.applied_scene(bus_id) == CONTENT_CAMERA_PIP_SCENE_ID
    projection.set_type("video")
    _deliver_controller_results(controller)
    assert controller.applied_scene(bus_id) == CONTENT_SCENE_ID


def test_session_selection_survives_playback_updates_but_expires_with_presentation(request) -> None:
    from solin.core.projection.application import ProjectionSession

    projection = ProjectionSession()
    _documents, _runtime, controller = _runtime_controller(request, _Engine(), projection, request_ids=())
    projection.set_state({"type": "video", "path": "first.mp4"})
    controller.select_scene(BusId.MEDIA_WINDOWS, NO_SIGNAL_SCENE_ID)
    session = projection.presentation_session_id
    projection.update_state(position=20, paused=True)
    assert projection.presentation_session_id == session
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == NO_SIGNAL_SCENE_ID
    projection.set_state({"type": "video", "path": "second.mp4"})
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == CONTENT_SCENE_ID


@pytest.mark.parametrize("bus_id", [BusId.MEDIA_WINDOWS, BusId.VIRTUAL_CAMERA])
@pytest.mark.parametrize("operation", ["select", "return", "disable", "resume"])
def test_failed_session_runtime_write_preserves_live_selection_and_return(
    request,
    monkeypatch: pytest.MonkeyPatch, bus_id: BusId, operation: str,
) -> None:
    class _FailingStore:
        def save(self, _state, *, expected_revision=None) -> None:
            raise OSError("Runtime save failed")

    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(request, _Engine(), projection, request_ids=())
    controller.select_scene(bus_id, CONTENT_CAMERA_PIP_SCENE_ID)
    projection.set_type("video")
    controller.select_scene(
        bus_id, CONTENT_CAMERA_PIP_SCENE_ID if operation == "return" else NO_SIGNAL_SCENE_ID,
    )
    # Resume must write the captured base after a whole-Program take changed it.
    if operation == "resume":
        controller.take_program_scene(NO_SIGNAL_SCENE_ID)
    before_state, before_desired = runtime.state, controller.desired_scenes
    monkeypatch.setattr(runtime, "_store", _FailingStore())
    with pytest.raises(OSError, match="Runtime save failed"):
        if operation == "select":
            controller.select_scene(bus_id, CAMERA_SCENE_ID)
        elif operation == "return":
            controller.set_return_scene(bus_id, CAMERA_SCENE_ID)
        elif operation == "disable":
            controller.set_program_automatic(False)
        else:
            controller.resume_program_automation()
    assert runtime.state is before_state
    assert controller.desired_scenes == before_desired
    monkeypatch.setattr(runtime, "_store", None)
    # A later selection exercises the restored transient return target.
    controller.select_scene(bus_id, CONTENT_SCENE_ID)
    projection.set_type("idle")
    assert controller.desired_scene(bus_id) == CONTENT_CAMERA_PIP_SCENE_ID


@pytest.mark.parametrize("bus_id", [BusId.MEDIA_WINDOWS, BusId.VIRTUAL_CAMERA])
def test_disabling_automation_pins_each_output_to_its_current_session_scene(request, bus_id: BusId) -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(request, _Engine(), projection, request_ids=())
    projection.set_type("video")
    controller.select_scene(bus_id, NO_SIGNAL_SCENE_ID)
    before_desired = controller.desired_scenes
    controller.set_program_automatic(False)
    assert controller.desired_scenes == before_desired
    assert all(output.mode is OutputMode.MANUAL for output in runtime.state.outputs)
    projection.set_type("idle")
    assert controller.desired_scenes == before_desired


@pytest.mark.parametrize("bus_id", [BusId.MEDIA_WINDOWS, BusId.VIRTUAL_CAMERA])
@pytest.mark.parametrize(("visible", "opacity", "has_content"), [
    (True, 1.0, True), (False, 1.0, False), (True, 0.0, False),
])
def test_conditional_return_follows_visible_nested_media(
    request,
    bus_id: BusId, visible: bool, opacity: float, has_content: bool,
) -> None:
    projection = _Projection()
    documents, _runtime, controller = _runtime_controller(request, _Engine(), projection, request_ids=())
    documents.create_source(SourceDefinition(
        id="nested-source", name="Nested media", kind=SourceKind.SCENE_REFERENCE,
        configuration=SceneReferenceConfig(target_scene_id=CONTENT_CAMERA_PIP_SCENE_ID),
    ))
    documents.create_scene("Nested media", scene_id="nested-media")
    documents.add_layer("nested-media", SceneLayer(
        id="nested-layer", name="Nested media", source_id="nested-source",
        visible=visible, opacity=opacity,
    ))
    projection.set_type("video")
    controller.select_scene(bus_id, "nested-media")
    assert controller.desired_scene(bus_id) == "nested-media"
    assert controller.return_scene_override_available(bus_id) is has_content
    projection.set_type("idle")
    assert controller.desired_scene(bus_id) == (CAMERA_SCENE_ID if has_content else "nested-media")


@pytest.mark.parametrize("bus_id", [BusId.MEDIA_WINDOWS, BusId.VIRTUAL_CAMERA])
def test_selection_restores_auto_during_media_and_preserves_previously_pinned_return(request, bus_id: BusId) -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(request, _Engine(), projection, request_ids=())
    controller.take_scene(bus_id, CONTENT_CAMERA_PIP_SCENE_ID)
    projection.set_type("video")
    assert controller.desired_scene(bus_id) == CONTENT_CAMERA_PIP_SCENE_ID
    assert not controller.return_scene_override_available(bus_id)
    controller.select_scene(bus_id, NO_SIGNAL_SCENE_ID)
    assert runtime.state.output(bus_id).mode is OutputMode.AUTO
    assert controller.desired_scene(bus_id) == NO_SIGNAL_SCENE_ID
    assert not controller.return_scene_override_available(bus_id)
    controller.select_scene(bus_id, CONTENT_SCENE_ID)
    assert runtime.state.output(bus_id).manual_scene_id == CONTENT_CAMERA_PIP_SCENE_ID
    controller.set_return_scene(bus_id, CAMERA_SCENE_ID)
    projection.set_type("idle")
    assert controller.desired_scene(bus_id) == CAMERA_SCENE_ID
    projection.set_type("video")
    assert controller.desired_scene(bus_id) == CONTENT_SCENE_ID


def test_removed_session_scene_and_return_fall_back_without_invalid_references(request) -> None:
    projection = _Projection()
    documents, _runtime, controller = _runtime_controller(request, _Engine(), projection, request_ids=())
    documents.create_scene("Return", scene_id="return")
    documents.create_scene("Temporary", scene_id="temporary")
    controller.select_scene(BusId.MEDIA_WINDOWS, "return")
    projection.set_type("video")
    controller.select_scene(BusId.MEDIA_WINDOWS, "temporary")
    documents.delete_scene("temporary")
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == CONTENT_SCENE_ID
    documents.delete_scene("return")
    controller.select_scene(BusId.MEDIA_WINDOWS, NO_SIGNAL_SCENE_ID)
    controller.resume_program_automation()
    projection.set_type("idle")
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == CAMERA_SCENE_ID


@pytest.mark.parametrize("stop_before_resuming", [False, True])
def test_automation_off_on_preserves_independent_return_bases(request, stop_before_resuming: bool) -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(request, _Engine(), projection, request_ids=())
    controller.select_scene(BusId.MEDIA_WINDOWS, CONTENT_CAMERA_PIP_SCENE_ID)
    controller.select_scene(BusId.VIRTUAL_CAMERA, NO_SIGNAL_SCENE_ID)
    projection.set_type("video")
    controller.set_program_automatic(False)
    if stop_before_resuming:
        projection.set_type("idle")
    controller.set_program_automatic(True)
    projection.set_type("idle")
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == CONTENT_CAMERA_PIP_SCENE_ID
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == NO_SIGNAL_SCENE_ID


def test_auto_media_does_not_return_after_operator_takes_a_scene_without_content(request) -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    projection.set_type("image")

    controller.take_program_scene(NO_SIGNAL_SCENE_ID)

    assert controller.program_automation_suspended
    assert not controller.program_return_override_available
    with pytest.raises(SceneValidationError, match="only change while"):
        controller.set_program_return_scene(CAMERA_SCENE_ID)

    projection.set_type("idle")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == NO_SIGNAL_SCENE_ID


def test_auto_media_returns_after_operator_takes_another_content_scene(request) -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    projection.set_type("image")
    return_base_before = runtime.state.output(BusId.VIRTUAL_CAMERA).manual_scene_id

    controller.take_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_CAMERA_PIP_SCENE_ID
    assert not controller.program_automation_suspended
    assert controller.program_return_scene_id == CAMERA_SCENE_ID
    assert runtime.state.output(BusId.VIRTUAL_CAMERA).manual_scene_id == return_base_before
    controller._set_applied_scenes(((BusId.VIRTUAL_CAMERA, CONTENT_CAMERA_PIP_SCENE_ID),))
    assert controller.program_return_override_available
    controller.set_program_return_scene(NO_SIGNAL_SCENE_ID)
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_CAMERA_PIP_SCENE_ID
    assert controller.program_return_scene_id == NO_SIGNAL_SCENE_ID

    controller.take_program_scene(CAMERA_SCENE_ID)
    assert controller.program_automation_suspended
    controller.take_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)
    assert not controller.program_automation_suspended
    assert controller.program_return_scene_id == NO_SIGNAL_SCENE_ID

    projection.set_type("idle")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == NO_SIGNAL_SCENE_ID


def test_taking_a_content_scene_resumes_a_suspended_media_session(request) -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    projection.set_type("image")
    return_base_before = runtime.state.output(BusId.VIRTUAL_CAMERA).manual_scene_id
    controller.take_program_scene(NO_SIGNAL_SCENE_ID)
    assert controller.program_automation_suspended
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == NO_SIGNAL_SCENE_ID
    assert runtime.state.output(BusId.VIRTUAL_CAMERA).manual_scene_id == NO_SIGNAL_SCENE_ID

    controller.take_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_CAMERA_PIP_SCENE_ID
    assert not controller.program_automation_suspended
    assert controller.program_return_scene_id == CAMERA_SCENE_ID
    assert runtime.state.output(BusId.VIRTUAL_CAMERA).manual_scene_id == return_base_before

    projection.set_type("idle")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID


def test_taking_the_live_automatic_media_scene_is_a_noop(request) -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    projection.set_type("image")
    controller._set_applied_scenes(((BusId.VIRTUAL_CAMERA, CONTENT_SCENE_ID),))
    before = runtime.state

    after = controller.take_program_scene(CONTENT_SCENE_ID)

    assert after is before
    assert not controller.program_automation_suspended
    assert controller.program_return_override_available


def test_taking_media_again_resumes_a_suspended_automatic_media_session(request) -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    projection.set_type("video")
    controller.take_program_scene(CAMERA_SCENE_ID)
    assert controller.program_automation_suspended
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID

    controller.take_program_scene(CONTENT_SCENE_ID)

    assert not controller.program_automation_suspended
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID


def test_resume_program_automation_reconciles_when_runtime_state_is_unchanged(request) -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    projection.set_type("image")
    controller.take_program_scene(NO_SIGNAL_SCENE_ID)
    assert controller.program_automation_suspended

    controller.resume_program_automation()

    assert not controller.program_automation_suspended
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID

    projection.set_type("idle")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID


def test_reenabling_program_automation_restores_the_session_return_base(request) -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    projection.set_type("image")
    controller.take_program_scene(NO_SIGNAL_SCENE_ID)
    assert controller.program_automation_suspended

    controller.set_program_automatic(True)

    assert not controller.program_automation_suspended
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID

    projection.set_type("idle")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID


def test_disabling_automation_preserves_the_latest_desired_scene_during_take(request) -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    projection.set_type("image")
    controller._set_applied_scenes(((BusId.VIRTUAL_CAMERA, CONTENT_SCENE_ID),))
    controller.take_program_scene(CAMERA_SCENE_ID)
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID

    controller.set_program_automatic(False)

    program = runtime.state.output(BusId.VIRTUAL_CAMERA)
    assert program.mode is OutputMode.MANUAL
    assert program.manual_scene_id == CAMERA_SCENE_ID
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID


def test_disabled_previous_scene_memory_returns_to_default(
    request,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "solin.controllers.scene_runtime_controller.MEMORIZE_PRE_MEDIA_SCENE",
        False,
    )
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
        request,
        _Engine(),
        projection,
        request_ids=(),
    )
    runtime.select_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)

    projection.set_type("image")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    assert controller.program_return_scene_id == CAMERA_SCENE_ID

    projection.set_type("idle")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID


@pytest.mark.parametrize("scene_id", [CONTENT_SCENE_ID, NO_SIGNAL_SCENE_ID])
def test_disabled_previous_scene_memory_resets_only_automatic_outputs_to_their_defaults(
    request,
    monkeypatch: pytest.MonkeyPatch,
    scene_id: str,
) -> None:
    monkeypatch.setattr("solin.controllers.scene_runtime_controller.MEMORIZE_PRE_MEDIA_SCENE", False)
    document = _document()
    document = replace(document, outputs=tuple(
        replace(route, default_scene_id=CONTENT_CAMERA_PIP_SCENE_ID)
        if route.bus_id is BusId.MEDIA_WINDOWS else route
        for route in document.outputs
    ))
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        _Engine(), projection, request_ids=(), document=document,
    )
    controller.select_scene(BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID)
    controller.take_scene(BusId.VIRTUAL_CAMERA, NO_SIGNAL_SCENE_ID)
    projection.set_type("video")
    controller.select_scene(BusId.MEDIA_WINDOWS, scene_id)
    projection.set_type("idle")
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == (
        CONTENT_CAMERA_PIP_SCENE_ID if scene_id == CONTENT_SCENE_ID else scene_id
    )
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == NO_SIGNAL_SCENE_ID


def test_transition_policy_changes_do_not_rehydrate_and_preview_always_cuts(request) -> None:
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=(
            "hydrate",
            "prepare-media",
            "take-media",
            "prepare-program",
            "take-program",
        ),
    )
    controller.start_engine()
    hydrated_revision = engine.snapshots[0][1].document.revision

    documents.set_program_transition(
        TransitionSpec(TransitionKind.FADE_TO_BLACK, 600)
    )
    documents.set_scene_transition_override(
        CONTENT_SCENE_ID,
        TransitionSpec(TransitionKind.DISSOLVE, 450),
    )

    assert len(engine.snapshots) == 1
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID

    controller.take_program_scene(CONTENT_SCENE_ID)

    prepared = {
        bus_id: transition
        for _, bus_id, scene_id, _sequence, transition in engine.preparations
        if scene_id == CONTENT_SCENE_ID
    }
    assert prepared[BusId.EDITOR] == TransitionSpec(TransitionKind.CUT, 0)
    assert prepared[BusId.VIRTUAL_CAMERA] == TransitionSpec(
        TransitionKind.DISSOLVE,
        450,
    )
    assert engine.takes[-1][1].document_revision == hydrated_revision
    assert len(engine.snapshots) == 1


def test_policy_only_revision_uses_hydrated_revision_for_auxiliary_commands(request) -> None:
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("hydrate", "output", "render", "preview-geometry"),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()
    hydrated_revision = engine.snapshots[0][1].document.revision

    documents.set_program_transition(
        TransitionSpec(TransitionKind.FADE_TO_BLACK, 600)
    )
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    scene = documents.document.scene(CAMERA_SCENE_ID)
    controller.preview_layer_geometry(scene.id, scene.layers[0])

    assert len(engine.snapshots) == 1
    assert engine.preview_geometries[-1][4] == hydrated_revision
    assert errors == []


def test_transition_fallback_is_reported_without_marking_engine_failed(request) -> None:
    class _FallbackEngine(_Engine):
        def prepare_scene(
            self,
            bus_id: BusId,
            scene_id: str,
            *,
            transition: TransitionSpec,
            document_revision: int,
            request_id: str,
            sequence: int,
            deadline_ms: int,
            content_media_epoch: int | None = None,
            content_source_kind: ContentSourceKind = ContentSourceKind.FRAMES,
        ) -> Future[ScenePreparation]:
            prepared = super().prepare_scene(
                bus_id,
                scene_id,
                transition=transition,
                document_revision=document_revision,
                request_id=request_id,
                sequence=sequence,
                deadline_ms=deadline_ms,
                content_media_epoch=content_media_epoch,
                content_source_kind=content_source_kind,
            ).result()
            if bus_id is BusId.VIRTUAL_CAMERA:
                prepared = replace(
                    prepared,
                    transition=TransitionSpec(TransitionKind.CUT, 0),
                    fallback_applied=True,
                    fallback_reason="transition_pipeline_unavailable",
                )
            return _completed(prepared)

    engine = _FallbackEngine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=(
            "hydrate",
            "prepare-media",
            "take-media",
            "prepare-program",
            "take-program",
        ),
    )
    fallbacks: list[str] = []
    controller.transition_fallback.connect(fallbacks.append)
    controller.start_engine()

    controller.take_program_scene(CONTENT_SCENE_ID)

    assert fallbacks == [
        "The selected transition is unavailable. The scene was cut instead."
    ]
    assert controller.last_engine_error_code == ""
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID


def test_auto_switch_resolves_transition_for_each_destination_scene(request) -> None:
    projection = _Projection()
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(
            "hydrate",
            "prepare-content-preview",
            "take-content-preview",
            "prepare-content-program",
            "take-content-program",
            "attach-window",
            "prepare-default-preview",
            "take-default-preview",
            "prepare-default-program",
            "take-default-program",
        ),
    )
    documents.set_scene_transition_override(
        CONTENT_SCENE_ID,
        TransitionSpec(TransitionKind.FADE_TO_BLACK, 700),
    )
    controller.start_engine()

    projection.set_type("image")
    target = OutputWindowTarget(
        bus_id=BusId.MEDIA_WINDOWS,
        target_id="automatic-media-window",
        screen_id="primary",
        native_handle=123,
        x=0,
        y=0,
        width=1280,
        height=720,
        device_pixel_ratio=1.0,
    )
    controller.set_window_targets((target,))
    projection.set_type("idle")

    program_transitions = [
        (scene_id, transition)
        for _, bus_id, scene_id, _sequence, transition in engine.preparations
        if bus_id is BusId.VIRTUAL_CAMERA
    ]
    preview_transitions = [
        transition
        for _, bus_id, _scene_id, _sequence, transition in engine.preparations
        if bus_id is BusId.EDITOR
    ]
    assert program_transitions == [
        (CONTENT_SCENE_ID, TransitionSpec(TransitionKind.FADE_TO_BLACK, 700)),
        (CAMERA_SCENE_ID, TransitionSpec(TransitionKind.DISSOLVE, 350)),
    ]
    # Raw media windows consume the content source directly. They do not pin
    # or otherwise mutate the editor Preview scene route.
    assert preview_transitions == [
        TransitionSpec(TransitionKind.CUT, 0),
        TransitionSpec(TransitionKind.CUT, 0),
    ]
    # program, projection and editor each prepare per reconcile
    assert engine.preparation_content_media_epochs == [1, 1, 1, None, None, None]
    assert len(engine.snapshots) == 1
    assert [targets for _request_id, targets in engine.window_target_updates] == [(target,)]


@pytest.mark.parametrize("completion", ["already_completed", "gui_thread", "worker_thread"])
@pytest.mark.parametrize("paused", [False, True])
def test_native_preparation_delivery_does_not_take_before_open_media(
    request, monkeypatch, completion: str, paused: bool,
) -> None:
    from solin.core.projection.application import ProjectionSession

    projection = ProjectionSession()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(),
    )
    controller.start_engine()
    events = []
    take = engine.take_prepared
    prepare = engine.prepare_scene
    completions = []

    def prepare_native(*args, **kwargs):
        result = prepare(*args, **kwargs)
        if completion == "already_completed" or completions:
            return result
        pending = Future()
        completions.append((pending, result.result()))
        return pending

    def take_after_open(preparation, **kwargs):
        assert QThread.isMainThread()
        events.append(("take", preparation.bus_id))
        return take(preparation, **kwargs)

    monkeypatch.setattr(engine, "take_prepared", take_after_open)
    monkeypatch.setattr(engine, "prepare_scene", prepare_native)

    # The production caller commits the identity before enqueueing open_media.
    projection.set_state({"type": "video", "is_audio": False, "paused": paused})
    assert len(engine.preparations) == 1
    assert events == []
    if completion != "already_completed":
        future, result = completions[0]
        if completion == "gui_thread":
            future.set_result(result)
        else:
            worker = Thread(target=future.set_result, args=(result,))
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive()
        assert events == []
    events.append(("open_media", projection.presentation_session_id))

    _deliver_controller_results(controller)

    assert events == [
        ("open_media", projection.presentation_session_id),
        ("take", BusId.VIRTUAL_CAMERA),
        ("take", BusId.MEDIA_WINDOWS),
        ("take", BusId.EDITOR),
    ]
    assert controller._pending == {}


@pytest.mark.parametrize("next_type", ["video", "image", "idle"])
def test_queued_native_preparation_cannot_take_a_replacement_presentation(
    request, next_type: str,
) -> None:
    from solin.core.projection.application import ProjectionSession

    projection = ProjectionSession()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(),
    )
    controller.start_engine()
    errors = []
    controller.engine_error.connect(errors.append)
    projection.set_state({"type": "video", "is_audio": False})
    obsolete_request_id = engine.preparations[0][0]

    # Both callers enqueue their media before returning control to Qt. Delivery
    # of the first completed Prepare must still check the newest visual identity.
    projection.set_state({"type": next_type})
    assert engine.takes == []
    assert len(engine.preparations) == 1
    _deliver_controller_results(controller)

    assert engine.cancelled == [obsolete_request_id]
    assert all(prepared.request_id != obsolete_request_id for _, prepared in engine.takes)
    if next_type == "idle":
        assert engine.takes == []
        assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    else:
        expected_kind = (
            ContentSourceKind.NATIVE_MEDIA if next_type == "video" else ContentSourceKind.FRAMES
        )
        assert len(engine.takes) == 3
        assert engine.preparation_content_media_epochs[1:] == [
            projection.presentation_session_id,
        ] * 3
        assert engine.preparation_content_source_kinds[1:] == [expected_kind] * 3
    assert controller._pending == {}
    assert errors == []


@pytest.mark.parametrize("restart", [False, True])
def test_queued_native_preparation_is_retired_when_the_engine_stops(
    request, restart: bool,
) -> None:
    from solin.core.projection.application import ProjectionSession

    projection = ProjectionSession()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(),
    )
    controller.start_engine()
    projection.set_state({"type": "video", "is_audio": False})
    obsolete_request_id = engine.preparations[0][0]
    controller.stop_engine()
    if restart:
        engine.generation = "engine-generation-2"
        controller.start_engine()

    _deliver_controller_results(controller)

    assert obsolete_request_id in engine.cancelled
    assert all(prepared.request_id != obsolete_request_id for _, prepared in engine.takes)
    if restart:
        assert len(engine.takes) == 3
        assert all(
            prepared.process_generation == engine.generation for _, prepared in engine.takes
        )
    else:
        assert engine.takes == []
        assert controller.applied_scenes == ()
    assert controller._pending == {}


@pytest.mark.parametrize("state_type", ["video", "image", "timer", "browser", "ndi"])
def test_auto_switch_identifies_the_content_producer_and_presentation(
    request,
    state_type: str,
) -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
    )
    controller.start_engine()

    # Projection publishes its identity before starting the video decoder or
    # submitting the first app-owned frame. The video decoder is in the sidecar;
    # it cannot produce a frame in the app's ingress channel.
    projection.set_type(state_type)
    _deliver_controller_results(controller)

    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    assert controller.applied_scene(BusId.MEDIA_WINDOWS) == CONTENT_SCENE_ID
    assert len(engine.preparations) == 3
    assert engine.preparation_content_media_epochs == [projection.session_id] * 3
    expected_kind = (
        ContentSourceKind.NATIVE_MEDIA if state_type == "video" else ContentSourceKind.FRAMES
    )
    assert engine.preparation_content_source_kinds == [expected_kind] * 3


@pytest.mark.parametrize("previous_type", ["video", "image", "timer", "browser", "ndi"])
@pytest.mark.parametrize("next_type", ["video", "image", "timer", "browser", "ndi"])
def test_new_content_takes_the_current_presentation_when_the_scene_is_unchanged(
    request, previous_type: str, next_type: str,
) -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(),
    )
    controller.start_engine()
    projection.set_type(previous_type)
    _deliver_controller_results(controller)
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    engine.preparations.clear()
    engine.preparation_content_media_epochs.clear()
    engine.preparation_content_source_kinds.clear()
    engine.takes.clear()

    projection.set_type(next_type)
    _deliver_controller_results(controller)

    assert len(engine.preparations) == 3
    assert all(scene_id == CONTENT_SCENE_ID for _, _, scene_id, _, _ in engine.preparations)
    assert engine.preparation_content_media_epochs == [projection.session_id] * 3
    expected_kind = (
        ContentSourceKind.NATIVE_MEDIA if next_type == "video" else ContentSourceKind.FRAMES
    )
    assert engine.preparation_content_source_kinds == [expected_kind] * 3
    assert [prepared.bus_id for _, prepared in engine.takes] == [
        BusId.VIRTUAL_CAMERA, BusId.MEDIA_WINDOWS, BusId.EDITOR,
    ]
    assert controller._take_reconciliation_required == set()
    assert len(engine.snapshots) == 1


@pytest.mark.parametrize("previous_type", ["video", "image", "timer"])
@pytest.mark.parametrize("next_type", ["image", "video"])
def test_new_content_during_hydration_commits_the_presentation_after_the_snapshot(
    request, previous_type: str, next_type: str,
) -> None:
    projection = _Projection()
    projection.set_type(previous_type)
    engine = _PendingGeometryEngine(defer_hydration=True)
    _documents, _runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(),
    )
    controller.start_engine()
    assert controller.applied_scenes == ()
    projection.set_type(next_type)  # same Content scene, but a new presentation
    assert engine.preparations == []
    engine.finish_hydration(0)
    _deliver_controller_results(controller)

    assert engine.preparation_content_media_epochs == [projection.session_id] * 3
    expected_kind = (
        ContentSourceKind.NATIVE_MEDIA if next_type == "video" else ContentSourceKind.FRAMES
    )
    assert engine.preparation_content_source_kinds == [expected_kind] * 3
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    assert controller._take_reconciliation_required == set()
    assert controller._pending_content_presentation_epoch is None


@pytest.mark.parametrize("state_type", ["image", "timer", "video"])
@pytest.mark.parametrize("idle_media_path", ["", "idle.png"])
def test_return_to_idle_commits_current_epoch_on_pinned_content_routes(
    request, state_type: str, idle_media_path: str,
) -> None:
    projection = _Projection()
    projection.idle_media_path = idle_media_path
    engine = _Engine()
    _documents, runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(),
    )
    controller.start_engine()
    projection.set_type(state_type)
    _deliver_controller_results(controller)
    runtime.take_program_scene(CONTENT_SCENE_ID)
    engine.preparations.clear()
    engine.preparation_content_media_epochs.clear()

    projection.set_type("idle")

    assert [bus for _, bus, _, _, _ in engine.preparations] == [
        BusId.VIRTUAL_CAMERA, BusId.MEDIA_WINDOWS, BusId.EDITOR,
    ]
    assert engine.preparation_content_media_epochs == [projection.session_id] * 3
    assert controller._pending_content_presentation_epoch is None
    assert controller._take_reconciliation_required == set()


def test_timer_return_to_custom_content_default_prepares_idle_pixels(request) -> None:
    document = _document()
    content = next(scene for scene in document.scenes if scene.id == CONTENT_SCENE_ID)
    custom_idle = replace(content, id="custom-idle", name="Custom idle")
    document = replace(
        document,
        scenes=document.scenes + (custom_idle,),
        outputs=tuple(
            replace(output, default_scene_id=custom_idle.id) for output in document.outputs
        ),
    )
    projection = _Projection()
    projection.idle_media_path = "idle.png"
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(), document=document,
    )
    controller.start_engine()
    projection.set_type("timer")
    engine.preparations.clear()
    engine.preparation_content_media_epochs.clear()

    projection.set_type("idle")

    assert len(engine.preparations) == 3
    assert all(scene == custom_idle.id for _, _, scene, _, _ in engine.preparations)
    assert engine.preparation_content_media_epochs == [projection.session_id] * 3


@pytest.mark.parametrize("state", [
    {"type": "image", "path": "image.png"},
    {"type": "timer", "remaining": 60},
    {"type": "video", "is_audio": False, "title": "Clip"},
])
def test_content_updates_within_an_epoch_do_not_take_again(request, state) -> None:
    from solin.core.projection.application import ProjectionSession

    projection = ProjectionSession()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(),
    )
    controller.start_engine()
    projection.set_state(state)
    _deliver_controller_results(controller)
    epoch = projection.presentation_session_id
    preparations = tuple(engine.preparations)
    takes = tuple(engine.takes)

    if state["type"] == "image":
        projection.update_image_transform((1.5, 0.1, 0.2), animate=True)
    elif state["type"] == "video":
        projection.update_state(title="Metadata title", position=10, paused=True)
    else:
        projection.update_state(remaining=59)

    assert projection.presentation_session_id == epoch
    assert tuple(engine.preparations) == preparations
    assert tuple(engine.takes) == takes


def test_representing_the_same_video_commits_a_new_visual_epoch(request) -> None:
    from solin.core.projection.application import ProjectionSession

    projection = ProjectionSession()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(),
    )
    controller.start_engine()
    video = {"type": "video", "is_audio": False, "title": "Clip", "paused": True}

    for _ in range(2):
        preparation_count = len(engine.preparations)
        take_count = len(engine.takes)
        projection.set_state(video)
        _deliver_controller_results(controller)

        assert len(engine.preparations) == preparation_count + 3
        assert len(engine.takes) == take_count + 3
        assert engine.preparation_content_media_epochs[preparation_count:] == [
            projection.presentation_session_id,
        ] * 3
        assert engine.preparation_content_source_kinds[preparation_count:] == [
            ContentSourceKind.NATIVE_MEDIA,
        ] * 3
        assert controller._pending_content_presentation_epoch is None


def test_audio_over_idle_preserves_content_epoch_without_extra_takes(request) -> None:
    from solin.core.projection.application import ProjectionSession

    projection = ProjectionSession()
    projection.set_idle_media_path("idle.png")
    engine = _Engine()
    _documents, runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(),
    )
    runtime.take_program_scene(CONTENT_SCENE_ID)
    controller.start_engine()
    epoch = projection.presentation_session_id
    preparations = tuple(engine.preparations)
    takes = tuple(engine.takes)

    projection.set_state({"type": "video", "path": "audio.mp3", "is_audio": True})
    projection.update_state(position=10, paused=True)
    projection.reset_state()

    assert projection.presentation_session_id == epoch
    assert tuple(engine.preparations) == preparations
    assert tuple(engine.takes) == takes


@pytest.mark.parametrize("state_type", ["image", "video"])
def test_same_scene_epoch_remains_pending_until_each_content_bus_accepts_take(
    request, monkeypatch, state_type,
) -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request, engine, projection, request_ids=(),
    )
    controller.start_engine()
    projection.set_type(state_type)
    _deliver_controller_results(controller)
    take = engine.take_prepared
    acknowledgements = []

    def defer_take(*args, **kwargs):
        ack = take(*args, **kwargs).result()
        future = Future()
        acknowledgements.append((future, ack))
        return future

    monkeypatch.setattr(engine, "take_prepared", defer_take)
    projection.set_type(state_type)
    _deliver_controller_results(controller)
    epoch = projection.session_id

    for index, bus in enumerate((BusId.VIRTUAL_CAMERA, BusId.MEDIA_WINDOWS, BusId.EDITOR)):
        assert len(acknowledgements) == index + 1
        assert controller._pending_content_presentation_epoch == epoch
        assert bus in controller._take_reconciliation_required
        future, ack = acknowledgements[index]
        future.set_result(ack)
        _deliver_controller_results(controller)
        assert bus not in controller._take_reconciliation_required

    assert controller._pending_content_presentation_epoch is None
    assert controller._pending == {}


@pytest.mark.parametrize("completion", ["accepted", "rejected", "failed", "cancelled"])
@pytest.mark.parametrize("state_type", ["image", "video"])
def test_new_content_during_profile_hydration_waits_until_activation_finishes(
    scene_workspace_factory, request, tmp_path: Path, completion: str, state_type: str,
) -> None:
    workspace, collection = _real_workspace(scene_workspace_factory, tmp_path)
    projection = _Projection()
    engine = _PendingGeometryEngine(defer_hydration=True)
    controller = SceneRuntimeController(
        workspace, projection, engine=engine, session_id="test-session",
    )
    request.addfinalizer(controller.close)
    controller.start_engine()
    engine.finish_hydration(0)
    projection.set_type("image")
    baseline = len(engine.preparations)
    controller.activate_scene_profile(collection.id)

    projection.set_type(state_type)

    assert len(engine.preparations) == baseline
    assert controller._pending_content_presentation_epoch == projection.session_id
    if completion == "accepted":
        engine.finish_hydration(1)
    elif completion == "rejected":
        request_id, snapshot = engine.snapshots[1]
        engine.hydration_futures[1].set_result(replace(
            engine._ack(request_id, snapshot.sequence, snapshot.document.revision),
            applied=False, error_code="source_unavailable",
        ))
    elif completion == "failed":
        engine.hydration_futures[1].set_exception(
            SceneEngineCommandRejectedError("source_unavailable"),
        )
    else:
        engine.hydration_futures[1].cancel()

    _deliver_controller_results(controller)
    expected_snapshot = engine.snapshots[1 if completion == "accepted" else 0][1]
    assert controller.document.document_id == expected_snapshot.document.document_id
    assert len(engine.preparations) == baseline + 3
    assert engine.preparation_content_media_epochs[baseline:] == [projection.session_id] * 3
    expected_kind = (
        ContentSourceKind.NATIVE_MEDIA if state_type == "video" else ContentSourceKind.FRAMES
    )
    assert engine.preparation_content_source_kinds[baseline:] == [expected_kind] * 3
    assert all(
        prepared.document_revision == expected_snapshot.document.revision
        for _, prepared in engine.takes[baseline:]
    )
    assert controller._pending_content_presentation_epoch is None


def test_frame_egress_readiness_is_forwarded_without_marking_the_engine_failed(request) -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=(),
    )
    observed = []
    errors = []
    controller.engine_event.connect(observed.append)
    controller.engine_error.connect(errors.append)
    controller.start_engine()
    observed.clear()
    event = FrameEgressReadyEvent("solin-program", 1, "frame-channel")
    assert engine.listener is not None

    engine.listener(event)

    assert observed == [event]
    assert errors == []
    assert controller.engine_ready
    assert controller.last_engine_error_code == ""


@pytest.mark.parametrize("completion", ["prepared", "rejected", "timed_out"])
@pytest.mark.parametrize("previous_type", ["video", "image"])
@pytest.mark.parametrize("next_type", ["image", "video"])
@pytest.mark.parametrize("stage", ["prepare", "take"])
def test_replaced_content_preparation_is_reconciled_for_the_latest_presentation(
    request,
    monkeypatch,
    completion: str,
    previous_type: str,
    next_type: str,
    stage: str,
) -> None:
    engine = _Engine()
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
    )
    controller.start_engine()
    method_name = "prepare_scene" if stage == "prepare" else "take_prepared"
    dispatch = getattr(engine, method_name)
    pending: Future = Future()
    obsolete: list[ScenePreparation] = []
    obsolete_results = []

    def defer_first_preparation(*args, **kwargs):
        result = dispatch(*args, **kwargs)
        if not obsolete:
            obsolete.append(result.result() if stage == "prepare" else args[0])
            obsolete_results.append(result.result())
            return pending
        return result

    monkeypatch.setattr(engine, method_name, defer_first_preparation)
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    projection.set_type(previous_type)
    _deliver_controller_results(controller)
    previous_epoch = projection.session_id
    projection.set_type(next_type)
    assert len(engine.preparations) == 1
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID

    if completion == "prepared":
        pending.set_result(obsolete_results[0])
    elif completion == "rejected":
        pending.set_exception(SceneEngineCommandRejectedError("source_unavailable"))
    else:
        pending.set_exception(SceneEngineRequestTimeoutError("obsolete preparation"))

    _deliver_controller_results(controller)
    # A new presentation can use the same scene. The previous preparation must
    # neither take that scene nor block its replacement with a stale failure.
    assert len(engine.preparations) == 4
    expected_cancelled = (
        [] if stage == "take" and completion == "prepared" else [obsolete[0].request_id]
    )
    assert engine.cancelled == expected_cancelled
    completed_takes = engine.takes if stage == "prepare" else engine.takes[1:]
    assert len(completed_takes) == 3
    assert all(prepared.request_id != obsolete[0].request_id for _, prepared in completed_takes)
    current_epoch = projection.session_id
    assert engine.preparation_content_media_epochs == [previous_epoch, *([current_epoch] * 3)]
    previous_kind = (
        ContentSourceKind.NATIVE_MEDIA if previous_type == "video" else ContentSourceKind.FRAMES
    )
    current_kind = (
        ContentSourceKind.NATIVE_MEDIA if next_type == "video" else ContentSourceKind.FRAMES
    )
    assert engine.preparation_content_source_kinds == [previous_kind, *([current_kind] * 3)]
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    assert errors == []


def test_a_new_video_retries_a_failed_content_take_without_changing_the_scene(
    request,
    monkeypatch,
) -> None:
    engine = _Engine()
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
    )
    controller.start_engine()
    prepare = engine.prepare_scene
    failed = False

    def fail_first_content_prepare(*args, **kwargs):
        nonlocal failed
        result = prepare(*args, **kwargs)
        if not failed:
            failed = True
            return _failed(SceneEngineCommandRejectedError("source_unavailable"))
        return result

    monkeypatch.setattr(engine, "prepare_scene", fail_first_content_prepare)
    projection.set_type("video")
    _deliver_controller_results(controller)
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    first_presentation_epoch = projection.session_id
    assert len(engine.preparations) == 3

    # Advancing a playlist can open another video without visiting idle or
    # changing the scene. A failure belongs to the old presentation only.
    projection.set_type("video")
    _deliver_controller_results(controller)

    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    # The other buses accepted the first video; they must also commit its replacement.
    assert len(engine.preparations) == 6
    assert engine.preparation_content_media_epochs == [
        *([first_presentation_epoch] * 3), *([projection.session_id] * 3),
    ]
    assert engine.preparation_content_source_kinds == [ContentSourceKind.NATIVE_MEDIA] * 6


@pytest.mark.parametrize("completion", ["applied", "timed_out"])
def test_obsolete_video_take_is_reconciled_after_returning_to_idle(
    request,
    monkeypatch,
    completion: str,
) -> None:
    engine = _Engine()
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
    )
    controller.start_engine()
    take = engine.take_prepared
    pending: Future = Future()
    acknowledgements = []

    def defer_first_take(*args, **kwargs):
        result = take(*args, **kwargs)
        if not acknowledgements:
            acknowledgements.append(result.result())
            return pending
        return result

    monkeypatch.setattr(engine, "take_prepared", defer_first_take)
    projection.set_type("video")
    _deliver_controller_results(controller)
    projection.set_type("idle")
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert len(engine.takes) == 1

    if completion == "applied":
        pending.set_result(acknowledgements[0])
    else:
        pending.set_exception(SceneEngineRequestTimeoutError("obsolete take"))

    # The original command may already have changed the physical output. The
    # previously applied scene is not evidence that it is still on that scene.
    assert [prepared.scene_id for _, prepared in engine.takes] == [
        CONTENT_SCENE_ID,
        CAMERA_SCENE_ID,
    ]
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert controller._pending == {}


def test_applied_obsolete_video_take_remains_known_when_replacement_preparation_fails(
    request,
    monkeypatch,
) -> None:
    engine = _Engine()
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
    )
    controller.start_engine()
    take = engine.take_prepared
    prepare = engine.prepare_scene
    pending: Future = Future()
    acknowledgements = []

    def defer_first_take(*args, **kwargs):
        result = take(*args, **kwargs)
        if not acknowledgements:
            acknowledgements.append(result.result())
            return pending
        return result

    def fail_replacement_preparation(bus_id, *args, **kwargs):
        result = prepare(bus_id, *args, **kwargs)
        if bus_id is BusId.VIRTUAL_CAMERA and projection.state["type"] == "image":
            return _failed(SceneEngineCommandRejectedError("source_unavailable"))
        return result

    monkeypatch.setattr(engine, "take_prepared", defer_first_take)
    monkeypatch.setattr(engine, "prepare_scene", fail_replacement_preparation)
    projection.set_type("video")
    _deliver_controller_results(controller)
    projection.set_type("image")
    pending.set_result(acknowledgements[0])

    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    assert (BusId.VIRTUAL_CAMERA, CONTENT_SCENE_ID) in controller._failed_takes
    assert controller._pending == {}
    attempts = len(engine.preparations)
    controller._reconcile_desired(prepare=True)
    assert len(engine.preparations) == attempts


def test_graph_hydration_preserves_readiness_for_a_replaced_video_take(
    request,
    monkeypatch,
) -> None:
    engine = _Engine()
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
    )
    controller.start_engine()
    take = engine.take_prepared
    pending: Future = Future()
    acknowledgements = []

    def defer_first_take(*args, **kwargs):
        result = take(*args, **kwargs)
        if not acknowledgements:
            acknowledgements.append(result.result())
            return pending
        return result

    monkeypatch.setattr(engine, "take_prepared", defer_first_take)
    projection.set_type("video")
    _deliver_controller_results(controller)
    projection.set_type("image")
    controller.set_program_egress(
        replace(
            _content_ingress_descriptor(FrameChannelTransport.SHARED_MEMORY_VIDEO, generation=1),
            channel_id="program-egress",
            producer_kind=FrameProducerKind.NATIVE_COMPOSITOR,
        )
    )
    assert len(engine.snapshots) == 1

    pending.set_result(acknowledgements[0])

    # Hydration records the physical scene, but cannot establish that the new
    # image's app-owned frame is ready. Program still needs its current epoch.
    assert len(engine.snapshots) == 2
    assert dict(engine.snapshots[-1][1].active_scenes)[BusId.VIRTUAL_CAMERA] == CONTENT_SCENE_ID
    program_epochs = [
        epoch
        for preparation, epoch in zip(
            engine.preparations, engine.preparation_content_media_epochs, strict=True
        )
        if preparation[1] is BusId.VIRTUAL_CAMERA
    ]
    assert program_epochs == [1, projection.session_id]
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    assert controller._take_reconciliation_required == set()
    assert controller._pending == {}


def test_runtime_coalesces_rapid_scene_selection_while_prepare_is_in_flight(request) -> None:
    class _PendingPrepareEngine(_Engine):
        def __init__(self) -> None:
            super().__init__()
            self.pending_preparations: list[Future[ScenePreparation]] = []

        def prepare_scene(
            self,
            bus_id: BusId,
            scene_id: str,
            *,
            transition: TransitionSpec,
            document_revision: int,
            request_id: str,
            sequence: int,
            deadline_ms: int,
            content_media_epoch: int | None = None,
            content_source_kind: ContentSourceKind = ContentSourceKind.FRAMES,
        ) -> Future[ScenePreparation]:
            assert deadline_ms > 0
            self.preparations.append((request_id, bus_id, scene_id, sequence, transition))
            self.preparation_content_media_epochs.append(content_media_epoch)
            self.preparation_content_source_kinds.append(content_source_kind)
            future: Future[ScenePreparation] = Future()
            self.pending_preparations.append(future)
            return future

        def complete_preparation(self, index: int) -> None:
            request_id, bus_id, scene_id, sequence, transition = self.preparations[index]
            self.pending_preparations[index].set_result(
                ScenePreparation(
                    request_id=request_id,
                    session_id=self.session_id,
                    process_generation=self.generation,
                    sequence=sequence,
                    document_revision=self.snapshots[-1][1].document.revision,
                    bus_id=bus_id,
                    scene_id=scene_id,
                    preparation_token=f"prepared-{sequence}",
                    transition=transition,
                )
            )

    engine = _PendingPrepareEngine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=tuple(f"request-{index}" for index in range(8)),
    )
    controller.start_engine()

    controller.set_preview_scene(CONTENT_SCENE_ID)
    controller.set_preview_scene(CONTENT_CAMERA_PIP_SCENE_ID)

    assert [item[2] for item in engine.preparations] == [CONTENT_SCENE_ID]
    engine.complete_preparation(0)

    assert engine.cancelled == [engine.preparations[0][0]]
    assert [item[2] for item in engine.preparations] == [
        CONTENT_SCENE_ID,
        CONTENT_CAMERA_PIP_SCENE_ID,
    ]
    assert engine.takes == []

    engine.complete_preparation(1)

    assert len(engine.takes) == 1
    assert engine.takes[0][1].scene_id == CONTENT_CAMERA_PIP_SCENE_ID
    assert controller.applied_scene(BusId.EDITOR) == CONTENT_CAMERA_PIP_SCENE_ID


def test_editor_preview_uses_its_own_channel_without_changing_program(request) -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("hydrate-preview",),
    )

    controller.set_preview_scene(CONTENT_SCENE_ID)
    controller.start_engine()

    assert controller.desired_scene(BusId.EDITOR) == CONTENT_SCENE_ID
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    snapshot = engine.snapshots[0][1]
    assert dict(snapshot.render_enabled)[BusId.EDITOR]
    assert not dict(snapshot.output_enabled)[BusId.MEDIA_WINDOWS]
    assert not controller.runtime.state.output(BusId.MEDIA_WINDOWS).enabled


def test_editor_preview_transport_is_idle_until_the_editor_requests_frames(request) -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=(
            "hydrate",
            "enable-preview-render",
            "preview-prepare",
            "preview-take",
            "disable-preview-render",
            "restore-media-prepare",
            "restore-media-take",
        ),
    )
    descriptor = _preview_egress_descriptor()
    controller.set_preview_egress(descriptor)
    controller.start_engine()

    assert engine.snapshots[0][1].preview_egress == descriptor
    assert not dict(engine.snapshots[0][1].render_enabled)[BusId.MEDIA_WINDOWS]

    controller.set_preview_scene(CONTENT_SCENE_ID)

    assert len(engine.snapshots) == 1
    assert engine.renders[-1] == (
        "enable-preview-render",
        BusId.EDITOR,
        True,
    )

    controller.set_preview_scene(None)

    assert len(engine.snapshots) == 1
    assert engine.renders[-1] == (
        "disable-preview-render",
        BusId.EDITOR,
        False,
    )


def test_preview_demand_is_published_when_the_media_scene_is_already_selected(request) -> None:
    projection = _Projection()
    projection.set_type("image")
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
    )
    assert controller.desired_scene(BusId.EDITOR) == CONTENT_SCENE_ID
    changes: list[object] = []
    desired_changes: list[object] = []
    controller.preview_scene_changed.connect(changes.append)
    controller.desired_scenes_changed.connect(desired_changes.append)

    controller.set_preview_scene(CONTENT_SCENE_ID)

    assert changes == [CONTENT_SCENE_ID]
    assert desired_changes == []
    controller.set_preview_scene(None)
    assert changes == [CONTENT_SCENE_ID, None]
    assert desired_changes == []


def test_editor_preview_remains_independent_while_program_is_mirrored(request) -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=(
            "initial-hydrate",
            "enable-media-window",
            "enable-program-render",
            "enable-preview-render",
            "preview-prepare",
            "preview-take",
        ),
    )
    controller.start_engine()
    controller.set_output_enabled(BusId.MEDIA_WINDOWS, True)

    controller.set_preview_scene(CONTENT_SCENE_ID)

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert controller.desired_scene(BusId.EDITOR) == CONTENT_SCENE_ID
    assert controller.applied_scene(BusId.EDITOR) == CONTENT_SCENE_ID


def test_native_window_target_updates_without_rehydrating_the_scene_graph(request) -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=(
            "initial-hydrate",
            "attach-window",
            "clear-window",
        ),
    )
    target = OutputWindowTarget(
        bus_id=BusId.MEDIA_WINDOWS,
        target_id="projection-preview",
        screen_id="primary",
        native_handle=123,
        x=0,
        y=0,
        width=1280,
        height=720,
        device_pixel_ratio=1.0,
    )
    controller.start_engine()

    controller.set_window_targets((target,))

    assert len(engine.snapshots) == 1
    assert engine.window_target_updates[-1][1] == (target,)
    assert controller.desired_scene(BusId.EDITOR) == CAMERA_SCENE_ID
    # a projection window means the projection output now has to render
    assert engine.renders[-1][1:] == (BusId.MEDIA_WINDOWS, True)

    controller.set_window_targets(())

    assert len(engine.snapshots) == 1
    assert engine.window_target_updates[-1][1] == ()
    assert controller.desired_scene(BusId.EDITOR) == CAMERA_SCENE_ID
    assert engine.renders[-1][1:] == (BusId.MEDIA_WINDOWS, False)


def test_editor_preview_target_merges_with_projection_window_targets(request) -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("initial-hydrate", "attach-window", "attach-editor", "clear-editor"),
    )
    projection = OutputWindowTarget(
        bus_id=BusId.MEDIA_WINDOWS, target_id="projection-preview", screen_id="primary",
        native_handle=123, x=0, y=0, width=1280, height=720, device_pixel_ratio=1.0)
    editor = OutputWindowTarget(
        bus_id=BusId.MEDIA_WINDOWS, target_id="editor-preview", screen_id="primary",
        native_handle=456, x=0, y=0, width=640, height=360, device_pixel_ratio=1.0,
        scene_id=CAMERA_SCENE_ID)
    controller.start_engine()
    controller.set_window_targets((projection,))
    assert engine.window_target_updates[-1][1] == (projection,)

    controller.set_editor_preview_target(editor)
    # dispatched together — the editor preview target does not replace projection's
    assert engine.window_target_updates[-1][1] == (projection, editor)

    controller.set_editor_preview_target(None)
    assert engine.window_target_updates[-1][1] == (projection,)


def test_content_ingress_demand_follows_visible_routes_and_scene_sources(request) -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
    )
    changes: list[bool] = []
    controller.content_ingress_demand_changed.connect(changes.append)
    assert not controller.content_ingress_required

    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    assert not controller.content_ingress_required

    projection.set_type("video")
    assert controller.content_ingress_required

    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, False)
    assert not controller.content_ingress_required

    target = OutputWindowTarget(
        bus_id=BusId.MEDIA_WINDOWS,
        target_id="raw-media-window",
        screen_id="primary",
        native_handle=123,
        x=0,
        y=0,
        width=1280,
        height=720,
        device_pixel_ratio=1.0,
    )
    controller.set_window_targets((target,))
    assert controller.content_ingress_required
    controller.set_window_targets(())
    assert not controller.content_ingress_required
    assert changes == [True, False, True, False]


def test_raw_native_window_target_is_independent_from_authored_scenes(request) -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("initial-hydrate", "attach-window"),
    )
    target = OutputWindowTarget(
        bus_id=BusId.MEDIA_WINDOWS,
        target_id="projection-preview",
        screen_id="primary",
        native_handle=123,
        x=0,
        y=0,
        width=1280,
        height=720,
        device_pixel_ratio=1.0,
    )

    controller.start_engine()
    controller.set_window_targets((target,))

    assert [targets for _id, targets in engine.window_target_updates] == [(target,)]
    assert controller.desired_scene(BusId.EDITOR) == CAMERA_SCENE_ID


def test_program_window_target_uses_program_without_an_editor_scene(request) -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("initial-hydrate", "program-render", "program-window"),
    )
    target = OutputWindowTarget(
        bus_id=BusId.VIRTUAL_CAMERA,
        target_id="program-window",
        screen_id="primary",
        native_handle=123,
        x=0,
        y=0,
        width=1280,
        height=720,
        device_pixel_ratio=1.0,
    )
    controller.start_engine()

    controller.set_window_targets((target,))

    assert len(engine.snapshots) == 1
    assert [targets for _id, targets in engine.window_target_updates] == [(target,)]
    assert engine.renders[-1] == ("program-render", BusId.VIRTUAL_CAMERA, True)
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID


def test_duplicate_native_window_target_update_is_a_noop(request) -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=(
            "initial-hydrate",
            "attach-window",
        ),
    )
    target = OutputWindowTarget(
        bus_id=BusId.MEDIA_WINDOWS,
        target_id="projection-preview",
        screen_id="primary",
        native_handle=123,
        x=0,
        y=0,
        width=1280,
        height=720,
        device_pixel_ratio=1.0,
    )
    controller.start_engine()
    controller.set_window_targets((target,))
    snapshot_count = len(engine.snapshots)
    update_count = len(engine.window_target_updates)

    controller.set_window_targets((target,))

    assert len(engine.snapshots) == snapshot_count
    assert len(engine.window_target_updates) == update_count


def test_runtime_clears_stale_applied_state_and_rehydrates_a_restarted_engine(request) -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=("initial-hydrate", "restart-hydrate"),
    )
    controller.start_engine()
    assert len(engine.snapshots) == 1
    assert controller.engine_ready
    assert controller.applied_scenes

    engine.emit_health(SceneEngineStatus.FAILED)
    assert not controller.engine_ready
    assert controller.applied_scenes == ()

    engine.generation = "engine-generation-2"
    engine.emit_health(SceneEngineStatus.READY)
    assert controller.engine_ready
    assert len(engine.snapshots) == 2
    assert controller.applied_scenes == controller.desired_scenes


def test_runtime_coalesces_hydration_changes_while_one_request_is_in_flight(request) -> None:
    class _PendingHydrateEngine(_Engine):
        def __init__(self) -> None:
            super().__init__()
            self.hydrations: list[Future[SceneEngineAck]] = []

        def hydrate(
            self,
            snapshot: SceneEngineSnapshot,
            *,
            request_id: str,
            deadline_ms: int,
        ) -> Future[SceneEngineAck]:
            assert deadline_ms > 0
            self.snapshots.append((request_id, snapshot))
            future: Future[SceneEngineAck] = Future()
            self.hydrations.append(future)
            return future

    engine = _PendingHydrateEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("hydrate-1", "hydrate-2"),
    )
    controller.start_engine()
    assert controller.hydration_in_progress
    assert not controller.native_window_routing_ready

    documents.rename_scene(CAMERA_SCENE_ID, "Camera wide")
    documents.rename_scene(CAMERA_SCENE_ID, "Camera overview")

    assert len(engine.snapshots) == 1
    first_request, first_snapshot = engine.snapshots[0]
    engine.hydrations[0].set_result(
        engine._ack(first_request, first_snapshot.sequence, first_snapshot.document.revision)
    )

    assert len(engine.snapshots) == 2
    assert controller.hydration_in_progress
    assert controller.native_window_routing_ready
    assert engine.snapshots[1][1].document.revision == documents.document.revision
    second_request, second_snapshot = engine.snapshots[1]
    engine.hydrations[1].set_result(
        engine._ack(second_request, second_snapshot.sequence, second_snapshot.document.revision)
    )
    assert not controller.hydration_in_progress
    assert controller.native_window_routing_ready
    assert controller.applied_scenes == controller.desired_scenes


def test_applied_native_window_route_remains_ready_during_graph_rehydration(request) -> None:
    class _PendingHydrateEngine(_Engine):
        def __init__(self) -> None:
            super().__init__()
            self.hydrations: list[Future[SceneEngineAck]] = []

        def hydrate(
            self,
            snapshot: SceneEngineSnapshot,
            *,
            request_id: str,
            deadline_ms: int,
        ) -> Future[SceneEngineAck]:
            assert deadline_ms > 0
            self.snapshots.append((request_id, snapshot))
            future: Future[SceneEngineAck] = Future()
            self.hydrations.append(future)
            return future

    engine = _PendingHydrateEngine()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("initial-hydrate", "document-hydrate"),
    )
    controller.start_engine()
    initial_request, initial_snapshot = engine.snapshots[0]
    engine.hydrations[0].set_result(
        engine._ack(
            initial_request,
            initial_snapshot.sequence,
            initial_snapshot.document.revision,
        )
    )
    assert controller.native_window_routing_ready

    documents.rename_scene(CAMERA_SCENE_ID, "Camera wide")

    assert controller.hydration_in_progress
    assert controller.native_window_routing_ready


def test_output_change_during_hydration_is_folded_into_a_new_snapshot(request) -> None:
    class _PendingHydrateEngine(_Engine):
        def __init__(self) -> None:
            super().__init__()
            self.hydrations: list[Future[SceneEngineAck]] = []

        def hydrate(
            self,
            snapshot: SceneEngineSnapshot,
            *,
            request_id: str,
            deadline_ms: int,
        ) -> Future[SceneEngineAck]:
            self.snapshots.append((request_id, snapshot))
            future: Future[SceneEngineAck] = Future()
            self.hydrations.append(future)
            return future

    engine = _PendingHydrateEngine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("hydrate-1", "hydrate-2"),
    )
    controller.start_engine()

    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)

    assert engine.outputs == []
    first_request, first_snapshot = engine.snapshots[0]
    engine.hydrations[0].set_result(
        engine._ack(first_request, first_snapshot.sequence, first_snapshot.document.revision)
    )

    assert len(engine.snapshots) == 2
    assert dict(engine.snapshots[1][1].output_enabled)[BusId.VIRTUAL_CAMERA]
    assert engine.outputs == []
    second_request, second_snapshot = engine.snapshots[1]
    engine.hydrations[1].set_result(
        engine._ack(second_request, second_snapshot.sequence, second_snapshot.document.revision)
    )
    assert controller.applied_scenes == controller.desired_scenes


def test_rejected_hydration_preserves_the_native_error_code(request) -> None:
    class _RejectingHydrateEngine(_Engine):
        def hydrate(
            self,
            snapshot: SceneEngineSnapshot,
            *,
            request_id: str,
            deadline_ms: int,
        ) -> Future[SceneEngineAck]:
            self.snapshots.append((request_id, snapshot))
            return _completed(
                SceneEngineAck(
                    request_id=request_id,
                    session_id=self.session_id,
                    process_generation=self.generation,
                    sequence=snapshot.sequence,
                    document_revision=snapshot.document.revision,
                    applied=False,
                    error_code="source_unavailable",
                    error_message="sensitive native detail",
                )
            )

    engine = _RejectingHydrateEngine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("hydrate",),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)

    controller.start_engine()

    assert controller.engine_ready
    assert not controller.hydration_in_progress
    assert controller.applied_scenes == ()
    assert controller.last_engine_error_code == "source_unavailable"
    assert errors == ["Scene engine hydrate rejected (source_unavailable)"]
    assert "sensitive" not in errors[0]


def test_rejected_output_preserves_the_native_error_code(request) -> None:
    class _RejectingOutputEngine(_Engine):
        def set_output_enabled(
            self,
            bus_id: BusId,
            enabled: bool,
            *,
            request_id: str,
            sequence: int,
            deadline_ms: int,
        ) -> Future[SceneEngineAck]:
            self.outputs.append((request_id, bus_id, enabled))
            return _completed(
                SceneEngineAck(
                    request_id=request_id,
                    session_id=self.session_id,
                    process_generation=self.generation,
                    sequence=sequence,
                    document_revision=self.snapshots[-1][1].document.revision,
                    applied=False,
                    error_code="media_graph_stopped",
                    error_message="sensitive native detail",
                )
            )

    engine = _RejectingOutputEngine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        _Projection(),
        request_ids=("hydrate", "output"),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()

    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)

    # The emitted signal is the durable surface: last_engine_error_code is
    # transient and a later successful command clears it.
    assert errors == ["Scene engine output rejected (media_graph_stopped)"]
    assert "sensitive" not in errors[0]


def test_failed_preparation_cancels_its_native_resource(request) -> None:
    class _FailingPrepareEngine(_Engine):
        def prepare_scene(
            self,
            bus_id: BusId,
            scene_id: str,
            *,
            transition: TransitionSpec,
            document_revision: int,
            request_id: str,
            sequence: int,
            deadline_ms: int,
            content_media_epoch: int | None = None,
            content_source_kind: ContentSourceKind = ContentSourceKind.FRAMES,
        ) -> Future[ScenePreparation]:
            assert deadline_ms > 0
            assert document_revision >= 0
            self.preparations.append((request_id, bus_id, scene_id, sequence))
            return _failed(RuntimeError("synthetic preparation timeout"))

    projection = _Projection()
    engine = _FailingPrepareEngine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=("hydrate", "prepare-media", "prepare-vcam"),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()

    projection.set_type("image")

    assert set(engine.cancelled) >= {"prepare-media", "prepare-vcam"}
    assert errors == [
        "Scene engine prepare failed (unexpected_engine_response)",
        "Scene engine prepare failed (unexpected_engine_response)",
        "Scene engine prepare failed (unexpected_engine_response)",
    ]
    assert controller.desired_scene(BusId.EDITOR) == CONTENT_SCENE_ID
    assert controller.applied_scene(BusId.EDITOR) == CAMERA_SCENE_ID


def test_rejected_preparation_preserves_the_native_error_code(request) -> None:
    class _RejectingPrepareEngine(_Engine):
        def prepare_scene(
            self,
            bus_id: BusId,
            scene_id: str,
            *,
            transition: TransitionSpec,
            document_revision: int,
            request_id: str,
            sequence: int,
            deadline_ms: int,
            content_media_epoch: int | None = None,
            content_source_kind: ContentSourceKind = ContentSourceKind.FRAMES,
        ) -> Future[ScenePreparation]:
            self.preparations.append((request_id, bus_id, scene_id, sequence, transition))
            return _failed(SceneEngineCommandRejectedError("source_unavailable"))

    engine = _RejectingPrepareEngine()
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=("hydrate", "prepare-media", "prepare-vcam"),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()

    projection.set_type("image")

    assert controller.last_engine_error_code == "source_unavailable"
    assert errors == [
        "Scene engine prepare rejected (source_unavailable)",
        "Scene engine prepare rejected (source_unavailable)",
        "Scene engine prepare rejected (source_unavailable)",
    ]
    assert set(engine.cancelled) >= {"prepare-media", "prepare-vcam"}


def test_cancelled_preparation_does_not_publish_a_sticky_engine_error(request) -> None:
    class _CancelledPrepareEngine(_Engine):
        def prepare_scene(
            self,
            bus_id: BusId,
            scene_id: str,
            *,
            transition: TransitionSpec,
            document_revision: int,
            request_id: str,
            sequence: int,
            deadline_ms: int,
            content_media_epoch: int | None = None,
            content_source_kind: ContentSourceKind = ContentSourceKind.FRAMES,
        ) -> Future[ScenePreparation]:
            return _failed(CancelledError())

    engine = _CancelledPrepareEngine()
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=("hydrate", "prepare-media", "prepare-vcam"),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()

    projection.set_type("image")

    assert controller.last_engine_error_code == ""
    assert errors == []


def test_rejected_take_keeps_applied_scene_and_reports_sanitized_error(request) -> None:
    class _RejectingEngine(_Engine):
        def take_prepared(
            self,
            preparation: ScenePreparation,
            *,
            request_id: str,
            sequence: int,
            deadline_ms: int,
        ) -> Future[SceneEngineAck]:
            return _completed(
                SceneEngineAck(
                    request_id=request_id,
                    session_id=self.session_id,
                    process_generation=self.generation,
                    sequence=sequence,
                    document_revision=preparation.document_revision,
                    applied=False,
                    error_code="source_unavailable",
                    error_message="rtsp://operator:secret@camera.local/stream",
                )
            )

    projection = _Projection()
    engine = _RejectingEngine()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(
            "hydrate",
            "prepare-program",
            "take-program",
            "prepare-media",
            "take-media",
        ),
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()

    projection.set_type("image")

    assert controller.desired_scene(BusId.EDITOR) == CONTENT_SCENE_ID
    assert controller.applied_scene(BusId.EDITOR) == CAMERA_SCENE_ID
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert errors == [
        "Scene engine take rejected (source_unavailable)",
        "Scene engine take rejected (source_unavailable)",
        "Scene engine take rejected (source_unavailable)",
    ]
    assert "secret" not in errors[0]


def test_scene_take_waits_for_ptz_positioning_before_cutting(request) -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(
            "hydrate",
            "prepare-program",
            "take-program",
            "prepare-media",
            "take-media",
        ),
        document=_document_with_ptz_action(PtzTimeoutPolicy.KEEP_CURRENT),
        ptz=ptz,
    )
    events: list[ScenePtzEvent] = []
    controller.ptz_event.connect(events.append)
    controller.start_engine()

    projection.set_type("image")

    assert ptz.calls == [(DEFAULT_CAMERA_SOURCE_ID, "preset-wide", 2500)]
    assert len(engine.takes) == 0
    assert controller.applied_scene(BusId.EDITOR) == CAMERA_SCENE_ID
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID

    ptz.futures[0].set_result(_ptz_result(PtzRecallStatus.SUCCEEDED))

    assert len(engine.takes) == 3  # program, projection, editor
    assert controller.applied_scene(BusId.EDITOR) == CONTENT_SCENE_ID
    assert events[0].result.succeeded


@pytest.mark.parametrize("status", [PtzRecallStatus.SUCCEEDED, PtzRecallStatus.FAILED])
def test_replaced_video_ptz_batch_cannot_take_or_block_the_next_presentation(
    request,
    status: PtzRecallStatus,
) -> None:
    class _CancellingPtzExecutor(_PtzExecutor):
        def cancel(self, future):
            super().cancel(future)
            future.cancel()

    projection = _Projection()
    engine = _Engine()
    ptz = _CancellingPtzExecutor()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
        document=_document_with_ptz_action(PtzTimeoutPolicy.KEEP_CURRENT),
        ptz=ptz,
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()
    projection.set_type("video")
    _deliver_controller_results(controller)
    obsolete_request_id = engine.preparations[0][0]
    projection.set_type("image")

    ptz.futures[0].set_result(_ptz_result(status))

    assert len(ptz.futures) == 2
    assert engine.cancelled == [obsolete_request_id]
    assert engine.preparation_content_media_epochs == [1, projection.session_id]
    assert engine.preparation_content_source_kinds == [
        ContentSourceKind.NATIVE_MEDIA, ContentSourceKind.FRAMES,
    ]
    assert engine.takes == []
    assert errors == []

    ptz.futures[1].set_result(_ptz_result(PtzRecallStatus.SUCCEEDED))

    assert len(engine.takes) == 3
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    assert errors == []


@pytest.mark.parametrize("status", [PtzRecallStatus.SUCCEEDED, PtzRecallStatus.FAILED])
def test_ptz_event_can_return_to_idle_without_leaving_a_pending_video_take(
    request,
    status: PtzRecallStatus,
) -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
        document=_document_with_ptz_action(PtzTimeoutPolicy.KEEP_CURRENT),
        ptz=ptz,
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.ptz_event.connect(lambda _event: projection.set_type("idle"))
    controller.start_engine()
    projection.set_type("video")
    _deliver_controller_results(controller)

    ptz.futures[0].set_result(_ptz_result(status))

    assert engine.takes == []
    assert controller._pending == {}
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert errors == []


def test_manual_ptz_motion_is_scaled_stopped_and_can_store_a_preset(request) -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(),
        document=_document_with_ptz_action(PtzTimeoutPolicy.KEEP_CURRENT),
        ptz=ptz,
    )

    moved = controller.move_camera(
        DEFAULT_CAMERA_SOURCE_ID,
        pan=-1.0,
        tilt=0.5,
        speed=0.4,
    ).result()
    stopped = controller.stop_camera(DEFAULT_CAMERA_SOURCE_ID).result()
    stored = controller.store_camera_preset("preset-wide").result()

    assert moved.succeeded and stopped.succeeded and stored.succeeded
    assert ptz.moves == [(DEFAULT_CAMERA_SOURCE_ID, -0.4, 0.2, 0.0)]
    assert ptz.stops == [DEFAULT_CAMERA_SOURCE_ID]
    assert ptz.stored == ["preset-wide"]


def test_keep_current_ptz_policy_blocks_take_on_failure(request) -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(
            "hydrate",
            "prepare-program",
            "prepare-media",
            "take-media",
        ),
        document=_document_with_ptz_action(PtzTimeoutPolicy.KEEP_CURRENT),
        ptz=ptz,
    )
    errors: list[str] = []
    controller.engine_error.connect(errors.append)
    controller.start_engine()
    projection.set_type("image")

    ptz.futures[0].set_result(
        _ptz_result(PtzRecallStatus.TIMED_OUT, "ptz_recall_timeout")
    )

    assert len(engine.takes) == 2  # projection took; the program stayed blocked
    assert engine.cancelled == ["prepare-program"]
    assert controller.applied_scene(BusId.EDITOR) == CONTENT_SCENE_ID
    assert controller.applied_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    assert errors == ["Scene PTZ recall blocked Take (ptz_recall_timeout)"]


def test_take_anyway_ptz_policy_cuts_after_failure(request) -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    _documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(
            "hydrate",
            "prepare-program",
            "take-program",
            "prepare-media",
            "take-media",
        ),
        document=_document_with_ptz_action(PtzTimeoutPolicy.TAKE_ANYWAY),
        ptz=ptz,
    )
    controller.start_engine()
    projection.set_type("image")

    ptz.futures[0].set_result(
        _ptz_result(PtzRecallStatus.FAILED, "ptz_authentication_failed")
    )

    assert len(engine.takes) == 3  # program, projection, editor
    assert controller.applied_scene(BusId.EDITOR) == CONTENT_SCENE_ID


def test_document_change_cancels_in_flight_ptz_and_native_preparation(request) -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    documents, _runtime, controller = _runtime_controller(
        request,
        engine,
        projection,
        request_ids=(
            "hydrate",
            "prepare-program",
            "rehydrate",
        ),
        document=_document_with_ptz_action(PtzTimeoutPolicy.KEEP_CURRENT),
        ptz=ptz,
    )
    controller.start_engine()
    projection.set_type("image")

    documents.rename_scene(CAMERA_SCENE_ID, "Renamed camera")

    assert ptz.cancelled == [ptz.futures[0]]
    assert engine.cancelled == ["prepare-program"]
    assert len(engine.snapshots) == 2


def test_projection_and_program_can_show_different_scenes(request) -> None:
    # The point of the whole two-output change: the room's screen and the virtual
    # camera are routed independently.
    engine = _Engine()
    _documents, runtime, controller = _runtime_controller(
        request,
        engine, _Projection(), request_ids=("hydrate",)
    )
    controller.start_engine()

    # both differ from the shared starting default, so both really move
    controller.take_scene(BusId.VIRTUAL_CAMERA, CONTENT_SCENE_ID)
    controller.take_scene(BusId.MEDIA_WINDOWS, CONTENT_CAMERA_PIP_SCENE_ID)

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CONTENT_SCENE_ID
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == CONTENT_CAMERA_PIP_SCENE_ID
    # each output holds its own runtime selection
    assert runtime.state.output(BusId.VIRTUAL_CAMERA).manual_scene_id == CONTENT_SCENE_ID
    assert (
        runtime.state.output(BusId.MEDIA_WINDOWS).manual_scene_id
        == CONTENT_CAMERA_PIP_SCENE_ID
    )
    # and each was prepared on its own bus
    prepared = {bus_id: scene_id for _id, bus_id, scene_id, *_ in engine.preparations}
    assert prepared[BusId.VIRTUAL_CAMERA] == CONTENT_SCENE_ID
    assert prepared[BusId.MEDIA_WINDOWS] == CONTENT_CAMERA_PIP_SCENE_ID


def test_each_output_keeps_its_own_default_scene_across_a_save() -> None:
    from solin.core.scenes.model import SceneDocument

    documents = SceneDocumentService(_document())
    documents.set_program_default_scene(CAMERA_SCENE_ID)
    diverged = replace(
        documents.document,
        outputs=tuple(
            replace(route, default_scene_id=CONTENT_SCENE_ID)
            if route.bus_id is BusId.MEDIA_WINDOWS
            else route
            for route in documents.document.outputs
        ),
    )

    restored = SceneDocument.from_record(diverged.to_record())

    assert restored.output(BusId.MEDIA_WINDOWS).default_scene_id == CONTENT_SCENE_ID
    assert restored.output(BusId.VIRTUAL_CAMERA).default_scene_id == CAMERA_SCENE_ID


def test_content_is_playing_tracks_what_the_content_channel_carries(request) -> None:
    """Idle blanks the content source — unless an idle video/image feeds it."""
    projection = _Projection()
    _, _, controller = _runtime_controller(
        request,
        _Engine(), projection, request_ids=("r1",)
    )

    assert controller.content_is_playing is False

    projection.set_type("video")
    assert controller.content_is_playing is True

    projection.set_type("idle")
    assert controller.content_is_playing is False

    # A configured idle video keeps the channel fed while the state stays idle.
    projection.idle_media_path = "loop.mp4"
    assert controller.content_is_playing is True
