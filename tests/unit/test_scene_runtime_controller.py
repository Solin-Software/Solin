from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from dataclasses import replace
from pathlib import Path

import pytest

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
    FrameProducerKind,
    LocalCameraDevice,
    LocalCameraDiscovery,
    LocalCameraProbe,
    LocalCameraProbeStatus,
    LocalVideoFormat,
    OutputWindowTarget,
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
    DEFAULT_CAMERA_SOURCE_ID,
    LocalCameraConfig,
    NormalizedRect,
    OnvifPtzBinding,
    OutputMode,
    PtzTimeoutPolicy,
    RecallPtzPresetAction,
    SceneDocument,
    SceneValidationError,
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
from solin.core.scenes.process_engine import SceneEngineCommandRejectedError
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
from solin.core.scenes.workspace import SceneWorkspaceService


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
    ) -> Future[ScenePreparation]:
        assert deadline_ms > 0
        self.preparations.append((request_id, bus_id, scene_id, sequence, transition))
        self.preparation_content_media_epochs.append(content_media_epoch)
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


def test_preview_geometry_coalesces_mouse_moves_without_hydrating_each_frame() -> None:
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
    controller.close()


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
    engine: _Engine,
    projection: _Projection,
    *,
    request_ids: tuple[str, ...],
    document: SceneDocument | None = None,
    ptz=None,
) -> tuple[SceneDocumentService, SceneRuntimeService, SceneRuntimeController]:
    documents = SceneDocumentService(document or _document())
    runtime = SceneRuntimeService(
        documents,
        create_default_runtime_state(documents.document),
    )
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
    return documents, runtime, controller


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


def test_content_transport_hydration_preserves_live_program_before_animated_take() -> None:
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
    controller.close()


def test_content_transport_retirement_waits_for_the_animated_program_exit() -> None:
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
    controller.close()


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


def test_source_health_is_retained_until_ready_or_stopped() -> None:
    engine = _Engine()
    _documents, runtime, controller = _runtime_controller(
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
    controller.close()
    runtime.close()
    assert content_category_for_projection({"type": "future_source"}) is (
        ContentCategory.EXTERNAL_STREAM
    )


def test_scene_profile_is_published_only_after_native_hydration_ack(tmp_path: Path) -> None:
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

    workspace, collection = _real_workspace(tmp_path)
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

    workspace, collection = _real_workspace(tmp_path)
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
    controller.start_engine()

    controller.activate_scene_profile(collection.id)

    assert workspace.active_collection.id == previous_id
    assert controller.document.document_id == "default-document"
    assert controller.last_engine_error_code == "source_unavailable"


def _real_workspace(tmp_path: Path):
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="profile-a",
    )
    paths.ensure_dirs()
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = SceneWorkspaceService(
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


def test_live_scene_deletion_requires_and_takes_the_chosen_replacement() -> None:
    documents, runtime, controller = _runtime_controller(
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
    controller.close()


def test_runtime_refreshes_local_cameras_without_blocking_the_caller() -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_runtime_coalesces_camera_refreshes_while_discovery_is_in_flight() -> None:
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
    controller.close()


def test_reload_yeartext_forwards_a_notification_to_the_engine() -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
        engine, projection, request_ids=("hydrate",)
    )

    controller.reload_yeartext()

    assert getattr(engine, "yeartext_reloads", 0) == 1
    controller.close()


def test_committing_a_layer_resize_does_not_rehydrate() -> None:
    # Resizing/moving a source in the canvas must NOT rebuild the scene graph
    # (which re-opens every source, e.g. the camera). It should be applied live.
    projection = _Projection()
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_runtime_prepares_and_takes_without_full_snapshot_on_hot_path() -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()
    assert engine.stopped
    assert not controller.engine_ready
    assert readiness == [True, False]


def test_manual_take_reconciles_when_the_saved_auto_return_scene_is_unchanged() -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, runtime, controller = _runtime_controller(
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
    controller.close()


def test_auto_media_returns_to_the_previous_program_base_scene() -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
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
    controller.close()


def test_audio_only_playback_does_not_trigger_native_scene_auto_switch() -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
        _Engine(),
        projection,
        request_ids=(),
    )

    projection.set_state({"type": "video", "is_audio": True})

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID
    controller.close()


def test_return_scene_override_does_not_take_program_until_media_ends() -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_auto_media_does_not_return_after_operator_takes_a_scene_without_content() -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_auto_media_returns_after_operator_takes_another_content_scene() -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
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
    controller.close()


def test_taking_a_content_scene_resumes_a_suspended_media_session() -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
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
    controller.close()


def test_taking_the_live_automatic_media_scene_is_a_noop() -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
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
    controller.close()


def test_taking_media_again_resumes_a_suspended_automatic_media_session() -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_resume_program_automation_reconciles_when_runtime_state_is_unchanged() -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_reenabling_program_automation_restores_the_session_return_base() -> None:
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_disabling_automation_preserves_the_latest_desired_scene_during_take() -> None:
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
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
    controller.close()


def test_disabled_previous_scene_memory_returns_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "solin.controllers.scene_runtime_controller.MEMORIZE_PRE_MEDIA_SCENE",
        False,
    )
    projection = _Projection()
    _documents, runtime, controller = _runtime_controller(
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
    controller.close()


def test_transition_policy_changes_do_not_rehydrate_and_preview_always_cuts() -> None:
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_policy_only_revision_uses_hydrated_revision_for_auxiliary_commands() -> None:
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_transition_fallback_is_reported_without_marking_engine_failed() -> None:
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
    controller.close()


def test_auto_switch_resolves_transition_for_each_destination_scene() -> None:
    projection = _Projection()
    engine = _Engine()
    documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_runtime_coalesces_rapid_scene_selection_while_prepare_is_in_flight() -> None:
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
        ) -> Future[ScenePreparation]:
            assert deadline_ms > 0
            self.preparations.append((request_id, bus_id, scene_id, sequence, transition))
            self.preparation_content_media_epochs.append(content_media_epoch)
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
    controller.close()


def test_editor_preview_uses_its_own_channel_without_changing_program() -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_editor_preview_transport_is_idle_until_the_editor_requests_frames() -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_preview_demand_is_published_when_the_media_scene_is_already_selected() -> None:
    projection = _Projection()
    projection.set_type("image")
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_editor_preview_remains_independent_while_program_is_mirrored() -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_native_window_target_updates_without_rehydrating_the_scene_graph() -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_editor_preview_target_merges_with_projection_window_targets() -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_content_ingress_demand_follows_visible_routes_and_scene_sources() -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_raw_native_window_target_is_independent_from_authored_scenes() -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_program_window_target_uses_program_without_an_editor_scene() -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_duplicate_native_window_target_update_is_a_noop() -> None:
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_runtime_clears_stale_applied_state_and_rehydrates_a_restarted_engine() -> None:
    projection = _Projection()
    engine = _Engine()
    _documents, _runtime, controller = _runtime_controller(
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


def test_runtime_coalesces_hydration_changes_while_one_request_is_in_flight() -> None:
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
    controller.close()


def test_applied_native_window_route_remains_ready_during_graph_rehydration() -> None:
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
    controller.close()


def test_output_change_during_hydration_is_folded_into_a_new_snapshot() -> None:
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
    controller.close()


def test_rejected_hydration_preserves_the_native_error_code() -> None:
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
    controller.close()


def test_rejected_output_preserves_the_native_error_code() -> None:
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
    controller.close()


def test_failed_preparation_cancels_its_native_resource() -> None:
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
        ) -> Future[ScenePreparation]:
            assert deadline_ms > 0
            assert document_revision >= 0
            self.preparations.append((request_id, bus_id, scene_id, sequence))
            return _failed(RuntimeError("synthetic preparation timeout"))

    projection = _Projection()
    engine = _FailingPrepareEngine()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_rejected_preparation_preserves_the_native_error_code() -> None:
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
        ) -> Future[ScenePreparation]:
            self.preparations.append((request_id, bus_id, scene_id, sequence, transition))
            return _failed(SceneEngineCommandRejectedError("source_unavailable"))

    engine = _RejectingPrepareEngine()
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_cancelled_preparation_does_not_publish_a_sticky_engine_error() -> None:
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
        ) -> Future[ScenePreparation]:
            return _failed(CancelledError())

    engine = _CancelledPrepareEngine()
    projection = _Projection()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_rejected_take_keeps_applied_scene_and_reports_sanitized_error() -> None:
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


def test_scene_take_waits_for_ptz_positioning_before_cutting() -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_manual_ptz_motion_is_scaled_stopped_and_can_store_a_preset() -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_keep_current_ptz_policy_blocks_take_on_failure() -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_take_anyway_ptz_policy_cuts_after_failure() -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    _documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_document_change_cancels_in_flight_ptz_and_native_preparation() -> None:
    projection = _Projection()
    engine = _Engine()
    ptz = _PtzExecutor()
    documents, _runtime, controller = _runtime_controller(
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
    controller.close()


def test_projection_and_program_can_show_different_scenes() -> None:
    # The point of the whole two-output change: the room's screen and the virtual
    # camera are routed independently.
    engine = _Engine()
    _documents, runtime, controller = _runtime_controller(
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
    controller.close()


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
