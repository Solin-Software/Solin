from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
import time

import pytest

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtTest import QSignalSpy

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.engine import (
    LocalCameraDevice,
    LocalCameraDiscovery,
    LocalCameraProbe,
    LocalCameraProbeStatus,
    LocalVideoFormat,
    SourceHealthEvent,
    SourceHealthStatus,
)
from solin.core.scenes.model import (
    BusId,
    CameraMediaType,
    Crop,
    FitMode,
    LocalCameraConfig,
    NormalizedRect,
    OnvifPtzBinding,
    OutputMode,
    PtzProtocol,
    RtspCameraConfig,
    SceneLayer,
    SceneReferenceConfig,
    SourceDefinition,
    SourceKind,
    TransitionKind,
    new_identity,
)
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_SCENE_ID,
    DEFAULT_SCENE_ID,
    SceneSeedNames,
)
from solin.core.scenes.recording import (
    AudioDevice,
    AudioDeviceDirection,
    AudioDeviceDiscovery,
    AudioDeviceSelection,
    AudioSelectionMode,
    ProgramRecordingState,
    ProgramRecordingStatus,
    SceneRecordingConfig,
)
from solin.core.scenes.ptz import PtzControlKind, PtzControlResult, PtzRecallStatus
from solin.core.scenes.workspace import SceneWorkspaceService
from solin.styles.icons import ICON_FOLDER, ICON_FOLDER_LINK, make_icon
from solin.ui.qml.scenes_bridge import ScenesBridge


class _Projection:
    state = {"type": "idle"}

    def subscribe(self, _listener):
        return lambda: None


class _PreviewStore:
    def __init__(self) -> None:
        self.generation = 0
        self.image = QImage()

    def publish(self, _image) -> str:
        self.generation += 1
        self.image = _image.copy()
        return f"image://test/frame/{self.generation}"

    def clear(self) -> str:
        self.generation += 1
        self.image = QImage()
        return f"image://test/empty/{self.generation}"


class _Notifications:
    def __init__(self) -> None:
        self.errors: list[tuple[str, dict[str, str]]] = []
        self.warnings: list[tuple[str, dict[str, str]]] = []

    def error(self, message: str, **options: str) -> None:
        self.errors.append((message, options))

    def warning(self, message: str, **options: str) -> None:
        self.warnings.append((message, options))


def test_recording_directory_uses_a_plain_folder_icon() -> None:
    assert ICON_FOLDER != ICON_FOLDER_LINK
    assert "M22 19" in ICON_FOLDER
    assert "M10 13" not in ICON_FOLDER
    assert not make_icon(ICON_FOLDER, 16, "#ffffff").isNull()


class _Recording(QObject):
    state_changed = Signal(object)
    audio_devices_changed = Signal(object)
    configuration_changed = Signal(object)
    busy_changed = Signal(bool)

    def __init__(self, output_directory: Path) -> None:
        super().__init__()
        self.supported = True
        self.state = ProgramRecordingState()
        self.audio_devices = AudioDeviceDiscovery(
            supported=True,
            ready=True,
            generation=1,
            devices=(
                AudioDevice(
                    "microphone-one",
                    "Lectern microphone",
                    AudioDeviceDirection.INPUT,
                    True,
                ),
                AudioDevice(
                    "output-one",
                    "Main speakers",
                    AudioDeviceDirection.OUTPUT,
                    True,
                ),
            ),
        )
        self.configuration = SceneRecordingConfig()
        self._default_output_directory = output_directory
        self.refresh_count = 0

    @property
    def busy(self) -> bool:
        return self.state.busy

    def toggle(self) -> None:
        if self.state.status is ProgramRecordingStatus.RECORDING:
            self.state = ProgramRecordingState()
        else:
            self.state = ProgramRecordingState(
                status=ProgramRecordingStatus.RECORDING,
                started_at_monotonic=time.monotonic() - 65,
                output_path=self.effective_output_directory() / "recording.mp4",
                active_config=self.configuration,
            )
        self.state_changed.emit(self.state)
        self.busy_changed.emit(self.busy)

    def refresh_audio_devices(self) -> None:
        self.refresh_count += 1

    def set_microphone_selection(self, selection: AudioDeviceSelection) -> None:
        self.configuration = replace(self.configuration, microphone=selection)
        self.configuration_changed.emit(self.configuration)

    def set_system_audio_selection(self, selection: AudioDeviceSelection) -> None:
        self.configuration = replace(self.configuration, system_audio=selection)
        self.configuration_changed.emit(self.configuration)

    def set_output_directory(self, path: Path | None) -> None:
        self.configuration = replace(
            self.configuration,
            output_directory="" if path is None else str(path),
        )
        self.configuration_changed.emit(self.configuration)

    def effective_output_directory(self) -> Path:
        return (
            Path(self.configuration.output_directory)
            if self.configuration.output_directory
            else self._default_output_directory
        )


class _Credentials:
    def __init__(self) -> None:
        self.saved = []
        self.deleted: list[str] = []

    def save(self, credentials) -> str:
        self.saved.append(credentials)
        return "credential-ref"

    def delete(self, credential_ref: str) -> None:
        self.deleted.append(credential_ref)


class _ManualPtz:
    def __init__(self) -> None:
        self.moves: list[str] = []
        self.stops: list[str] = []

    @staticmethod
    def _result(camera_source_id: str, kind: PtzControlKind) -> Future[PtzControlResult]:
        future: Future[PtzControlResult] = Future()
        future.set_result(
            PtzControlResult(
                request_id=kind.value,
                camera_source_id=camera_source_id,
                kind=kind,
                status=PtzRecallStatus.SUCCEEDED,
            )
        )
        return future

    def move(self, *, camera_source_id, binding, **_motion):
        del binding
        self.moves.append(camera_source_id)
        return self._result(camera_source_id, PtzControlKind.MOVE)

    def stop(self, *, camera_source_id, binding):
        del binding
        self.stops.append(camera_source_id)
        return self._result(camera_source_id, PtzControlKind.STOP)

    def close(self) -> None:
        pass


def _seed_names() -> SceneSeedNames:
    return SceneSeedNames(
        content_source="Current content",
        default_camera_source="Default camera",
        no_signal_source="No signal background",
        content_scene="Content",
        camera_scene="Camera",
        content_camera_pip_scene="Content and camera",
        no_signal_scene="No signal",
        content_layer="Content",
        camera_layer="Camera",
        background_layer="Background",
    )


def _bridge(
    tmp_path: Path,
    *,
    notifications=None,
    credentials=None,
    ptz=None,
    recording=None,
    recording_directory_picker=None,
    recording_directory_opener=None,
) -> tuple[SceneWorkspaceService, SceneRuntimeController, ScenesBridge, _PreviewStore]:
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="qml-scenes-bridge-test",
    )
    paths.ensure_dirs()
    workspace = SceneWorkspaceService(paths, seed_names=_seed_names())
    controller = SceneRuntimeController(workspace, _Projection(), ptz=ptz)
    preview_store = _PreviewStore()
    bridge = ScenesBridge(
        controller,
        preview_store=preview_store,
        recording=recording,
        notifications=notifications,
        credentials=credentials,
        recording_directory_picker=recording_directory_picker,
        recording_directory_opener=recording_directory_opener,
    )
    return workspace, controller, bridge, preview_store


def _enable_default_camera_ptz(
    workspace: SceneWorkspaceService,
    controller: SceneRuntimeController,
) -> tuple[str, str]:
    camera = workspace.configured_cameras[0]
    updated = replace(
        camera,
        configuration=LocalCameraConfig(
            ptz_binding=OnvifPtzBinding(
                endpoint="https://camera.test/onvif/ptz",
                profile_token="profile-one",
            )
        ),
    )
    workspace.upsert_camera(updated, add_to_active_document=False)
    scene = controller.document.scenes[0]
    layer = scene.layers[0]
    return camera.id, layer.id


def test_recording_bridge_projects_one_controller_and_applies_live_settings(
    tmp_path: Path,
) -> None:
    default_directory = tmp_path / "Videos" / "Solin"
    chosen_directory = tmp_path / "Recordings"
    opened: list[str] = []
    recording = _Recording(default_directory)
    workspace, controller, bridge, _preview = _bridge(
        tmp_path,
        recording=recording,
        recording_directory_picker=lambda current: (
            str(chosen_directory) if current == str(default_directory) else ""
        ),
        recording_directory_opener=lambda path: opened.append(path) is None,
    )

    # Recording captures the virtual camera, so the control follows that output.
    assert not bridge.recordingAvailable
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    assert bridge.recordingAvailable
    assert bridge.recordingStatus == "idle"
    assert not bridge.recordingCanToggle
    assert bridge.recordingToggleUnavailableReason == (
        "The scene engine must be ready to record."
    )
    controller._set_engine_ready(True)
    assert bridge.recordingCanToggle
    recording.supported = False
    assert not bridge.recordingCanToggle
    assert bridge.recordingToggleUnavailableReason == (
        "Recording is unavailable in this scene engine."
    )
    recording.supported = True
    assert not bridge.recordingBusy
    assert bridge.recordingOutputDirectory == str(default_directory)
    assert bridge.recordingOutputDirectoryIsDefault
    assert [choice["key"] for choice in bridge.recordingMicrophoneChoices] == [
        "system_default",
        "none",
        "device:microphone-one",
    ]
    assert [choice["key"] for choice in bridge.recordingSystemAudioChoices] == [
        "system_default",
        "none",
        "device:output-one",
    ]

    bridge.setRecordingMicrophone("device:microphone-one")
    bridge.setRecordingSystemAudio("none")
    bridge.chooseRecordingDirectory()

    assert recording.configuration.microphone == AudioDeviceSelection(
        AudioSelectionMode.DEVICE,
        "microphone-one",
        "Lectern microphone",
    )
    assert recording.configuration.system_audio.mode is AudioSelectionMode.NONE
    assert recording.configuration.output_directory == str(chosen_directory)
    assert not bridge.recordingOutputDirectoryIsDefault

    missing_microphone = AudioDeviceSelection(
        AudioSelectionMode.DEVICE,
        "missing-microphone",
        "Side microphone",
    )
    recording.set_microphone_selection(missing_microphone)
    missing_choice = bridge.recordingMicrophoneChoices[-1]
    assert missing_choice == {
        "key": "device:missing-microphone",
        "name": "Side microphone (unavailable)",
        "available": False,
    }
    bridge.setRecordingMicrophone("device:missing-microphone")
    assert recording.configuration.microphone == missing_microphone

    bridge.toggleProgramRecording()

    assert bridge.recordingStatus == "recording"
    assert bridge.recordingBusy
    assert bridge.profileChangesBlocked
    assert bridge.recordingElapsedSeconds >= 65
    recording.state = replace(
        recording.state,
        microphone_warning="Microphone unavailable; recording silence.",
        system_audio_warning="System audio unavailable; recording silence.",
    )
    recording.state_changed.emit(recording.state)
    assert bridge.recordingAudioWarning == (
        "Microphone unavailable; recording silence.\n"
        "System audio unavailable; recording silence."
    )
    bridge.chooseRecordingDirectory()
    assert recording.configuration.output_directory == str(chosen_directory)

    bridge.toggleProgramRecording()
    bridge.useDefaultRecordingDirectory()
    bridge.openRecordingDirectory()
    bridge.refreshRecordingAudioDevices()

    assert not bridge.recordingBusy
    assert recording.configuration.output_directory == ""
    assert opened == [str(default_directory)]
    assert recording.refresh_count == 1

    bridge.close()
    controller.close()
    del workspace


def test_camera_source_health_surfaces_an_actionable_layer_warning(tmp_path: Path) -> None:
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    bridge.selectScene(CAMERA_SCENE_ID)  # the camera layer lives in the Camera scene
    camera_id = workspace.configured_cameras[0].id
    controller._consume_engine_event(  # noqa: SLF001 - exercise the queued event boundary
        SourceHealthEvent(
            source_id=camera_id,
            status=SourceHealthStatus.FAILED,
            error_code="local_camera_stream_failed",
        )
    )

    camera_record = next(
        bridge.layersModel.get(row)
        for row in range(bridge.layersModel.rowCount())
        if bridge.layersModel.get(row)["source_id"] == camera_id
    )
    assert camera_record["source_warning"] is True
    assert "privacy permissions" in str(camera_record["source_warning_text"])

    controller._consume_engine_event(  # noqa: SLF001 - exercise the queued event boundary
        SourceHealthEvent(source_id=camera_id, status=SourceHealthStatus.READY)
    )
    recovered_record = next(
        bridge.layersModel.get(row)
        for row in range(bridge.layersModel.rowCount())
        if bridge.layersModel.get(row)["source_id"] == camera_id
    )
    assert recovered_record["source_warning"] is False
    assert recovered_record["source_warning_text"] == ""
    bridge.close()
    controller.close()
    workspace.close()


def test_bridge_persists_shared_ptz_presets_and_scene_actions(tmp_path: Path) -> None:
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    camera_id, _ = _enable_default_camera_ptz(workspace, controller)
    # The default camera layer lives in the Camera scene (scene 0 is now Default).
    bridge.selectScene(CAMERA_SCENE_ID)
    layer_id = controller.document.scene(CAMERA_SCENE_ID).layers[0].id
    bridge.selectLayer(layer_id)

    assert bridge.savePtzPreset(
        {
            "cameraId": camera_id,
            "name": "Lectern",
            "targetMode": "position",
            "pan": 0.2,
            "tilt": -0.1,
            "zoom": 0.4,
        }
    )

    preset = workspace.resources.camera_presets[0]
    assert preset.name == "Lectern"
    assert preset.position is not None
    assert bridge.selectedLayerPtzPresets[0]["id"] == preset.id
    assert bridge.saveSceneEntryAction(
        {
            "presetId": preset.id,
            "timeoutMs": 3500,
            "onTimeout": "take_anyway",
        }
    )
    assert bridge.selectedSceneEntryActions == [
        {
            "presetId": preset.id,
            "presetName": "Lectern",
            "cameraId": camera_id,
            "cameraName": "Default camera",
            "timeoutMs": 3500,
            "onTimeout": "take_anyway",
        }
    ]

    bridge.deletePtzPreset(preset.id)

    assert workspace.resources.camera_presets == ()
    assert controller.document.scene(bridge.selectedSceneId).entry_actions == ()
    bridge.close()
    controller.close()


def test_bridge_rejects_absolute_position_for_non_onvif_camera(tmp_path: Path) -> None:
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    camera = workspace.configured_cameras[0]
    camera_id, layer_id = _enable_default_camera_ptz(workspace, controller)
    bridge.selectLayer(layer_id)
    workspace.upsert_camera(
        replace(camera, configuration=LocalCameraConfig()),
        add_to_active_document=False,
    )

    assert not bridge.savePtzPreset(
        {
            "cameraId": camera_id,
            "name": "Invalid",
            "targetMode": "position",
            "pan": 0.0,
            "tilt": 0.0,
            "zoom": 0.0,
        }
    )
    assert workspace.resources.camera_presets == ()
    bridge.close()
    controller.close()


def test_bridge_edits_a_scene_and_tracks_desired_separately_from_applied(
    tmp_path: Path,
) -> None:
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)

    bridge.createScene("Custom")
    camera_id = workspace.configured_cameras[0].id
    bridge.addConfiguredCamera(camera_id)
    bridge.takeLive()

    scene = controller.document.scene(bridge.selectedSceneId)
    virtual = controller.runtime.state.output(BusId.VIRTUAL_CAMERA)
    # First-run ships Default + Camera + Content; the new "Custom" scene makes four.
    assert bridge.scenesModel.rowCount() == 4
    assert len(scene.layers) == 1
    assert virtual.mode is OutputMode.AUTO
    assert virtual.manual_scene_id == scene.id
    assert bridge.selectedSceneDesired
    assert not bridge.selectedSceneLive
    assert bridge.engineStatus == "Scene engine unavailable"
    bridge.close()
    controller.close()


def test_bridge_add_year_text_adds_a_full_size_layer_and_reuses_one_source(
    tmp_path: Path,
) -> None:
    from solin.core.scenes.model import SourceKind

    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    bridge.createScene("Talk")
    talk_id = bridge.selectedSceneId

    bridge.addYearText()

    scene = controller.document.scene(talk_id)
    assert len(scene.layers) == 1
    layer = scene.layers[0]
    source = controller.document.source(layer.source_id)
    assert source.kind is SourceKind.YEARTEXT
    # full-size by default
    assert (layer.rect.x, layer.rect.y, layer.rect.width, layer.rect.height) == (
        0.0,
        0.0,
        1.0,
        1.0,
    )

    # Adding the year text to another scene reuses the single global source.
    bridge.createScene("Consideration")
    bridge.addYearText()
    yeartext_sources = [
        s for s in controller.document.sources if s.kind is SourceKind.YEARTEXT
    ]
    assert len(yeartext_sources) == 1
    bridge.close()
    controller.close()


def test_bridge_preserves_enums_and_transform_contracts(tmp_path: Path) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    layer_id = controller.document.scene(bridge.selectedSceneId).layers[0].id
    bridge.selectLayer(layer_id)

    bridge.setLayerFit(layer_id, "contain")
    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert layer.fit_mode is FitMode.CONTAIN
    controller.documents.update_layer(
        bridge.selectedSceneId,
        layer_id,
        replace(
            layer,
            rect=NormalizedRect(x=0.2, y=0.2, width=0.4, height=0.4),
            crop=Crop(left=0.2, top=0.1),
            fit_mode=FitMode.STRETCH,
            mirror_x=True,
        ),
    )
    bridge.resetLayerTransform(layer_id)
    reset = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert reset.rect == NormalizedRect()
    assert reset.crop == Crop()
    assert reset.fit_mode is FitMode.CONTAIN
    assert not reset.mirror_x

    bridge.applyPreciseTransform(
        layer_id,
        {"x": 0.125, "y": 0.25, "width": 0.5, "height": 0.4},
    )
    precise = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert precise.rect == NormalizedRect(x=0.125, y=0.25, width=0.5, height=0.4)
    bridge.centerLayer(layer_id)
    centered = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert centered.rect.x == 0.25
    assert centered.rect.y == 0.3
    bridge.close()
    controller.close()


def test_bridge_toggles_horizontal_layer_mirroring_with_undo(tmp_path: Path) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    layer_id = controller.document.scene(bridge.selectedSceneId).layers[0].id
    bridge.selectLayer(layer_id)
    revision = controller.document.revision

    bridge.setLayerMirrored(layer_id, True)

    mirrored = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert mirrored.mirror_x
    assert not mirrored.mirror_y
    assert bridge.selectedLayer["mirror_x"] is True
    assert controller.document.revision == revision + 1

    bridge.undo()
    restored = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert not restored.mirror_x
    assert controller.document.revision == revision + 2

    bridge.setLayerLocked(layer_id, True)
    locked_revision = controller.document.revision
    bridge.setLayerMirrored(layer_id, True)
    locked = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert not locked.mirror_x
    assert controller.document.revision == locked_revision
    bridge.close()
    controller.close()


def test_bridge_framing_session_commits_one_revision_and_undoes_cleanly(
    tmp_path: Path,
) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    layer_id = controller.document.scene(bridge.selectedSceneId).layers[0].id
    bridge.selectLayer(layer_id)
    seed = controller.document.scene(bridge.selectedSceneId).layers[0]
    controller.documents.update_layer(
        bridge.selectedSceneId,
        layer_id,
        replace(
            seed,
            opacity=0.64,
            mirror_x=True,
            mirror_y=True,
            border_color="#3B82F6FF",
            border_width=0.008,
            corner_radius=0.04,
        ),
    )
    original = controller.document.scene(bridge.selectedSceneId).layers[0]
    revision = controller.document.revision

    draft = bridge.beginLayerFraming(layer_id)
    updated_draft = bridge.updateLayerFraming(
        {
            "operation": "scale",
            "scale": 0.5,
            "anchorX": 0.5,
            "anchorY": 0.5,
        }
    )

    assert draft["width"] == 1.0
    assert bridge.framingActive
    assert updated_draft["x"] == 0.25
    assert updated_draft["width"] == 0.5
    assert controller.document.revision == revision
    assert controller.document.scene(bridge.selectedSceneId).layers[0] == original

    assert bridge.commitLayerFraming()

    framed = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert controller.document.revision == revision + 1
    assert not bridge.framingActive
    assert framed.rect == NormalizedRect()
    assert framed.crop == Crop(left=0.25, top=0.25, right=0.25, bottom=0.25)
    assert framed.fit_mode is FitMode.COVER
    assert (
        framed.opacity,
        framed.mirror_x,
        framed.mirror_y,
        framed.border_color,
        framed.border_width,
        framed.corner_radius,
    ) == (
        original.opacity,
        original.mirror_x,
        original.mirror_y,
        original.border_color,
        original.border_width,
        original.corner_radius,
    )

    bridge.undo()
    assert controller.document.scene(bridge.selectedSceneId).layers[0] == original
    bridge.close()
    controller.close()


def test_bridge_cancels_and_invalidates_framing_without_document_mutation(
    tmp_path: Path,
) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    bridge.selectScene(CAMERA_SCENE_ID)  # a framable camera layer (not the year text)
    layer_id = controller.document.scene(bridge.selectedSceneId).layers[0].id
    bridge.selectLayer(layer_id)
    revision = controller.document.revision

    assert bridge.beginLayerFraming(layer_id)
    bridge.updateLayerFraming({"operation": "move", "dx": -0.2, "dy": 0.1})
    bridge.cancelLayerFraming()

    assert not bridge.framingActive
    assert controller.document.revision == revision

    assert bridge.beginLayerFraming(layer_id)
    bridge.selectScene(CONTENT_SCENE_ID)
    assert not bridge.framingActive
    assert controller.document.revision == revision

    bridge.selectScene(CAMERA_SCENE_ID)
    bridge.selectLayer(layer_id)
    assert bridge.beginLayerFraming(layer_id)
    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    controller.documents.update_layer(
        bridge.selectedSceneId,
        layer_id,
        replace(layer, opacity=0.8),
    )

    assert not bridge.framingActive
    assert bridge.updateLayerFraming({"operation": "move", "dx": 0.1, "dy": 0.1}) == {}
    bridge.close()
    controller.close()


def test_bridge_fill_from_crop_expands_then_fills_without_source_kind_coupling(
    tmp_path: Path,
) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    bridge.selectScene(CONTENT_SCENE_ID)
    content_layer_id = controller.document.scene(CONTENT_SCENE_ID).layers[0].id
    bridge.selectLayer(content_layer_id)
    layer = controller.document.scene(CONTENT_SCENE_ID).layers[0]
    controller.documents.update_layer(
        CONTENT_SCENE_ID,
        content_layer_id,
        replace(
            layer,
            rect=NormalizedRect(x=0.3, y=0.1, width=0.3, height=0.8),
            crop=Crop(left=0.3, top=0.1, right=0.4, bottom=0.1),
        ),
    )
    revision = controller.document.revision

    assert bridge.selectedLayer["kind"] == SourceKind.SOLIN_CONTENT.value
    assert bridge.fillLayerFromCrop(content_layer_id)

    filled = controller.document.scene(CONTENT_SCENE_ID).layers[0]
    assert controller.document.revision == revision + 1
    assert filled.rect == NormalizedRect()
    assert abs(filled.crop.left - 0.05) < 1e-9
    assert abs(filled.crop.right - 0.15) < 1e-9
    assert abs(filled.crop.top - 0.1) < 1e-9
    assert abs(filled.crop.bottom - 0.1) < 1e-9
    assert filled.fit_mode is FitMode.COVER
    bridge.close()
    controller.close()


def test_bridge_explains_why_rotated_layers_cannot_be_framed(tmp_path: Path) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    controller.documents.update_layer(
        bridge.selectedSceneId,
        layer.id,
        replace(layer, rotation_degrees=15.0),
    )
    bridge.selectLayer(layer.id)

    assert bridge.selectedLayer["framing_available"] is False
    assert "rotation" in str(bridge.selectedLayer["framing_unavailable_reason"]).lower()
    assert bridge.beginLayerFraming(layer.id) == {}
    assert not bridge.fillLayerFromCrop(layer.id)
    bridge.close()
    controller.close()


def test_bridge_explains_why_locked_layers_cannot_be_framed(tmp_path: Path) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    controller.documents.update_layer(
        bridge.selectedSceneId,
        layer.id,
        replace(layer, locked=True),
    )
    bridge.selectLayer(layer.id)

    assert bridge.selectedLayer["framing_available"] is False
    assert "unlock" in str(bridge.selectedLayer["framing_unavailable_reason"]).lower()
    assert bridge.beginLayerFraming(layer.id) == {}
    assert not bridge.fillLayerFromCrop(layer.id)
    bridge.close()
    controller.close()


def test_bridge_geometry_preview_commits_exactly_one_revision(tmp_path: Path) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    layer_id = controller.document.scene(bridge.selectedSceneId).layers[0].id
    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    controller.documents.update_layer(
        bridge.selectedSceneId,
        layer_id,
        replace(
            layer,
            rect=NormalizedRect(x=0.1, y=0.1, width=0.5, height=0.5),
        ),
    )
    revision = controller.document.revision
    geometry = bridge.calculateLayerGeometry(
        layer_id,
        "move",
        "",
        -0.1,
        -0.1,
        960.0,
        540.0,
        False,
        False,
        True,
    )
    bridge.previewLayerGeometry(geometry)
    bridge.previewLayerGeometry(geometry)
    assert controller.document.revision == revision

    bridge.commitLayerGeometry(geometry)

    assert controller.document.revision == revision + 1
    bridge.close()
    controller.close()


def test_bridge_scene_roles_are_removable_secondary_metadata(tmp_path: Path) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    default_id = controller.documents.program_default_scene_id
    records = [
        bridge.scenesModel.get(row) for row in range(bridge.scenesModel.rowCount())
    ]
    default_record = next(record for record in records if record["id"] == default_id)
    assert "Default" in default_record["metadata"]

    bridge.setDefaultScene(str(default_id), False)

    assert controller.documents.program_default_scene_id is None
    assert not controller.documents.program_automation_configured
    bridge.close()
    controller.close()


def test_bridge_delete_requires_a_replacement_only_for_program_scene(
    tmp_path: Path,
) -> None:
    notifications = _Notifications()
    _workspace, controller, bridge, _preview_store = _bridge(
        tmp_path,
        notifications=notifications,
    )
    bridge.setVirtualCameraEnabled(True)
    # The Default scene is the program/default role holder now, so deleting it is
    # what requires a replacement.
    bridge.selectScene(DEFAULT_SCENE_ID)
    assert bridge.selectedSceneRequiresReplacement
    assert not bridge.selectedSceneLive

    bridge.deleteScene(DEFAULT_SCENE_ID, "")
    assert controller.document.scene(DEFAULT_SCENE_ID).id == DEFAULT_SCENE_ID
    assert notifications.errors[-1][1]["dedupe_key"] == (
        "scenes-live-delete-replacement-required"
    )

    bridge.deleteScene(DEFAULT_SCENE_ID, CONTENT_SCENE_ID)

    assert all(scene.id != DEFAULT_SCENE_ID for scene in controller.document.scenes)
    assert {
        output.manual_scene_id for output in controller.runtime.state.outputs
    } == {CONTENT_SCENE_ID}
    bridge.close()
    controller.close()


def test_bridge_deletes_offline_scene_without_confusing_editor_preview(
    tmp_path: Path,
) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    bridge.selectScene(CONTENT_SCENE_ID)
    bridge.setActive(True)
    assert not bridge.selectedSceneRequiresReplacement

    bridge.deleteScene(CONTENT_SCENE_ID, "")

    assert all(scene.id != CONTENT_SCENE_ID for scene in controller.document.scenes)
    assert all(not mapping.assignments for mapping in controller.document.automation)
    bridge.close()
    controller.close()


def test_bridge_keeps_preview_across_scene_switch_but_invalidates_on_profile_change(
    tmp_path: Path,
) -> None:
    workspace, controller, bridge, preview_store = _bridge(tmp_path)
    bridge.setActive(True)
    scene_id = bridge.selectedSceneId
    frame = QImage(64, 36, QImage.Format.Format_ARGB32)
    frame.fill(Qt.GlobalColor.red)
    controller.preview_frame_changed.emit(scene_id, frame)
    assert bridge.previewAvailable
    assert not preview_store.image.isNull()

    other_scene_id = next(
        scene.id for scene in controller.document.scenes if scene.id != scene_id
    )
    # Switching scenes must NOT blank the canvas (no blink): the last frame is
    # retained until the newly selected scene renders its first frame.
    bridge.selectScene(other_scene_id)
    assert bridge.previewAvailable
    # A frame from the previously selected scene is ignored — only the newly
    # selected scene updates the canvas.
    controller.preview_frame_changed.emit(scene_id, frame)
    assert bridge.previewAvailable
    other_frame = QImage(64, 36, QImage.Format.Format_ARGB32)
    other_frame.fill(Qt.GlobalColor.green)
    controller.preview_frame_changed.emit(other_scene_id, other_frame)
    assert bridge.previewAvailable

    bridge.selectScene(scene_id)
    controller.preview_frame_changed.emit(scene_id, frame)
    generation = bridge.documentGeneration
    workspace.create_collection("Second")
    target = next(
        profile
        for profile in workspace.catalog.collections
        if profile.id != workspace.catalog.active_collection_id
    )
    bridge.activateSceneProfile(target.id)
    assert bridge.documentGeneration == generation + 1
    assert not bridge.previewAvailable
    bridge.close()
    controller.close()


def test_bridge_source_rows_reorder_front_to_back_and_unlock_cleanly(
    tmp_path: Path,
) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    scene_id = bridge.selectedSceneId
    source_id = controller.document.scene(scene_id).layers[0].source_id
    original_id = controller.document.scene(scene_id).layers[0].id
    for name in ("Middle", "Front"):
        controller.documents.add_layer(
            scene_id,
            SceneLayer(id=new_identity(), source_id=source_id, name=name),
        )
    visual = [
        str(bridge.layersModel.get(row)["id"])
        for row in range(bridge.layersModel.rowCount())
    ]
    assert visual[-1] == original_id

    bridge.reorderLayer(original_id, 0)
    assert controller.document.scene(scene_id).layers[-1].id == original_id
    bridge.setLayerLocked(original_id, True)
    bridge.setLayerVisible(original_id, False)
    bridge.setLayerLocked(original_id, False)
    updated = next(
        layer for layer in controller.document.scene(scene_id).layers if layer.id == original_id
    )
    assert not updated.visible
    assert not updated.locked
    bridge.close()
    controller.close()


def test_bridge_resolves_transitive_ptz_camera_once(tmp_path: Path) -> None:
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    camera_id, _layer_id = _enable_default_camera_ptz(workspace, controller)
    reference = SourceDefinition(
        id=new_identity(),
        name="Camera scene",
        kind=SourceKind.SCENE_REFERENCE,
        configuration=SceneReferenceConfig(target_scene_id=CAMERA_SCENE_ID),
    )
    controller.documents.create_source(reference)
    layer = SceneLayer(id=new_identity(), source_id=reference.id, name=reference.name)
    controller.documents.add_layer(CONTENT_SCENE_ID, layer)
    bridge.selectScene(CONTENT_SCENE_ID)
    bridge.selectLayer(layer.id)

    assert [camera["id"] for camera in bridge.selectedLayerPtzCameras] == [camera_id]
    bridge.close()
    controller.close()


def test_bridge_stops_ptz_motion_on_deactivation(tmp_path: Path) -> None:
    ptz = _ManualPtz()
    workspace, controller, bridge, _preview_store = _bridge(tmp_path, ptz=ptz)
    camera_id, layer_id = _enable_default_camera_ptz(workspace, controller)
    bridge.selectLayer(layer_id)
    bridge.setActive(True)
    bridge.movePtz(camera_id, 1.0, 0.0, 0.0, 0.5)

    bridge.setActive(False)

    assert ptz.moves == [camera_id]
    assert ptz.stops == [camera_id]
    bridge.close()
    controller.close()


def test_bridge_reports_profile_failures_through_central_notifications(
    tmp_path: Path,
) -> None:
    notifications = _Notifications()
    _workspace, controller, bridge, _preview_store = _bridge(
        tmp_path,
        notifications=notifications,
    )

    succeeded = bridge._run_workspace(
        lambda: (_ for _ in ()).throw(RuntimeError("sensitive detail"))
    )

    assert not succeeded
    assert notifications.errors == [
        (
            "The Scene profile could not be updated.",
            {"title": "Scenes", "dedupe_key": "scenes-profile-update-failed"},
        )
    ]
    bridge.close()
    controller.close()


def test_bridge_edits_profile_and_scene_transition_policies(tmp_path: Path) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    scene_id = bridge.selectedSceneId

    bridge.setProgramTransition(TransitionKind.DISSOLVE.value, 450)

    assert bridge.programTransitionKind == TransitionKind.DISSOLVE.value
    assert bridge.programTransitionDurationMs == 450
    assert bridge.programTransitionLabel == "Dissolve · 450 ms"

    bridge.setSceneTransitionOverride(
        scene_id,
        TransitionKind.FADE_TO_BLACK.value,
        -1,
    )

    override = controller.documents.effective_transition(scene_id)
    assert override.kind is TransitionKind.FADE_TO_BLACK
    assert override.duration_ms == 450
    assert bridge.selectedSceneTransitionOverrideKind == TransitionKind.FADE_TO_BLACK.value
    assert bridge.selectedSceneTransitionOverrideDurationMs == 450
    row = next(
        bridge.scenesModel.get(index)
        for index in range(bridge.scenesModel.rowCount())
        if bridge.scenesModel.get(index)["id"] == scene_id
    )
    assert row["transition_override"] is True
    assert row["transition_kind"] == TransitionKind.FADE_TO_BLACK.value
    assert row["transition_label"] == "Fade through black · 450 ms"
    assert "Fade through black · 450 ms" in str(row["metadata"])

    bridge.setSceneTransitionOverride(scene_id, TransitionKind.DISSOLVE.value, -1)
    assert controller.documents.effective_transition(scene_id).duration_ms == 450

    bridge.clearSceneTransitionOverride(scene_id)
    assert bridge.selectedSceneTransitionOverrideKind == ""
    assert controller.documents.effective_transition(scene_id).kind is TransitionKind.DISSOLVE
    bridge.close()
    controller.close()


def test_bridge_reports_invalid_transition_without_mutating_policy(
    tmp_path: Path,
) -> None:
    notifications = _Notifications()
    _workspace, controller, bridge, _preview_store = _bridge(
        tmp_path,
        notifications=notifications,
    )
    original = controller.document.transition_policy

    bridge.setProgramTransition(TransitionKind.DISSOLVE.value, 49)

    assert controller.document.transition_policy == original
    assert notifications.errors[-1][1]["dedupe_key"] == "scenes-transition-invalid"
    bridge.close()
    controller.close()


def test_bridge_does_not_duplicate_application_transition_notifications(
    tmp_path: Path,
) -> None:
    notifications = _Notifications()
    _workspace, controller, bridge, _preview_store = _bridge(
        tmp_path,
        notifications=notifications,
    )

    controller.transition_fallback.emit("The transition used a cut instead.")

    assert notifications.warnings == []
    bridge.close()
    controller.close()


@pytest.mark.parametrize("rate", [(30_000, 1_001), (10_000_000, 333_333)])
def test_bridge_uses_exact_local_camera_format(tmp_path: Path, rate: tuple[int, int]) -> None:
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    video_format = LocalVideoFormat(
        media_type=CameraMediaType.JPEG,
        pixel_format="JPEG",
        width=1920,
        height=1080,
        fps_numerator=rate[0],
        fps_denominator=rate[1],
    )
    controller._set_local_cameras(
        LocalCameraDiscovery(
            supported=True,
            ready=True,
            generation=1,
            devices=(
                LocalCameraDevice(
                    device_id="camera://stable-id",
                    display_name="Camera",
                    software_device=False,
                    formats=(video_format,),
                    probe=LocalCameraProbe(
                        status=LocalCameraProbeStatus.READY,
                        backend="media_foundation",
                    ),
                ),
            ),
        )
    )

    assert bridge.saveCamera(
        {
            "kind": "local_camera",
            "name": "Front camera",
            "deviceId": "camera://stable-id",
            "formatId": "format-0",
        },
        False,
    )

    camera = next(camera for camera in workspace.configured_cameras if camera.name == "Front camera")
    configuration = camera.configuration
    assert isinstance(configuration, LocalCameraConfig)
    assert configuration.device_id == "camera://stable-id"
    assert configuration.media_type is CameraMediaType.JPEG
    assert configuration.fps_numerator == rate[0]
    assert configuration.fps_denominator == rate[1]
    assert configuration.keep_active
    bridge.close()
    controller.close()


def test_bridge_exposes_unverified_windows_camera_for_automatic_capture(
    tmp_path: Path,
) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    controller._set_local_cameras(
        LocalCameraDiscovery(
            supported=True,
            ready=True,
            generation=1,
            devices=(
                LocalCameraDevice(
                    device_id="camera://c920",
                    display_name="HD Pro Webcam C920",
                    software_device=False,
                    formats=(),
                    probe=LocalCameraProbe(
                        status=LocalCameraProbeStatus.UNVERIFIED,
                        backend="media_foundation",
                        failure_stage="capture_provider",
                        error_code="capture_provider_not_reported",
                    ),
                ),
            ),
        )
    )

    camera = bridge.localCameraDevices[1]

    assert camera["id"] == "camera://c920"
    assert camera["available"] is True
    assert camera["probeStatus"] == "unverified"
    assert camera["formats"] == [{"id": "", "label": "Automatic"}]
    assert "not verified" in str(camera["name"])
    assert "application log" in bridge.localCameraStatus
    assert bridge.saveCamera(
        {
            "kind": "local_camera",
            "name": "C920",
            "deviceId": "camera://c920",
            "formatId": "",
        },
        False,
    )
    saved_camera = next(
        item for item in controller.workspace.configured_cameras if item.name == "C920"
    )
    configuration = saved_camera.configuration
    assert isinstance(configuration, LocalCameraConfig)
    assert configuration.device_id == "camera://c920"
    assert configuration.media_type is None
    assert configuration.width == 0
    bridge.close()
    controller.close()


def test_camera_list_refreshes_from_pending_to_two_then_three_devices(tmp_path: Path) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    pending = LocalCameraDiscovery(supported=True, ready=False, generation=0, devices=())
    camera = LocalCameraDevice(
        device_id="camera://integrated",
        display_name="Integrated camera",
        software_device=False,
        formats=(LocalVideoFormat(CameraMediaType.RAW, "NV12", 1920, 1080, 30, 1),),
        probe=LocalCameraProbe(LocalCameraProbeStatus.READY, "media_foundation"),
    )
    virtual = replace(camera, device_id="camera://virtual", software_device=True)
    controller._set_local_cameras(pending)
    assert len(bridge.localCameraDevices) == 1  # Automatic is not a discovered device.
    controller._set_local_cameras(replace(pending, ready=True, generation=1, devices=(camera, virtual)))
    assert len(bridge.localCameraDevices) == 3

    usb = replace(
        camera,
        device_id="camera://usb",
        formats=(replace(camera.formats[0], fps_numerator=10_000_000, fps_denominator=333_333),),
    )
    controller._set_local_cameras(
        replace(pending, ready=True, generation=2, devices=(camera, virtual, usb))
    )
    assert [device["id"] for device in bridge.localCameraDevices] == [
        "", camera.device_id, virtual.device_id, usb.device_id,
    ]
    bridge.close()
    controller.close()


def test_bridge_preserves_persisted_format_when_camera_recovers_before_save(
    tmp_path: Path,
) -> None:
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    video_format = LocalVideoFormat(
        media_type=CameraMediaType.JPEG,
        pixel_format="JPEG",
        width=1920,
        height=1080,
        fps_numerator=30,
        fps_denominator=1,
    )
    ready_probe = LocalCameraProbe(
        status=LocalCameraProbeStatus.READY,
        backend="media_foundation",
    )
    controller._set_local_cameras(
        LocalCameraDiscovery(
            supported=True,
            ready=True,
            generation=1,
            devices=(
                LocalCameraDevice(
                    device_id="camera://stable-id",
                    display_name="Camera",
                    software_device=False,
                    formats=(video_format,),
                    probe=ready_probe,
                ),
            ),
        )
    )
    assert bridge.saveCamera(
        {
            "kind": "local_camera",
            "name": "Front camera",
            "deviceId": "camera://stable-id",
            "formatId": "format-0",
        },
        False,
    )
    camera = next(camera for camera in workspace.configured_cameras if camera.name == "Front camera")
    controller._set_local_cameras(
        LocalCameraDiscovery(
            supported=True,
            ready=True,
            generation=2,
            devices=(
                LocalCameraDevice(
                    device_id="camera://stable-id",
                    display_name="Camera",
                    software_device=False,
                    formats=(),
                    probe=LocalCameraProbe(
                        status=LocalCameraProbeStatus.UNVERIFIED,
                        backend="media_foundation",
                        failure_stage="capture_provider",
                        error_code="capture_provider_not_reported",
                    ),
                ),
            ),
        )
    )
    draft = bridge.cameraDraft(camera.id)
    draft["name"] = "Renamed camera"
    draft["formatSelectionChanged"] = False
    controller._set_local_cameras(
        LocalCameraDiscovery(
            supported=True,
            ready=True,
            generation=3,
            devices=(
                LocalCameraDevice(
                    device_id="camera://stable-id",
                    display_name="Camera",
                    software_device=False,
                    formats=(video_format,),
                    probe=ready_probe,
                ),
            ),
        )
    )

    assert bridge.saveCamera(draft, False)

    updated = next(item for item in workspace.configured_cameras if item.id == camera.id)
    configuration = updated.configuration
    assert isinstance(configuration, LocalCameraConfig)
    assert configuration.media_type is CameraMediaType.JPEG
    assert configuration.width == 1920
    assert configuration.height == 1080
    assert configuration.fps_numerator == 30
    bridge.close()
    controller.close()


def test_bridge_protects_onvif_credentials_from_scene_documents(tmp_path: Path) -> None:
    credentials = _Credentials()
    workspace, controller, bridge, _preview_store = _bridge(
        tmp_path,
        credentials=credentials,
    )

    assert bridge.saveCamera(
        {
            "kind": "rtsp_camera",
            "name": "Network camera",
            "uri": "rtsp://camera.local/stream",
            "transport": "tcp",
            "latencyMs": 200,
            "ptzProtocol": PtzProtocol.ONVIF.value,
            "ptzEndpoint": "https://camera.local/onvif/ptz",
            "ptzProfileToken": "profile-1",
            "ptzUsername": "operator",
            "ptzPassword": "secret",
        },
        False,
    )

    camera = next(
        camera for camera in workspace.configured_cameras if camera.name == "Network camera"
    )
    assert isinstance(camera.configuration, RtspCameraConfig)
    binding = camera.configuration.ptz_binding
    assert isinstance(binding, OnvifPtzBinding)
    assert binding.credential_ref == "credential-ref"
    assert credentials.saved[0].username == "operator"
    assert credentials.saved[0].password == "secret"
    assert "secret" not in repr(camera)
    bridge.close()
    controller.close()


def test_bridge_disconnects_runtime_callbacks_before_qml_teardown(tmp_path: Path) -> None:
    _workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    changed = QSignalSpy(bridge.changed)

    bridge.close()
    controller.document_changed.emit(controller.document)
    controller.runtime_changed.emit(controller.runtime.state)

    assert changed.count() == 0
    controller.close()


def test_a_camera_used_by_another_scene_can_still_be_added(tmp_path: Path) -> None:
    """One camera, many scenes: a capture device can only be opened once.

    Hiding cameras that are already on air elsewhere left no way to build a second
    scene around the same camera — creating a duplicate source for the device just
    fails to open it.
    """
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    camera_id = workspace.configured_cameras[0].id

    bridge.createScene("Stage")
    bridge.addConfiguredCamera(camera_id)
    stage_id = bridge.selectedSceneId

    bridge.createScene("Wide")
    offered = {camera["id"] for camera in bridge.configuredCameras}
    assert camera_id in offered, "a camera live in another scene must stay offerable"

    bridge.addConfiguredCamera(camera_id)

    wide = controller.document.scene(bridge.selectedSceneId)
    stage = controller.document.scene(stage_id)
    assert [layer.source_id for layer in wide.layers] == [camera_id]
    # The other scene keeps it too — one source shown by both, not a copy.
    assert [layer.source_id for layer in stage.layers] == [camera_id]
    assert sum(1 for s in controller.document.sources if s.id == camera_id) == 1
    bridge.close()
    controller.close()


def test_a_camera_already_in_this_scene_is_not_offered_again(tmp_path: Path) -> None:
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    camera_id = workspace.configured_cameras[0].id

    bridge.createScene("Stage")
    assert camera_id in {camera["id"] for camera in bridge.configuredCameras}

    bridge.addConfiguredCamera(camera_id)

    assert camera_id not in {camera["id"] for camera in bridge.configuredCameras}
    bridge.close()
    controller.close()
