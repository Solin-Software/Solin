"""Typed command and presentation boundary for the QML Scenes workspace."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
import logging
import math
from pathlib import Path
import time
from typing import Any, Protocol

from PySide6.QtCore import QObject, Property, Signal, Slot
from PySide6.QtGui import QImage

from solin.controllers.program_recording_controller import ProgramRecordingController
from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.scenes.editor_geometry import (
    MINIMUM_LAYER_SIZE,
    MINIMUM_VISIBLE_SOURCE,
    NORMALIZED_CANVAS_ASPECT,
    crop_for_framing_rect,
    crop_geometry,
    fill_crop_to_canvas,
    inscribed_aspect_rect,
    intersect_rect,
    move_framing_rect,
    resize_framing_rect,
    resize_rect,
    scale_framing_rect,
    snap_move_rect,
)
from solin.core.scenes.engine import (
    LocalCameraDevice,
    LocalCameraProbeStatus,
    LocalVideoFormat,
    SourceHealthStatus,
)
from solin.core.scenes.model import (
    CONTENT_SOURCE_ID,
    BusId,
    CameraPreset,
    Crop,
    FitMode,
    LocalCameraConfig,
    NormalizedRect,
    OnvifPtzBinding,
    OutputMode,
    PtzBinding,
    PtzPosition,
    PtzProtocol,
    PtzTimeoutPolicy,
    RecallPtzPresetAction,
    RtspCameraConfig,
    RtspTransport,
    SceneLayer,
    SceneReferenceConfig,
    SceneValidationError,
    SourceDefinition,
    SourceKind,
    TransitionKind,
    TransitionSpec,
    ViscaIpPtzBinding,
    ViscaSerialPtzBinding,
    ViscaTransport,
    YeartextSourceConfig,
    new_identity,
)
from solin.core.scenes.recording import (
    AudioDeviceDirection,
    AudioDeviceSelection,
    AudioSelectionMode,
    ProgramRecordingStatus,
)
from solin.core.scenes.ptz import (
    PtzControlResult,
    PtzCredentialVault,
    PtzCredentials,
    PtzRecallResult,
)
from solin.core.scenes.workspace import SceneWorkspaceBusyError
from solin.ui.qml.scenes_models import SceneLayerListModel, SceneListModel
from solin.ui.scene_engine_status import scene_engine_error_summary
from solin.ui.scene_recording_status import (
    scene_recording_audio_warning,
    scene_recording_error_summary,
)


log = logging.getLogger(__name__)

_SOURCE_COLORS = {
    SourceKind.SOLIN_CONTENT: "#3B82F6",
    SourceKind.LOCAL_CAMERA: "#10B981",
    SourceKind.RTSP_CAMERA: "#14B8A6",
    SourceKind.IMAGE: "#8B5CF6",
    SourceKind.COLOR: "#64748B",
    SourceKind.SCENE_REFERENCE: "#F59E0B",
    SourceKind.YEARTEXT: "#EAB308",
}


def _qml_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError("QML value must be numeric")
    return float(value)


def _qml_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError("QML value must be an integer")
    return int(value)


class ScenePreviewStore(Protocol):
    def publish(self, image: QImage) -> str: ...

    def clear(self) -> str: ...


@dataclass(slots=True)
class _LayerFramingSession:
    document_id: str
    document_revision: int
    scene_id: str
    layer: SceneLayer
    bounds: NormalizedRect
    frame: NormalizedRect
    minimum_width: float


class ScenesBridge(QObject):
    """Expose presentation state and validated commands without leaking domain objects."""

    changed = Signal()
    previewChanged = Signal()
    documentGenerationChanged = Signal()
    framingChanged = Signal()
    contentIdleChanged = Signal()
    pointerCursorEntered = Signal(str, int)
    pointerCursorChanged = Signal(str, int)
    pointerCursorExited = Signal(str)
    pointerOverrideStarted = Signal(str, int)
    pointerOverrideEnded = Signal(str)
    ptzResult = Signal(str, bool, str)
    # (x, y, width, height) of the preview canvas frame in the QQuickWidget's
    # logical coordinates — reported by QML so a native GPU preview surface can be
    # pinned exactly over it. (0,0,0,0) means "no canvas" (hide the surface).
    canvasRectChanged = Signal(float, float, float, float)

    def __init__(
        self,
        controller: SceneRuntimeController,
        *,
        preview_store: ScenePreviewStore,
        recording: ProgramRecordingController | None = None,
        credentials: PtzCredentialVault | None = None,
        notifications: Any | None = None,
        recording_directory_picker: Callable[[str], str] | None = None,
        recording_directory_opener: Callable[[str], bool] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._recording = recording
        self._preview_store = preview_store
        self._credentials = credentials
        self._notifications = notifications
        self._recording_directory_picker = recording_directory_picker
        self._recording_directory_opener = recording_directory_opener
        self._scenes = SceneListModel(self)
        self._layers = SceneLayerListModel(self)
        self._active = False
        self._closed = False
        self._selected_scene_id = controller.document.scenes[0].id
        self._selected_layer_id = ""
        self._preview_url = preview_store.clear()
        self._preview_available = False
        self._document_generation = 0
        self._active_guides: tuple[tuple[str, float], ...] = ()
        self._framing_session: _LayerFramingSession | None = None
        self._ptz_recall_futures: set[Future[PtzRecallResult]] = set()
        self._controller_connections: tuple[tuple[Any, Any], ...] = ()
        self._recording_connections: tuple[tuple[Any, Any], ...] = ()
        self._connect_controller()
        self._connect_recording()
        self._refresh_models()

    def _connect_controller(self) -> None:
        controller = self._controller
        self._controller_connections = (
            (controller.document_changed, self._on_document_changed),
            (controller.desired_scenes_changed, self._runtime_changed),
            (controller.applied_scenes_changed, self._runtime_changed),
            (controller.engine_ready_changed, self._runtime_changed),
            (controller.operational_state_changed, self._runtime_changed),
            (controller.runtime_changed, self._runtime_changed),
            (controller.scene_profiles_changed, self._on_profiles_changed),
            (controller.preview_frame_changed, self._on_preview_frame),
            (controller.preview_egress_changed, self._runtime_changed),
            (controller.local_cameras_changed, self._runtime_changed),
            (controller.source_health_changed, self._runtime_changed),
            (controller.content_playing_changed, self._on_content_playing_changed),
        )
        for signal, handler in self._controller_connections:
            signal.connect(handler)

    def _disconnect_controller(self) -> None:
        connections, self._controller_connections = self._controller_connections, ()
        for signal, handler in connections:
            try:
                signal.disconnect(handler)
            except (RuntimeError, TypeError):
                log.debug("Scene bridge signal was already disconnected", exc_info=True)

    def _connect_recording(self) -> None:
        if self._recording is None:
            return
        self._recording_connections = (
            (self._recording.state_changed, self._on_recording_changed),
            (self._recording.audio_devices_changed, self._on_recording_changed),
            (self._recording.configuration_changed, self._on_recording_changed),
            (self._recording.busy_changed, self._on_recording_changed),
        )
        for signal, handler in self._recording_connections:
            signal.connect(handler)

    def _disconnect_recording(self) -> None:
        connections, self._recording_connections = self._recording_connections, ()
        for signal, handler in connections:
            try:
                signal.disconnect(handler)
            except (RuntimeError, TypeError):
                log.debug("Recording bridge signal was already disconnected", exc_info=True)

    @Property(QObject, constant=True)
    def scenesModel(self) -> SceneListModel:
        return self._scenes

    @Property(QObject, constant=True)
    def layersModel(self) -> SceneLayerListModel:
        return self._layers

    @Property(str, notify=changed)
    def selectedSceneId(self) -> str:
        return self._selected_scene_id

    @Property(str, notify=changed)
    def selectedSceneName(self) -> str:
        return self._selected_scene().name

    @Property(str, notify=changed)
    def selectedLayerId(self) -> str:
        return self._selected_layer_id

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def selectedLayer(self) -> dict[str, object]:
        layer = self._selected_layer()
        return {} if layer is None else self._layer_record(layer)

    @Property(bool, notify=framingChanged)
    def framingActive(self) -> bool:
        return self._framing_session is not None

    @Property(str, notify=previewChanged)
    def previewUrl(self) -> str:
        return self._preview_url

    @Property(bool, notify=previewChanged)
    def previewAvailable(self) -> bool:
        return self._preview_available

    @Property(bool, notify=contentIdleChanged)
    def contentIdle(self) -> bool:
        """Whether the content source has nothing to show right now.

        Idle publishes a transparent frame, so a content layer draws nothing. The
        canvas marks it with a media glyph rather than leaving an empty rectangle
        the operator cannot tell from a broken source.
        """
        return not self._controller.content_is_playing

    @Property(int, notify=documentGenerationChanged)
    def documentGeneration(self) -> int:
        return self._document_generation

    @Property(str, notify=changed)
    def outputFormatLabel(self) -> str:
        video_format = self._controller.document.output(BusId.MEDIA_WINDOWS).video_format
        return f"{video_format.width} × {video_format.height}"

    @Property(int, notify=changed)
    def outputWidth(self) -> int:
        return self._controller.document.output(BusId.MEDIA_WINDOWS).video_format.width

    @Property(int, notify=changed)
    def outputHeight(self) -> int:
        return self._controller.document.output(BusId.MEDIA_WINDOWS).video_format.height

    @Property(str, notify=changed)
    def programTransitionKind(self) -> str:
        return self._controller.document.transition_policy.default.kind.value

    @Property(int, notify=changed)
    def programTransitionDurationMs(self) -> int:
        return self._controller.document.transition_policy.default.duration_ms

    @Property(str, notify=changed)
    def programTransitionLabel(self) -> str:
        return self._transition_label(
            self._controller.document.transition_policy.default,
            include_duration=True,
        )

    @Property(str, notify=changed)
    def selectedSceneTransitionOverrideKind(self) -> str:
        override = self._scene_transition_override(self._selected_scene_id)
        return "" if override is None else override.kind.value

    @Property(int, notify=changed)
    def selectedSceneTransitionOverrideDurationMs(self) -> int:
        override = self._scene_transition_override(self._selected_scene_id)
        return 0 if override is None else override.duration_ms

    @Property(bool, notify=changed)
    def canUndo(self) -> bool:
        return self._controller.documents.can_undo

    @Property(bool, notify=changed)
    def canRedo(self) -> bool:
        return self._controller.documents.can_redo

    @Property(bool, notify=changed)
    def selectedSceneLive(self) -> bool:
        return self._controller.applied_scene(BusId.VIRTUAL_CAMERA) == self._selected_scene_id

    @Property(bool, notify=changed)
    def selectedSceneRequiresReplacement(self) -> bool:
        return self._controller.program_scene_is_live(self._selected_scene_id)

    @Property(bool, notify=changed)
    def selectedSceneDesired(self) -> bool:
        return self._controller.desired_scene(BusId.VIRTUAL_CAMERA) == self._selected_scene_id

    @Property(bool, notify=changed)
    def canTakeLive(self) -> bool:
        return not self.selectedSceneDesired and not self._controller.hydration_in_progress

    @Property(bool, notify=changed)
    def virtualCameraEnabled(self) -> bool:
        return self._controller.runtime.state.output(BusId.VIRTUAL_CAMERA).enabled

    @Property(bool, notify=changed)
    def mediaMirrorEnabled(self) -> bool:
        return self._controller.runtime.state.output(BusId.MEDIA_WINDOWS).enabled

    @Property(bool, notify=changed)
    def automaticEnabled(self) -> bool:
        output = self._controller.runtime.state.output(BusId.VIRTUAL_CAMERA)
        return output.mode is OutputMode.AUTO

    @Property(bool, notify=changed)
    def automationConfigured(self) -> bool:
        return self._controller.documents.program_automation_configured

    @Property(str, notify=changed)
    def outputStateLabel(self) -> str:
        if self._controller.hydration_in_progress:
            return self.tr("Switching")
        if self._controller.last_engine_error_code:
            return self.tr("Error")
        return self.tr("On") if self.virtualCameraEnabled else self.tr("Off")

    @Property(str, notify=changed)
    def engineStatus(self) -> str:
        if self._controller.hydration_in_progress:
            return self.tr("Preparing scenes…")
        error_code = self._controller.last_engine_error_code
        if error_code:
            return scene_engine_error_summary(error_code)
        if not self._controller.engine_configured:
            return self.tr("Scene engine unavailable")
        if not self._controller.engine_ready:
            return self.tr("Starting scene engine…")
        return ""

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def sceneProfiles(self) -> list[dict[str, object]]:
        catalog = self._controller.workspace.catalog
        return [
            {
                "id": profile.id,
                "name": profile.name,
                "active": profile.id == catalog.active_collection_id,
            }
            for profile in catalog.collections
        ]

    @Property(str, notify=changed)
    def activeProfileName(self) -> str:
        return self._controller.workspace.active_collection.name

    @Property(bool, notify=changed)
    def profileChangesBlocked(self) -> bool:
        return (
            self._controller.workspace.virtual_camera_enabled
            or self._controller.profile_activation_in_progress
            or bool(self._recording and self._recording.busy)
        )

    @Property(bool, notify=changed)
    def recordingAvailable(self) -> bool:
        # Recording captures Program — the virtual camera's mix. With that output
        # off there is nothing to capture, so the control goes away with it.
        return self._recording is not None and self._controller.program_output_enabled

    @Property(str, notify=changed)
    def recordingStatus(self) -> str:
        if self._recording is None:
            return ProgramRecordingStatus.IDLE.value
        return self._recording.state.status.value

    @Property(bool, notify=changed)
    def recordingActive(self) -> bool:
        return self.recordingStatus in {
            ProgramRecordingStatus.STARTING.value,
            ProgramRecordingStatus.RECORDING.value,
            ProgramRecordingStatus.STOPPING.value,
        }

    @Property(bool, notify=changed)
    def recordingBusy(self) -> bool:
        return bool(self._recording and self._recording.busy)

    @Property(bool, notify=changed)
    def recordingCanToggle(self) -> bool:
        if self._recording is None:
            return False
        status = self._recording.state.status
        return status is ProgramRecordingStatus.RECORDING or (
            self._controller.engine_ready
            and self._recording.supported
            and status in {
                ProgramRecordingStatus.IDLE,
                ProgramRecordingStatus.FAILED,
            }
        )

    @Property(str, notify=changed)
    def recordingToggleUnavailableReason(self) -> str:
        if self._recording is None:
            return self.tr("Recording is unavailable")
        if (
            self._recording.state.status
            in {ProgramRecordingStatus.IDLE, ProgramRecordingStatus.FAILED}
            and not self._controller.engine_ready
        ):
            return self.tr("The scene engine must be ready to record.")
        if not self._recording.supported:
            return self.tr("Recording is unavailable in this scene engine.")
        return ""

    @Property(int, notify=changed)
    def recordingElapsedSeconds(self) -> int:
        if self._recording is None:
            return 0
        started = self._recording.state.started_at_monotonic
        if started is None:
            return 0
        return max(0, int(time.monotonic() - started))

    @Property(str, notify=changed)
    def recordingStatusLabel(self) -> str:
        if self._recording is not None and not self._recording.supported:
            return self.tr("Recording is unavailable")
        labels = {
            ProgramRecordingStatus.IDLE.value: self.tr("Ready to record"),
            ProgramRecordingStatus.STARTING.value: self.tr("Starting recording…"),
            ProgramRecordingStatus.RECORDING.value: self.tr("Recording"),
            ProgramRecordingStatus.STOPPING.value: self.tr("Finishing recording…"),
            ProgramRecordingStatus.FAILED.value: self.tr("Recording failed"),
        }
        status = (
            ProgramRecordingStatus.IDLE.value
            if self._recording is None
            else self._recording.state.status.value
        )
        return labels[status]

    @Property(str, notify=changed)
    def recordingMessage(self) -> str:
        if self._recording is None:
            return self.tr("Recording is unavailable")
        if not self._recording.supported:
            return self.tr("Recording is unavailable in this scene engine.")
        state = self._recording.state
        if state.status is ProgramRecordingStatus.FAILED:
            return scene_recording_error_summary(state.error_code)
        return state.message or state.error_code

    @Property(str, notify=changed)
    def recordingAudioWarning(self) -> str:
        if self._recording is None:
            return ""
        state = self._recording.state
        warnings = tuple(
            value
            for value in (
                scene_recording_audio_warning(
                    state.microphone_warning,
                    "microphone",
                ),
                scene_recording_audio_warning(
                    state.system_audio_warning,
                    "system_audio",
                ),
            )
            if value
        )
        return "\n".join(warnings)

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def recordingMicrophoneChoices(self) -> list[dict[str, object]]:
        return self._recording_audio_choices(AudioDeviceDirection.INPUT)

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def recordingSystemAudioChoices(self) -> list[dict[str, object]]:
        return self._recording_audio_choices(AudioDeviceDirection.OUTPUT)

    @Property(str, notify=changed)
    def recordingAudioDeviceStatus(self) -> str:
        if self._recording is None:
            return ""
        discovery = self._recording.audio_devices
        if not discovery.ready:
            return self.tr("Looking for audio devices…")
        if not discovery.supported or discovery.error_code:
            return self.tr("Audio devices could not be listed.")
        if not discovery.devices:
            return self.tr("No audio devices found.")
        return ""

    @Property(str, notify=changed)
    def recordingMicrophoneSelection(self) -> str:
        if self._recording is None:
            return ""
        return self._recording_selection_key(self._recording.configuration.microphone)

    @Property(str, notify=changed)
    def recordingSystemAudioSelection(self) -> str:
        if self._recording is None:
            return ""
        return self._recording_selection_key(self._recording.configuration.system_audio)

    @Property(str, notify=changed)
    def recordingOutputDirectory(self) -> str:
        if self._recording is None:
            return ""
        return str(self._recording.effective_output_directory())

    @Property(bool, notify=changed)
    def recordingOutputDirectoryIsDefault(self) -> bool:
        return bool(
            self._recording is not None
            and not self._recording.configuration.output_directory
        )

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def configuredCameras(self) -> list[dict[str, object]]:
        """Cameras the operator can drop into the selected scene.

        A camera already used by another scene stays on the list: one source shown
        by several scenes is exactly the point, and a capture device can only be
        opened once, so making the operator create a second source for the same
        device just fails to open. Only cameras already in *this* scene are hidden,
        where a second layer would be a duplicate of itself.
        """
        try:
            in_this_scene = {layer.source_id for layer in self._selected_scene().layers}
        except Exception:  # noqa: BLE001 - a stale selection must not empty the menu
            in_this_scene = set()
        return [
            self._camera_choice(camera)
            for camera in self._controller.workspace.configured_cameras
            if camera.id not in in_this_scene
        ]

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def referencedScenes(self) -> list[dict[str, object]]:
        return [
            {"id": scene.id, "name": scene.name}
            for scene in self._controller.document.scenes
            if scene.id != self._selected_scene_id
        ]

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def localCameraDevices(self) -> list[dict[str, object]]:
        discovery = self._controller.local_cameras
        records: list[dict[str, object]] = [
            {
                "id": "",
                "name": self.tr("Default camera (automatic)"),
                "software": False,
                "available": True,
                "probeStatus": LocalCameraProbeStatus.READY.value,
                "formats": [{"id": "", "label": self.tr("Automatic")}],
            }
        ]
        records.extend(self._local_device_record(device) for device in discovery.devices)
        return records

    @Property(str, notify=changed)
    def localCameraStatus(self) -> str:
        discovery = self._controller.local_cameras
        if not discovery.ready:
            return self.tr("Looking for cameras…")
        if not discovery.supported:
            return self.tr("Camera discovery is available when the scene engine is ready.")
        if discovery.error_code:
            return self.tr("Camera discovery could not be completed.")
        if not discovery.devices:
            return self.tr("No cameras found. The automatic camera remains available.")
        if any(
            device.probe.status is LocalCameraProbeStatus.UNVERIFIED
            for device in discovery.devices
        ):
            return self.tr(
                "Some cameras were detected by Windows but could not be verified. "
                "You can still try them in automatic mode; check the application log "
                "if capture fails."
            )
        return ""

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def selectedLayerPtzCameras(self) -> list[dict[str, object]]:
        return [
            self._camera_choice(self._controller.document.source(camera_id))
            for camera_id in self._ptz_camera_ids_for_layer(self._selected_layer_id)
        ]

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def selectedLayerPtzPresets(self) -> list[dict[str, object]]:
        camera_ids = set(self._ptz_camera_ids_for_layer(self._selected_layer_id))
        camera_names = {
            camera.id: camera.name for camera in self._controller.workspace.configured_cameras
        }
        return [
            self._preset_record(preset, camera_names.get(preset.camera_source_id, ""))
            for preset in self._controller.document.camera_presets
            if preset.camera_source_id in camera_ids
        ]

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def selectedScenePtzPresets(self) -> list[dict[str, object]]:
        camera_ids = set(self._ptz_camera_ids_for_scene(self._selected_scene_id))
        camera_names = {
            camera.id: camera.name for camera in self._controller.workspace.configured_cameras
        }
        return [
            self._preset_record(preset, camera_names.get(preset.camera_source_id, ""))
            for preset in self._controller.document.camera_presets
            if preset.camera_source_id in camera_ids
        ]

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def selectedSceneEntryActions(self) -> list[dict[str, object]]:
        document = self._controller.document
        preset_by_id = {preset.id: preset for preset in document.camera_presets}
        source_by_id = {source.id: source for source in document.sources}
        records: list[dict[str, object]] = []
        for action in self._selected_scene().entry_actions:
            preset = preset_by_id.get(action.preset_id)
            if preset is None:
                continue
            camera = source_by_id.get(preset.camera_source_id)
            records.append(
                {
                    "presetId": preset.id,
                    "presetName": preset.name,
                    "cameraId": preset.camera_source_id,
                    "cameraName": camera.name if camera is not None else "",
                    "timeoutMs": action.timeout_ms,
                    "onTimeout": action.on_timeout.value,
                }
            )
        return records

    @Property(bool, notify=changed)
    def ptzControlsAvailable(self) -> bool:
        return self._controller.ptz_controls_available

    @Slot(bool)
    def setActive(self, active: bool) -> None:
        active = bool(active)
        if self._closed or active == self._active:
            return
        self._active = active
        self._controller.set_preview_scene(self._selected_scene_id if active else None)
        if not active:
            self._clear_framing_session()
            self.stopAllPtz()

    @Slot(float, float, float, float)
    def reportCanvasRect(self, x: float, y: float, width: float, height: float) -> None:
        """QML reports the preview canvas frame's geometry (in QQuickWidget logical
        coords) so a native GPU preview surface can be pinned over it."""
        self.canvasRectChanged.emit(float(x), float(y), float(width), float(height))

    @Slot(str)
    def selectScene(self, scene_id: str) -> None:
        if scene_id == self._selected_scene_id:
            return
        self._controller.document.scene(scene_id)
        self._clear_framing_session()
        self._selected_scene_id = scene_id
        self._selected_layer_id = ""
        self._active_guides = ()
        # Do NOT blank the canvas here: _on_preview_frame already ignores frames
        # from other scenes, so the last frame stays until the newly selected
        # scene produces its first frame — a smooth cross-over instead of a blink.
        self._preview_selected_scene()
        self._refresh_models()

    def _preview_selected_scene(self) -> None:
        """Point the editor canvas at whatever scene is selected now.

        Moving ``_selected_scene_id`` only moves the highlight in the scene list;
        the preview keeps rendering the scene it was last pointed at. Creating,
        duplicating or deleting a scene changes the selection too, so each of those
        has to re-point the canvas as well — otherwise the new scene looks selected
        while the canvas still shows the old one.
        """
        if self._active:
            self._controller.set_preview_scene(self._selected_scene_id)

    @Slot(str)
    def selectLayer(self, layer_id: str) -> None:
        if layer_id and not any(layer.id == layer_id for layer in self._selected_scene().layers):
            return
        if layer_id == self._selected_layer_id:
            return
        self._clear_framing_session()
        self._selected_layer_id = layer_id
        self.changed.emit()

    @Slot()
    def takeLive(self) -> None:
        self._run_runtime(lambda: self._controller.take_program_scene(self._selected_scene_id))

    @Slot(bool)
    def setVirtualCameraEnabled(self, enabled: bool) -> None:
        self._run_runtime(
            lambda: self._controller.set_output_enabled(BusId.VIRTUAL_CAMERA, bool(enabled))
        )

    @Slot(bool)
    def setMediaMirrorEnabled(self, enabled: bool) -> None:
        self._run_runtime(
            lambda: self._controller.set_output_enabled(BusId.MEDIA_WINDOWS, bool(enabled))
        )

    @Slot(bool)
    def setAutomaticEnabled(self, enabled: bool) -> None:
        self._run_runtime(lambda: self._controller.set_program_automatic(bool(enabled)))

    @Slot()
    def toggleProgramRecording(self) -> None:
        self._run_recording(self._recording.toggle if self._recording is not None else None)

    @Slot()
    def refreshRecordingAudioDevices(self) -> None:
        self._run_recording(
            self._recording.refresh_audio_devices
            if self._recording is not None
            else None,
            notify=False,
        )

    @Slot(str)
    def setRecordingMicrophone(self, selection_key: str) -> None:
        recording = self._recording
        if recording is None:
            return
        selection = self._recording_selection(
            selection_key,
            AudioDeviceDirection.INPUT,
            recording.configuration.microphone,
        )
        self._run_recording(lambda: recording.set_microphone_selection(selection))

    @Slot(str)
    def setRecordingSystemAudio(self, selection_key: str) -> None:
        recording = self._recording
        if recording is None:
            return
        selection = self._recording_selection(
            selection_key,
            AudioDeviceDirection.OUTPUT,
            recording.configuration.system_audio,
        )
        self._run_recording(lambda: recording.set_system_audio_selection(selection))

    @Slot()
    def chooseRecordingDirectory(self) -> None:
        recording = self._recording
        if (
            recording is None
            or recording.busy
            or self._recording_directory_picker is None
        ):
            return
        current = str(recording.effective_output_directory())
        selected = self._recording_directory_picker(current)
        if selected:
            self._run_recording(
                lambda: recording.set_output_directory(Path(selected))
            )

    @Slot()
    def useDefaultRecordingDirectory(self) -> None:
        recording = self._recording
        if recording is None or recording.busy:
            return
        self._run_recording(lambda: recording.set_output_directory(None))

    @Slot()
    def openRecordingDirectory(self) -> None:
        if self._recording is None or self._recording_directory_opener is None:
            return
        path = str(self._recording.effective_output_directory())
        if not self._recording_directory_opener(path):
            self._notify_failure(
                self.tr("The recording folder could not be opened."),
                dedupe_key="scenes-recording-folder-open-failed",
            )

    @Slot()
    def undo(self) -> None:
        self._run_edit(self._controller.documents.undo)

    @Slot()
    def redo(self) -> None:
        self._run_edit(self._controller.documents.redo)

    @Slot(str)
    def createScene(self, name: str) -> None:
        clean_name = name.strip()
        if not clean_name:
            return

        def create() -> None:
            document = self._controller.documents.create_scene(clean_name)
            self._selected_scene_id = document.scenes[-1].id
            self._selected_layer_id = ""

        if self._run_edit(create):
            self._preview_selected_scene()

    @Slot(str, str)
    def renameScene(self, scene_id: str, name: str) -> None:
        self._run_edit(lambda: self._controller.documents.rename_scene(scene_id, name.strip()))

    @Slot(str)
    def duplicateScene(self, scene_id: str) -> None:
        source = self._controller.document.scene(scene_id)

        def duplicate() -> None:
            document = self._controller.documents.duplicate_scene(
                scene_id,
                name=self.tr("%1 copy").replace("%1", source.name),
            )
            self._selected_scene_id = document.scenes[-1].id
            self._selected_layer_id = ""

        if self._run_edit(duplicate):
            self._preview_selected_scene()

    @Slot(str, int)
    def reorderScene(self, scene_id: str, target_row: int) -> None:
        self._run_edit(lambda: self._controller.documents.reorder_scene(scene_id, target_row))

    @Slot(str, str)
    def deleteScene(self, scene_id: str, replacement_scene_id: str) -> None:
        document = self._controller.document
        scene_index = next(
            index for index, scene in enumerate(document.scenes) if scene.id == scene_id
        )
        replacements = [scene for scene in document.scenes if scene.id != scene_id]
        if not replacements:
            return
        live = self._controller.program_scene_is_live(scene_id)
        replacement = replacement_scene_id or None
        if live and replacement is None:
            self._notify_failure(
                self.tr("Choose which scene should replace the live scene."),
                dedupe_key="scenes-live-delete-replacement-required",
            )
            return
        fallback = replacements[min(scene_index, len(replacements) - 1)].id

        def delete() -> None:
            self._controller.delete_scene(
                scene_id,
                live_replacement_scene_id=replacement,
            )
            self._selected_scene_id = replacement or fallback
            self._selected_layer_id = ""

        if self._run_edit(delete):
            self._preview_selected_scene()

    @Slot(str, bool)
    def setDefaultScene(self, scene_id: str, enabled: bool) -> None:
        self._run_edit(
            lambda: self._controller.documents.set_program_default_scene(
                scene_id if enabled else None
            )
        )

    @Slot(str, bool)
    def setMediaScene(self, scene_id: str, enabled: bool) -> None:
        self._run_edit(
            lambda: self._controller.documents.set_program_media_scene(
                scene_id if enabled else None
            )
        )

    @Slot(str, int)
    def setProgramTransition(self, kind: str, duration_ms: int) -> None:
        try:
            transition_kind = TransitionKind(kind)
            spec = TransitionSpec(
                kind=transition_kind,
                duration_ms=0 if transition_kind is TransitionKind.CUT else duration_ms,
            )
        except (TypeError, ValueError, SceneValidationError):
            self._notify_failure(
                self.tr("Choose a valid transition and duration."),
                dedupe_key="scenes-transition-invalid",
            )
            return
        self._run_edit(lambda: self._controller.documents.set_program_transition(spec))

    @Slot(str, str, int)
    def setSceneTransitionOverride(
        self,
        scene_id: str,
        kind: str,
        duration_ms: int,
    ) -> None:
        try:
            transition_kind = TransitionKind(kind)
            resolved_duration = (
                0
                if transition_kind is TransitionKind.CUT
                else self._animated_transition_duration(scene_id, duration_ms)
            )
            spec = TransitionSpec(
                kind=transition_kind,
                duration_ms=resolved_duration,
            )
        except (TypeError, ValueError, SceneValidationError):
            self._notify_failure(
                self.tr("Choose a valid transition and duration."),
                dedupe_key="scenes-transition-override-invalid",
            )
            return
        self._run_edit(
            lambda: self._controller.documents.set_scene_transition_override(
                scene_id,
                spec,
            )
        )

    @Slot(str)
    def clearSceneTransitionOverride(self, scene_id: str) -> None:
        self._run_edit(
            lambda: self._controller.documents.set_scene_transition_override(scene_id, None)
        )

    @Slot(str)
    def createSceneProfile(self, name: str) -> None:
        self._run_workspace(lambda: self._controller.workspace.create_collection(name.strip()))

    @Slot(str)
    def renameActiveProfile(self, name: str) -> None:
        current = self._controller.workspace.active_collection
        self._run_workspace(
            lambda: self._controller.workspace.rename_collection(current.id, name.strip())
        )

    @Slot(str)
    def activateSceneProfile(self, collection_id: str) -> None:
        self._run_workspace(lambda: self._controller.activate_scene_profile(collection_id))

    @Slot(str)
    def deleteActiveProfile(self, replacement_collection_id: str) -> None:
        current = self._controller.workspace.active_collection
        self._run_workspace(
            lambda: self._controller.delete_scene_profile(
                current.id,
                replacement_collection_id=replacement_collection_id,
            )
        )

    @Slot()
    def addContentSource(self) -> None:
        self._add_source_layer(CONTENT_SOURCE_ID)

    @Slot()
    def addYearText(self) -> None:
        # The year text is a single global source (all layers show the same rendered
        # image); reuse the document's year-text source if one exists, else create it.
        document = self._controller.document
        source = next(
            (candidate for candidate in document.sources
             if candidate.kind is SourceKind.YEARTEXT),
            None,
        )
        if source is None:
            source = SourceDefinition(
                id=new_identity(),
                kind=SourceKind.YEARTEXT,
                name=self.tr("Year text"),
                configuration=YeartextSourceConfig(),
            )
            if not self._run_edit(lambda: self._controller.documents.create_source(source)):
                return
        self._add_source_layer(source.id)

    @Slot(str)
    def addConfiguredCamera(self, camera_id: str) -> None:
        if not self._run_edit(
            lambda: self._controller.workspace.add_configured_camera_to_active_document(
                camera_id
            )
        ):
            return
        self._add_source_layer(camera_id)

    @Slot(str, bool)
    def setCameraKeepActive(self, camera_id: str, keep_active: bool) -> None:
        camera = next(
            (
                candidate
                for candidate in self._controller.workspace.configured_cameras
                if candidate.id == camera_id
            ),
            None,
        )
        if camera is None or not isinstance(
            camera.configuration,
            (LocalCameraConfig, RtspCameraConfig),
        ):
            return
        normalized = bool(keep_active)
        if camera.configuration.keep_active == normalized:
            return
        updated = replace(
            camera,
            configuration=replace(camera.configuration, keep_active=normalized),
        )
        self._run_edit(
            lambda: self._controller.workspace.upsert_camera(
                updated,
                add_to_active_document=False,
            )
        )

    @Slot(str)
    def addSceneReference(self, scene_id: str) -> None:
        document = self._controller.document
        target = document.scene(scene_id)
        source = next(
            (
                candidate
                for candidate in document.sources
                if candidate.kind is SourceKind.SCENE_REFERENCE
                and isinstance(candidate.configuration, SceneReferenceConfig)
                and candidate.configuration.target_scene_id == scene_id
            ),
            None,
        )
        if source is None:
            source = SourceDefinition(
                id=new_identity(),
                kind=SourceKind.SCENE_REFERENCE,
                name=target.name,
                configuration=SceneReferenceConfig(target_scene_id=scene_id),
            )
            if not self._run_edit(lambda: self._controller.documents.create_source(source)):
                return
        self._add_source_layer(source.id)

    @Slot(str)
    def removeLayer(self, layer_id: str) -> None:
        if not layer_id:
            return
        self._selected_layer_id = ""
        self._run_edit(
            lambda: self._controller.documents.delete_layer_and_orphaned_source(
                self._selected_scene_id,
                layer_id,
            )
        )

    @Slot(str, int)
    def reorderLayer(self, layer_id: str, visual_target_row: int) -> None:
        layers = self._selected_scene().layers
        model_index = len(layers) - 1 - visual_target_row
        self._selected_layer_id = layer_id
        self._run_edit(
            lambda: self._controller.documents.reorder_layer(
                self._selected_scene_id,
                layer_id,
                model_index,
            )
        )

    @Slot(str, bool)
    def setLayerVisible(self, layer_id: str, visible: bool) -> None:
        self._update_layer(
            layer_id,
            lambda layer: replace(layer, visible=bool(visible)),
            allow_locked=True,
        )

    @Slot(str, bool)
    def setLayerLocked(self, layer_id: str, locked: bool) -> None:
        self._update_layer(
            layer_id,
            lambda layer: replace(layer, locked=bool(locked)),
            allow_locked=True,
        )

    @Slot(str, str)
    def setLayerFit(self, layer_id: str, fit_mode: str) -> None:
        self._update_layer(layer_id, lambda layer: replace(layer, fit_mode=FitMode(fit_mode)))

    @Slot(str, bool)
    def setLayerMirrored(self, layer_id: str, mirrored: bool) -> None:
        self._update_layer(
            layer_id,
            lambda layer: replace(layer, mirror_x=bool(mirrored)),
        )

    @Slot(str)
    def resetLayerTransform(self, layer_id: str) -> None:
        self._update_layer(
            layer_id,
            lambda layer: replace(
                layer,
                rect=NormalizedRect(),
                crop=Crop(),
                rotation_degrees=0.0,
                fit_mode=FitMode.CONTAIN,
                mirror_x=False,
                mirror_y=False,
            ),
        )

    @Slot(str)
    def centerLayer(self, layer_id: str) -> None:
        self._update_layer(
            layer_id,
            lambda layer: replace(
                layer,
                rect=replace(
                    layer.rect,
                    x=(1.0 - layer.rect.width) / 2.0,
                    y=(1.0 - layer.rect.height) / 2.0,
                ),
            ),
        )

    @Slot(str, result="QVariantMap")
    def beginLayerFraming(self, layer_id: str) -> dict[str, object]:
        layer = self._layer(layer_id)
        if layer is None or self._framing_unavailable_reason(layer):
            return {}
        bounds = intersect_rect(layer.rect, NormalizedRect())
        if bounds is None:
            return {}
        frame = inscribed_aspect_rect(bounds, aspect=NORMALIZED_CANVAS_ASPECT)
        minimum_width = self._minimum_framing_width(layer)
        if frame.width < minimum_width:
            return {}
        self._clear_framing_session()
        document = self._controller.document
        self._framing_session = _LayerFramingSession(
            document_id=document.document_id,
            document_revision=document.revision,
            scene_id=self._selected_scene_id,
            layer=layer,
            bounds=bounds,
            frame=frame,
            minimum_width=minimum_width,
        )
        self.framingChanged.emit()
        return self._framing_record(self._framing_session)

    @Slot("QVariantMap", result="QVariantMap")  # type: ignore[arg-type]
    def updateLayerFraming(self, values: dict[str, object]) -> dict[str, object]:
        session = self._validated_framing_session()
        if session is None:
            return {}
        try:
            operation = str(values.get("operation", ""))
            if operation == "move":
                session.frame = move_framing_rect(
                    session.frame,
                    session.bounds,
                    _qml_float(values.get("dx", 0.0)),
                    _qml_float(values.get("dy", 0.0)),
                )
            elif operation == "resize":
                session.frame = resize_framing_rect(
                    session.frame,
                    session.bounds,
                    str(values.get("handle", "")),
                    _qml_float(values.get("dx", 0.0)),
                    _qml_float(values.get("dy", 0.0)),
                    aspect=NORMALIZED_CANVAS_ASPECT,
                    minimum_width=session.minimum_width,
                )
            elif operation == "scale":
                session.frame = scale_framing_rect(
                    session.frame,
                    session.bounds,
                    _qml_float(values.get("scale", 1.0)),
                    _qml_float(values.get("anchorX", session.frame.x + session.frame.width / 2.0)),
                    _qml_float(values.get("anchorY", session.frame.y + session.frame.height / 2.0)),
                    aspect=NORMALIZED_CANVAS_ASPECT,
                    minimum_width=session.minimum_width,
                )
            else:
                return self._framing_record(session)
        except (TypeError, ValueError, SceneValidationError):
            log.debug("Rejected invalid layer framing update", exc_info=True)
        return self._framing_record(session)

    @Slot(result=bool)
    def commitLayerFraming(self) -> bool:
        session = self._validated_framing_session()
        if session is None:
            return False
        layer = session.layer
        try:
            crop = crop_for_framing_rect(
                layer.rect,
                layer.crop,
                session.frame,
                mirror_x=layer.mirror_x,
                mirror_y=layer.mirror_y,
            )
            updated = replace(
                layer,
                rect=NormalizedRect(),
                crop=crop,
                fit_mode=FitMode.COVER,
            )
        except (ValueError, SceneValidationError):
            log.exception("Could not finalize layer framing geometry")
            return False
        self._clear_framing_session()
        if updated == layer:
            return False
        self._selected_layer_id = layer.id
        return self._run_edit(
            lambda: self._controller.documents.update_layer(
                session.scene_id,
                layer.id,
                updated,
            )
        )

    @Slot()
    def cancelLayerFraming(self) -> None:
        self._clear_framing_session()

    @Slot(str, result=bool)
    def fillLayerFromCrop(self, layer_id: str) -> bool:
        layer = self._layer(layer_id)
        if layer is None or self._framing_unavailable_reason(layer):
            return False
        try:
            updated = replace(
                layer,
                rect=NormalizedRect(),
                crop=fill_crop_to_canvas(
                    layer.rect,
                    layer.crop,
                    mirror_x=layer.mirror_x,
                    mirror_y=layer.mirror_y,
                    aspect=NORMALIZED_CANVAS_ASPECT,
                ),
                fit_mode=FitMode.COVER,
            )
        except (ValueError, SceneValidationError):
            log.exception("Could not fit layer crop to the canvas")
            return False
        if updated == layer:
            return False
        self._clear_framing_session()
        self._selected_layer_id = layer_id
        return self._run_edit(
            lambda: self._controller.documents.update_layer(
                self._selected_scene_id,
                layer_id,
                updated,
            )
        )

    @Slot(str, "QVariantMap")
    def applyPreciseTransform(self, layer_id: str, values: dict[str, object]) -> None:
        def update(layer: SceneLayer) -> SceneLayer:
            return replace(
                layer,
                rect=NormalizedRect(
                    x=_qml_float(values.get("x", layer.rect.x)),
                    y=_qml_float(values.get("y", layer.rect.y)),
                    width=_qml_float(values.get("width", layer.rect.width)),
                    height=_qml_float(values.get("height", layer.rect.height)),
                ),
                crop=Crop(
                    left=_qml_float(values.get("cropLeft", layer.crop.left)),
                    top=_qml_float(values.get("cropTop", layer.crop.top)),
                    right=_qml_float(values.get("cropRight", layer.crop.right)),
                    bottom=_qml_float(values.get("cropBottom", layer.crop.bottom)),
                ),
                rotation_degrees=_qml_float(
                    values.get("rotation", layer.rotation_degrees)
                ),
            )

        self._update_layer(layer_id, update)

    @Slot(
        str,
        str,
        str,
        float,
        float,
        float,
        float,
        bool,
        bool,
        bool,
        result="QVariantMap",
    )
    def calculateLayerGeometry(
        self,
        layer_id: str,
        mode: str,
        handle: str,
        dx: float,
        dy: float,
        viewport_width: float,
        viewport_height: float,
        free_resize: bool,
        crop: bool,
        disable_snap: bool,
    ) -> dict[str, object]:
        layer = self._layer(layer_id)
        if layer is None or layer.locked:
            return {}
        rect = layer.rect
        updated_crop = layer.crop
        if mode == "move":
            moved = NormalizedRect(
                x=min(1.0 - rect.width, max(0.0, rect.x + dx)),
                y=min(1.0 - rect.height, max(0.0, rect.y + dy)),
                width=rect.width,
                height=rect.height,
            )
            rect, self._active_guides = snap_move_rect(
                moved,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                disabled=disable_snap,
                active_guides=self._active_guides,
            )
        elif mode == "resize" and crop:
            rect, updated_crop = crop_geometry(layer.rect, layer.crop, handle, dx, dy)
            self._active_guides = ()
        elif mode == "resize":
            rect = resize_rect(
                layer.rect,
                handle,
                dx,
                dy,
                preserve_aspect=not free_resize,
            )
            self._active_guides = ()
        else:
            return {}
        return self._geometry_record(layer_id, rect, updated_crop, self._active_guides)

    @Slot("QVariantMap")
    def previewLayerGeometry(self, values: dict[str, object]) -> None:
        parsed = self._parse_geometry(values)
        if parsed is None:
            return
        layer_id, rect, crop = parsed
        layer = self._layer(layer_id)
        if layer is None or layer.locked:
            return
        self._controller.preview_layer_geometry(
            self._selected_scene_id,
            replace(layer, rect=rect, crop=crop),
        )

    @Slot("QVariantMap")
    def commitLayerGeometry(self, values: dict[str, object]) -> None:
        parsed = self._parse_geometry(values)
        self._active_guides = ()
        if parsed is None:
            return
        layer_id, rect, crop = parsed
        self._update_layer(layer_id, lambda layer: replace(layer, rect=rect, crop=crop))

    @Slot(result="QVariantMap")
    def selectedCameraDraft(self) -> dict[str, object]:
        layer = self._selected_layer()
        source = None if layer is None else self._controller.document.source(layer.source_id)
        return self._camera_draft(source)

    @Slot(str, result="QVariantMap")
    def cameraDraft(self, camera_id: str) -> dict[str, object]:
        source = (
            next(
                (
                    camera
                    for camera in self._controller.workspace.configured_cameras
                    if camera.id == camera_id
                ),
                None,
            )
            if camera_id
            else None
        )
        return self._camera_draft(source)

    @Slot("QVariantMap", bool, result=bool)
    def saveCamera(self, values: dict[str, object], add_to_scene: bool) -> bool:
        source_id = str(values.get("id", ""))
        existing = next(
            (
                source
                for source in self._controller.workspace.configured_cameras
                if source.id == source_id
            ),
            None,
        )
        created_credential_ref = ""
        try:
            kind = SourceKind(str(values.get("kind", SourceKind.LOCAL_CAMERA.value)))
            ptz_binding, credentials = self._ptz_binding(values, existing)
            if kind is SourceKind.LOCAL_CAMERA:
                configuration = self._local_camera_configuration(
                    values,
                    existing,
                    ptz_binding,
                )
            elif kind is SourceKind.RTSP_CAMERA:
                configuration = RtspCameraConfig(
                    uri=str(values.get("uri", "")).strip(),
                    transport=RtspTransport(str(values.get("transport", "tcp"))),
                    latency_ms=_qml_int(values.get("latencyMs", 200)),
                    ptz_binding=ptz_binding,
                    keep_active=bool(values.get("keepActive", True)),
                )
            else:
                raise SceneValidationError("Camera kind is invalid")
            camera = SourceDefinition(
                id=existing.id if existing is not None else new_identity(),
                kind=kind,
                name=str(values.get("name", "")).strip(),
                configuration=configuration,
                enabled=existing.enabled if existing is not None else True,
                credential_ref=existing.credential_ref if existing is not None else "",
            )
            if credentials is not None:
                if self._credentials is None:
                    raise SceneValidationError("Protected credential storage is unavailable")
                created_credential_ref = self._credentials.save(credentials)
                camera = self._with_ptz_credential_ref(camera, created_credential_ref)
            elif bool(values.get("clearCredentials", False)):
                camera = self._with_ptz_credential_ref(camera, "")
        except (SceneValidationError, ValueError, TypeError):
            log.warning("Invalid camera configuration", exc_info=True)
            self._notify_failure(
                self.tr("Check the camera name, connection and PTZ settings."),
                dedupe_key="scenes-camera-invalid",
            )
            return False
        previous_ref = self._ptz_credential_ref(existing)
        committed = self._run_edit(
            lambda: self._controller.workspace.upsert_camera(
                camera,
                add_to_active_document=existing is None and add_to_scene,
            )
        )
        if not committed:
            if created_credential_ref:
                self._delete_credential(created_credential_ref, notify=False)
            return False
        current_ref = self._ptz_credential_ref(camera)
        if previous_ref and previous_ref != current_ref:
            self._delete_credential(previous_ref, notify=True)
        if existing is None and add_to_scene:
            self._add_source_layer(camera.id)
        return True

    @Slot()
    def refreshLocalCameras(self) -> None:
        try:
            self._controller.refresh_local_cameras()
        except Exception:  # noqa: BLE001 - engine discovery boundary
            log.warning("Could not refresh local cameras", exc_info=True)
            self._notify_failure(
                self.tr("Camera discovery could not be refreshed."),
                dedupe_key="scenes-camera-discovery-failed",
            )

    @Slot(str, float, float, float, float)
    def movePtz(
        self,
        camera_id: str,
        pan: float,
        tilt: float,
        zoom: float,
        speed: float,
    ) -> None:
        try:
            future = self._controller.move_camera(
                camera_id,
                pan=float(pan),
                tilt=float(tilt),
                zoom=float(zoom),
                speed=float(speed),
            )
        except Exception:  # noqa: BLE001 - PTZ boundary
            self.ptzResult.emit(camera_id, False, self.tr("PTZ control failed."))
            return
        self._observe_ptz_future(camera_id, future)

    @Slot(str, result="QVariantList")
    def ptzPresets(self, camera_id: str) -> list[dict[str, object]]:
        return [
            {"id": preset.id, "name": preset.name}
            for preset in self._controller.document.camera_presets
            if preset.camera_source_id == camera_id
        ]

    @Slot(str, result="QVariantMap")
    def ptzPresetDraft(self, preset_id: str) -> dict[str, object]:
        preset = next(
            (
                candidate
                for candidate in self._controller.workspace.resources.camera_presets
                if candidate.id == preset_id
            ),
            None,
        )
        if preset is None:
            camera_ids = self._ptz_camera_ids_for_layer(self._selected_layer_id)
            return {
                "id": "",
                "cameraId": camera_ids[0] if camera_ids else "",
                "name": "",
                "targetMode": "token",
                "token": "",
                "pan": 0.0,
                "tilt": 0.0,
                "zoom": 0.0,
            }
        return self._preset_record(preset, "")

    @Slot("QVariantMap", result=bool)
    def savePtzPreset(self, values: dict[str, object]) -> bool:
        preset_id = str(values.get("id", ""))
        camera_id = str(values.get("cameraId", ""))
        camera = next(
            (
                candidate
                for candidate in self._controller.workspace.configured_cameras
                if candidate.id == camera_id
            ),
            None,
        )
        binding = self._camera_ptz_binding(camera)
        try:
            if camera is None or binding is None:
                raise SceneValidationError("PTZ camera is unavailable")
            target_mode = str(values.get("targetMode", "token"))
            remote_token = ""
            position = None
            if target_mode == "position":
                if not isinstance(binding, OnvifPtzBinding):
                    raise SceneValidationError("Absolute PTZ positions require ONVIF")
                position = PtzPosition(
                    pan=_qml_float(values.get("pan", 0.0)),
                    tilt=_qml_float(values.get("tilt", 0.0)),
                    zoom=_qml_float(values.get("zoom", 0.0)),
                )
            elif target_mode == "token":
                remote_token = str(values.get("token", "")).strip()
                if isinstance(binding, (ViscaIpPtzBinding, ViscaSerialPtzBinding)):
                    preset_number = int(remote_token, 0)
                    if not 0 <= preset_number <= 127:
                        raise SceneValidationError("VISCA preset is outside the valid range")
            else:
                raise SceneValidationError("PTZ preset target is invalid")
            preset = CameraPreset(
                id=preset_id or new_identity(),
                camera_source_id=camera_id,
                name=str(values.get("name", "")).strip(),
                remote_token=remote_token,
                position=position,
            )
        except (SceneValidationError, TypeError, ValueError):
            log.warning("Invalid PTZ preset", exc_info=True)
            self._notify_failure(
                self.tr("Check the preset name and target."),
                dedupe_key="scenes-ptz-preset-invalid",
            )
            return False
        if preset_id:
            return self._run_edit(
                lambda: self._controller.workspace.update_camera_preset(preset_id, preset)
            )
        return self._run_edit(lambda: self._controller.workspace.create_camera_preset(preset))

    @Slot(str)
    def deletePtzPreset(self, preset_id: str) -> None:
        if preset_id:
            self._run_edit(lambda: self._controller.workspace.delete_camera_preset(preset_id))

    @Slot(str)
    def storePtzPreset(self, preset_id: str) -> None:
        if not preset_id:
            return
        try:
            future = self._controller.store_camera_preset(preset_id)
        except Exception:  # noqa: BLE001 - PTZ boundary
            self.ptzResult.emit("", False, "ptz_store_failed")
            return
        preset = next(
            (
                candidate
                for candidate in self._controller.document.camera_presets
                if candidate.id == preset_id
            ),
            None,
        )
        self._observe_ptz_future(
            preset.camera_source_id if preset is not None else "",
            future,
        )

    @Slot(str, result="QVariantMap")
    def sceneEntryActionDraft(self, preset_id: str) -> dict[str, object]:
        action = next(
            (
                candidate
                for candidate in self._selected_scene().entry_actions
                if candidate.preset_id == preset_id
            ),
            None,
        )
        return {
            "presetId": action.preset_id if action is not None else "",
            "timeoutMs": action.timeout_ms if action is not None else 4000,
            "onTimeout": (
                action.on_timeout.value
                if action is not None
                else PtzTimeoutPolicy.KEEP_CURRENT.value
            ),
        }

    @Slot("QVariantMap", result=bool)
    def saveSceneEntryAction(self, values: dict[str, object]) -> bool:
        try:
            action = RecallPtzPresetAction(
                preset_id=str(values.get("presetId", "")),
                timeout_ms=_qml_int(values.get("timeoutMs", 4000)),
                on_timeout=PtzTimeoutPolicy(str(values.get("onTimeout", "keep_current"))),
            )
        except (SceneValidationError, TypeError, ValueError):
            self._notify_failure(
                self.tr("Check the PTZ scene action."),
                dedupe_key="scenes-ptz-action-invalid",
            )
            return False
        return self._run_edit(
            lambda: self._controller.documents.set_scene_entry_action(
                self._selected_scene_id,
                action,
            )
        )

    @Slot(str)
    def deleteSceneEntryAction(self, preset_id: str) -> None:
        if preset_id:
            self._run_edit(
                lambda: self._controller.documents.delete_scene_entry_action(
                    self._selected_scene_id,
                    preset_id,
                )
            )

    @Slot(str)
    def recallPtzPreset(self, preset_id: str) -> None:
        if not preset_id:
            return
        try:
            future = self._controller.recall_camera_preset(preset_id)
        except Exception:  # noqa: BLE001 - PTZ boundary
            self.ptzResult.emit("", False, self.tr("PTZ preset recall failed."))
            return
        self._ptz_recall_futures.add(future)

        def completed(done: Future[PtzRecallResult]) -> None:
            self._ptz_recall_futures.discard(done)
            try:
                result = done.result()
                succeeded = result.succeeded
                error_code = result.error_code
                camera_id = result.camera_source_id
            except Exception:  # noqa: BLE001 - async PTZ boundary
                succeeded = False
                error_code = "ptz_recall_failed"
                camera_id = ""
            self.ptzResult.emit(camera_id, succeeded, error_code)

        future.add_done_callback(completed)

    @Slot(str)
    def stopPtz(self, camera_id: str) -> None:
        try:
            future = self._controller.stop_camera(camera_id)
        except Exception:  # noqa: BLE001 - PTZ boundary
            return
        self._observe_ptz_future(camera_id, future)

    @Slot()
    def stopAllPtz(self) -> None:
        self._controller.stop_all_camera_movements()

    @Slot(str, int)
    def beginPointer(self, cursor_source: str, cursor_shape: int) -> None:
        self.pointerCursorEntered.emit(cursor_source, cursor_shape)

    @Slot(str, int)
    def updatePointer(self, cursor_source: str, cursor_shape: int) -> None:
        self.pointerCursorChanged.emit(cursor_source, cursor_shape)

    @Slot(str)
    def endPointer(self, cursor_source: str) -> None:
        self.pointerCursorExited.emit(cursor_source)

    @Slot(str, int)
    def beginPointerOverride(self, cursor_source: str, cursor_shape: int) -> None:
        self.pointerOverrideStarted.emit(cursor_source, cursor_shape)

    @Slot(str)
    def endPointerOverride(self, cursor_source: str) -> None:
        self.pointerOverrideEnded.emit(cursor_source)

    def refresh_language(self) -> None:
        self._refresh_models()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._clear_framing_session()
        self._disconnect_controller()
        self._disconnect_recording()
        self.stopAllPtz()
        for future in tuple(self._ptz_recall_futures):
            self._controller.cancel_ptz_recall(future)
        self._ptz_recall_futures.clear()
        if self._active:
            self._active = False
            self._controller.set_preview_scene(None)
        self._clear_preview()

    def _on_document_changed(self, _document: object) -> None:
        self._clear_framing_session()
        document = self._controller.document
        previous_document_id = getattr(self, "_document_id", "")
        self._document_id = document.document_id
        scene_ids = {scene.id for scene in document.scenes}
        if self._selected_scene_id not in scene_ids:
            self._selected_scene_id = document.scenes[0].id
        layer_ids = {layer.id for layer in self._selected_scene().layers}
        if self._selected_layer_id not in layer_ids:
            self._selected_layer_id = ""
        if previous_document_id and previous_document_id != document.document_id:
            self._document_generation += 1
            self.documentGenerationChanged.emit()
            self._clear_preview()
        if self._active:
            self._controller.set_preview_scene(self._selected_scene_id)
        self._refresh_models()

    def _on_profiles_changed(self, change: object) -> None:
        del change
        self.changed.emit()

    def _on_recording_changed(self, _value: object = None) -> None:
        if not self._closed:
            self.changed.emit()

    def _runtime_changed(self, _value: object = None) -> None:
        self._refresh_models()

    def _on_content_playing_changed(self, _playing: bool = False) -> None:
        if self._closed:
            return
        self.contentIdleChanged.emit()

    def _on_preview_frame(self, scene_id: str, image: object) -> None:
        if (
            self._closed
            or scene_id != self._selected_scene_id
            or not isinstance(image, QImage)
            or image.isNull()
        ):
            return
        self._preview_url = self._preview_store.publish(image)
        self._preview_available = True
        self.previewChanged.emit()

    def _clear_preview(self) -> None:
        self._preview_url = self._preview_store.clear()
        self._preview_available = False
        self.previewChanged.emit()

    def _refresh_models(self) -> None:
        if self._closed:
            return
        document = self._controller.document
        self._document_id = document.document_id
        default_id = self._controller.documents.program_default_scene_id
        media_id = self._controller.documents.program_media_scene_id
        live_id = self._controller.applied_scene(BusId.VIRTUAL_CAMERA)
        scene_records = []
        for scene in document.scenes:
            roles = []
            if scene.id == default_id:
                roles.append(self.tr("Default"))
            if scene.id == media_id:
                roles.append(self.tr("Media"))
            if scene.id == live_id:
                roles.append(self.tr("Live"))
            override = self._scene_transition_override(scene.id)
            if override is not None:
                roles.append(self._transition_label(override, include_duration=True))
            scene_records.append(
                {
                    "id": scene.id,
                    "name": scene.name,
                    "metadata": " · ".join(roles),
                    "default": scene.id == default_id,
                    "media": scene.id == media_id,
                    "live": scene.id == live_id,
                    "transition_override": override is not None,
                    "transition_kind": "" if override is None else override.kind.value,
                    "transition_duration_ms": 0 if override is None else override.duration_ms,
                    "transition_label": ""
                    if override is None
                    else self._transition_label(override, include_duration=True),
                }
            )
        self._scenes.replace_items(scene_records)
        scene = self._selected_scene()
        source_by_id = {source.id: source for source in document.sources}
        self._layers.replace_items(
            [
                self._layer_record(layer, source_by_id[layer.source_id])
                for layer in reversed(scene.layers)
            ]
        )
        self.changed.emit()

    def _scene_transition_override(self, scene_id: str) -> TransitionSpec | None:
        return self._controller.document.transition_policy.override_for(scene_id)

    def _animated_transition_duration(self, scene_id: str, requested: int) -> int:
        if requested >= 0:
            return requested
        override = self._scene_transition_override(scene_id)
        if override is not None and override.kind is not TransitionKind.CUT:
            return override.duration_ms
        default = self._controller.document.transition_policy.default
        if default.kind is not TransitionKind.CUT:
            return default.duration_ms
        return 350

    def _transition_label(
        self,
        spec: TransitionSpec,
        *,
        include_duration: bool,
    ) -> str:
        labels = {
            TransitionKind.CUT: self.tr("Cut"),
            TransitionKind.DISSOLVE: self.tr("Dissolve"),
            TransitionKind.FADE_TO_BLACK: self.tr("Fade through black"),
        }
        label = labels[spec.kind]
        if include_duration and spec.kind is not TransitionKind.CUT:
            return f"{label} · {spec.duration_ms} ms"
        return label

    def _selected_scene(self):
        return self._controller.document.scene(self._selected_scene_id)

    def _selected_layer(self) -> SceneLayer | None:
        return self._layer(self._selected_layer_id)

    def _layer(self, layer_id: str) -> SceneLayer | None:
        if not layer_id:
            return None
        return next(
            (layer for layer in self._selected_scene().layers if layer.id == layer_id),
            None,
        )

    def _framing_unavailable_reason(self, layer: SceneLayer) -> str:
        if layer.locked:
            return self.tr("Unlock the source to frame it.")
        if not layer.visible:
            return self.tr("Show the source to frame it.")
        rotation = layer.rotation_degrees % 360.0
        if not math.isclose(rotation, 0.0, abs_tol=1e-6):
            return self.tr("Reset rotation before framing.")
        bounds = intersect_rect(layer.rect, NormalizedRect())
        if bounds is None:
            return self.tr("Move or resize the source into the canvas first.")
        frame = inscribed_aspect_rect(bounds, aspect=NORMALIZED_CANVAS_ASPECT)
        if frame.width < self._minimum_framing_width(layer):
            return self.tr("The visible source area is too small to frame.")
        return ""

    @staticmethod
    def _minimum_framing_width(layer: SceneLayer) -> float:
        visible_x = 1.0 - layer.crop.left - layer.crop.right
        visible_y = 1.0 - layer.crop.top - layer.crop.bottom
        minimum_source_width = layer.rect.width * MINIMUM_VISIBLE_SOURCE / visible_x
        minimum_source_height = layer.rect.height * MINIMUM_VISIBLE_SOURCE / visible_y
        return max(
            MINIMUM_LAYER_SIZE,
            minimum_source_width,
            minimum_source_height * NORMALIZED_CANVAS_ASPECT,
        )

    def _validated_framing_session(self) -> _LayerFramingSession | None:
        session = self._framing_session
        if session is None:
            return None
        document = self._controller.document
        if (
            document.document_id != session.document_id
            or document.revision != session.document_revision
            or self._selected_scene_id != session.scene_id
            or self._selected_layer_id != session.layer.id
        ):
            self._clear_framing_session()
            return None
        return session

    def _clear_framing_session(self) -> None:
        if self._framing_session is None:
            return
        self._framing_session = None
        self.framingChanged.emit()

    @staticmethod
    def _framing_record(session: _LayerFramingSession) -> dict[str, object]:
        frame = session.frame
        return {
            "layerId": session.layer.id,
            "x": frame.x,
            "y": frame.y,
            "width": frame.width,
            "height": frame.height,
        }

    def _layer_record(
        self,
        layer: SceneLayer,
        source: SourceDefinition | None = None,
    ) -> dict[str, object]:
        if source is None:
            source = self._controller.document.source(layer.source_id)
        camera_configuration = (
            source.configuration
            if isinstance(source.configuration, (LocalCameraConfig, RtspCameraConfig))
            else None
        )
        health = self._controller.source_health(source.id)
        source_warning = bool(
            source.kind is SourceKind.LOCAL_CAMERA
            and health is not None
            and (
                health.error_code
                or health.status
                in {SourceHealthStatus.DEGRADED, SourceHealthStatus.FAILED}
            )
        )
        framing_unavailable_reason = self._framing_unavailable_reason(layer)
        return {
            "id": layer.id,
            "source_id": source.id,
            "name": layer.name,
            "kind": source.kind.value,
            "visible": layer.visible,
            "locked": layer.locked,
            "x": layer.rect.x,
            "y": layer.rect.y,
            "width": layer.rect.width,
            "height": layer.rect.height,
            "crop_left": layer.crop.left,
            "crop_top": layer.crop.top,
            "crop_right": layer.crop.right,
            "crop_bottom": layer.crop.bottom,
            "fit_mode": layer.fit_mode.value,
            "mirror_x": layer.mirror_x,
            "rotation": layer.rotation_degrees,
            "opacity": layer.opacity,
            "color": _SOURCE_COLORS[source.kind],
            "ptz_available": bool(self._ptz_camera_ids_for_layer(layer.id)),
            "camera_keep_active": bool(
                camera_configuration is not None and camera_configuration.keep_active
            ),
            "source_warning": source_warning,
            "source_warning_text": (
                self.tr(
                    "Camera unavailable. Check its connection, privacy permissions, "
                    "or whether another application is using it."
                )
                if source_warning
                else ""
            ),
            "framing_available": not framing_unavailable_reason,
            "framing_unavailable_reason": framing_unavailable_reason,
        }

    def _add_source_layer(self, source_id: str) -> None:
        document = self._controller.document
        source = document.source(source_id)
        scene = self._selected_scene()
        contains_content = any(layer.source_id == CONTENT_SOURCE_ID for layer in scene.layers)
        rect = (
            NormalizedRect(x=0.72, y=0.68, width=0.25, height=0.25)
            if contains_content
            and source.kind in {SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA}
            else NormalizedRect()
        )
        layer = SceneLayer(
            id=new_identity(),
            source_id=source.id,
            name=source.name,
            rect=rect,
        )
        self._selected_layer_id = layer.id
        self._run_edit(lambda: self._controller.documents.add_layer(scene.id, layer))

    def _update_layer(self, layer_id: str, update, *, allow_locked: bool = False) -> None:
        layer = self._layer(layer_id)
        if layer is None or (layer.locked and not allow_locked):
            return
        self._selected_layer_id = layer_id
        self._run_edit(
            lambda: self._controller.documents.update_layer(
                self._selected_scene_id,
                layer_id,
                update(layer),
            )
        )

    @staticmethod
    def _geometry_record(
        layer_id: str,
        rect: NormalizedRect,
        crop: Crop,
        guides: tuple[tuple[str, float], ...],
    ) -> dict[str, object]:
        guide_by_axis = dict(guides)
        return {
            "layerId": layer_id,
            "x": rect.x,
            "y": rect.y,
            "width": rect.width,
            "height": rect.height,
            "cropLeft": crop.left,
            "cropTop": crop.top,
            "cropRight": crop.right,
            "cropBottom": crop.bottom,
            "guideX": guide_by_axis.get("x", -1.0),
            "guideY": guide_by_axis.get("y", -1.0),
        }

    @staticmethod
    def _parse_geometry(
        values: dict[str, object],
    ) -> tuple[str, NormalizedRect, Crop] | None:
        try:
            return (
                str(values["layerId"]),
                NormalizedRect(
                    x=_qml_float(values["x"]),
                    y=_qml_float(values["y"]),
                    width=_qml_float(values["width"]),
                    height=_qml_float(values["height"]),
                ),
                Crop(
                    left=_qml_float(values["cropLeft"]),
                    top=_qml_float(values["cropTop"]),
                    right=_qml_float(values["cropRight"]),
                    bottom=_qml_float(values["cropBottom"]),
                ),
            )
        except (KeyError, TypeError, ValueError, SceneValidationError):
            return None

    def _camera_choice(self, camera: SourceDefinition) -> dict[str, object]:
        binding = self._camera_ptz_binding(camera)
        return {
            "id": camera.id,
            "name": camera.name,
            "kind": camera.kind.value,
            "ptz": binding is not None,
            "ptzProtocol": binding.protocol.value if binding is not None else "",
        }

    @staticmethod
    def _preset_record(preset: CameraPreset, camera_name: str) -> dict[str, object]:
        position = preset.position
        return {
            "id": preset.id,
            "cameraId": preset.camera_source_id,
            "cameraName": camera_name,
            "name": preset.name,
            "targetMode": "position" if position is not None else "token",
            "token": preset.remote_token,
            "pan": position.pan if position is not None else 0.0,
            "tilt": position.tilt if position is not None else 0.0,
            "zoom": position.zoom if position is not None else 0.0,
            "canStore": bool(preset.remote_token),
        }

    def _local_device_record(self, device: LocalCameraDevice) -> dict[str, object]:
        name = (
            self.tr("%1 (virtual)").replace("%1", device.display_name)
            if device.software_device
            else device.display_name
        )
        if device.probe.status is LocalCameraProbeStatus.UNVERIFIED:
            name = self.tr("%1 (not verified)").replace("%1", name)
        return {
            "id": device.device_id,
            "name": name,
            "software": device.software_device,
            "available": True,
            "probeStatus": device.probe.status.value,
            "probeErrorCode": device.probe.error_code,
            "formats": [
                {"id": "", "label": self.tr("Automatic")},
                *[
                    {
                        "id": self._format_id(index),
                        "label": self._format_label(video_format),
                    }
                    for index, video_format in enumerate(device.formats)
                ],
            ],
        }

    def _local_camera_configuration(
        self,
        values: dict[str, object],
        existing: SourceDefinition | None,
        ptz_binding: PtzBinding | None,
    ) -> LocalCameraConfig:
        device_id = str(values.get("deviceId", ""))
        format_id = str(values.get("formatId", ""))
        current = (
            existing.configuration
            if existing is not None and isinstance(existing.configuration, LocalCameraConfig)
            else None
        )
        preserve_current_format = bool(
            current is not None
            and current.device_id == device_id
            and current.media_type is not None
            and not bool(values.get("formatSelectionChanged", False))
            and not format_id
        )
        if preserve_current_format:
            assert current is not None
            return replace(
                current,
                ptz_binding=ptz_binding,
                keep_active=bool(values.get("keepActive", True)),
            )
        selected_format = self._local_format(device_id, format_id)
        return LocalCameraConfig(
            device_id=device_id,
            width=selected_format.width if selected_format is not None else 0,
            height=selected_format.height if selected_format is not None else 0,
            fps_numerator=selected_format.fps_numerator if selected_format is not None else 0,
            fps_denominator=selected_format.fps_denominator if selected_format is not None else 1,
            media_type=selected_format.media_type if selected_format is not None else None,
            pixel_format=selected_format.pixel_format if selected_format is not None else "",
            ptz_binding=ptz_binding,
            keep_active=bool(values.get("keepActive", True)),
        )

    @staticmethod
    def _format_id(index: int) -> str:
        return f"format-{index}"

    def _local_format(self, device_id: str, format_id: str) -> LocalVideoFormat | None:
        if not format_id:
            return None
        device = next(
            (
                candidate
                for candidate in self._controller.local_cameras.devices
                if candidate.device_id == device_id
            ),
            None,
        )
        if device is None or not format_id.startswith("format-"):
            raise SceneValidationError("Local camera format is unavailable")
        try:
            return device.formats[int(format_id.removeprefix("format-"))]
        except (ValueError, IndexError) as error:
            raise SceneValidationError("Local camera format is unavailable") from error

    def _format_label(self, video_format: LocalVideoFormat) -> str:
        fps = (
            str(video_format.fps_numerator)
            if video_format.fps_denominator == 1
            else f"{video_format.frames_per_second:.2f}"
        )
        return self.tr("%1 × %2 · %3 fps · %4").replace(
            "%1", str(video_format.width)
        ).replace("%2", str(video_format.height)).replace("%3", fps).replace(
            "%4", video_format.pixel_format
        )

    def _camera_draft(self, source: SourceDefinition | None) -> dict[str, object]:
        kind = source.kind if source is not None else SourceKind.LOCAL_CAMERA
        configuration = source.configuration if source is not None else LocalCameraConfig()
        binding = self._camera_ptz_binding(source)
        values: dict[str, object] = {
            "id": source.id if source is not None else "",
            "kind": kind.value,
            "name": source.name if source is not None else "",
            "deviceId": "",
            "formatId": "",
            "uri": "",
            "transport": RtspTransport.TCP.value,
            "latencyMs": 200,
            "ptzProtocol": "",
            "ptzEndpoint": "",
            "ptzProfileToken": "",
            "ptzHost": "",
            "ptzPort": 52381,
            "ptzTransport": ViscaTransport.UDP.value,
            "ptzSerialDevice": "",
            "ptzBaudRate": 9600,
            "ptzCameraAddress": 1,
            "hasCredentials": bool(self._ptz_credential_ref(source)),
            "keepActive": bool(
                isinstance(configuration, (LocalCameraConfig, RtspCameraConfig))
                and configuration.keep_active
            ),
        }
        if isinstance(configuration, LocalCameraConfig):
            values["deviceId"] = configuration.device_id
            values["formatId"] = self._configured_format_id(configuration)
        elif isinstance(configuration, RtspCameraConfig):
            values.update(
                uri=configuration.uri,
                transport=configuration.transport.value,
                latencyMs=configuration.latency_ms,
            )
        if isinstance(binding, OnvifPtzBinding):
            values.update(
                ptzProtocol=PtzProtocol.ONVIF.value,
                ptzEndpoint=binding.endpoint,
                ptzProfileToken=binding.profile_token,
            )
        elif isinstance(binding, ViscaIpPtzBinding):
            values.update(
                ptzProtocol=PtzProtocol.VISCA_IP.value,
                ptzHost=binding.host,
                ptzPort=binding.port,
                ptzTransport=binding.transport.value,
            )
        elif isinstance(binding, ViscaSerialPtzBinding):
            values.update(
                ptzProtocol=PtzProtocol.VISCA_SERIAL.value,
                ptzSerialDevice=binding.device_id,
                ptzBaudRate=binding.baud_rate,
                ptzCameraAddress=binding.camera_address,
            )
        return values

    def _configured_format_id(self, configuration: LocalCameraConfig) -> str:
        if configuration.media_type is None:
            return ""
        device = next(
            (
                candidate
                for candidate in self._controller.local_cameras.devices
                if candidate.device_id == configuration.device_id
            ),
            None,
        )
        if device is None:
            return ""
        for index, video_format in enumerate(device.formats):
            if (
                video_format.media_type is configuration.media_type
                and video_format.pixel_format == configuration.pixel_format
                and video_format.width == configuration.width
                and video_format.height == configuration.height
                and video_format.fps_numerator == configuration.fps_numerator
                and video_format.fps_denominator == configuration.fps_denominator
            ):
                return self._format_id(index)
        return ""

    def _ptz_binding(
        self,
        values: dict[str, object],
        existing: SourceDefinition | None,
    ) -> tuple[PtzBinding | None, PtzCredentials | None]:
        protocol_value = str(values.get("ptzProtocol", ""))
        if not protocol_value:
            return None, None
        protocol = PtzProtocol(protocol_value)
        if protocol is PtzProtocol.ONVIF:
            binding = OnvifPtzBinding(
                endpoint=str(values.get("ptzEndpoint", "")).strip(),
                profile_token=str(values.get("ptzProfileToken", "")).strip(),
                credential_ref=(
                    ""
                    if bool(values.get("clearCredentials", False))
                    else self._ptz_credential_ref(existing)
                ),
            )
            username = str(values.get("ptzUsername", "")).strip()
            password = str(values.get("ptzPassword", ""))
            if bool(username) != bool(password):
                raise SceneValidationError("ONVIF credentials must be entered together")
            return binding, PtzCredentials(username, password) if username else None
        if protocol is PtzProtocol.VISCA_IP:
            return (
                ViscaIpPtzBinding(
                    host=str(values.get("ptzHost", "")).strip(),
                    port=_qml_int(values.get("ptzPort", 52381)),
                    transport=ViscaTransport(str(values.get("ptzTransport", "udp"))),
                ),
                None,
            )
        return (
            ViscaSerialPtzBinding(
                device_id=str(values.get("ptzSerialDevice", "")).strip(),
                baud_rate=_qml_int(values.get("ptzBaudRate", 9600)),
                camera_address=_qml_int(values.get("ptzCameraAddress", 1)),
            ),
            None,
        )

    @staticmethod
    def _camera_ptz_binding(source: SourceDefinition | None) -> PtzBinding | None:
        if source is None:
            return None
        configuration = source.configuration
        return (
            configuration.ptz_binding
            if isinstance(configuration, (LocalCameraConfig, RtspCameraConfig))
            else None
        )

    @classmethod
    def _ptz_credential_ref(cls, source: SourceDefinition | None) -> str:
        binding = cls._camera_ptz_binding(source)
        return binding.credential_ref if isinstance(binding, OnvifPtzBinding) else ""

    @staticmethod
    def _with_ptz_credential_ref(
        source: SourceDefinition,
        credential_ref: str,
    ) -> SourceDefinition:
        configuration = source.configuration
        if not isinstance(configuration, (LocalCameraConfig, RtspCameraConfig)):
            raise SceneValidationError("PTZ credentials require a camera source")
        binding = configuration.ptz_binding
        if not isinstance(binding, OnvifPtzBinding):
            raise SceneValidationError("PTZ credentials require ONVIF")
        return replace(
            source,
            configuration=replace(
                configuration,
                ptz_binding=replace(binding, credential_ref=credential_ref),
            ),
        )

    def _delete_credential(self, credential_ref: str, *, notify: bool) -> None:
        if not credential_ref or self._credentials is None:
            return
        try:
            self._credentials.delete(credential_ref)
        except Exception:  # noqa: BLE001 - OS credential boundary
            log.warning("Could not delete protected PTZ credentials", exc_info=True)
            if notify:
                self._notify_failure(
                    self.tr("Obsolete PTZ credentials could not be removed."),
                    dedupe_key="scenes-credential-cleanup-failed",
                )

    def _ptz_camera_ids_for_layer(self, layer_id: str) -> tuple[str, ...]:
        layer = self._layer(layer_id)
        if layer is None:
            return ()
        document = self._controller.document
        result: list[str] = []
        visited_scenes: set[str] = set()

        def visit_source(source_id: str) -> None:
            source = document.source(source_id)
            if self._camera_ptz_binding(source) is not None:
                if source.id not in result:
                    result.append(source.id)
                return
            configuration = source.configuration
            if not isinstance(configuration, SceneReferenceConfig):
                return
            if configuration.target_scene_id in visited_scenes:
                return
            visited_scenes.add(configuration.target_scene_id)
            for nested in document.scene(configuration.target_scene_id).layers:
                visit_source(nested.source_id)

        visit_source(layer.source_id)
        return tuple(result)

    def _ptz_camera_ids_for_scene(self, scene_id: str) -> tuple[str, ...]:
        result: list[str] = []
        for layer in self._controller.document.scene(scene_id).layers:
            for camera_id in self._ptz_camera_ids_for_layer(layer.id):
                if camera_id not in result:
                    result.append(camera_id)
        return tuple(result)

    def _observe_ptz_future(
        self,
        camera_id: str,
        future: Future[PtzControlResult],
    ) -> None:
        def completed(done: Future[PtzControlResult]) -> None:
            try:
                result = done.result()
                succeeded = result.succeeded
                error_code = result.error_code
            except Exception:  # noqa: BLE001 - async PTZ boundary
                succeeded = False
                error_code = "ptz_control_failed"
            self.ptzResult.emit(camera_id, succeeded, error_code)

        future.add_done_callback(completed)

    @staticmethod
    def _recording_selection_key(selection: AudioDeviceSelection) -> str:
        if selection.mode is AudioSelectionMode.DEVICE:
            return f"device:{selection.device_id}"
        return selection.mode.value

    def _recording_audio_choices(
        self,
        direction: AudioDeviceDirection,
    ) -> list[dict[str, object]]:
        recording = self._recording
        if recording is None:
            return []
        choices: list[dict[str, object]] = [
            {
                "key": AudioSelectionMode.SYSTEM_DEFAULT.value,
                "name": self.tr("System default"),
                "available": True,
            },
            {
                "key": AudioSelectionMode.NONE.value,
                "name": self.tr("None"),
                "available": True,
            },
        ]
        known_ids: set[str] = set()
        for device in recording.audio_devices.devices:
            if device.direction is not direction:
                continue
            known_ids.add(device.device_id)
            choices.append(
                {
                    "key": f"device:{device.device_id}",
                    "name": device.display_name,
                    "available": True,
                }
            )
        configuration = recording.configuration
        selected = (
            configuration.microphone
            if direction is AudioDeviceDirection.INPUT
            else configuration.system_audio
        )
        if (
            selected.mode is AudioSelectionMode.DEVICE
            and selected.device_id not in known_ids
        ):
            display_name = selected.display_name or self.tr("Unavailable device")
            choices.append(
                {
                    "key": f"device:{selected.device_id}",
                    "name": self.tr("%1 (unavailable)").replace("%1", display_name),
                    "available": False,
                }
            )
        return choices

    def _recording_selection(
        self,
        selection_key: str,
        direction: AudioDeviceDirection,
        current: AudioDeviceSelection,
    ) -> AudioDeviceSelection:
        recording = self._recording
        if recording is None:
            raise RuntimeError("Program recording is unavailable")
        if selection_key == AudioSelectionMode.SYSTEM_DEFAULT.value:
            return AudioDeviceSelection(AudioSelectionMode.SYSTEM_DEFAULT)
        if selection_key == AudioSelectionMode.NONE.value:
            return AudioDeviceSelection(AudioSelectionMode.NONE)
        prefix = "device:"
        if not selection_key.startswith(prefix):
            raise ValueError("Unknown recording audio selection")
        device_id = selection_key[len(prefix) :]
        display_name = ""
        for device in recording.audio_devices.devices:
            if device.direction is direction and device.device_id == device_id:
                display_name = device.display_name
                break
        if not display_name and current.device_id == device_id:
            display_name = current.display_name
        return AudioDeviceSelection(
            AudioSelectionMode.DEVICE,
            device_id=device_id,
            display_name=display_name,
        )

    def _run_recording(self, operation, *, notify: bool = True) -> bool:
        if not callable(operation):
            return False
        try:
            operation()
        except Exception:  # noqa: BLE001 - recording controller boundary
            log.exception("Could not update Program recording")
            if notify:
                self._notify_failure(
                    self.tr("The recording setting could not be updated."),
                    dedupe_key="scenes-recording-update-failed",
                )
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    def _run_edit(self, operation) -> bool:
        try:
            operation()
        except Exception:  # noqa: BLE001 - persistence boundary
            log.exception("Could not persist scene configuration")
            self._notify_failure(
                self.tr("The scene configuration could not be saved."),
                dedupe_key="scenes-configuration-save-failed",
            )
            self._refresh_models()
            return False
        self._refresh_models()
        return True

    def _run_runtime(self, operation) -> bool:
        try:
            operation()
        except Exception:  # noqa: BLE001 - runtime persistence boundary
            log.exception("Could not persist live scene state")
            self._notify_failure(
                self.tr("The live scene state could not be saved."),
                dedupe_key="scenes-runtime-save-failed",
            )
            self._refresh_models()
            return False
        self._refresh_models()
        return True

    def _run_workspace(self, operation) -> bool:
        try:
            operation()
        except SceneWorkspaceBusyError as error:
            self._notify_failure(
                str(error),
                dedupe_key="scenes-profile-busy",
                warning=True,
            )
            self.changed.emit()
            return False
        except Exception:  # noqa: BLE001 - profile transaction boundary
            log.exception("Could not update Scene profiles")
            self._notify_failure(
                self.tr("The Scene profile could not be updated."),
                dedupe_key="scenes-profile-update-failed",
            )
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    def _notify_failure(
        self,
        message: str,
        *,
        dedupe_key: str,
        warning: bool = False,
    ) -> None:
        notify = getattr(self._notifications, "warning" if warning else "error", None)
        if callable(notify):
            notify(message, title=self.tr("Scenes"), dedupe_key=dedupe_key)


__all__ = ["ScenePreviewStore", "ScenesBridge"]
