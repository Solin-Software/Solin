from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass
from typing import Any, Protocol

from PySide6.QtCore import QObject, Signal, Slot

from solin.core.foundation.constants import MEMORIZE_PRE_MEDIA_SCENE
from solin.core.projection.application import projection_presentation_type
from solin.core.scenes.application import SceneDocumentChange, SceneDocumentService
from solin.core.scenes.composition import scene_uses_content_source
from solin.core.scenes.engine import (
    DEFAULT_ENGINE_STARTUP_DEADLINE_MS,
    EngineHealthEvent,
    FrameChannelDescriptor,
    LocalCameraDiscovery,
    OutputWindowTarget,
    ProgramRecordingEvent,
    SceneEngine,
    SceneEngineAck,
    SceneEngineCapabilities,
    SceneEngineEvent,
    SceneEngineSnapshot,
    SceneEngineStatus,
    ScenePreparation,
    SourceHealthEvent,
    SourceHealthStatus,
    scene_engine_graph_signature,
)
from solin.core.scenes.model import (
    AUTOMATIC_MEDIA_CATEGORIES,
    BusId,
    ContentCategory,
    OutputMode,
    PtzBinding,
    SceneDocument,
    SceneLayer,
    SceneValidationError,
    TransitionKind,
    TransitionSpec,
    new_identity,
)
from solin.core.scenes.model import (
    LocalCameraConfig,
    PtzTimeoutPolicy,
    RecallPtzPresetAction,
    RtspCameraConfig,
)
from solin.core.scenes.ptz import (
    PtzControlKind,
    PtzControlResult,
    PtzRecallExecutor,
    PtzRecallResult,
    PtzRecallStatus,
)
from solin.core.scenes.process_engine import (
    SceneEngineCommandRejectedError,
    SceneEngineNotReadyError,
    SceneEngineProcessError,
    SceneEngineProtocolError,
    SceneEngineRequestTimeoutError,
)
from solin.core.scenes.runtime import SceneRuntimeService, SceneRuntimeState
from solin.core.scenes.workspace import (
    SceneWorkspaceActivation,
    SceneWorkspaceChange,
    SceneWorkspaceChangeKind,
    SceneWorkspaceService,
)


_START_DEADLINE_MS = DEFAULT_ENGINE_STARTUP_DEADLINE_MS
_HYDRATE_DEADLINE_MS = 12000
_PREPARE_DEADLINE_MS = 1500
_TAKE_DEADLINE_MS = 1000
_PREVIEW_GEOMETRY_DEADLINE_MS = 500
_CAMERA_DISCOVERY_DEADLINE_MS = 3000

log = logging.getLogger(__name__)

_CATEGORY_BY_PROJECTION_TYPE = {
    "idle": ContentCategory.IDLE,
    "image": ContentCategory.IMAGE,
    "video": ContentCategory.VIDEO,
    "timer": ContentCategory.TIMER,
    "browser": ContentCategory.BROWSER,
    "obs_stream": ContentCategory.EXTERNAL_STREAM,
}


class ProjectionStateSource(Protocol):
    @property
    def state(self) -> Mapping[str, Any]: ...

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...


@dataclass(frozen=True, slots=True)
class _PendingTake:
    scene_id: str
    prepare_request_id: str
    prepare_sequence: int
    document_revision: int
    take_request_id: str = ""
    take_sequence: int = -1


@dataclass(frozen=True, slots=True)
class ScenePtzEvent:
    bus_id: BusId
    scene_id: str
    camera_source_id: str
    preset_id: str
    policy: PtzTimeoutPolicy
    result: PtzRecallResult


@dataclass(frozen=True, slots=True)
class _PtzActionContext:
    bus_id: BusId
    expected: _PendingTake
    preparation: ScenePreparation
    action: RecallPtzPresetAction
    camera_source_id: str
    future: Future[PtzRecallResult]


@dataclass(slots=True)
class _PendingPtzBatch:
    expected: _PendingTake
    preparation: ScenePreparation
    remaining: set[Future[PtzRecallResult]]
    blocking_error_codes: list[str]


@dataclass(frozen=True, slots=True)
class _PendingProfileActivation:
    request_id: str
    snapshot: SceneEngineSnapshot
    activation: SceneWorkspaceActivation


@dataclass(frozen=True, slots=True)
class _PendingLayerPreview:
    request_id: str
    sequence: int
    document_revision: int
    scene_id: str
    layer: SceneLayer


@dataclass(frozen=True, slots=True)
class _PendingWindowTargets:
    request_id: str
    sequence: int
    document_revision: int
    targets: tuple[OutputWindowTarget, ...]


@dataclass(frozen=True, slots=True)
class _AutomaticMediaSceneSelection:
    projection_session_id: int
    scene_id: str
    return_scene_id: str | None


def content_category_for_projection(state: Mapping[str, Any]) -> ContentCategory:
    state_type = projection_presentation_type(state)
    return _CATEGORY_BY_PROJECTION_TYPE.get(
        state_type,
        ContentCategory.EXTERNAL_STREAM,
    )


class SceneRuntimeController(QObject):
    """Reconciles desired, pending, and applied state without blocking Qt."""

    document_changed = Signal(object)
    desired_scenes_changed = Signal(object)
    applied_scenes_changed = Signal(object)
    engine_capabilities_changed = Signal(object)
    engine_ready_changed = Signal(bool)
    engine_event = Signal(object)
    ptz_event = Signal(object)
    engine_error = Signal(str)
    transition_fallback = Signal(str)
    operational_state_changed = Signal()
    local_cameras_changed = Signal(object)
    content_ingress_demand_changed = Signal(bool)
    source_health_changed = Signal(str)
    preview_scene_changed = Signal(object)
    preview_frame_changed = Signal(str, object)
    preview_egress_changed = Signal(object)
    scene_profiles_changed = Signal(object)
    runtime_changed = Signal(object)
    _async_result = Signal(object)
    _async_engine_event = Signal(object)

    def __init__(
        self,
        workspace: SceneWorkspaceService,
        projection: ProjectionStateSource,
        *,
        engine: SceneEngine | None = None,
        ptz: PtzRecallExecutor | None = None,
        request_id_factory: Callable[[], str] = new_identity,
        session_id: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._workspace = workspace
        self._documents = workspace.documents
        self._runtime = workspace.runtime
        self._projection = projection
        self._engine = engine
        self._ptz = ptz
        self._request_id_factory = request_id_factory
        self._session_id = session_id or new_identity()
        self._sequence = 0
        self._engine_started = False
        self._engine_ready = False
        self._process_generation = ""
        self._hydrate_in_flight: tuple[str, SceneEngineSnapshot] | None = None
        self._hydrate_dirty = False
        self._engine_document_revision = 0
        self._observed_graph_record = scene_engine_graph_signature(self._documents.document)
        self._preview_geometry_in_flight: _PendingLayerPreview | None = None
        self._queued_preview_geometry: tuple[str, SceneLayer, int] | None = None
        self._profile_activation: _PendingProfileActivation | None = None
        self._profile_preview_scene_id: str | None = None
        self._delete_after_profile_activation = ""
        self._committing_hydrated_profile = False
        self._last_engine_error_code = ""
        self._content_ingress: FrameChannelDescriptor | None = None
        self._preview_egress: FrameChannelDescriptor | None = None
        self._program_egress: FrameChannelDescriptor | None = None
        self._program_recording_required = False
        self._preview_scene_id: str | None = None
        self._window_targets: tuple[OutputWindowTarget, ...] = ()
        # A standalone MEDIA_WINDOWS target for the editor's direct-GPU preview,
        # merged with the projection targets on dispatch. Independent of the
        # projection native_presentation gate so the editor gets a GPU preview
        # even where projection stays on the app-side path.
        self._editor_preview_target: OutputWindowTarget | None = None
        self._local_cameras = _unavailable_local_cameras("engine_unavailable")
        self._source_health: dict[str, SourceHealthEvent] = {}
        self._local_camera_request_id = ""
        self._local_camera_future: Future[LocalCameraDiscovery] | None = None
        self._suspended_media_session_id: int | None = None
        self._automatic_media_scene_selection: _AutomaticMediaSceneSelection | None = None
        self._last_projection_session_id = self._projection_session_id()
        self._desired_scenes = self._resolve_desired_scenes()
        self._applied_scenes: tuple[tuple[BusId, str], ...] = ()
        self._pending: dict[BusId, _PendingTake] = {}
        self._failed_takes: set[tuple[BusId, str]] = set()
        self._ptz_batches: dict[BusId, _PendingPtzBatch] = {}
        self._moving_camera_bindings: dict[str, PtzBinding] = {}
        self._last_destination_enabled = self._destination_enabled()
        self._last_render_enabled = self._render_enabled()
        self._last_content_ingress_required = self._content_ingress_required()
        self._async_result.connect(self._consume_async_result)
        self._async_engine_event.connect(self._consume_engine_event)
        self._unsubscribe_document = self._documents.subscribe(self._on_document_changed)
        self._unsubscribe_runtime = self._runtime.subscribe(self._on_runtime_changed)
        self._unsubscribe_workspace = workspace.subscribe(self._on_workspace_changed)
        self._unsubscribe_projection = projection.subscribe(self._on_projection_changed)
        self._unsubscribe_engine = (
            engine.subscribe(self._on_engine_event) if engine is not None else None
        )

    @property
    def documents(self) -> SceneDocumentService:
        return self._documents

    @property
    def runtime(self) -> SceneRuntimeService:
        return self._runtime

    @property
    def workspace(self) -> SceneWorkspaceService:
        return self._workspace

    @property
    def document(self) -> SceneDocument:
        return self._documents.document

    @property
    def desired_scenes(self) -> tuple[tuple[BusId, str], ...]:
        return self._desired_scenes

    @property
    def applied_scenes(self) -> tuple[tuple[BusId, str], ...]:
        return self._applied_scenes

    @property
    def engine_configured(self) -> bool:
        return self._engine is not None

    @property
    def ptz_controls_available(self) -> bool:
        return callable(getattr(self._ptz, "move", None)) and callable(
            getattr(self._ptz, "stop", None)
        )

    @property
    def engine_ready(self) -> bool:
        return self._engine_ready

    @property
    def native_window_routing_ready(self) -> bool:
        """Whether native windows have an applied graph they can keep presenting."""
        return self._engine_ready and len(self._applied_scenes) == len(BusId)

    @property
    def local_cameras(self) -> LocalCameraDiscovery:
        return self._local_cameras

    def source_health(self, source_id: str) -> SourceHealthEvent | None:
        return self._source_health.get(source_id)

    @property
    def content_ingress_required(self) -> bool:
        return self._content_ingress_required()

    @property
    def program_recording_required(self) -> bool:
        return self._program_recording_required

    @property
    def hydration_in_progress(self) -> bool:
        return self._hydrate_in_flight is not None or self._profile_activation is not None

    @property
    def profile_activation_in_progress(self) -> bool:
        return self._profile_activation is not None

    @property
    def preview_scene_id(self) -> str | None:
        return self._preview_scene_id

    @property
    def preview_egress(self) -> FrameChannelDescriptor | None:
        return self._preview_egress

    @property
    def last_engine_error_code(self) -> str:
        return self._last_engine_error_code

    def desired_scene(self, bus_id: BusId) -> str:
        return _scene_for_bus(self._desired_scenes, bus_id)

    def applied_scene(self, bus_id: BusId) -> str | None:
        return next(
            (
                scene_id
                for candidate_bus_id, scene_id in self._applied_scenes
                if candidate_bus_id is bus_id
            ),
            None,
        )

    def take_program_scene(self, scene_id: str) -> SceneRuntimeState:
        self._documents.document.scene(scene_id)
        runtime = self._runtime.state.output(BusId.VIRTUAL_CAMERA)
        category = content_category_for_projection(self._projection.state)
        automatic_media_session = (
            runtime.mode is OutputMode.AUTO and category in AUTOMATIC_MEDIA_CATEGORIES
        )
        safe_media_scene = (
            automatic_media_session and self._scene_keeps_media_automation(scene_id)
        )
        if automatic_media_session:
            previous_suspension = self._suspended_media_session_id
            previous_selection = self._automatic_media_scene_selection
            selection = self._media_session_selection(scene_id)
            self._automatic_media_scene_selection = selection
            self._suspended_media_session_id = (
                None if safe_media_scene else selection.projection_session_id
            )
            try:
                if safe_media_scene:
                    state = self._runtime.state
                    if previous_suspension == selection.projection_session_id:
                        state = self._runtime.select_program_scene(
                            selection.return_scene_id
                        )
                else:
                    state = self._runtime.select_program_scene(scene_id)
            except Exception:  # noqa: BLE001 - restore transient state on write failure
                self._suspended_media_session_id = previous_suspension
                self._automatic_media_scene_selection = previous_selection
                raise
            self._reconcile_desired(prepare=True)
            return state
        if scene_id == self.desired_scene(BusId.VIRTUAL_CAMERA):
            return self._runtime.state
        state = self._runtime.select_program_scene(scene_id)
        self._reconcile_desired(prepare=True)
        return state

    @property
    def program_automation_suspended(self) -> bool:
        runtime = self._runtime.state.output(BusId.VIRTUAL_CAMERA)
        category = content_category_for_projection(self._projection.state)
        return (
            runtime.mode is OutputMode.AUTO
            and category in AUTOMATIC_MEDIA_CATEGORIES
            and self._suspended_media_session_id == self._projection_session_id()
        )

    @property
    def program_return_scene_id(self) -> str:
        """Return the current automatic-media exit target."""

        runtime = self._runtime.state.output(BusId.VIRTUAL_CAMERA)
        scene_ids = {scene.id for scene in self._documents.document.scenes}
        media_scene_id = self._documents.program_media_scene_id
        if runtime.manual_scene_id in scene_ids and runtime.manual_scene_id != media_scene_id:
            return runtime.manual_scene_id
        default_scene_id = self._documents.program_default_scene_id
        return default_scene_id if default_scene_id in scene_ids else ""

    @property
    def program_return_override_available(self) -> bool:
        runtime = self._runtime.state.output(BusId.VIRTUAL_CAMERA)
        category = content_category_for_projection(self._projection.state)
        desired_scene_id = self.desired_scene(BusId.VIRTUAL_CAMERA)
        return bool(
            runtime.mode is OutputMode.AUTO
            and not self.program_automation_suspended
            and category in AUTOMATIC_MEDIA_CATEGORIES
            and self._scene_keeps_media_automation(desired_scene_id)
            and self.applied_scene(BusId.VIRTUAL_CAMERA) == desired_scene_id
        )

    def set_program_return_scene(self, scene_id: str) -> SceneRuntimeState:
        """Override where automatic Program returns after the active media."""

        self._documents.document.scene(scene_id)
        if not self.program_return_override_available:
            raise SceneValidationError(
                "Program return can only change while the automatic media scene is live"
            )
        if scene_id == self._documents.program_media_scene_id:
            raise SceneValidationError("Program return scene must differ from media scene")
        state = self._runtime.select_program_scene(scene_id)
        selection = self._automatic_media_scene_selection
        if (
            selection is not None
            and selection.projection_session_id == self._projection_session_id()
        ):
            self._automatic_media_scene_selection = _AutomaticMediaSceneSelection(
                projection_session_id=selection.projection_session_id,
                scene_id=selection.scene_id,
                return_scene_id=scene_id,
            )
        return state

    def resume_program_automation(self) -> SceneRuntimeState:
        if not self._documents.program_automation_configured:
            raise SceneValidationError(
                "Automatic switching requires different default and media scenes"
            )
        self._restore_media_session_return_base()
        state = self._runtime.resume_program_automation()
        self._reconcile_desired(prepare=True)
        return state

    def set_program_automatic(self, enabled: bool) -> SceneRuntimeState:
        if enabled and not self._documents.program_automation_configured:
            raise SceneValidationError(
                "Automatic switching requires different default and media scenes"
            )
        current_scene_id = self.desired_scene(BusId.VIRTUAL_CAMERA)
        if enabled:
            self._restore_media_session_return_base()
        else:
            self._suspended_media_session_id = None
            self._automatic_media_scene_selection = None
        media_scene_id = self._documents.program_media_scene_id
        automatic_base_scene_id = (
            self._documents.program_default_scene_id
            if enabled
            and media_scene_id
            and self._runtime.state.output(BusId.VIRTUAL_CAMERA).manual_scene_id == media_scene_id
            else None
        )
        state = self._runtime.set_program_automatic(
            bool(enabled),
            current_scene_id=current_scene_id,
            automatic_base_scene_id=automatic_base_scene_id,
        )
        self._reconcile_desired(prepare=True)
        return state

    def set_output_enabled(self, bus_id: BusId, enabled: bool) -> SceneRuntimeState:
        return self._runtime.set_output_enabled(bus_id, enabled)

    def program_scene_is_live(self, scene_id: str) -> bool:
        """Return whether an enabled Program destination uses or is applying the scene."""

        self._documents.document.scene(scene_id)
        program_enabled = any(output.enabled for output in self._runtime.state.outputs)
        return program_enabled and scene_id in {
            self.desired_scene(BusId.VIRTUAL_CAMERA),
            self.applied_scene(BusId.VIRTUAL_CAMERA),
        }

    def delete_scene(
        self,
        scene_id: str,
        *,
        live_replacement_scene_id: str | None = None,
    ) -> SceneDocument:
        """Delete configuration references after safely moving a live Program scene."""

        self._documents.document.scene(scene_id)
        is_live = self.program_scene_is_live(scene_id)
        if is_live and live_replacement_scene_id is None:
            raise SceneValidationError("A live scene requires a replacement")
        if not is_live and live_replacement_scene_id is not None:
            raise SceneValidationError("A non-live scene does not accept a replacement")
        if live_replacement_scene_id is not None:
            if live_replacement_scene_id == scene_id:
                raise SceneValidationError("Live replacement scene must be different")
            self._documents.document.scene(live_replacement_scene_id)
            self.take_program_scene(live_replacement_scene_id)
        return self._documents.delete_scene(scene_id)

    def activate_scene_profile(self, collection_id: str) -> Future[SceneEngineAck] | None:
        if self._profile_activation is not None:
            raise RuntimeError("A Scene profile switch is already in progress")
        self.stop_all_camera_movements()
        activation = self._workspace.prepare_collection_activation(collection_id)
        target_scene_ids = {scene.id for scene in activation.documents.document.scenes}
        self._profile_preview_scene_id = (
            self._preview_scene_id
            if self._preview_scene_id in target_scene_ids
            else activation.documents.document.scenes[0].id
            if self._preview_scene_id is not None
            else None
        )
        if self._engine is None or not self._engine_ready:
            try:
                self._workspace.commit_collection_activation(activation)
            finally:
                self._profile_preview_scene_id = None
            return None
        self._cancel_all_pending(cancel_native=True)
        request_id = self._request_id_factory()
        sequence = self._next_sequence()
        category = content_category_for_projection(self._projection.state)
        program_scene = activation.runtime.resolve_scene(BusId.VIRTUAL_CAMERA, category)
        active_scenes = (
            (BusId.MEDIA_WINDOWS, self._profile_preview_scene_id or program_scene),
            (BusId.VIRTUAL_CAMERA, program_scene),
        )
        output_enabled = tuple(
            (
                bus_id,
                activation.runtime.state.output(bus_id).enabled,
            )
            for bus_id in BusId
        )
        preview_required = self._profile_preview_scene_id is not None
        program_required = (
            activation.runtime.state.output(BusId.VIRTUAL_CAMERA).enabled
            or activation.runtime.state.output(BusId.MEDIA_WINDOWS).enabled
            or self._program_egress is not None
            or self._program_recording_required
            or any(target.bus_id is BusId.VIRTUAL_CAMERA for target in self._window_targets)
        )
        snapshot = SceneEngineSnapshot(
            session_id=self._session_id,
            sequence=sequence,
            document=activation.documents.document,
            active_scenes=active_scenes,
            render_enabled=(
                (BusId.MEDIA_WINDOWS, preview_required),
                (BusId.VIRTUAL_CAMERA, program_required),
            ),
            output_enabled=output_enabled,
            content_ingress=self._content_ingress,
            preview_egress=self._preview_egress,
            program_egress=self._program_egress,
            window_targets=self._window_targets,
        )
        context = _PendingProfileActivation(
            request_id=request_id,
            snapshot=snapshot,
            activation=activation,
        )
        self._profile_activation = context
        self.operational_state_changed.emit()
        future = self._engine.hydrate(
            snapshot,
            request_id=request_id,
            deadline_ms=_HYDRATE_DEADLINE_MS,
        )
        self._track_future(future, "profile_hydrate", context)
        return future

    def delete_scene_profile(
        self,
        collection_id: str,
        *,
        replacement_collection_id: str,
    ) -> None:
        if collection_id != self._workspace.catalog.active_collection_id:
            self._workspace.delete_collection(collection_id)
            return
        self._delete_after_profile_activation = collection_id
        try:
            future = self.activate_scene_profile(replacement_collection_id)
        except Exception:  # noqa: BLE001 - restore staged deletion state before propagation
            self._delete_after_profile_activation = ""
            raise
        if future is None:
            self._workspace.delete_collection(collection_id)
            self._delete_after_profile_activation = ""

    def set_preview_scene(self, scene_id: str | None) -> None:
        if scene_id is not None:
            self._documents.document.scene(scene_id)
        if scene_id == self._preview_scene_id:
            return
        self._preview_scene_id = scene_id
        self._reconcile_content_ingress_demand()
        self._reconcile_engine_outputs()
        self._reconcile_desired(prepare=True)
        # Preview demand is independent from the resolved Media bus scene.  If
        # both scene ids already match, desired_scenes_changed is intentionally
        # silent, but the application still has to attach/detach the frame
        # egress used by the editor.
        self.preview_scene_changed.emit(scene_id)

    def preview_layer_geometry(self, scene_id: str, layer: SceneLayer) -> None:
        scene = self._documents.document.scene(scene_id)
        authored = next(
            (candidate for candidate in scene.layers if candidate.id == layer.id),
            None,
        )
        if authored is None or authored.source_id != layer.source_id:
            raise SceneValidationError("Preview layer does not match the scene document")
        self._queued_preview_geometry = (
            scene_id,
            layer,
            self._engine_document_revision,
        )
        self._dispatch_preview_geometry()

    @Slot(object)
    def publish_preview_frame(self, image: object) -> None:
        scene_id = self.applied_scene(BusId.MEDIA_WINDOWS)
        if scene_id is not None:
            self.preview_frame_changed.emit(scene_id, image)

    def start_engine(self) -> Future[SceneEngineCapabilities] | None:
        if self._engine is None:
            return None
        if self._engine_started:
            raise RuntimeError("Scene engine is already started")
        self._engine_started = True
        future = self._engine.start(
            session_id=self._session_id,
            deadline_ms=_START_DEADLINE_MS,
        )
        self._track_future(future, "start", None)
        return future

    @Slot()
    def reload_yeartext(self) -> None:
        """Notify the engine to re-read the year-text source image in place.

        Called after the app re-renders the year-text PNG so a mid-session change
        shows without a full re-hydrate. A no-op when no engine hosts the source."""
        if self._engine is not None:
            self._engine.reload_yeartext()

    @Slot(object)
    def set_content_ingress(self, descriptor: FrameChannelDescriptor | None) -> None:
        if descriptor == self._content_ingress:
            return
        self._content_ingress = descriptor
        if self._pending:
            desired_program = self.desired_scene(BusId.VIRTUAL_CAMERA)
            applied_program = self.applied_scene(BusId.VIRTUAL_CAMERA)
            entering_content_scene = (
                desired_program != applied_program
                and scene_uses_content_source(
                    self._documents.document,
                    desired_program,
                )
            )
            if entering_content_scene:
                # A preparation bound to the previous SHM/D3D11 generation can
                # only animate stale pixels. Rebuild the currently live scene on
                # the new registry generation first, then prepare the desired
                # content scene against that same generation.
                self._cancel_all_pending(cancel_native=self._engine_ready)
            else:
                # When leaving content, the previous generation is precisely the
                # transition origin. Let Take consume it before retiring the
                # transport; hydrating now would replace Program with the target
                # scene as an implicit Cut.
                self._hydrate_dirty = True
                return
        self._hydrate_if_ready()

    def set_window_targets(
        self,
        targets: tuple[OutputWindowTarget, ...],
    ) -> None:
        if not isinstance(targets, tuple) or not all(
            isinstance(target, OutputWindowTarget) for target in targets
        ):
            raise TypeError("Window targets must be an immutable target tuple")
        if targets == self._window_targets:
            return
        self._window_targets = targets
        self._reconcile_content_ingress_demand()
        if (
            self._engine is None
            or not self._engine_ready
            or self._hydrate_in_flight is not None
            or not self._applied_scenes
        ):
            desired = self._resolve_desired_scenes()
            if desired != self._desired_scenes:
                self._desired_scenes = desired
                self.desired_scenes_changed.emit(desired)
            self._last_destination_enabled = self._destination_enabled()
            self._last_render_enabled = self._render_enabled()
            self._hydrate_if_ready()
            return
        # Window ownership is output topology, not graph state. Raw media
        # targets consume the canonical Solin content source directly; Program
        # targets follow the already committed Program render bus.
        self._reconcile_engine_outputs()
        self._dispatch_window_targets()

    @Slot(object)
    def set_preview_egress(
        self,
        descriptor: FrameChannelDescriptor | None,
    ) -> None:
        if descriptor == self._preview_egress:
            return
        if descriptor is not None and not isinstance(
            descriptor,
            FrameChannelDescriptor,
        ):
            raise TypeError("Invalid preview egress descriptor")
        self._preview_egress = descriptor
        self.preview_egress_changed.emit(descriptor)
        self._last_render_enabled = self._render_enabled()
        self._reconcile_content_ingress_demand()
        self._hydrate_if_ready()

    @Slot(object)
    def set_program_egress(
        self,
        descriptor: FrameChannelDescriptor | None,
    ) -> None:
        if descriptor == self._program_egress:
            return
        if descriptor is not None and not isinstance(
            descriptor,
            FrameChannelDescriptor,
        ):
            raise TypeError("Invalid program egress descriptor")
        self._program_egress = descriptor
        self._last_render_enabled = self._render_enabled()
        self._reconcile_content_ingress_demand()
        self._hydrate_if_ready()

    def set_program_recording_required(self, required: bool) -> None:
        if type(required) is not bool:
            raise TypeError("Program recording render demand must be a boolean")
        if required == self._program_recording_required:
            return
        self._program_recording_required = required
        self._reconcile_engine_outputs()

    def refresh_local_cameras(self) -> Future[LocalCameraDiscovery] | None:
        if self._engine is None or not self._engine_ready:
            self._set_local_cameras(_unavailable_local_cameras("engine_not_ready"))
            return None
        if self._local_camera_request_id and self._local_camera_future is not None:
            return self._local_camera_future
        request_id = self._request_id_factory()
        self._local_camera_request_id = request_id
        future = self._engine.list_local_cameras(
            request_id=request_id,
            deadline_ms=_CAMERA_DISCOVERY_DEADLINE_MS,
        )
        self._local_camera_future = future
        self._track_future(future, "local_cameras", request_id)
        return future

    def recall_camera_preset(
        self,
        preset_id: str,
        *,
        timeout_ms: int = 4000,
    ) -> Future[PtzRecallResult]:
        if not 500 <= timeout_ms <= 10_000:
            raise ValueError("PTZ recall timeout must be between 500 and 10000 ms")
        preset = next(
            (
                candidate
                for candidate in self._documents.document.camera_presets
                if candidate.id == preset_id
            ),
            None,
        )
        if preset is None:
            return _completed_ptz_failure(
                preset_id,
                "ptz-camera-unavailable",
                "ptz_preset_not_found",
            )
        source = self._documents.document.source(preset.camera_source_id)
        configuration = source.configuration
        if not isinstance(configuration, (LocalCameraConfig, RtspCameraConfig)):
            return _completed_ptz_failure(
                preset.id,
                preset.camera_source_id,
                "ptz_camera_configuration_invalid",
            )
        if configuration.ptz_binding is None:
            return _completed_ptz_failure(
                preset.id,
                preset.camera_source_id,
                "ptz_binding_unavailable",
            )
        if self._ptz is None:
            return _completed_ptz_failure(
                preset.id,
                preset.camera_source_id,
                "ptz_executor_unavailable",
            )
        return self._ptz.recall(
            camera_source_id=preset.camera_source_id,
            binding=configuration.ptz_binding,
            preset=preset,
            timeout_ms=timeout_ms,
        )

    def cancel_ptz_recall(
        self,
        future: Future[PtzRecallResult],
    ) -> None:
        if self._ptz is not None:
            self._ptz.cancel(future)

    def move_camera(
        self,
        camera_source_id: str,
        *,
        pan: float = 0.0,
        tilt: float = 0.0,
        zoom: float = 0.0,
        speed: float = 0.5,
    ) -> Future[PtzControlResult]:
        if not 0.05 <= speed <= 1.0:
            raise ValueError("PTZ speed must be between 0.05 and 1.0")
        binding = self._camera_ptz_binding(camera_source_id)
        if binding is None:
            return _completed_ptz_control_failure(
                camera_source_id,
                PtzControlKind.MOVE,
                "ptz_binding_unavailable",
            )
        ptz = self._ptz
        if ptz is None:
            return _completed_ptz_control_failure(
                camera_source_id,
                PtzControlKind.MOVE,
                "ptz_executor_unavailable",
            )
        self._moving_camera_bindings[camera_source_id] = binding
        return ptz.move(
            camera_source_id=camera_source_id,
            binding=binding,
            pan=pan * speed,
            tilt=tilt * speed,
            zoom=zoom * speed,
        )

    def stop_camera(self, camera_source_id: str) -> Future[PtzControlResult]:
        binding = self._moving_camera_bindings.get(camera_source_id)
        if binding is None:
            binding = self._camera_ptz_binding(camera_source_id)
        if binding is None:
            return _completed_ptz_control_failure(
                camera_source_id,
                PtzControlKind.STOP,
                "ptz_binding_unavailable",
            )
        ptz = self._ptz
        if ptz is None:
            return _completed_ptz_control_failure(
                camera_source_id,
                PtzControlKind.STOP,
                "ptz_executor_unavailable",
            )
        self._moving_camera_bindings.pop(camera_source_id, None)
        return ptz.stop(camera_source_id=camera_source_id, binding=binding)

    def stop_all_camera_movements(self) -> None:
        for camera_source_id in tuple(self._moving_camera_bindings):
            self.stop_camera(camera_source_id)

    def store_camera_preset(self, preset_id: str) -> Future[PtzControlResult]:
        preset = next(
            (
                candidate
                for candidate in self._documents.document.camera_presets
                if candidate.id == preset_id
            ),
            None,
        )
        if preset is None:
            return _completed_ptz_control_failure(
                "ptz-camera-unavailable",
                PtzControlKind.STORE_PRESET,
                "ptz_preset_not_found",
            )
        binding = self._camera_ptz_binding(preset.camera_source_id)
        if binding is None:
            return _completed_ptz_control_failure(
                preset.camera_source_id,
                PtzControlKind.STORE_PRESET,
                "ptz_binding_unavailable",
            )
        ptz = self._ptz
        if ptz is None:
            return _completed_ptz_control_failure(
                preset.camera_source_id,
                PtzControlKind.STORE_PRESET,
                "ptz_executor_unavailable",
            )
        return ptz.store_preset(
            camera_source_id=preset.camera_source_id,
            binding=binding,
            preset=preset,
        )

    def stop_engine(self) -> None:
        if self._profile_activation is not None:
            self._workspace.discard_collection_activation(
                self._profile_activation.activation
            )
            self._profile_activation = None
        self._profile_preview_scene_id = None
        self._delete_after_profile_activation = ""
        if self._engine is not None and self._engine_started:
            self._cancel_all_pending(cancel_native=True)
            self._engine.stop()
        else:
            self._cancel_all_pending(cancel_native=False)
        self._local_camera_request_id = ""
        self._local_camera_future = None
        self._set_local_cameras(_unavailable_local_cameras("engine_not_ready"))
        self._set_engine_ready(False)
        self._engine_started = False
        self._process_generation = ""
        self._hydrate_in_flight = None
        self._hydrate_dirty = False
        self._engine_document_revision = 0
        self._failed_takes.clear()
        self._preview_geometry_in_flight = None
        self._queued_preview_geometry = None
        self._set_last_engine_error_code("")
        if self._applied_scenes:
            self._applied_scenes = ()
            self.applied_scenes_changed.emit(())

    def close(self) -> None:
        self.stop_all_camera_movements()
        self.stop_engine()
        self._unsubscribe_document()
        self._unsubscribe_runtime()
        self._unsubscribe_workspace()
        self._unsubscribe_projection()
        self._workspace.close()
        if self._unsubscribe_engine is not None:
            self._unsubscribe_engine()
        if self._ptz is not None:
            self._ptz.close()

    def _on_document_changed(self, change: SceneDocumentChange) -> None:
        graph_record = scene_engine_graph_signature(change.document)
        graph_changed = graph_record != self._observed_graph_record
        self._observed_graph_record = graph_record
        self.document_changed.emit(change.document)
        if not graph_changed:
            return
        self._cancel_all_pending(cancel_native=self._engine_ready)
        self._reconcile_desired(prepare=False)
        self._hydrate_if_ready()

    def _on_workspace_changed(self, change: SceneWorkspaceChange) -> None:
        if change.kind is not SceneWorkspaceChangeKind.ACTIVATED:
            self.scene_profiles_changed.emit(change)
            return
        self.stop_all_camera_movements()
        self._cancel_all_pending(cancel_native=self._engine_ready)
        self._unsubscribe_document()
        self._unsubscribe_runtime()
        self._documents = self._workspace.documents
        self._runtime = self._workspace.runtime
        self._unsubscribe_document = self._documents.subscribe(self._on_document_changed)
        self._unsubscribe_runtime = self._runtime.subscribe(self._on_runtime_changed)
        self._hydrate_in_flight = None
        self._hydrate_dirty = False
        self._engine_document_revision = 0
        self._failed_takes.clear()
        self._clear_source_health()
        self._observed_graph_record = scene_engine_graph_signature(self._documents.document)
        self._preview_geometry_in_flight = None
        self._queued_preview_geometry = None
        self._preview_scene_id = self._profile_preview_scene_id
        self._suspended_media_session_id = None
        self._automatic_media_scene_selection = None
        self._desired_scenes = self._resolve_desired_scenes()
        self._last_destination_enabled = self._destination_enabled()
        self._last_render_enabled = self._render_enabled()
        self._reconcile_content_ingress_demand()
        self._set_applied_scenes(())
        self.scene_profiles_changed.emit(change)
        self.document_changed.emit(self._documents.document)
        self.desired_scenes_changed.emit(self._desired_scenes)
        if not self._committing_hydrated_profile:
            self._hydrate_if_ready()

    def _on_runtime_changed(self, _state: SceneRuntimeState) -> None:
        self.runtime_changed.emit(_state)
        self._reconcile_engine_outputs()
        self._reconcile_desired(prepare=True)

    def _on_projection_changed(self) -> None:
        session_id = self._projection_session_id()
        if session_id != self._last_projection_session_id:
            self._last_projection_session_id = session_id
            self._suspended_media_session_id = None
            self._automatic_media_scene_selection = None
            category = content_category_for_projection(self._projection.state)
            runtime = self._runtime.state.output(BusId.VIRTUAL_CAMERA)
            default_scene_id = self._documents.program_default_scene_id
            if (
                not MEMORIZE_PRE_MEDIA_SCENE
                and runtime.mode is OutputMode.AUTO
                and category in AUTOMATIC_MEDIA_CATEGORIES
                and default_scene_id
                and runtime.manual_scene_id != default_scene_id
            ):
                self._runtime.select_program_scene(default_scene_id)
        self._reconcile_desired(prepare=True)

    def _on_engine_event(self, event: SceneEngineEvent) -> None:
        self._async_engine_event.emit(event)

    def _consume_engine_event(self, event: object) -> None:
        if not isinstance(
            event,
            (EngineHealthEvent, SourceHealthEvent, ProgramRecordingEvent),
        ):
            self._report_exception("event", RuntimeError("Invalid scene engine event"))
            return
        self.engine_event.emit(event)
        if isinstance(event, ProgramRecordingEvent):
            return
        if isinstance(event, SourceHealthEvent):
            previous = self._source_health.get(event.source_id)
            if event.status is SourceHealthStatus.STOPPED:
                if previous is not None:
                    self._source_health.pop(event.source_id, None)
                    self.source_health_changed.emit(event.source_id)
            elif previous != event:
                self._source_health[event.source_id] = event
                self.source_health_changed.emit(event.source_id)
            return
        if not isinstance(event, EngineHealthEvent) or not self._engine_started:
            return
        health = event.health
        if health.session_id != self._session_id:
            self._report_exception("event", RuntimeError("Stale scene engine session"))
            return
        if health.status is SceneEngineStatus.READY:
            generation_changed = health.process_generation != self._process_generation
            was_ready = self._engine_ready
            self._process_generation = health.process_generation
            self._set_engine_ready(True)
            if generation_changed or not was_ready:
                self._hydrate_if_ready()
            return
        if health.status in {
            SceneEngineStatus.STARTING,
            SceneEngineStatus.DEGRADED,
            SceneEngineStatus.FAILED,
            SceneEngineStatus.STOPPED,
        }:
            self._cancel_all_pending(cancel_native=False)
            self._local_camera_request_id = ""
            self._local_camera_future = None
            self._set_local_cameras(_unavailable_local_cameras("engine_not_ready"))
            self._set_engine_ready(False)
            self._clear_source_health()
            self._set_applied_scenes(())
            self._hydrate_in_flight = None
            self._hydrate_dirty = False
            self._failed_takes.clear()
            self.operational_state_changed.emit()

    def _reconcile_desired(self, *, prepare: bool) -> None:
        desired = self._resolve_desired_scenes()
        if desired != self._desired_scenes:
            self._desired_scenes = desired
            self._failed_takes.clear()
            self.desired_scenes_changed.emit(desired)
        self._reconcile_content_ingress_demand()
        if not prepare or not self._engine_ready:
            return
        if self._hydrate_in_flight is not None or not self._applied_scenes:
            self._hydrate_if_ready()
            return
        self._schedule_next_take()

    def _schedule_next_take(
        self,
        *,
        skip: tuple[BusId, str] | None = None,
    ) -> None:
        """Serialize scene transactions with the live Program bus first."""

        if (
            not self._engine_ready
            or self._hydrate_in_flight is not None
            or not self._applied_scenes
            or self._pending
        ):
            return
        for bus_id in (BusId.VIRTUAL_CAMERA, BusId.MEDIA_WINDOWS):
            desired_scene_id = self.desired_scene(bus_id)
            if skip == (bus_id, desired_scene_id):
                continue
            if (bus_id, desired_scene_id) in self._failed_takes:
                continue
            if self.applied_scene(bus_id) != desired_scene_id:
                self._prepare_take(bus_id, desired_scene_id)
                return

    def _reconcile_engine_outputs(self) -> None:
        destinations = self._destination_enabled()
        previous_destinations = dict(self._last_destination_enabled)
        self._last_destination_enabled = destinations
        renders = self._render_enabled()
        previous_renders = dict(self._last_render_enabled)
        self._last_render_enabled = renders
        self._reconcile_content_ingress_demand()
        if self._engine is None or not self._engine_ready:
            return
        if self._hydrate_in_flight is not None or not self._applied_scenes:
            self._hydrate_if_ready()
            return
        for bus_id, enabled in destinations:
            if previous_destinations.get(bus_id) == enabled:
                continue
            request_id = self._request_id_factory()
            sequence = self._next_sequence()
            future = self._engine.set_output_enabled(
                bus_id,
                enabled,
                request_id=request_id,
                sequence=sequence,
                deadline_ms=_TAKE_DEADLINE_MS,
            )
            self._track_future(
                future,
                "output",
                (
                    request_id,
                    sequence,
                    bus_id,
                    enabled,
                    self._engine_document_revision,
                ),
            )
        for bus_id, enabled in renders:
            if previous_renders.get(bus_id) == enabled:
                continue
            request_id = self._request_id_factory()
            sequence = self._next_sequence()
            future = self._engine.set_render_enabled(
                bus_id,
                enabled,
                request_id=request_id,
                sequence=sequence,
                deadline_ms=_TAKE_DEADLINE_MS,
            )
            self._track_future(
                future,
                "render",
                (
                    request_id,
                    sequence,
                    bus_id,
                    enabled,
                    self._engine_document_revision,
                ),
            )

    def _hydrate_if_ready(self) -> None:
        if self._engine is None or not self._engine_ready:
            return
        if self._pending:
            self._hydrate_dirty = True
            return
        if (
            self._preview_geometry_in_flight is not None
            or self._queued_preview_geometry is not None
        ):
            self._hydrate_dirty = True
            return
        if self._hydrate_in_flight is not None:
            self._hydrate_dirty = True
            return
        request_id = self._request_id_factory()
        sequence = self._next_sequence()
        snapshot = SceneEngineSnapshot(
            session_id=self._session_id,
            sequence=sequence,
            document=self._documents.document,
            active_scenes=self._hydration_active_scenes(),
            render_enabled=self._render_enabled(),
            output_enabled=self._destination_enabled(),
            content_ingress=self._content_ingress,
            preview_egress=self._preview_egress,
            program_egress=self._program_egress,
            window_targets=self._window_targets,
        )
        context = (request_id, snapshot)
        self._hydrate_in_flight = context
        self._hydrate_dirty = False
        self.operational_state_changed.emit()
        future = self._engine.hydrate(
            snapshot,
            request_id=request_id,
            deadline_ms=_HYDRATE_DEADLINE_MS,
        )
        self._track_future(future, "hydrate", context)

    def _hydration_active_scenes(self) -> tuple[tuple[BusId, str], ...]:
        """Preserve on-air ownership while graph topology is replaced."""

        if len(self._applied_scenes) != len(BusId):
            return self._desired_scenes
        scene_ids = {scene.id for scene in self._documents.document.scenes}
        if all(scene_id in scene_ids for _, scene_id in self._applied_scenes):
            return self._applied_scenes
        return self._desired_scenes

    def _dispatch_preview_geometry(self) -> None:
        if (
            self._engine is None
            or not self._engine_ready
            or self._hydrate_in_flight is not None
            or self._preview_geometry_in_flight is not None
            or self._queued_preview_geometry is None
        ):
            return
        scene_id, layer, document_revision = self._queued_preview_geometry
        self._queued_preview_geometry = None
        context = _PendingLayerPreview(
            request_id=self._request_id_factory(),
            sequence=self._next_sequence(),
            document_revision=document_revision,
            scene_id=scene_id,
            layer=layer,
        )
        self._preview_geometry_in_flight = context
        future = self._engine.preview_layer_geometry(
            BusId.MEDIA_WINDOWS,
            scene_id,
            layer,
            document_revision=context.document_revision,
            request_id=context.request_id,
            sequence=context.sequence,
            deadline_ms=_PREVIEW_GEOMETRY_DEADLINE_MS,
        )
        self._track_future(future, "preview_geometry", context)

    def _combined_window_targets(self) -> tuple[OutputWindowTarget, ...]:
        """Projection targets plus the editor preview target (if any)."""
        if self._editor_preview_target is None:
            return self._window_targets
        return self._window_targets + (self._editor_preview_target,)

    def set_editor_preview_target(self, target: OutputWindowTarget | None) -> None:
        """Install (or clear) the editor's direct-GPU preview window target.

        Dispatched together with the projection targets; safe to call before the
        engine is ready (it's picked up on the next dispatch)."""
        if target is not None and not isinstance(target, OutputWindowTarget):
            raise TypeError("editor preview target must be an OutputWindowTarget or None")
        if target == self._editor_preview_target:
            return
        self._editor_preview_target = target
        self._dispatch_window_targets()

    def _dispatch_window_targets(self) -> None:
        if self._engine is None or not self._engine_ready:
            return
        context = _PendingWindowTargets(
            request_id=self._request_id_factory(),
            sequence=self._next_sequence(),
            document_revision=self._engine_document_revision,
            targets=self._combined_window_targets(),
        )
        future = self._engine.set_window_targets(
            context.targets,
            request_id=context.request_id,
            sequence=context.sequence,
            deadline_ms=_TAKE_DEADLINE_MS,
        )
        self._track_future(future, "window_targets", context)

    def _prepare_take(self, bus_id: BusId, scene_id: str) -> None:
        if self._engine is None:
            return
        current = self._pending.get(bus_id)
        if current is not None:
            if current.scene_id == scene_id:
                return
            # Preparation and Take share the sidecar's ordered control stream.
            # Queueing another preparation here only puts its cancellation behind
            # the expensive work that it is meant to cancel, and can starve the
            # heartbeat during rapid selection.  Desired state already records the
            # newest scene, so let the one in-flight transaction reach a safe
            # boundary and reconcile exactly once from its completion callback.
            if bus_id not in self._ptz_batches:
                return
            # PTZ is the only pending stage which can be cancelled locally before
            # another native command is issued.
            self._cancel_pending(bus_id, cancel_native=True)
        request_id = self._request_id_factory()
        sequence = self._next_sequence()
        pending = _PendingTake(
            scene_id=scene_id,
            prepare_request_id=request_id,
            prepare_sequence=sequence,
            document_revision=self._engine_document_revision,
        )
        self._pending[bus_id] = pending
        transition = (
            TransitionSpec(TransitionKind.CUT, 0)
            if bus_id is BusId.MEDIA_WINDOWS
            else self._documents.effective_transition(scene_id)
        )
        future = self._engine.prepare_scene(
            bus_id,
            scene_id,
            transition=transition,
            document_revision=pending.document_revision,
            request_id=request_id,
            sequence=sequence,
            deadline_ms=_PREPARE_DEADLINE_MS,
            content_media_epoch=(
                self._projection_session_id()
                if scene_uses_content_source(self._documents.document, scene_id)
                else None
            ),
        )
        self._track_future(future, "prepare", (bus_id, pending))

    def _run_ptz_entry_actions(
        self,
        preparation: ScenePreparation,
        expected: _PendingTake,
    ) -> None:
        scene = self._documents.document.scene(preparation.scene_id)
        if preparation.bus_id is BusId.MEDIA_WINDOWS or not scene.entry_actions:
            self._take_prepared(preparation)
            return
        document = self._documents.document
        preset_by_id = {preset.id: preset for preset in document.camera_presets}
        contexts: list[_PtzActionContext] = []
        for action in scene.entry_actions:
            preset = preset_by_id[action.preset_id]
            source = document.source(preset.camera_source_id)
            configuration = source.configuration
            if not isinstance(configuration, (LocalCameraConfig, RtspCameraConfig)):
                future = _completed_ptz_failure(
                    preset.id,
                    preset.camera_source_id,
                    "ptz_camera_configuration_invalid",
                )
            elif configuration.ptz_binding is None:
                future = _completed_ptz_failure(
                    preset.id,
                    preset.camera_source_id,
                    "ptz_binding_unavailable",
                )
            elif self._ptz is None:
                future = _completed_ptz_failure(
                    preset.id,
                    preset.camera_source_id,
                    "ptz_executor_unavailable",
                )
            else:
                try:
                    future = self._ptz.recall(
                        camera_source_id=preset.camera_source_id,
                        binding=configuration.ptz_binding,
                        preset=preset,
                        timeout_ms=action.timeout_ms,
                    )
                except (RuntimeError, TypeError, ValueError):
                    future = _completed_ptz_failure(
                        preset.id,
                        preset.camera_source_id,
                        "ptz_recall_failed",
                    )
            contexts.append(
                _PtzActionContext(
                    bus_id=preparation.bus_id,
                    expected=expected,
                    preparation=preparation,
                    action=action,
                    camera_source_id=preset.camera_source_id,
                    future=future,
                )
            )
        batch = _PendingPtzBatch(
            expected=expected,
            preparation=preparation,
            remaining={context.future for context in contexts},
            blocking_error_codes=[],
        )
        self._ptz_batches[preparation.bus_id] = batch
        for context in contexts:
            self._track_future(context.future, "ptz", context)

    def _take_prepared(self, preparation: ScenePreparation) -> None:
        if self._engine is None:
            return
        pending = self._pending.get(preparation.bus_id)
        if (
            pending is None
            or pending.prepare_request_id != preparation.request_id
            or pending.scene_id != preparation.scene_id
            or self.desired_scene(preparation.bus_id) != preparation.scene_id
        ):
            return
        request_id = self._request_id_factory()
        sequence = self._next_sequence()
        pending = _PendingTake(
            scene_id=pending.scene_id,
            prepare_request_id=pending.prepare_request_id,
            prepare_sequence=pending.prepare_sequence,
            document_revision=pending.document_revision,
            take_request_id=request_id,
            take_sequence=sequence,
        )
        self._pending[preparation.bus_id] = pending
        future = self._engine.take_prepared(
            preparation,
            request_id=request_id,
            sequence=sequence,
            deadline_ms=_TAKE_DEADLINE_MS,
        )
        self._track_future(future, "take", (preparation.bus_id, pending))

    def _track_future(
        self,
        future: Future[Any],
        operation: str,
        context: object,
    ) -> None:
        def completed(result_future: Future[Any]) -> None:
            try:
                result = result_future.result()
                error: BaseException | None = None
            except BaseException as exc:  # noqa: BLE001 - async process boundary
                result = None
                error = exc
            self._async_result.emit((operation, context, result, error))

        future.add_done_callback(completed)

    def _consume_async_result(self, payload: object) -> None:
        values = _context_tuple(payload, 4, "async result")
        operation, context, result, error = values
        if error is not None:
            if operation == "preview_geometry":
                self._finish_preview_geometry(context, error=error)
                return
            if operation == "profile_hydrate":
                self._fail_profile_activation(context, error)
                return
            if operation == "ptz":
                self._handle_ptz_exception(context)
                return
            if operation == "local_cameras" and context != self._local_camera_request_id:
                return
            if operation == "start":
                self._set_engine_ready(False)
            self._clear_failed_pending(operation, context)
            if isinstance(error, CancelledError):
                if operation == "hydrate":
                    self._finish_hydration(context)
                return
            self._report_exception(operation, error)
            if operation == "hydrate":
                self._finish_hydration(context)
            return
        try:
            if operation == "start":
                self._handle_started(result)
            elif operation == "hydrate":
                self._handle_hydrated(context, result)
            elif operation == "profile_hydrate":
                self._handle_profile_hydrated(context, result)
            elif operation == "prepare":
                self._handle_prepared(context, result)
            elif operation == "take":
                self._handle_taken(context, result)
            elif operation == "output":
                self._handle_output_ack("output", context, result)
            elif operation == "render":
                self._handle_output_ack("render", context, result)
            elif operation == "window_targets":
                self._handle_window_targets_ack(context, result)
            elif operation == "local_cameras":
                self._handle_local_cameras(context, result)
            elif operation == "ptz":
                self._handle_ptz_result(context, result)
            elif operation == "preview_geometry":
                self._finish_preview_geometry(context, result=result)
        except Exception as exc:  # noqa: BLE001 - untrusted engine response boundary
            self._clear_failed_pending(operation, context)
            self._report_exception(operation, exc)
            if operation == "profile_hydrate":
                if (
                    isinstance(context, _PendingProfileActivation)
                    and self._profile_activation == context
                ):
                    self._workspace.discard_collection_activation(context.activation)
                    self._profile_activation = None
                self._profile_preview_scene_id = None
                self._delete_after_profile_activation = ""
                self._hydrate_if_ready()
                self.operational_state_changed.emit()
        finally:
            if operation == "hydrate":
                self._finish_hydration(context)

    def _handle_started(self, result: object) -> None:
        if not isinstance(result, SceneEngineCapabilities):
            raise RuntimeError("Scene engine returned invalid capabilities")
        generation_changed = result.process_generation != self._process_generation
        was_ready = self._engine_ready
        self._process_generation = result.process_generation
        self._set_engine_ready(True)
        self.engine_capabilities_changed.emit(result)
        if generation_changed or not was_ready:
            self._hydrate_if_ready()

    def _handle_hydrated(self, context: object, result: object) -> None:
        if context != self._hydrate_in_flight:
            return
        request_id, snapshot = _context_tuple(context, 2, "hydration")
        if not isinstance(request_id, str) or not isinstance(snapshot, SceneEngineSnapshot):
            raise RuntimeError("Invalid hydration context")
        ack = self._validated_ack(
            result,
            request_id=request_id,
            sequence=snapshot.sequence,
            document_revision=snapshot.document.revision,
        )
        if not ack.applied:
            self._report_rejection("hydrate", ack)
            return
        self._set_last_engine_error_code("")
        self._engine_document_revision = snapshot.document.revision
        self._failed_takes.clear()
        self._set_applied_scenes(snapshot.active_scenes)
        if self._hydrate_dirty:
            return
        self._schedule_next_take()

    def _handle_profile_hydrated(self, context: object, result: object) -> None:
        if not isinstance(context, _PendingProfileActivation):
            raise RuntimeError("Invalid Scene profile hydration context")
        if context != self._profile_activation:
            return
        ack = self._validated_ack(
            result,
            request_id=context.request_id,
            sequence=context.snapshot.sequence,
            document_revision=context.snapshot.document.revision,
        )
        if not ack.applied:
            self._workspace.discard_collection_activation(context.activation)
            self._profile_activation = None
            self._profile_preview_scene_id = None
            self._delete_after_profile_activation = ""
            self._report_rejection("profile_hydrate", ack)
            self.operational_state_changed.emit()
            return
        self._committing_hydrated_profile = True
        try:
            self._workspace.commit_collection_activation(context.activation)
        finally:
            self._committing_hydrated_profile = False
            self._profile_activation = None
            self._profile_preview_scene_id = None
        self._set_last_engine_error_code("")
        self._engine_document_revision = context.snapshot.document.revision
        self._failed_takes.clear()
        self._observed_graph_record = scene_engine_graph_signature(context.snapshot.document)
        self._set_applied_scenes(context.snapshot.active_scenes)
        delete_collection_id = self._delete_after_profile_activation
        self._delete_after_profile_activation = ""
        if delete_collection_id:
            self._workspace.delete_collection(delete_collection_id)
        self.operational_state_changed.emit()

    def _fail_profile_activation(self, context: object, error: object) -> None:
        if not isinstance(context, _PendingProfileActivation):
            self._report_exception("profile_hydrate", error)
            return
        if context != self._profile_activation:
            return
        self._workspace.discard_collection_activation(context.activation)
        self._profile_activation = None
        self._profile_preview_scene_id = None
        self._delete_after_profile_activation = ""
        if not isinstance(error, CancelledError):
            self._report_exception("profile_hydrate", error)
        self.operational_state_changed.emit()

    def _handle_prepared(self, context: object, result: object) -> None:
        bus_id, expected = _context_tuple(context, 2, "prepare")
        if not isinstance(bus_id, BusId) or not isinstance(expected, _PendingTake):
            raise RuntimeError("Invalid prepare context")
        if self._pending.get(bus_id) != expected:
            return
        if not isinstance(result, ScenePreparation):
            raise RuntimeError("Scene engine returned an invalid preparation")
        if (
            result.session_id != self._session_id
            or result.process_generation != self._process_generation
        ):
            raise RuntimeError("Scene engine preparation belongs to a stale process")
        self._validate_response_identity(
            request_id=result.request_id,
            sequence=result.sequence,
            document_revision=result.document_revision,
            expected_request_id=expected.prepare_request_id,
            expected_sequence=expected.prepare_sequence,
            expected_document_revision=expected.document_revision,
        )
        if result.bus_id is not bus_id or result.scene_id != expected.scene_id:
            raise RuntimeError("Scene engine prepared a different scene")
        if self.desired_scene(bus_id) != expected.scene_id:
            if self._engine is not None:
                self._engine.cancel_preparation(expected.prepare_request_id)
            self._pending.pop(bus_id, None)
            self._continue_pending_reconciliation(bus_id, expected.scene_id)
            return
        if result.fallback_applied:
            log.warning(
                "Scene transition fell back to Cut (%s)",
                result.fallback_reason,
            )
            self.transition_fallback.emit(
                self.tr("The selected transition is unavailable. The scene was cut instead.")
            )
        self._run_ptz_entry_actions(result, expected)

    def _handle_taken(self, context: object, result: object) -> None:
        bus_id, expected = _context_tuple(context, 2, "take")
        if not isinstance(bus_id, BusId) or not isinstance(expected, _PendingTake):
            raise RuntimeError("Invalid take context")
        if self._pending.get(bus_id) != expected:
            return
        ack = self._validated_ack(
            result,
            request_id=expected.take_request_id,
            sequence=expected.take_sequence,
            document_revision=expected.document_revision,
        )
        if not ack.applied:
            self._clear_failed_pending("take", context)
            self._report_rejection("take", ack)
            return
        self._pending.pop(bus_id, None)
        applied = dict(self._applied_scenes)
        applied[bus_id] = expected.scene_id
        self._set_applied_scenes(
            tuple((candidate, applied[candidate]) for candidate in BusId if candidate in applied)
        )
        self._set_last_engine_error_code("")
        self._continue_pending_reconciliation(bus_id, expected.scene_id)

    def _handle_ptz_exception(self, context: object) -> None:
        if not isinstance(context, _PtzActionContext):
            self._report_exception("ptz", RuntimeError("Invalid PTZ context"))
            return
        result = PtzRecallResult(
            request_id="ptz-exception",
            camera_source_id=context.camera_source_id,
            preset_id=context.action.preset_id,
            status=PtzRecallStatus.FAILED,
            error_code="ptz_recall_failed",
        )
        self._complete_ptz_action(context, result)

    def _handle_ptz_result(self, context: object, result: object) -> None:
        if not isinstance(context, _PtzActionContext):
            raise RuntimeError("Invalid PTZ context")
        if not isinstance(result, PtzRecallResult):
            raise RuntimeError("PTZ executor returned an invalid result")
        if (
            result.camera_source_id != context.camera_source_id
            or result.preset_id != context.action.preset_id
        ):
            raise RuntimeError("PTZ executor returned a mismatched result")
        self._complete_ptz_action(context, result)

    def _complete_ptz_action(
        self,
        context: _PtzActionContext,
        result: PtzRecallResult,
    ) -> None:
        batch = self._ptz_batches.get(context.bus_id)
        if (
            batch is None
            or batch.expected != context.expected
            or batch.preparation != context.preparation
            or context.future not in batch.remaining
            or self._pending.get(context.bus_id) != context.expected
        ):
            return
        batch.remaining.remove(context.future)
        if not result.succeeded and context.action.on_timeout is PtzTimeoutPolicy.KEEP_CURRENT:
            batch.blocking_error_codes.append(result.error_code or "ptz_recall_failed")
        self.ptz_event.emit(
            ScenePtzEvent(
                bus_id=context.bus_id,
                scene_id=context.preparation.scene_id,
                camera_source_id=context.camera_source_id,
                preset_id=context.action.preset_id,
                policy=context.action.on_timeout,
                result=result,
            )
        )
        if batch.remaining:
            return
        self._ptz_batches.pop(context.bus_id, None)
        if batch.blocking_error_codes:
            if self._engine is not None:
                self._engine.cancel_preparation(context.expected.prepare_request_id)
            self._pending.pop(context.bus_id, None)
            self._failed_takes.add(
                (context.bus_id, context.expected.scene_id)
            )
            self.engine_error.emit(
                f"Scene PTZ recall blocked Take ({batch.blocking_error_codes[0]})"
            )
            self._schedule_next_take(
                skip=(context.bus_id, context.expected.scene_id),
            )
            return
        self._take_prepared(context.preparation)

    def _handle_output_ack(
        self,
        operation: str,
        context: object,
        result: object,
    ) -> None:
        request_id, sequence, _bus_id, _enabled, document_revision = _context_tuple(
            context,
            5,
            operation,
        )
        if (
            not isinstance(request_id, str)
            or not isinstance(sequence, int)
            or not isinstance(document_revision, int)
        ):
            raise RuntimeError(f"Invalid {operation} context")
        ack = self._validated_ack(
            result,
            request_id=request_id,
            sequence=sequence,
            document_revision=document_revision,
        )
        if not ack.applied:
            self._report_rejection(operation, ack)
            return
        self._set_last_engine_error_code("")

    def _handle_window_targets_ack(self, context: object, result: object) -> None:
        if not isinstance(context, _PendingWindowTargets):
            raise RuntimeError("Invalid window targets context")
        ack = self._validated_ack(
            result,
            request_id=context.request_id,
            sequence=context.sequence,
            document_revision=context.document_revision,
        )
        if not ack.applied:
            self._report_rejection("window_targets", ack)
            return
        self._set_last_engine_error_code("")

    def _handle_local_cameras(self, context: object, result: object) -> None:
        if not isinstance(context, str):
            raise RuntimeError("Invalid local camera discovery context")
        if context != self._local_camera_request_id:
            return
        if not isinstance(result, LocalCameraDiscovery):
            raise RuntimeError("Scene engine returned invalid local cameras")
        self._local_camera_request_id = ""
        self._local_camera_future = None
        self._set_local_cameras(result)

    def _validated_ack(
        self,
        result: object,
        *,
        request_id: str,
        sequence: int,
        document_revision: int,
    ) -> SceneEngineAck:
        if not isinstance(result, SceneEngineAck):
            raise RuntimeError("Scene engine returned an invalid acknowledgement")
        if (
            result.session_id != self._session_id
            or result.process_generation != self._process_generation
        ):
            raise RuntimeError("Scene engine acknowledgement belongs to a stale process")
        self._validate_response_identity(
            request_id=result.request_id,
            sequence=result.sequence,
            document_revision=result.document_revision,
            expected_request_id=request_id,
            expected_sequence=sequence,
            expected_document_revision=document_revision,
        )
        return result

    def _validate_response_identity(
        self,
        *,
        request_id: str,
        sequence: int,
        document_revision: int,
        expected_request_id: str,
        expected_sequence: int,
        expected_document_revision: int | None = None,
    ) -> None:
        if request_id != expected_request_id or sequence != expected_sequence:
            raise RuntimeError("Scene engine response does not match its request")
        if expected_document_revision is None:
            expected_document_revision = self._documents.document.revision
        if document_revision != expected_document_revision:
            raise RuntimeError("Scene engine response has a stale document revision")

    def _set_applied_scenes(
        self,
        scenes: tuple[tuple[BusId, str], ...],
    ) -> None:
        if scenes == self._applied_scenes:
            return
        self._applied_scenes = scenes
        self.applied_scenes_changed.emit(scenes)

    def _cancel_pending(self, bus_id: BusId, *, cancel_native: bool) -> None:
        batch = self._ptz_batches.pop(bus_id, None)
        if batch is not None and self._ptz is not None:
            for future in tuple(batch.remaining):
                self._ptz.cancel(future)
        pending = self._pending.pop(bus_id, None)
        if cancel_native and pending is not None and self._engine is not None:
            self._engine.cancel_preparation(pending.prepare_request_id)

    def _cancel_all_pending(self, *, cancel_native: bool) -> None:
        for bus_id in set(self._pending) | set(self._ptz_batches):
            self._cancel_pending(bus_id, cancel_native=cancel_native)

    def _clear_failed_pending(self, operation: object, context: object) -> None:
        if operation == "local_cameras" and context == self._local_camera_request_id:
            self._local_camera_request_id = ""
            self._local_camera_future = None
            self._set_local_cameras(_unavailable_local_cameras("discovery_request_failed"))
            return
        if operation == "ptz" and isinstance(context, _PtzActionContext):
            if self._pending.get(context.bus_id) == context.expected:
                self._cancel_pending(context.bus_id, cancel_native=True)
            return
        if operation not in {"prepare", "take"} or not isinstance(context, tuple):
            return
        bus_id = context[0]
        expected = context[1]
        if (
            not isinstance(bus_id, BusId)
            or not isinstance(expected, _PendingTake)
            or self._pending.get(bus_id) != expected
        ):
            return
        if self._engine is not None:
            self._engine.cancel_preparation(expected.prepare_request_id)
        self._pending.pop(bus_id, None)
        self._failed_takes.add((bus_id, expected.scene_id))
        self._continue_pending_reconciliation(bus_id, expected.scene_id)

    def _continue_pending_reconciliation(
        self,
        bus_id: BusId,
        completed_scene_id: str,
    ) -> None:
        if (
            not self._engine_ready
            or self._hydrate_in_flight is not None
            or not self._applied_scenes
            or self._pending
        ):
            return
        if self._hydrate_dirty:
            self._hydrate_if_ready()
            return
        self._schedule_next_take(skip=(bus_id, completed_scene_id))

    def _report_exception(self, operation: object, error: object) -> None:
        if isinstance(error, SceneEngineCommandRejectedError):
            error_code = error.error_code or "request_rejected"
            self._set_last_engine_error_code(error_code)
            log.warning("Scene engine %s rejected request (%s)", operation, error_code)
            self.engine_error.emit(f"Scene engine {operation} rejected ({error_code})")
            return
        if isinstance(error, SceneEngineNotReadyError):
            error_code = "engine_not_ready"
        elif isinstance(error, SceneEngineRequestTimeoutError):
            error_code = "engine_request_timed_out"
        elif isinstance(error, SceneEngineProtocolError):
            error_code = "engine_protocol_error"
        elif isinstance(error, SceneEngineProcessError):
            error_code = "engine_process_failed"
        else:
            error_code = "unexpected_engine_response"
        self._set_last_engine_error_code(error_code)
        log.warning(
            "Scene engine %s failed (%s: %s)",
            operation,
            error_code,
            type(error).__name__,
        )
        self.engine_error.emit(f"Scene engine {operation} failed ({error_code})")

    def _report_rejection(self, operation: str, ack: SceneEngineAck) -> None:
        error_code = ack.error_code or "request_rejected"
        self._set_last_engine_error_code(error_code)
        log.warning("Scene engine %s rejected request (%s)", operation, error_code)
        self.engine_error.emit(f"Scene engine {operation} rejected ({error_code})")

    def _finish_hydration(self, context: object) -> None:
        if self._hydrate_in_flight != context:
            return
        self._hydrate_in_flight = None
        should_retry = self._hydrate_dirty
        self._hydrate_dirty = False
        self._dispatch_preview_geometry()
        if self._preview_geometry_in_flight is not None:
            self._hydrate_dirty = should_retry
        elif should_retry:
            self._hydrate_if_ready()
            return
        self.operational_state_changed.emit()
        self._schedule_next_take()

    def _finish_preview_geometry(
        self,
        context: object,
        *,
        result: object | None = None,
        error: object | None = None,
    ) -> None:
        if context != self._preview_geometry_in_flight:
            return
        self._preview_geometry_in_flight = None
        if isinstance(context, _PendingLayerPreview):
            if error is not None:
                log.debug(
                    "Scene preview geometry update failed (%s)",
                    type(error).__name__,
                )
            elif result is not None:
                try:
                    ack = self._validated_ack(
                        result,
                        request_id=context.request_id,
                        sequence=context.sequence,
                        document_revision=context.document_revision,
                    )
                    if not ack.applied:
                        log.debug(
                            "Scene preview geometry update was not applied (%s)",
                            ack.error_code or "request_rejected",
                        )
                except Exception as exc:  # noqa: BLE001 - transient engine boundary
                    log.debug(
                        "Scene preview geometry acknowledgement was invalid (%s)",
                        type(exc).__name__,
                    )
        self._dispatch_preview_geometry()
        if (
            self._hydrate_dirty
            and self._hydrate_in_flight is None
            and self._preview_geometry_in_flight is None
            and self._queued_preview_geometry is None
        ):
            self._hydrate_dirty = False
            self._hydrate_if_ready()

    def _resolve_desired_scenes(self) -> tuple[tuple[BusId, str], ...]:
        category = content_category_for_projection(self._projection.state)
        runtime = self._runtime.state.output(BusId.VIRTUAL_CAMERA)
        if (
            runtime.mode is OutputMode.AUTO
            and category in AUTOMATIC_MEDIA_CATEGORIES
            and self._suspended_media_session_id == self._projection_session_id()
        ):
            category = ContentCategory.IDLE
        selection = self._automatic_media_scene_selection
        selected_scene_id = (
            selection.scene_id
            if runtime.mode is OutputMode.AUTO
            and category in AUTOMATIC_MEDIA_CATEGORIES
            and selection is not None
            and selection.projection_session_id == self._projection_session_id()
            and any(scene.id == selection.scene_id for scene in self._documents.document.scenes)
            else None
        )
        program_scene = selected_scene_id or self._runtime.resolve_scene(
            BusId.VIRTUAL_CAMERA,
            category,
        )
        media_scene = self._preview_scene_id or program_scene
        return (
            (BusId.MEDIA_WINDOWS, media_scene),
            (BusId.VIRTUAL_CAMERA, program_scene),
        )

    def _projection_session_id(self) -> int:
        value = getattr(
            self._projection,
            "presentation_session_id",
            getattr(self._projection, "session_id", 0),
        )
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _scene_keeps_media_automation(self, scene_id: str) -> bool:
        return (
            scene_id == self._documents.program_media_scene_id
            or scene_uses_content_source(
                self._documents.document,
                scene_id,
            )
        )

    def _current_media_session_selection(self) -> _AutomaticMediaSceneSelection | None:
        selection = self._automatic_media_scene_selection
        return (
            selection
            if selection is not None
            and selection.projection_session_id == self._projection_session_id()
            else None
        )

    def _media_session_selection(self, scene_id: str) -> _AutomaticMediaSceneSelection:
        current = self._current_media_session_selection()
        if current is not None:
            return _AutomaticMediaSceneSelection(
                projection_session_id=current.projection_session_id,
                scene_id=scene_id,
                return_scene_id=current.return_scene_id,
            )
        runtime = self._runtime.state.output(BusId.VIRTUAL_CAMERA)
        return_scene_id = runtime.manual_scene_id or None
        if return_scene_id == self._documents.program_media_scene_id:
            return_scene_id = None
        return _AutomaticMediaSceneSelection(
            projection_session_id=self._projection_session_id(),
            scene_id=scene_id,
            return_scene_id=return_scene_id,
        )

    def _restore_media_session_return_base(self) -> None:
        selection = self._current_media_session_selection()
        previous_suspension = self._suspended_media_session_id
        previous_selection = self._automatic_media_scene_selection
        self._suspended_media_session_id = None
        self._automatic_media_scene_selection = None
        try:
            if selection is not None:
                self._runtime.select_program_scene(selection.return_scene_id)
        except Exception:  # noqa: BLE001 - restore transient state on write failure
            self._suspended_media_session_id = previous_suspension
            self._automatic_media_scene_selection = previous_selection
            raise

    def _destination_enabled(self) -> tuple[tuple[BusId, bool], ...]:
        return tuple(
            (bus_id, self._runtime.state.output(bus_id).enabled) for bus_id in BusId
        )

    def _render_enabled(self) -> tuple[tuple[BusId, bool], ...]:
        destination = self._runtime.state
        preview_required = self._preview_scene_id is not None
        program_required = (
            destination.output(BusId.VIRTUAL_CAMERA).enabled
            or destination.output(BusId.MEDIA_WINDOWS).enabled
            or self._program_egress is not None
            or self._program_recording_required
            or any(
                target.bus_id is BusId.VIRTUAL_CAMERA for target in self._window_targets
            )
        )
        return (
            (BusId.MEDIA_WINDOWS, preview_required),
            (BusId.VIRTUAL_CAMERA, program_required),
        )

    def _content_ingress_required(self) -> bool:
        if any(
            target.visible and target.bus_id is BusId.MEDIA_WINDOWS
            for target in self._window_targets
        ):
            return True
        desired = dict(self._desired_scenes)
        return any(
            enabled
            and scene_uses_content_source(
                self._documents.document,
                desired[bus_id],
            )
            for bus_id, enabled in self._render_enabled()
        )

    def _reconcile_content_ingress_demand(self) -> None:
        required = self._content_ingress_required()
        if required == self._last_content_ingress_required:
            return
        self._last_content_ingress_required = required
        self.content_ingress_demand_changed.emit(required)

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _set_engine_ready(self, ready: bool) -> None:
        ready = bool(ready)
        if self._engine_ready == ready:
            return
        self._engine_ready = ready
        self.engine_ready_changed.emit(ready)
        self.operational_state_changed.emit()

    def _set_last_engine_error_code(self, error_code: str) -> None:
        if self._last_engine_error_code == error_code:
            return
        self._last_engine_error_code = error_code
        self.operational_state_changed.emit()

    def _clear_source_health(self) -> None:
        source_ids = tuple(self._source_health)
        self._source_health.clear()
        for source_id in source_ids:
            self.source_health_changed.emit(source_id)

    def _set_local_cameras(self, discovery: LocalCameraDiscovery) -> None:
        if discovery == self._local_cameras:
            return
        self._local_cameras = discovery
        self.local_cameras_changed.emit(discovery)

    def _camera_ptz_binding(self, camera_source_id: str) -> PtzBinding | None:
        try:
            source = self._documents.document.source(camera_source_id)
        except StopIteration:
            return self._moving_camera_bindings.get(camera_source_id)
        configuration = source.configuration
        if not isinstance(configuration, (LocalCameraConfig, RtspCameraConfig)):
            return None
        return configuration.ptz_binding


def _scene_for_bus(scenes: tuple[tuple[BusId, str], ...], bus_id: BusId) -> str:
    return next(scene_id for candidate, scene_id in scenes if candidate is bus_id)


def _unavailable_local_cameras(error_code: str) -> LocalCameraDiscovery:
    return LocalCameraDiscovery(
        supported=False,
        ready=True,
        generation=0,
        devices=(),
        error_code=error_code,
    )


def _completed_ptz_failure(
    preset_id: str,
    camera_source_id: str,
    error_code: str,
) -> Future[PtzRecallResult]:
    future: Future[PtzRecallResult] = Future()
    future.set_result(
        PtzRecallResult(
            request_id="ptz-unavailable",
            camera_source_id=camera_source_id,
            preset_id=preset_id,
            status=PtzRecallStatus.FAILED,
            error_code=error_code,
        )
    )
    return future


def _completed_ptz_control_failure(
    camera_source_id: str,
    kind: PtzControlKind,
    error_code: str,
) -> Future[PtzControlResult]:
    future: Future[PtzControlResult] = Future()
    future.set_result(
        PtzControlResult(
            request_id="ptz-unavailable",
            camera_source_id=camera_source_id,
            kind=kind,
            status=PtzRecallStatus.FAILED,
            error_code=error_code,
        )
    )
    return future


def _context_tuple(value: object, size: int, operation: str) -> tuple[object, ...]:
    if not isinstance(value, tuple) or len(value) != size:
        raise RuntimeError(f"Invalid {operation} context")
    return value
