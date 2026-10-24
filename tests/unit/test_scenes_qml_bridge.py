from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
import time

import pytest

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtTest import QSignalSpy

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.engine import (
    scene_engine_graph_signature,
    LocalCameraDevice,
    LocalCameraDiscovery,
    LocalCameraProbe,
    LocalCameraProbeStatus,
    LocalVideoFormat,
    LocalVideoFrameInterval,
    LocalVideoFrameRateRange,
    LocalVideoResolutionRange,
    SourceHealthEvent,
    SourceHealthStatus,
    SceneSourcePreview,
)
from solin.core.scenes.onvif_discovery import (
    OnvifDeviceDetails,
    OnvifDiscoveredCamera,
    OnvifDiscoveryError,
    OnvifMediaProfile,
)
from solin.core.scenes.model import (
    BusId,
    DEFAULT_CAMERA_SOURCE_ID,
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
    SolinContentConfig,
    SolinContentMode,
    TransitionKind,
    new_identity,
)
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_SCENE_ID,
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
from solin.core.scenes.ptz import PtzControlKind, PtzControlResult, PtzCredentials, PtzRecallStatus
from solin.core.scenes.process_engine import SceneEngineRequestTimeoutError
from solin.core.scenes.output_health import OutputHealth, OutputHealthStatus
from solin.core.scenes.workspace import SceneWorkspaceService
from solin.styles.icons import ICON_FOLDER, ICON_FOLDER_LINK, make_icon
from solin.ui.qml.scenes_bridge import ScenesBridge
from tests.fixtures.editor_source_preview import EditorSourcePreview


class _Projection:
    state = {"type": "idle"}
    idle_media_path = ""

    def set_idle_media_path(self, path):
        self.idle_media_path = path

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


def test_requested_virtual_camera_does_not_claim_ready_before_output_confirmation(
    scene_bridge_factory,
) -> None:
    _, controller, bridge, _ = scene_bridge_factory()
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)

    assert bridge.virtualCameraEnabled
    assert bridge.outputStateLabel != QCoreApplication.translate("SceneOutputStatus", "On")
    assert not bridge.virtualCameraReady


@pytest.mark.parametrize("requested", [False, True])
@pytest.mark.parametrize("status", list(OutputHealthStatus))
def test_output_presentation_separates_requested_activation_and_delivery_health(
    scene_bridge_factory, requested, status,
) -> None:
    _, controller, bridge, _ = scene_bridge_factory()
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, requested)
    state = OutputHealth(
        status, "virtual_camera_unavailable" if status in {
            OutputHealthStatus.RECOVERING, OutputHealthStatus.FAILED,
        } else "", generation=1,
    )
    controller._set_output_health(BusId.VIRTUAL_CAMERA, state)
    # A failure from another operation must not become the output's status.
    controller._set_last_engine_error_code("source_unavailable")

    assert bridge.virtualCameraEnabled is requested
    assert bridge.virtualCameraReady is (requested and status is OutputHealthStatus.READY)
    if not requested:
        assert bridge.outputStateLabel == QCoreApplication.translate("SceneOutputStatus", "Off")
    elif status is OutputHealthStatus.READY:
        assert bridge.outputStateLabel == QCoreApplication.translate("SceneOutputStatus", "On")
    elif status is OutputHealthStatus.FAILED:
        assert bridge.outputStateLabel == QCoreApplication.translate("SceneOutputStatus", "Error")
    else:
        assert bridge.outputStateLabel not in {
            QCoreApplication.translate("SceneOutputStatus", source)
            for source in ("On", "Off", "Error")
        }


def test_output_recovery_updates_presentation_despite_previous_global_output_error(
    scene_bridge_factory,
) -> None:
    _, controller, bridge, _ = scene_bridge_factory()
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    controller._set_last_engine_error_code("virtual_camera_unavailable")
    changes = QSignalSpy(bridge.changed)
    for status in (OutputHealthStatus.RECOVERING, OutputHealthStatus.FAILED, OutputHealthStatus.READY):
        before = changes.count()
        controller._set_output_health(BusId.VIRTUAL_CAMERA, OutputHealth(
            status, "virtual_camera_unavailable" if status is not OutputHealthStatus.READY else "", 1,
        ))
        assert changes.count() > before
        assert bridge.virtualCameraEnabled
        assert bridge.virtualCameraReady is (status is OutputHealthStatus.READY)
    assert bridge.outputStateLabel == QCoreApplication.translate("SceneOutputStatus", "On")


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


@pytest.fixture
def scene_bridge_factory(request, scene_workspace_factory, tmp_path):
    def create(
        *,
        notifications=None,
        credentials=None,
        ptz=None,
        recording=None,
        recording_directory_picker=None,
        recording_directory_opener=None,
        source_preview=None,
        onvif=None,
    ) -> tuple[SceneWorkspaceService, SceneRuntimeController, ScenesBridge, _PreviewStore]:
        cleanup = ExitStack()
        request.addfinalizer(cleanup.close)
        paths = ProfilePaths.from_roots(
            data_dir=tmp_path / "data",
            cache_dir=tmp_path / "cache",
            profile_id="qml-scenes-bridge-test",
        )
        paths.ensure_dirs()
        workspace = scene_workspace_factory(paths, seed_names=_seed_names())
        camera = SourceDefinition(
            id=DEFAULT_CAMERA_SOURCE_ID, kind=SourceKind.LOCAL_CAMERA,
            name="Default camera", configuration=LocalCameraConfig(),
        )
        if not workspace.configured_cameras:
            workspace.upsert_camera(camera, add_to_active_document=True)
            workspace.documents.add_layer(CAMERA_SCENE_ID, SceneLayer(
                id="configured-camera-layer", source_id=camera.id, name="Camera",
            ))
        controller = SceneRuntimeController(workspace, _Projection(), ptz=ptz)
        controller.set_editor_source_preview = source_preview or EditorSourcePreview()
        cleanup.callback(controller.close)
        preview_store = _PreviewStore()
        bridge = ScenesBridge(
            controller,
            preview_store=preview_store,
            recording=recording,
            notifications=notifications,
            credentials=credentials,
            recording_directory_picker=recording_directory_picker,
            recording_directory_opener=recording_directory_opener,
            onvif=onvif,
        )

        def close_bridge() -> None:
            if not bridge._closed:
                bridge.close()

        cleanup.callback(close_bridge)
        return workspace, controller, bridge, preview_store

    return create


@pytest.mark.parametrize("failure", ["setup", "assertion", "bridge_close"])
def test_bridge_factory_closes_dependencies_when_setup_or_teardown_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from tests.conftest import scene_workspace_factory

    workspaces = []
    closed_controllers = []
    original_controller = SceneRuntimeController

    def controller_factory(*args, **kwargs):
        controller = original_controller(*args, **kwargs)
        original_close = controller.close

        def close() -> None:
            closed_controllers.append(controller)
            original_close()

        monkeypatch.setattr(controller, "close", close)
        return controller

    monkeypatch.setattr(__name__ + ".SceneRuntimeController", controller_factory)

    with pytest.raises(RuntimeError, match="simulated bridge failure"):
        with ExitStack() as workspace_finalizers, ExitStack() as finalizers:
            request = SimpleNamespace(addfinalizer=finalizers.callback)
            workspace_request = SimpleNamespace(addfinalizer=workspace_finalizers.callback)
            create_workspace = scene_workspace_factory.__wrapped__(workspace_request, tmp_path)

            def capture_workspace(*args, **kwargs):
                workspace = create_workspace(*args, **kwargs)
                workspaces.append(workspace)
                return workspace

            create_bridge = scene_bridge_factory.__wrapped__(request, capture_workspace, tmp_path)

            def fail(*_args, **_kwargs):
                raise RuntimeError("simulated bridge failure")

            if failure == "setup":
                monkeypatch.setattr(__name__ + ".ScenesBridge", fail)
            _workspace, _controller, bridge, _preview = create_bridge()
            if failure == "assertion":
                raise RuntimeError("simulated bridge failure")
            monkeypatch.setattr(bridge, "close", fail)

    assert len(workspaces) == 1
    assert len(closed_controllers) == 1
    assert workspaces[0]._runtime_persistence.flush(timeout_seconds=0)
    assert not workspaces[0]._runtime_persistence._thread.is_alive()


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
    scene = controller.document.scene(CAMERA_SCENE_ID)
    layer = scene.layers[0]
    return camera.id, layer.id


def test_recording_bridge_projects_one_controller_and_applies_live_settings(
    scene_bridge_factory,
    tmp_path: Path,
) -> None:
    default_directory = tmp_path / "Videos" / "Solin"
    chosen_directory = tmp_path / "Recordings"
    opened: list[str] = []
    recording = _Recording(default_directory)
    workspace, controller, bridge, _preview = scene_bridge_factory(
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

    del workspace


def test_camera_source_health_surfaces_an_actionable_layer_warning(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


@pytest.mark.parametrize("code,expected", [
    ("camera_mode_mismatch", "different resolution than selected"),
    ("camera_no_frames", "No frames received"),
    ("camera_update_failed", "could not apply the selected mode"),
    ("camera_observation_failed", "capture could not be checked"),
    ("camera_create_failed", "Camera unavailable"),
])
def test_camera_capture_errors_have_contextual_status(scene_bridge_factory, code, expected) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
    bridge.selectScene(CAMERA_SCENE_ID)
    camera_id = workspace.configured_cameras[0].id
    controller._consume_engine_event(SourceHealthEvent(
        source_id=camera_id, status=SourceHealthStatus.FAILED,
        error_code=code, message="Untrusted backend detail",
    ))
    assert expected in bridge.cameraCurrentStatus(camera_id)
    record = next(
        bridge.layersModel.get(row) for row in range(bridge.layersModel.rowCount())
        if bridge.layersModel.get(row)["source_id"] == camera_id
    )
    assert record["source_warning_text"] == bridge.cameraCurrentStatus(camera_id)
    assert "Untrusted" not in record["source_warning_text"]


def test_bridge_persists_shared_ptz_presets_and_scene_actions(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
    camera_id, _ = _enable_default_camera_ptz(workspace, controller)
    # The configured camera layer lives in the Camera scene.
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


def test_bridge_rejects_absolute_position_for_non_onvif_camera(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


@pytest.mark.parametrize("bus_id", [BusId.VIRTUAL_CAMERA, BusId.MEDIA_WINDOWS])
def test_bridge_edits_a_scene_and_tracks_desired_separately_from_applied(
    scene_bridge_factory, bus_id,
) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()

    bridge.createScene("Custom")
    camera_id = workspace.configured_cameras[0].id
    bridge.addConfiguredCamera(camera_id, "Stage camera", bridge.sourceEditContext())
    other_bus = BusId.MEDIA_WINDOWS if bus_id is BusId.VIRTUAL_CAMERA else BusId.VIRTUAL_CAMERA
    other_before = controller.runtime.state.output(other_bus)
    bridge.takeScene(bus_id.value)

    scene = controller.document.scene(bridge.selectedSceneId)
    output = controller.runtime.state.output(bus_id)
    # First-run ships Content + Camera; the new Custom scene makes three.
    assert bridge.scenesModel.rowCount() == 3
    assert len(scene.layers) == 1
    assert output.mode is OutputMode.AUTO
    assert output.base_scene_id == scene.id
    assert controller.runtime.state.output(other_bus) == other_before
    outputs = {item["busId"]: item for item in bridge.selectedSceneOutputs}
    assert not outputs[bus_id.value]["canTake"]
    assert outputs[other_bus.value]["canTake"]
    assert not bridge.selectedSceneLive
    assert bridge.engineStatus == "Scene engine unavailable"


def test_bridge_add_content_adds_full_size_layers_and_reuses_presentation_source(
    scene_bridge_factory,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    bridge.createScene("Talk")
    bridge.addContentSource()
    first = controller.document.scene(bridge.selectedSceneId).layers[0]
    source = controller.document.source(first.source_id)
    assert source.kind is SourceKind.SOLIN_CONTENT
    assert source.configuration.mode is SolinContentMode.PRESENTATION
    assert first.rect == NormalizedRect()
    assert first.fit_mode is FitMode.CONTAIN
    bridge.createScene("Consideration")
    bridge.addContentSource()
    second = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert second.source_id == source.id
    assert second.fit_mode is FitMode.CONTAIN
    assert not hasattr(bridge, "addIdleScreen")
    assert not hasattr(bridge, "contentIdle")


def test_bridge_add_content_creates_presentation_when_only_media_exists(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    for source in controller.document.sources:
        if source.kind is SourceKind.SOLIN_CONTENT:
            controller.documents.delete_source(source.id, cascade=True)
    only = SourceDefinition(
        id="foreground-only", kind=SourceKind.SOLIN_CONTENT, name="Only Media",
        configuration=SolinContentConfig(mode=SolinContentMode.MEDIA_ONLY),
    )
    controller.documents.create_source(only)
    bridge.createScene("Talk")
    bridge.addContentSource()
    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    source = controller.document.source(layer.source_id)
    assert source.id != only.id
    assert source.name == "Solin Content"
    assert source.configuration.mode is SolinContentMode.PRESENTATION
    assert bridge.layersModel.get(0)["kind"] == "solin_content"
    assert layer.fit_mode is FitMode.CONTAIN


def test_bridge_add_content_reuses_arbitrary_presentation_identity(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    for source in controller.document.sources:
        if source.kind is SourceKind.SOLIN_CONTENT:
            controller.documents.delete_source(source.id, cascade=True)
    source = SourceDefinition(
        id="migrated-idle", kind=SourceKind.SOLIN_CONTENT,
        name="Custom presentation", configuration=SolinContentConfig(),
    )
    controller.documents.create_source(source)
    bridge.createScene("Talk")
    bridge.addContentSource()
    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert layer.source_id == source.id
    assert layer.fit_mode is FitMode.CONTAIN


def test_bridge_new_content_layer_preserves_existing_authored_transform_and_undo(
    scene_bridge_factory,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    original = controller.document.scene(CONTENT_SCENE_ID).layers[0]
    authored = replace(
        original,
        rect=NormalizedRect(x=0.1, y=0.2, width=0.6, height=0.5),
        crop=Crop(left=0.1, top=0.2),
        fit_mode=FitMode.COVER,
    )
    controller.documents.update_layer(CONTENT_SCENE_ID, original.id, authored)
    bridge.createScene("Talk")
    scene_id = bridge.selectedSceneId

    bridge.addContentSource()

    added = controller.document.scene(scene_id).layers[0]
    assert added.source_id == authored.source_id
    assert added.rect == NormalizedRect()
    assert added.crop == Crop()
    assert added.fit_mode is FitMode.CONTAIN
    assert controller.document.scene(CONTENT_SCENE_ID).layers[0] == authored
    controller.documents.undo()
    assert not controller.document.scene(scene_id).layers
    controller.documents.redo()
    assert controller.document.scene(scene_id).layers == (added,)
    assert controller.document.scene(CONTENT_SCENE_ID).layers[0] == authored


@pytest.mark.parametrize("kind", [SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA])
@pytest.mark.parametrize("over_content", [False, True])
def test_bridge_new_camera_layers_contain_the_complete_frame(
    scene_bridge_factory, kind: SourceKind, over_content: bool,
) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
    camera = SourceDefinition(
        id=new_identity(), kind=kind, name="Stage camera",
        configuration=(
            LocalCameraConfig(device_id="stage-camera")
            if kind is SourceKind.LOCAL_CAMERA
            else RtspCameraConfig(uri="rtsp://camera.example/live")
        ),
    )
    bridge.createScene("Stage")
    if over_content:
        bridge.addContentSource()
    workspace.upsert_camera(camera, add_to_active_document=False)

    bridge.addConfiguredCamera(camera.id, "Stage camera", bridge.sourceEditContext())

    layer = controller.document.scene(bridge.selectedSceneId).layers[-1]
    assert layer.source_id == camera.id
    assert layer.fit_mode is FitMode.CONTAIN
    assert layer.crop == Crop()
    assert layer.rect == (
        NormalizedRect(x=0.72, y=0.68, width=0.25, height=0.25)
        if over_content else NormalizedRect()
    )


def test_bridge_new_scene_reference_uses_generic_fit_and_allows_explicit_cover(
    scene_bridge_factory,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    bridge.createScene("Nested camera")
    bridge.addSceneReference(CAMERA_SCENE_ID, "Camera", bridge.sourceEditContext())

    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert layer.fit_mode is FitMode.CONTAIN
    bridge.setLayerFit(layer.id, "cover")
    changed = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert changed.fit_mode is FitMode.COVER
    assert changed.to_record()["fit_mode"] == "cover"
    controller.documents.undo()
    assert controller.document.scene(bridge.selectedSceneId).layers[0] == layer


def test_bridge_adds_remote_participants_sharing_one_source_per_slot(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    bridge.createScene("Remote part")
    bridge.addRemoteParticipantPair()

    layers = controller.document.scene(bridge.selectedSceneId).layers
    sources = [controller.document.source(layer.source_id) for layer in layers]
    assert [source.kind for source in sources] == [SourceKind.REMOTE_PARTICIPANT] * 2
    assert [source.configuration.slot for source in sources] == [1, 2]
    assert [layer.name for layer in layers] == ["Remote participant 1", "Remote participant 2"]
    # Side by side, each half keeping the canvas shape, with a gutter between them.
    left, right = (layer.rect for layer in layers)
    assert left.width == left.height == right.width == right.height
    assert left.x + left.width < right.x and left.y == right.y
    assert bridge.selectedLayerId == layers[1].id

    bridge.createScene("Remote part, solo")
    bridge.addRemoteParticipant(1)
    solo = controller.document.scene(bridge.selectedSceneId).layers[0]
    assert solo.source_id == layers[0].source_id  # the slot's source is reused
    assert solo.rect == NormalizedRect()
    assert [s.id for s in controller.document.sources].count(solo.source_id) == 1


def test_bridge_preserves_enums_and_transform_contracts(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_toggles_horizontal_layer_mirroring_with_undo(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_framing_session_commits_one_revision_and_undoes_cleanly(
    scene_bridge_factory,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_cancels_and_invalidates_framing_without_document_mutation(
    scene_bridge_factory,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    bridge.selectScene(CAMERA_SCENE_ID)  # a configured, framable camera layer
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


def test_reopening_framing_restores_full_source_and_can_remove_previous_zoom(
    scene_bridge_factory,
) -> None:
    preview = EditorSourcePreview()
    _, controller, bridge, _ = scene_bridge_factory(source_preview=preview)
    layer_id = controller.document.scene(bridge.selectedSceneId).layers[0].id
    bridge.selectLayer(layer_id)
    bridge.beginLayerFraming(layer_id)
    bridge.updateLayerFraming({"operation": "scale", "scale": 0.5})
    assert bridge.commitLayerFraming()
    revision = controller.document.revision

    reopened = bridge.beginLayerFraming(layer_id)
    assert reopened["x"] == pytest.approx(0.25)
    assert reopened["y"] == pytest.approx(0.25)
    assert reopened["width"] == pytest.approx(0.5)
    assert controller.document.revision == revision
    assert preview.requests[-1] == (bridge.selectedSceneId, layer_id)
    expanded = bridge.updateLayerFraming({"operation": "scale", "scale": 2.0})
    assert expanded["width"] == pytest.approx(1.0)
    assert bridge.commitLayerFraming()
    assert controller.document.scene(bridge.selectedSceneId).layers[0].crop == Crop()
    assert controller.document.revision == revision + 1
    bridge.undo()
    assert controller.document.scene(bridge.selectedSceneId).layers[0].crop == Crop(
        left=0.25, top=0.25, right=0.25, bottom=0.25,
    )


@pytest.mark.parametrize("mirror_x,mirror_y", [(False, False), (True, False), (False, True), (True, True)])
def test_reopening_asymmetric_framing_preserves_focus_and_does_not_create_history(
    scene_bridge_factory, mirror_x: bool, mirror_y: bool,
) -> None:
    _, controller, bridge, _ = scene_bridge_factory()
    scene_id = bridge.selectedSceneId
    layer = controller.document.scene(scene_id).layers[0]
    original = replace(layer, crop=Crop(left=0.1, top=0.2, right=0.5, bottom=0.4),
                       mirror_x=mirror_x, mirror_y=mirror_y, fit_mode=FitMode.COVER)
    controller.documents.update_layer(scene_id, layer.id, original)
    bridge.selectLayer(layer.id)
    revision = controller.document.revision

    for _ in range(3):
        draft = bridge.beginLayerFraming(layer.id)
        assert draft["x"] == pytest.approx(0.5 if mirror_x else 0.1)
        assert draft["y"] == pytest.approx(0.4 if mirror_y else 0.2)
        assert draft["width"] == pytest.approx(0.4)
        assert not bridge.commitLayerFraming()
        assert not bridge.framingActive
    assert controller.document.revision == revision
    assert controller.document.scene(scene_id).layers[0] == original


@pytest.mark.parametrize("source_size", [(1080, 1920), (1440, 1080), (3840, 1080)])
def test_unchanged_implicit_cover_framing_does_not_create_history(
    scene_bridge_factory, source_size: tuple[int, int],
) -> None:
    preview = EditorSourcePreview(*source_size)
    _, controller, bridge, _ = scene_bridge_factory(source_preview=preview)
    scene_id = bridge.selectedSceneId
    layer = controller.document.scene(scene_id).layers[0]
    layer = replace(layer, fit_mode=FitMode.COVER)
    controller.documents.update_layer(scene_id, layer.id, layer)
    bridge.selectLayer(layer.id)
    revision = controller.document.revision
    for _ in range(3):
        assert bridge.beginLayerFraming(layer.id)
        assert not bridge.commitLayerFraming()
        assert not bridge.framingActive
    assert controller.document.scene(scene_id).layers[0] == layer
    assert controller.document.revision == revision


def test_manual_crop_is_proposed_at_output_aspect_and_cancel_preserves_it(scene_bridge_factory) -> None:
    preview = EditorSourcePreview()
    _, controller, bridge, _ = scene_bridge_factory(source_preview=preview)
    scene_id = bridge.selectedSceneId
    layer = controller.document.scene(scene_id).layers[0]
    original = replace(layer, crop=Crop(left=0.2, top=0.1, right=0.4, bottom=0.1),
                       rect=NormalizedRect(x=0.2, y=0.1, width=0.4, height=0.8),
                       fit_mode=FitMode.CONTAIN)
    controller.documents.update_layer(scene_id, layer.id, original)
    bridge.selectLayer(layer.id)
    revision = controller.document.revision
    draft = bridge.beginLayerFraming(layer.id)
    assert draft["x"] == pytest.approx(0.2)
    assert draft["y"] == pytest.approx(0.3)
    assert draft["width"] == pytest.approx(0.4)
    assert draft["height"] == pytest.approx(0.4)
    bridge.cancelLayerFraming()
    assert preview.requests[-1] == (scene_id, None)
    assert controller.document.revision == revision
    assert controller.document.scene(scene_id).layers[0] == original


@pytest.mark.parametrize("cancel", ["cancel", "document", "engine", "content", "close"])
def test_pending_source_preview_cannot_restore_a_cancelled_session(
    scene_bridge_factory, cancel: str,
) -> None:
    preview = EditorSourcePreview()
    pending: Future[SceneSourcePreview] = Future()
    preview.responses.append(pending)
    _, controller, bridge, _ = scene_bridge_factory(source_preview=preview)
    scene_id = bridge.selectedSceneId
    layer = controller.document.scene(scene_id).layers[0]
    bridge.selectLayer(layer.id)
    assert bridge.beginLayerFraming(layer.id) == {}
    assert bridge.framingActive and not bridge.framingReady
    assert not bridge.commitLayerFraming()
    if cancel == "document":
        controller.documents.update_layer(scene_id, layer.id, replace(layer, opacity=0.8))
    elif cancel in {"engine", "content"}:
        controller.editor_source_preview_invalidated.emit()
    elif cancel == "close":
        bridge.close()
    else:
        bridge.cancelLayerFraming()
    revision = controller.document.revision
    pending.set_result(SceneSourcePreview(1920, 1080))
    QCoreApplication.processEvents()
    assert not bridge.framingActive
    assert bridge.framingDraft == {}
    assert controller.document.revision == revision
    assert preview.requests[-1] == (scene_id, None)


@pytest.mark.parametrize("failure", ["exception", "unavailable", "dimensions_changed"])
def test_framing_apply_validates_source_before_saving(scene_bridge_factory, failure: str) -> None:
    preview = EditorSourcePreview()
    notifications = _Notifications()
    _, controller, bridge, _ = scene_bridge_factory(source_preview=preview, notifications=notifications)
    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    bridge.selectLayer(layer.id)
    bridge.beginLayerFraming(layer.id)
    bridge.updateLayerFraming({"operation": "scale", "scale": 0.5})
    pending: Future[SceneSourcePreview] = Future()
    preview.responses.append(pending)
    revision = controller.document.revision
    assert not bridge.commitLayerFraming()
    assert bridge.framingActive and not bridge.framingReady
    if failure == "exception":
        pending.set_exception(RuntimeError("source disconnected"))
    else:
        pending.set_result(
            SceneSourcePreview(error_code="source_unavailable") if failure == "unavailable"
            else SceneSourcePreview(1080, 1920)
        )
    QCoreApplication.processEvents()
    assert not bridge.framingActive
    assert controller.document.revision == revision
    assert controller.document.scene(bridge.selectedSceneId).layers[0] == layer
    assert len(notifications.errors) == 1


@pytest.mark.parametrize("crop", [Crop(left=0.495, top=0.495, right=0.495, bottom=0.495),
                                   Crop(left=0.99, top=0.99, right=0.0095, bottom=0.0095)])
def test_framing_preserves_a_tiny_authored_crop_until_user_expands_it(scene_bridge_factory, crop: Crop) -> None:
    _, controller, bridge, _ = scene_bridge_factory()
    scene_id = bridge.selectedSceneId
    layer = controller.document.scene(scene_id).layers[0]
    original = replace(layer, crop=crop, fit_mode=FitMode.COVER)
    controller.documents.update_layer(scene_id, layer.id, original)
    bridge.selectLayer(layer.id)
    revision = controller.document.revision
    draft = bridge.beginLayerFraming(layer.id)
    assert draft
    assert draft["width"] == pytest.approx(1 - crop.left - crop.right)
    assert not bridge.commitLayerFraming()
    assert controller.document.revision == revision
    assert controller.document.scene(scene_id).layers[0] == original
    assert bridge.beginLayerFraming(layer.id)
    bridge.updateLayerFraming({"operation": "scale", "scale": 10000.0})
    assert bridge.commitLayerFraming()
    assert controller.document.scene(scene_id).layers[0].crop == Crop()


def test_applying_a_shifted_crop_proposal_updates_the_authored_focus(scene_bridge_factory) -> None:
    _, controller, bridge, _ = scene_bridge_factory()
    scene_id = bridge.selectedSceneId
    layer = controller.document.scene(scene_id).layers[0]
    original = replace(layer, crop=Crop(left=0.99, top=0.4998, bottom=0.4998),
                       fit_mode=FitMode.COVER)
    controller.documents.update_layer(scene_id, layer.id, original)
    bridge.selectLayer(layer.id)
    revision = controller.document.revision
    draft = bridge.beginLayerFraming(layer.id)
    assert draft["x"] == pytest.approx(0.99)
    assert bridge.commitLayerFraming()
    assert controller.document.revision == revision + 1
    assert controller.document.scene(scene_id).layers[0].crop.right == pytest.approx(0.0096)


def test_framing_can_recover_an_off_canvas_or_tiny_layer(scene_bridge_factory) -> None:
    _, controller, bridge, _ = scene_bridge_factory()
    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    controller.documents.update_layer(bridge.selectedSceneId, layer.id, replace(
        layer, rect=NormalizedRect(x=-2.0, y=-2.0, width=0.01, height=0.01),
        crop=Crop(left=0.99),
    ))
    bridge.selectLayer(layer.id)
    assert bridge.selectedLayer["framing_available"]
    assert bridge.beginLayerFraming(layer.id)
    assert bridge.updateLayerFraming({"operation": "scale", "scale": 100.0})["width"] == 1.0
    assert bridge.commitLayerFraming()
    assert controller.document.scene(bridge.selectedSceneId).layers[0].crop == Crop()


def test_bridge_fill_from_crop_expands_then_fills_without_source_kind_coupling(
    scene_bridge_factory,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_explains_why_rotated_layers_cannot_be_framed(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_explains_why_locked_layers_cannot_be_framed(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_geometry_preview_commits_exactly_one_revision(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_only_media_scene_role_is_removable(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    records = [bridge.scenesModel.get(row) for row in range(bridge.scenesModel.rowCount())]
    media = next(record for record in records if record["media"])
    assert "Media" in media["metadata"]
    assert all("default" not in record for record in records)
    assert not hasattr(bridge, "setDefaultScene")
    bridge.setMediaScene(media["id"], False)
    assert controller.documents.program_media_scene_id is None
    assert not controller.documents.media_automation_configured


def test_bridge_delete_live_camera_preserves_projection_selection(scene_bridge_factory) -> None:
    notifications = _Notifications()
    _workspace, controller, bridge, _preview_store = scene_bridge_factory(notifications=notifications)
    bridge.setVirtualCameraEnabled(True)
    bridge.selectScene(CAMERA_SCENE_ID)
    assert bridge.selectedSceneRequiresReplacement
    assert not bridge.selectedSceneLive
    bridge.deleteScene(CAMERA_SCENE_ID, "")
    assert controller.document.scene(CAMERA_SCENE_ID).id == CAMERA_SCENE_ID
    assert notifications.errors[-1][1]["dedupe_key"] == "scenes-live-delete-replacement-required"
    projection_before = controller.runtime.state.output(BusId.MEDIA_WINDOWS)
    bridge.deleteScene(CAMERA_SCENE_ID, CONTENT_SCENE_ID)
    assert all(scene.id != CAMERA_SCENE_ID for scene in controller.document.scenes)
    assert controller.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == CONTENT_SCENE_ID
    assert controller.runtime.state.output(BusId.MEDIA_WINDOWS) == projection_before


def test_bridge_deletes_offline_scene_without_confusing_editor_preview(
    scene_bridge_factory,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    bridge.selectScene(CONTENT_SCENE_ID)
    bridge.setActive(True)
    assert not bridge.selectedSceneRequiresReplacement

    bridge.deleteScene(CONTENT_SCENE_ID, "")

    assert all(scene.id != CONTENT_SCENE_ID for scene in controller.document.scenes)
    assert all(not mapping.assignments for mapping in controller.document.automation)


def test_bridge_keeps_preview_across_scene_switch_but_invalidates_on_profile_change(
    scene_bridge_factory,
) -> None:
    workspace, controller, bridge, preview_store = scene_bridge_factory()
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


def test_bridge_source_rows_reorder_front_to_back_and_unlock_cleanly(
    scene_bridge_factory,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_resolves_transitive_ptz_camera_once(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_stops_ptz_motion_on_deactivation(scene_bridge_factory) -> None:
    ptz = _ManualPtz()
    workspace, controller, bridge, _preview_store = scene_bridge_factory(ptz=ptz)
    camera_id, layer_id = _enable_default_camera_ptz(workspace, controller)
    bridge.selectLayer(layer_id)
    bridge.setActive(True)
    bridge.movePtz(camera_id, 1.0, 0.0, 0.0, 0.5)

    bridge.setActive(False)

    assert ptz.moves == [camera_id]
    assert ptz.stops == [camera_id]


def test_bridge_reports_profile_failures_through_central_notifications(
    scene_bridge_factory,
) -> None:
    notifications = _Notifications()
    _workspace, controller, bridge, _preview_store = scene_bridge_factory(
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


def test_bridge_edits_profile_and_scene_transition_policies(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_reports_invalid_transition_without_mutating_policy(
    scene_bridge_factory,
) -> None:
    notifications = _Notifications()
    _workspace, controller, bridge, _preview_store = scene_bridge_factory(
        notifications=notifications,
    )
    original = controller.document.transition_policy

    bridge.setProgramTransition(TransitionKind.DISSOLVE.value, 49)

    assert controller.document.transition_policy == original
    assert notifications.errors[-1][1]["dedupe_key"] == "scenes-transition-invalid"


def test_bridge_does_not_duplicate_application_transition_notifications(
    scene_bridge_factory,
) -> None:
    notifications = _Notifications()
    _workspace, controller, bridge, _preview_store = scene_bridge_factory(
        notifications=notifications,
    )

    controller.transition_fallback.emit("The transition used a cut instead.")

    assert notifications.warnings == []


@pytest.mark.parametrize("rate", [(30_000, 1_001), (10_000_000, 333_333)])
def test_bridge_uses_exact_local_camera_format(scene_bridge_factory, rate: tuple[int, int]) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
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

    assert bridge.addCamera(
        {
            "kind": "local_camera",
            "name": "Front camera",
            "deviceId": "camera://stable-id",
            "formatId": bridge.localCameraDevices[1]["formats"][1]["id"],
        },
        bridge.sourceEditContext(),
    )

    camera = next(camera for camera in workspace.configured_cameras if camera.name == "Front camera")
    configuration = camera.configuration
    assert isinstance(configuration, LocalCameraConfig)
    assert configuration.device_id == "camera://stable-id"
    assert configuration.media_type is CameraMediaType.JPEG
    assert configuration.fps_numerator == rate[0]
    assert configuration.fps_denominator == rate[1]


def test_bridge_exposes_camera_with_unavailable_capabilities_for_automatic_capture(
    scene_bridge_factory,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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
    assert camera["name"] == "HD Pro Webcam C920"
    assert camera["capabilitiesStatus"] == (
        "Camera capabilities are unavailable. You can try automatic mode."
    )
    assert bridge.localCameraStatus == ""
    assert bridge.addCamera(
        {
            "kind": "local_camera",
            "name": "C920",
            "deviceId": "camera://c920",
            "formatId": "",
        },
        bridge.sourceEditContext(),
    )
    saved_camera = next(
        item for item in controller.workspace.configured_cameras if item.name == "C920"
    )
    configuration = saved_camera.configuration
    assert isinstance(configuration, LocalCameraConfig)
    assert configuration.device_id == "camera://c920"
    assert configuration.media_type is None
    assert configuration.width == 0


def test_camera_discovery_without_ready_engine_keeps_readiness_guidance(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview = scene_bridge_factory()

    assert not controller.engine_ready
    assert bridge.localCameraStatus == "Camera discovery is available when the scene engine is ready."


@pytest.mark.parametrize("failure", ["request-timeout", "helper-failure"])
@pytest.mark.parametrize("manual_mode", [False, True])
@pytest.mark.parametrize("previous_failure", [False, True])
def test_camera_discovery_failure_is_contextual_and_preserves_configured_device(
    scene_bridge_factory, monkeypatch, failure, manual_mode, previous_failure,
) -> None:
    workspace, controller, bridge, _preview = scene_bridge_factory()
    configuration = LocalCameraConfig(device_id="camera://configured-but-unlisted")
    if manual_mode:
        configuration = replace(
            configuration, width=1280, height=720, fps_numerator=30,
            media_type=CameraMediaType.JPEG, pixel_format="MJPG",
        )
    camera = replace(workspace.configured_cameras[0], configuration=configuration)
    workspace.upsert_camera(camera)
    bridge.selectScene(CAMERA_SCENE_ID)
    bridge.selectLayer(controller.document.scene(CAMERA_SCENE_ID).layers[0].id)
    before_document, before_resources = controller.document, workspace.resources
    before_draft = bridge.selectedCameraDraft()
    pending = Future()
    monkeypatch.setattr(controller, "_engine", SimpleNamespace(
        list_local_cameras=lambda **_kwargs: pending,
    ))
    controller._set_engine_ready(True)
    if previous_failure:
        controller._set_local_cameras(LocalCameraDiscovery(
            supported=failure == "request-timeout", ready=True, generation=0, devices=(),
            error_code="discovery_timed_out" if failure == "request-timeout" else "discovery_request_failed",
        ))
    last_completed = controller.local_cameras
    statuses = []
    bridge.changed.connect(lambda: statuses.append(bridge.localCameraStatus))
    assert controller.refresh_local_cameras() is pending
    assert controller.local_cameras_pending
    assert controller.local_cameras == last_completed
    assert bridge.localCameraStatus == "Looking for cameras…"
    assert bridge.engineStatus == ""

    if failure == "request-timeout":
        pending.set_exception(SceneEngineRequestTimeoutError("metadata deadline exceeded"))
    else:
        pending.set_result(LocalCameraDiscovery(
            supported=False, ready=True, generation=0, devices=(),
            error_code="discovery_request_failed",
        ))

    assert controller.engine_ready
    assert not controller.local_cameras_pending
    assert controller.local_cameras.ready
    assert controller.local_cameras.error_code
    assert bridge.engineStatus == ""
    assert bridge.localCameraStatus == "Camera discovery could not be completed."
    assert statuses[-1] == bridge.localCameraStatus
    missing = next(item for item in bridge.localCameraDevices if item["id"] == configuration.device_id)
    assert missing["available"] is False
    assert bridge.selectedCameraDraft()["deviceId"] == before_draft["deviceId"]
    assert bridge.selectedCameraDraft()["formatId"] == before_draft["formatId"]

    _publish_camera_modes(controller, (LocalVideoFormat(
        CameraMediaType.RAW, "NV12", 640, 480, 30, 1,
    ),), generation=2)

    assert bridge.selectedCameraDraft()["deviceId"] == configuration.device_id
    assert bridge.selectedCameraDraft()["formatId"] == before_draft["formatId"]
    assert controller.document == before_document
    assert workspace.resources == before_resources


def test_camera_refresh_shows_loading_with_cached_devices_then_ready_cache(
    scene_bridge_factory, monkeypatch,
) -> None:
    _workspace, controller, bridge, _preview = scene_bridge_factory()
    _publish_camera_modes(controller, (LocalVideoFormat(
        CameraMediaType.RAW, "NV12", 640, 480, 30, 1,
    ),))
    before = bridge.localCameraDevices
    result = controller.local_cameras
    pending, calls = Future(), []

    def discover(**kwargs):
        calls.append(kwargs["request_id"])
        return pending

    monkeypatch.setattr(controller, "_engine", SimpleNamespace(list_local_cameras=discover))
    controller._set_engine_ready(True)
    statuses = []
    bridge.changed.connect(lambda: statuses.append(bridge.localCameraStatus))

    assert controller.refresh_local_cameras() is pending
    assert bridge.localCameraStatus == "Looking for cameras…"
    assert bridge.localCameraDevices == before
    bridge.ensureLocalCameras()
    assert len(calls) == 1

    pending.set_result(result)

    assert controller.local_cameras.ready
    assert not controller.local_cameras_pending
    assert bridge.localCameraStatus == ""
    assert statuses[-1] == ""
    assert bridge.localCameraDevices == before
    bridge.ensureLocalCameras()
    assert len(calls) == 1


def test_camera_list_refreshes_from_pending_to_two_then_three_devices(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
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


def test_bridge_preserves_persisted_format_when_camera_recovers_before_save(
    scene_bridge_factory,
) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
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
    assert bridge.addCamera(
        {
            "kind": "local_camera",
            "name": "Front camera",
            "deviceId": "camera://stable-id",
            "formatId": bridge.localCameraDevices[1]["formats"][1]["id"],
        },
        bridge.sourceEditContext(),
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

    assert bridge.updateCamera(draft, bridge.sourceEditContext())

    updated = next(item for item in workspace.configured_cameras if item.id == camera.id)
    configuration = updated.configuration
    assert isinstance(configuration, LocalCameraConfig)
    assert configuration.media_type is CameraMediaType.JPEG
    assert configuration.width == 1920
    assert configuration.height == 1080
    assert configuration.fps_numerator == 30


def _publish_camera_modes(controller, formats, *, generation=1):
    controller._set_local_cameras(LocalCameraDiscovery(
        supported=True, ready=True, generation=generation,
        devices=(LocalCameraDevice(
            device_id="camera://stable-id", display_name="Camera", software_device=False,
            formats=tuple(formats),
            probe=LocalCameraProbe(LocalCameraProbeStatus.READY, "libobs"),
        ),),
    ))


def test_camera_mode_identity_survives_reordered_discovery(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
    hd = LocalVideoFormat(CameraMediaType.JPEG, "MJPG", 1920, 1080, 30_000, 1_001)
    sd = LocalVideoFormat(CameraMediaType.RAW, "YUY2", 640, 480, 30, 1)
    _publish_camera_modes(controller, (hd, sd))
    mode_id = bridge.localCameraDevices[1]["formats"][1]["id"]
    _publish_camera_modes(controller, (sd, hd), generation=2)

    assert bridge.localCameraDevices[1]["formats"][2]["id"] == mode_id
    assert bridge.addCamera({
        "kind": "local_camera", "name": "Front camera",
        "deviceId": "camera://stable-id", "formatId": mode_id,
    }, bridge.sourceEditContext())
    config = next(c.configuration for c in workspace.configured_cameras if c.name == "Front camera")
    assert (config.width, config.height, config.pixel_format) == (1920, 1080, "MJPG")
    assert (config.fps_numerator, config.fps_denominator) == (30_000, 1_001)


@pytest.mark.parametrize("missing_device", [False, True])
def test_camera_draft_retains_unavailable_mode_and_device(scene_bridge_factory, missing_device) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
    hd = LocalVideoFormat(CameraMediaType.JPEG, "MJPG", 1920, 1080, 30, 1)
    sd = LocalVideoFormat(CameraMediaType.RAW, "YUY2", 640, 480, 30, 1)
    _publish_camera_modes(controller, (hd,))
    original_id = bridge.localCameraDevices[1]["formats"][1]["id"]
    assert bridge.addCamera({
        "kind": "local_camera", "name": "Front camera",
        "deviceId": "camera://stable-id", "formatId": original_id,
    }, bridge.sourceEditContext())
    camera = next(c for c in workspace.configured_cameras if c.name == "Front camera")
    if missing_device:
        controller._set_local_cameras(LocalCameraDiscovery(
            supported=True, ready=True, generation=2, devices=(),
        ))
    else:
        _publish_camera_modes(controller, (sd,), generation=2)

    draft = bridge.cameraDraft(camera.id)
    assert draft["deviceId"] == "camera://stable-id"
    assert draft["formatId"] == original_id
    assert draft["format"]["available"] is False
    assert draft["format"]["width"] == 1920
    draft.update(name="Renamed camera", formatSelectionChanged=False)
    assert bridge.updateCamera(draft, bridge.sourceEditContext())
    updated = next(c for c in workspace.configured_cameras if c.id == camera.id)
    assert updated.configuration == camera.configuration


@pytest.mark.parametrize("invalid_id", ["format--1", "format-0", "invented-mode"])
def test_camera_rejects_non_tuple_format_identifiers(scene_bridge_factory, invalid_id) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
    _publish_camera_modes(controller, (LocalVideoFormat(CameraMediaType.RAW, "NV12", 640, 480, 30, 1),))
    original = workspace.configured_cameras
    assert not bridge.addCamera({
        "kind": "local_camera", "name": "Invalid camera",
        "deviceId": "camera://stable-id", "formatId": invalid_id,
    }, bridge.sourceEditContext())
    assert workspace.configured_cameras == original


def test_camera_revalidates_mode_removed_after_selection(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
    hd = LocalVideoFormat(CameraMediaType.JPEG, "MJPG", 1920, 1080, 30, 1)
    sd = LocalVideoFormat(CameraMediaType.RAW, "YUY2", 640, 480, 30, 1)
    _publish_camera_modes(controller, (hd,))
    mode_id = bridge.localCameraDevices[1]["formats"][1]["id"]
    _publish_camera_modes(controller, (sd,), generation=2)
    original = workspace.configured_cameras
    assert not bridge.addCamera({
        "kind": "local_camera", "name": "Front camera",
        "deviceId": "camera://stable-id", "formatId": mode_id, "formatSelectionChanged": True,
    }, bridge.sourceEditContext())
    assert workspace.configured_cameras == original


@pytest.mark.parametrize("step,rate,accepted", [
    (None, "29.97", True),
    (None, "30000/1001", True),
    (None, "60", False),
    (LocalVideoFrameInterval(1, 30), "20", False),
    (LocalVideoFrameInterval(1, 30), "15", True),
])
def test_camera_validates_custom_frame_rate_against_exact_size_and_format(
    scene_bridge_factory, step, rate, accepted,
) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
    mode = LocalVideoFormat(CameraMediaType.RAW, "NV12", 1280, 720, 30, 1)
    _publish_camera_modes(controller, (mode,))
    discovery = controller.local_cameras
    camera = discovery.devices[0]
    interval_range = LocalVideoFrameRateRange(
        mode.media_type, mode.pixel_format, mode.width, mode.height,
        LocalVideoFrameInterval(1, 30), LocalVideoFrameInterval(1, 10), step,
    )
    controller._set_local_cameras(replace(discovery, devices=(replace(
        camera, frame_rate_ranges=(interval_range,),
    ),)))
    mode_id = bridge.localCameraDevices[1]["formats"][1]["id"]
    custom = bridge.localCameraCustomFrameRate(camera.device_id, mode_id, rate)
    assert bool(custom) is accepted
    if accepted:
        assert bridge.addCamera({
            "kind": "local_camera", "name": "Custom rate", "deviceId": camera.device_id,
            "formatId": custom["id"], "formatSelectionChanged": True,
        }, bridge.sourceEditContext())
        config = next(c.configuration for c in workspace.configured_cameras if c.name == "Custom rate")
        assert f"{config.fps_numerator}/{config.fps_denominator}" == custom["fpsId"]


def test_camera_custom_rate_does_not_cross_format_or_resolution(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    mode = LocalVideoFormat(CameraMediaType.RAW, "NV12", 1280, 720, 30, 1)
    other = replace(mode, pixel_format="YUY2", width=640, height=480)
    _publish_camera_modes(controller, (mode, other))
    discovery = controller.local_cameras
    controller._set_local_cameras(replace(discovery, devices=(replace(
        discovery.devices[0], frame_rate_ranges=(LocalVideoFrameRateRange(
            mode.media_type, mode.pixel_format, mode.width, mode.height,
            LocalVideoFrameInterval(1, 30), LocalVideoFrameInterval(1, 10),
        ),),
    ),)))
    wrong_mode = next(m for m in bridge.localCameraDevices[1]["formats"] if m.get("pixelFormat") == "YUY2")
    assert bridge.localCameraCustomFrameRate("camera://stable-id", wrong_mode["id"], "20") == {}


def test_camera_size_grid_alone_does_not_invent_frame_rates(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    camera = LocalCameraDevice(
        "camera://grid", "Camera", False, (),
        LocalCameraProbe(LocalCameraProbeStatus.READY, "v4l2"),
        resolution_ranges=(LocalVideoResolutionRange(
            CameraMediaType.RAW, "YUY2", 320, 1920, 16, 240, 1080, 16,
        ),),
    )
    controller._set_local_cameras(LocalCameraDiscovery(
        supported=True, ready=True, generation=1, devices=(camera,),
    ))
    record = bridge.localCameraDevices[1]
    assert record["formats"] == [{"id": "", "label": "Automatic"}]
    assert record["capabilitiesStatus"]
    assert record["frameRateRanges"] == []


@pytest.mark.parametrize("width,height,accepted", [
    (1280, 720, True), (1281, 720, False), (1280, 721, False),
    (0, 720, False), (1921, 1080, False),
])
def test_camera_custom_resolution_checks_grid_before_async_query(
    scene_bridge_factory, monkeypatch, width, height, accepted,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    mode = LocalVideoFormat(CameraMediaType.RAW, "YUY2", 640, 480, 30, 1)
    _publish_camera_modes(controller, (mode,))
    discovery = controller.local_cameras
    controller._set_local_cameras(replace(discovery, devices=(replace(
        discovery.devices[0], resolution_ranges=(LocalVideoResolutionRange(
            mode.media_type, mode.pixel_format, 320, 1920, 16, 240, 1080, 16,
        ),),
    ),)))
    requests = []
    pending = Future()
    monkeypatch.setattr(controller, "query_local_camera_modes",
        lambda *args: requests.append(args) or pending, raising=False)
    assert bridge.queryLocalCameraModes("camera://stable-id", width, height) is accepted
    assert len(requests) == int(accepted)
    assert bridge.localCameraDevices[1]["resolutionRanges"][0]["stepWidth"] == 16
    assert all(m.get("width") != 1280 for m in bridge.localCameraDevices[1]["formats"])


def test_camera_custom_resolution_ignores_replaced_query_completion(
    scene_bridge_factory, monkeypatch,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    mode = LocalVideoFormat(CameraMediaType.RAW, "YUY2", 640, 480, 30, 1)
    _publish_camera_modes(controller, (mode,))
    discovery = controller.local_cameras
    device = replace(discovery.devices[0], resolution_ranges=(LocalVideoResolutionRange(
        mode.media_type, mode.pixel_format, 320, 1920, 16, 240, 1080, 16,
    ),))
    controller._set_local_cameras(replace(discovery, devices=(device,)))
    futures = [Future(), Future()]
    monkeypatch.setattr(controller, "query_local_camera_modes",
        lambda *args: futures.pop(0), raising=False)
    old, current = futures
    results = []
    bridge.localCameraModesReady.connect(lambda *args: results.append(args))
    assert bridge.queryLocalCameraModes(device.device_id, 1280, 720)
    assert bridge.queryLocalCameraModes(device.device_id, 1600, 960)
    queried = replace(device, formats=(replace(mode, width=1600, height=960),), resolution_ranges=())
    current.set_result(replace(discovery, devices=(queried,)))
    old.set_exception(RuntimeError("replaced"))
    QCoreApplication.processEvents()
    assert results == [(device.device_id, 1600, 960, True)]


def test_camera_load_ensures_current_inventory(
    scene_bridge_factory, monkeypatch,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    calls = []
    monkeypatch.setattr(controller, "refresh_local_cameras", lambda force=True: calls.append(force))
    bridge.ensureLocalCameras()
    assert calls == [False]


@pytest.mark.parametrize("token,label", [
    ("201", "NV12"), ("301", "YUY2"),
    ("400", "MJPEG"), ("401", "H.264"),
    ("420v", "NV12 · 420v"),
])
def test_camera_ui_humanizes_native_format_without_changing_token(
    scene_bridge_factory, token, label,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    mode = LocalVideoFormat(CameraMediaType.RAW, token, 1280, 720, 30, 1)
    _publish_camera_modes(controller, (mode,))
    record = bridge.localCameraDevices[1]["formats"][1]
    assert record["pixelFormat"] == token
    assert record["formatLabel"] == label
    assert bridge.localCameraFormat("camera://stable-id", record["id"])["pixelFormat"] == token


@pytest.mark.parametrize("text", ["", "0", "1/0", "-30", "NaN", "1e100", "x" * 65])
def test_camera_custom_rate_rejects_malformed_or_unbounded_values(
    scene_bridge_factory, text,
) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    mode = LocalVideoFormat(CameraMediaType.RAW, "NV12", 1280, 720, 30, 1)
    _publish_camera_modes(controller, (mode,))
    mode_id = bridge.localCameraDevices[1]["formats"][1]["id"]
    assert bridge.localCameraCustomFrameRate("camera://stable-id", mode_id, text) == {}


def test_bridge_protects_onvif_credentials_from_scene_documents(scene_bridge_factory) -> None:
    credentials = _Credentials()
    workspace, controller, bridge, _preview_store = scene_bridge_factory(
        credentials=credentials,
    )

    assert bridge.addCamera(
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
        bridge.sourceEditContext(),
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


def test_bridge_disconnects_runtime_callbacks_before_qml_teardown(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview_store = scene_bridge_factory()
    changed = QSignalSpy(bridge.changed)

    bridge.close()
    controller.document_changed.emit(controller.document)
    controller.runtime_changed.emit(controller.runtime.state)

    assert changed.count() == 0


def test_a_camera_used_by_another_scene_can_still_be_added(scene_bridge_factory) -> None:
    """One camera, many scenes: a capture device can only be opened once.

    Hiding cameras that are already on air elsewhere left no way to build a second
    scene around the same camera — creating a duplicate source for the device just
    fails to open it.
    """
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
    camera_id = workspace.configured_cameras[0].id

    bridge.createScene("Stage")
    bridge.addConfiguredCamera(camera_id, "Stage camera", bridge.sourceEditContext())
    stage_id = bridge.selectedSceneId

    bridge.createScene("Wide")
    offered = {camera["id"] for camera in bridge.configuredCameras}
    assert camera_id in offered, "a camera live in another scene must stay offerable"

    bridge.addConfiguredCamera(camera_id, "Stage camera", bridge.sourceEditContext())

    wide = controller.document.scene(bridge.selectedSceneId)
    stage = controller.document.scene(stage_id)
    assert [layer.source_id for layer in wide.layers] == [camera_id]
    # The other scene keeps it too — one source shown by both, not a copy.
    assert [layer.source_id for layer in stage.layers] == [camera_id]
    assert sum(1 for s in controller.document.sources if s.id == camera_id) == 1


def test_a_camera_already_in_this_scene_remains_available_for_another_occurrence(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview_store = scene_bridge_factory()
    camera_id = workspace.configured_cameras[0].id

    bridge.createScene("Stage")
    assert camera_id in {camera["id"] for camera in bridge.configuredCameras}

    bridge.addConfiguredCamera(camera_id, "Stage camera", bridge.sourceEditContext())

    assert camera_id in {camera["id"] for camera in bridge.configuredCameras}
    first = controller.document.scene(bridge.selectedSceneId).layers[0]
    second_id = bridge.addConfiguredCamera(camera_id, "Detail", bridge.sourceEditContext())
    layers = controller.document.scene(bridge.selectedSceneId).layers
    assert len(layers) == 2
    assert [layer.source_id for layer in layers] == [camera_id, camera_id]
    assert layers[1].id == second_id != first.id
    assert layers[1].name == "Detail"
    assert len(workspace.configured_cameras) == 1


def test_a_new_scene_becomes_the_one_being_edited(scene_bridge_factory) -> None:
    """Creating a scene must move the canvas to it, not just the list highlight.

    The preview only renders the scene it was last pointed at, so setting the
    selection alone left the new scene looking selected while the canvas still
    showed the previous one.
    """
    _workspace, controller, bridge, _preview = scene_bridge_factory()
    bridge.setActive(True)
    starting = bridge.selectedSceneId

    bridge.createScene("Fresh")

    created = controller.document.scenes[-1]
    assert created.name == "Fresh"
    assert bridge.selectedSceneId == created.id
    assert controller.preview_scene_id == created.id != starting


def test_duplicating_a_scene_moves_the_canvas_to_the_copy(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview = scene_bridge_factory()
    bridge.setActive(True)

    bridge.duplicateScene(bridge.selectedSceneId)

    copy = controller.document.scenes[-1]
    assert bridge.selectedSceneId == copy.id
    assert controller.preview_scene_id == copy.id


def test_deleting_a_scene_moves_the_canvas_to_what_replaces_it(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview = scene_bridge_factory()
    bridge.setActive(True)
    bridge.createScene("Doomed")
    doomed = bridge.selectedSceneId

    bridge.deleteScene(doomed, "")

    assert bridge.selectedSceneId != doomed
    assert controller.preview_scene_id == bridge.selectedSceneId


def test_the_canvas_is_not_pointed_anywhere_while_the_editor_is_inactive(
    scene_bridge_factory,
) -> None:
    """An off-screen editor must not claim the preview by creating a scene."""
    _workspace, controller, bridge, _preview = scene_bridge_factory()

    bridge.createScene("Offscreen")

    assert controller.preview_scene_id is None


def test_an_rtsp_address_without_credentials_is_not_flagged(scene_bridge_factory) -> None:
    """The vendor in-path login form must still save; only userinfo is rejected."""
    _workspace, controller, bridge, _preview = scene_bridge_factory()

    saved = bridge.addCamera(
        {
            "kind": "rtsp_camera",
            "name": "externa",
            "uri": (
                "rtsp://192.168.0.100:554/user=admin_password=FrwI7MsQ"
                "_channel=0_stream=0&protocol=unicast.sdp?real_stream"
            ),
        },
        bridge.sourceEditContext(),
    )

    assert isinstance(saved, str) and saved


def test_a_pasted_address_with_a_login_is_split_not_rejected(scene_bridge_factory) -> None:
    """Operators paste vendor URLs carrying a login; refusing them helps nobody.

    The address is persisted and printed by libobs, so the credential cannot stay
    in it — but it can be lifted out and stored, which turns the paste into a valid
    camera instead of an error.
    """
    credentials = _Credentials()
    _workspace, controller, bridge, _preview = scene_bridge_factory(
        credentials=credentials
    )

    saved = bridge.addCamera(
        {
            "kind": "rtsp_camera",
            "name": "externa",
            "uri": "rtsp://admin:hunter2@192.168.0.100:554/stream",
        },
        bridge.sourceEditContext(),
    )

    assert isinstance(saved, str) and saved
    camera = controller.workspace.configured_cameras[-1]
    assert camera.configuration.uri == "rtsp://192.168.0.100:554/stream"
    assert "hunter2" not in camera.configuration.uri
    assert camera.credential_ref, "the login must be stored, not discarded"


def test_the_stored_address_never_keeps_the_login(scene_bridge_factory) -> None:
    credentials = _Credentials()
    _workspace, controller, bridge, _preview = scene_bridge_factory(
        credentials=credentials
    )

    bridge.addCamera(
        {
            "kind": "rtsp_camera",
            "name": "externa",
            "uri": "rtsp://admin:p%40ss@10.0.0.5:554/s",
        },
        bridge.sourceEditContext(),
    )

    camera = controller.workspace.configured_cameras[-1]
    assert "admin" not in camera.configuration.uri
    assert "p%40ss" not in camera.configuration.uri
    assert repr(camera.configuration) .count("pass") == 0


def test_rename_changes_only_the_occurrence_and_survives_undo_and_reload(
    scene_bridge_factory, monkeypatch,
) -> None:
    workspace, controller, bridge, _preview = scene_bridge_factory()
    bridge.selectScene(CAMERA_SCENE_ID)
    original = controller.document.scene(CAMERA_SCENE_ID).layers[0]
    second_id = bridge.addConfiguredCamera(
        original.source_id, "Detail", bridge.sourceEditContext(),
    )
    before = controller.document
    hydrate_requests = []
    monkeypatch.setattr(controller, "_hydrate_if_ready", lambda: hydrate_requests.append(True))

    assert bridge.renameLayer(original.id, "Wide angle", bridge.sourceEditContext())

    renamed = controller.document.scene(CAMERA_SCENE_ID).layers
    assert renamed[0] == replace(original, name="Wide angle")
    assert renamed[1].id == second_id and renamed[1].name == "Detail"
    assert controller.document.source(original.source_id).name == "Default camera"
    row = next(bridge.layersModel.get(i) for i in range(bridge.layersModel.rowCount())
               if bridge.layersModel.get(i)["id"] == original.id)
    assert row["name"] == "Wide angle"
    assert scene_engine_graph_signature(controller.document) == scene_engine_graph_signature(before)
    assert hydrate_requests == []
    saved = workspace._document_repository(workspace.catalog.active_collection_id).load()
    assert saved.scene(CAMERA_SCENE_ID).layers == renamed
    controller.documents.undo()
    assert controller.document.scene(CAMERA_SCENE_ID).layers[0] == original
    controller.documents.redo()
    assert controller.document.scene(CAMERA_SCENE_ID).layers == renamed
    assert hydrate_requests == []


@pytest.mark.parametrize("reuse_by", ["id", "deviceId"])
def test_adding_registered_camera_ignores_configuration_edits(
    scene_bridge_factory, monkeypatch, reuse_by,
) -> None:
    credentials = _Credentials()
    workspace, controller, bridge, _preview = scene_bridge_factory(credentials=credentials)
    camera = replace(workspace.configured_cameras[0], configuration=LocalCameraConfig(device_id="physical-device"))
    workspace.upsert_camera(camera)
    updates = []
    monkeypatch.setattr(workspace, "upsert_camera", lambda *_a, **_k: updates.append(True))
    original_resources = workspace.resources
    values = {
        "kind": "local_camera", "name": "Ignored shared name", "layerName": "Another view",
        "formatId": "invalid-format-would-fail-if-applied", "ptzProtocol": "invalid-protocol",
        reuse_by: camera.id if reuse_by == "id" else camera.configuration.device_id,
    }

    layer_id = bridge.addCamera(values, bridge.sourceEditContext())

    assert layer_id
    layer = controller.document.scene(bridge.selectedSceneId).layers[-1]
    assert layer.id == layer_id and layer.name == "Another view"
    assert layer.source_id == camera.id
    assert workspace.resources == original_resources
    assert controller.document.source(camera.id) == camera
    assert updates == [] and credentials.saved == []


@pytest.mark.parametrize("action", ["camera", "configured", "reference", "rename"])
def test_source_editor_context_is_consumed_only_after_success(scene_bridge_factory, action) -> None:
    workspace, controller, bridge, _preview = scene_bridge_factory()
    context = bridge.sourceEditContext()
    camera_id = workspace.configured_cameras[0].id
    original_layer = controller.document.scene(bridge.selectedSceneId).layers[0]

    def save():
        if action == "camera":
            return bridge.addCamera({"kind": "rtsp_camera", "name": "Entrance", "uri": "rtsp://camera.test/live"}, context)
        if action == "configured":
            return bridge.addConfiguredCamera(camera_id, "Camera", context)
        if action == "reference":
            return bridge.addSceneReference(CAMERA_SCENE_ID, "Nested", context)
        return bridge.renameLayer(original_layer.id, "Renamed", context)

    assert save()
    document = controller.document
    resources = workspace.resources
    assert not save()
    assert bridge.sourceEditError
    assert controller.document == document
    assert workspace.resources == resources


@pytest.mark.parametrize("changed", ["scene", "profile"])
def test_source_editor_rejects_save_after_context_change(scene_bridge_factory, changed) -> None:
    workspace, controller, bridge, _preview = scene_bridge_factory()
    context = bridge.sourceEditContext()
    if changed == "scene":
        bridge.selectScene(CAMERA_SCENE_ID)
    else:
        workspace.create_collection("Other profile")
        target = next(c for c in workspace.catalog.collections if c.id != workspace.catalog.active_collection_id)
        bridge.activateSceneProfile(target.id)
    before = controller.document
    resources = workspace.resources

    assert not bridge.addCamera({"kind": "rtsp_camera", "name": "Stale", "uri": "rtsp://camera.test/live"}, context)

    assert bridge.sourceEditError
    assert controller.document == before
    assert workspace.resources == resources


def test_shared_configuration_rejects_camera_changed_since_open(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview = scene_bridge_factory()
    camera = workspace.configured_cameras[0]
    draft = bridge.cameraDraft(camera.id)
    context = bridge.sourceEditContext()
    external = replace(camera, configuration=LocalCameraConfig(device_id="changed-externally"))
    workspace.upsert_camera(external)
    before = controller.document

    assert not bridge.updateCamera(draft, context)

    assert bridge.sourceEditError
    assert workspace.resources.camera(camera.id) == external
    assert controller.document == before


def test_shared_configuration_ignores_unrelated_resource_revision_changes(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview = scene_bridge_factory()
    camera_id, _layer_id = _enable_default_camera_ptz(workspace, controller)
    draft = bridge.cameraDraft(camera_id)
    context = bridge.sourceEditContext()
    assert bridge.savePtzPreset({
        "cameraId": camera_id, "name": "Lectern", "targetMode": "position",
        "pan": 0.2, "tilt": -0.1, "zoom": 0.4,
    })
    assert workspace.resources.revision > context["resourceRevision"]
    original = workspace.resources.camera(camera_id)

    assert bridge.updateCamera(draft, context)

    assert not bridge.sourceEditError
    assert workspace.resources.camera(camera_id) == original
    assert len(workspace.resources.camera_presets) == 1


def test_invalid_rename_keeps_context_available_for_corrected_name(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview = scene_bridge_factory()
    layer = controller.document.scene(bridge.selectedSceneId).layers[0]
    context = bridge.sourceEditContext()
    before = controller.document

    assert not bridge.renameLayer(layer.id, "   ", context)

    assert bridge.sourceEditError == "Check the source name and settings."
    assert controller.document == before
    assert bridge.renameLayer(layer.id, "Corrected name", context)
    assert not bridge.sourceEditError
    assert controller.document.scene(bridge.selectedSceneId).layers[0].name == "Corrected name"


@pytest.mark.parametrize("action", ["camera", "configured", "reference", "rename"])
def test_duplicate_layer_name_is_contextual_and_can_be_corrected_without_reopening(
    scene_bridge_factory, monkeypatch, action,
) -> None:
    credentials = _Credentials()
    workspace, controller, bridge, _preview = scene_bridge_factory(credentials=credentials)
    bridge.selectScene(CAMERA_SCENE_ID)
    existing = controller.document.scene(CAMERA_SCENE_ID).layers[0]
    other_id = bridge.addConfiguredCamera(existing.source_id, "Other angle", bridge.sourceEditContext())
    before = controller.document
    resources = workspace.resources
    writes = []
    save = workspace._resource_repository.save

    def record_save(*args, **kwargs):
        writes.append(True)
        return save(*args, **kwargs)

    monkeypatch.setattr(workspace._resource_repository, "save", record_save)
    context = bridge.sourceEditContext()

    def submit(name):
        if action == "camera":
            return bridge.addCamera({
                "kind": "rtsp_camera", "name": "Another connection", "layerName": name,
                "uri": "rtsp://example.invalid/live", "streamUsername": "user", "streamPassword": "secret",
            }, context)
        if action == "configured":
            return bridge.addConfiguredCamera(existing.source_id, name, context)
        if action == "reference":
            return bridge.addSceneReference(CONTENT_SCENE_ID, name, context)
        return bridge.renameLayer(other_id, name, context)

    message = "A source with this name already exists in this scene."
    assert bridge.layerNameError(f"  {existing.name.upper()}  ", "") == message
    assert bridge.layerNameError(existing.name, existing.id) == ""
    assert not submit(f"  {existing.name.upper()}  ")
    assert bridge.sourceEditError == message
    assert controller.document is before
    assert workspace.resources == resources
    assert writes == []
    assert credentials.saved == [] and credentials.deleted == []
    assert submit("Unique view")
    assert not bridge.sourceEditError
    assert controller.document.scene(CAMERA_SCENE_ID).layers[-1].name == "Unique view"


def test_content_insertion_assigns_unique_names_and_reuses_source(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview = scene_bridge_factory()
    bridge.createScene("Composition")
    bridge.addContentSource()
    bridge.addContentSource()
    bridge.addContentSource()

    layers = controller.document.scene(bridge.selectedSceneId).layers
    assert len({layer.source_id for layer in layers}) == 1
    assert [layer.name for layer in layers] == ["Current content", "Current content (2)", "Current content (3)"]
    controller.documents.undo()
    assert len(controller.document.scene(bridge.selectedSceneId).layers) == 2


@pytest.mark.parametrize("failure_at", ["catalog", "document"])
def test_failed_camera_add_preserves_document_cleans_credentials_and_allows_retry(
    scene_bridge_factory, monkeypatch, failure_at,
) -> None:
    credentials = _Credentials()
    workspace, controller, bridge, _preview = scene_bridge_factory(credentials=credentials)
    before = controller.document
    resources = workspace.resources
    values = {
        "kind": "rtsp_camera", "name": "Entrance", "layerName": "Entrance view",
        "uri": "rtsp://operator:secret@camera.test/live",
    }
    context = bridge.sourceEditContext()
    repository = workspace._resource_repository if failure_at == "catalog" else controller.documents._store
    save = repository.save

    def fail(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(repository, "save", fail)
    assert bridge.addCamera(values, context) == ""
    assert bridge.sourceEditError
    assert controller.document == before
    assert workspace.configured_cameras == resources.cameras
    assert credentials.deleted == ["credential-ref"]
    monkeypatch.setattr(repository, "save", save)

    layer_id = bridge.addCamera(values, context)

    assert layer_id
    assert not bridge.sourceEditError
    assert controller.document.scene(bridge.selectedSceneId).layers[-1].id == layer_id
    assert len(workspace.configured_cameras) == len(resources.cameras) + 1


def test_cancelled_camera_draft_does_not_change_resources_or_credentials(scene_bridge_factory) -> None:
    credentials = _Credentials()
    workspace, controller, bridge, _preview = scene_bridge_factory(credentials=credentials)
    before = controller.document
    resources = workspace.resources
    bridge.sourceEditContext()
    bridge.cameraDraft(workspace.configured_cameras[0].id)
    assert controller.document == before
    assert workspace.resources == resources
    assert credentials.saved == [] and credentials.deleted == []


def test_network_cameras_with_same_address_keep_independent_connection_identities(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview = scene_bridge_factory()
    values = {"kind": "rtsp_camera", "name": "Entrance", "uri": "rtsp://camera.test/live"}
    first = bridge.addCamera(values, bridge.sourceEditContext())
    second = bridge.addCamera(dict(values, name="Backup credentials"), bridge.sourceEditContext())
    assert first and second and first != second
    layers = controller.document.scene(bridge.selectedSceneId).layers[-2:]
    assert layers[0].source_id != layers[1].source_id
    connections = [workspace.resources.camera(layer.source_id) for layer in layers]
    assert connections[0].configuration.uri == connections[1].configuration.uri


def test_registered_disconnected_camera_can_be_referenced_without_device_replacement(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview = scene_bridge_factory()
    camera = replace(workspace.configured_cameras[0], configuration=LocalCameraConfig(device_id="missing-device"))
    workspace.upsert_camera(camera)
    controller._set_local_cameras(LocalCameraDiscovery(supported=True, ready=True, generation=1, devices=()))
    record = next(d for d in bridge.localCameraDevices if d["id"] == "missing-device")
    assert record["available"] is False
    assert bridge.configuredLocalCamera("missing-device")["id"] == camera.id
    layer_id = bridge.addConfiguredCamera(camera.id, "Offline view", bridge.sourceEditContext())
    assert layer_id
    assert controller.document.source(camera.id).configuration.device_id == "missing-device"


def test_scene_reference_cycle_is_disabled_and_rejected_without_orphan_source(scene_bridge_factory) -> None:
    _workspace, controller, bridge, _preview = scene_bridge_factory()
    assert bridge.addSceneReference(CAMERA_SCENE_ID, "Nested camera", bridge.sourceEditContext())
    original_scene = bridge.selectedSceneId
    bridge.selectScene(CAMERA_SCENE_ID)
    candidate = next(scene for scene in bridge.referencedScenes if scene["id"] == original_scene)
    assert candidate["available"] is False and candidate["reason"]
    assert CAMERA_SCENE_ID not in {scene["id"] for scene in bridge.referencedScenes}
    before = controller.document
    assert not bridge.addSceneReference(original_scene, "Cycle", bridge.sourceEditContext())
    assert bridge.sourceEditError
    assert controller.document == before


def test_configuring_shared_camera_preserves_connection_and_occurrence_names(scene_bridge_factory) -> None:
    workspace, controller, bridge, _preview = scene_bridge_factory()
    bridge.selectScene(CAMERA_SCENE_ID)
    original = controller.document.scene(CAMERA_SCENE_ID).layers[0]
    camera = workspace.resources.camera(original.source_id)
    draft = bridge.cameraDraft(camera.id)
    draft.update(name="Not a layer rename", deviceId="new-device")

    assert bridge.updateCamera(draft, bridge.sourceEditContext())

    updated = workspace.resources.camera(camera.id)
    assert updated.name == camera.name
    assert updated.configuration.device_id == "new-device"
    assert controller.document.scene(CAMERA_SCENE_ID).layers[0] == original
    assert bridge.renameLayer(original.id, "Personal view", bridge.sourceEditContext())
    assert controller.document.scene(CAMERA_SCENE_ID).layers[0].name == "Personal view"
    assert workspace.resources.camera(camera.id) == updated


def test_partial_credential_storage_failure_cleans_only_new_secrets(scene_bridge_factory, monkeypatch) -> None:
    credentials = _Credentials()
    workspace, controller, bridge, _preview = scene_bridge_factory(credentials=credentials)
    before = controller.document
    resources = workspace.resources
    values = {
        "kind": "rtsp_camera", "name": "Entrance", "uri": "rtsp://camera.test/live",
        "streamUsername": "viewer", "streamPassword": "stream-secret",
        "ptzProtocol": PtzProtocol.ONVIF.value,
        "ptzEndpoint": "https://camera.test/onvif/ptz", "ptzProfileToken": "profile-one",
        "ptzUsername": "operator", "ptzPassword": "ptz-secret",
    }

    def save(secret):
        credentials.saved.append(secret)
        if len(credentials.saved) == 2:
            raise OSError("protected storage unavailable")
        return "new-ptz-ref"

    monkeypatch.setattr(credentials, "save", save)

    assert not bridge.addCamera(values, bridge.sourceEditContext())

    assert bridge.sourceEditError
    assert credentials.deleted == ["new-ptz-ref"]
    assert controller.document == before
    assert workspace.resources == resources


def test_failed_compensation_preserves_credentials_still_referenced_by_catalog(scene_bridge_factory, monkeypatch):
    credentials = _Credentials()
    workspace, controller, bridge, _preview = scene_bridge_factory(credentials=credentials)
    before = controller.document
    save_resources = workspace._resource_repository.save
    writes = []

    def fail_compensation(resources, **kwargs):
        writes.append(resources)
        if len(writes) > 1:
            raise OSError("resource rollback unavailable")
        save_resources(resources, **kwargs)

    def fail_document(*_args, **_kwargs):
        raise OSError("document write unavailable")

    monkeypatch.setattr(workspace._resource_repository, "save", fail_compensation)
    monkeypatch.setattr(controller.documents._store, "save", fail_document)
    assert not bridge.addCamera({
        "kind": "rtsp_camera", "name": "Entrance", "uri": "rtsp://viewer:secret@camera.test/live",
    }, bridge.sourceEditContext())
    assert bridge.sourceEditError
    assert controller.document == before
    assert workspace.resources == workspace._resource_repository.load()
    assert workspace.configured_cameras[-1].credential_ref == "credential-ref"
    # A second storage failure cannot justify deleting a persisted secret.
    assert credentials.deleted == []


def test_updating_one_network_connection_preserves_secret_owned_by_another(scene_bridge_factory):
    credentials = _Credentials()
    workspace, controller, bridge, _preview = scene_bridge_factory(credentials=credentials)
    connections = []
    for index in range(2):
        camera = SourceDefinition(
            id=f"shared-secret-{index}", kind=SourceKind.RTSP_CAMERA, name=f"Connection {index}",
            configuration=RtspCameraConfig(uri=f"rtsp://camera.test/stream{index}"),
            credential_ref="legacy-shared-secret",
        )
        workspace.add_camera_layer(bridge.selectedSceneId, camera, SceneLayer(
            id=f"shared-secret-layer-{index}", source_id=camera.id, name=camera.name,
        ))
        connections.append(camera)
    for index, camera in enumerate(connections):
        draft = bridge.cameraDraft(camera.id)
        draft['clearStreamCredentials'] = True
        assert bridge.updateCamera(draft, bridge.sourceEditContext())
        assert credentials.deleted == ([] if index == 0 else ['legacy-shared-secret'])
    assert not workspace.resources.credential_references


class _Onvif:
    """Answers at once, from a worker thread, like the real service."""

    def __init__(self, *, cameras=(), details=None, error=None) -> None:
        self.cameras = tuple(cameras)
        self.details = details
        self.error = error
        self.queries: list[tuple[str, PtzCredentials | None]] = []
        self.searches: list[str] = []
        self.found_by_address: tuple[OnvifDiscoveredCamera, ...] = ()

    def discover(self, cancellation=None):
        return self.cameras

    def search(self, target, cancellation=None):
        self.searches.append(target)
        if target == "bad":
            raise OnvifDiscoveryError("onvif_search_target_invalid")
        if target == "huge":
            raise OnvifDiscoveryError("onvif_search_target_too_large")
        return self.found_by_address

    def query(self, device_service, credentials, cancellation=None):
        self.queries.append((device_service, credentials))
        if self.error is not None:
            raise self.error
        return self.details


def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for the bridge"
        QCoreApplication.processEvents()
        time.sleep(0.005)


def _onvif_camera(host: str = "192.168.1.20", **values) -> OnvifDiscoveredCamera:
    return OnvifDiscoveredCamera(
        key=values.pop("key", f"urn:uuid:{host}"),
        device_service=f"http://{host}/onvif/device_service",
        host=host,
        port=80,
        **values,
    )


def test_onvif_search_lists_cameras_and_names_a_saved_twin(scene_bridge_factory) -> None:
    onvif = _Onvif(cameras=[
        _onvif_camera("192.168.1.20", name="Acme", hardware="PTZ-200", location="Hall"),
        _onvif_camera("192.168.1.30"),
    ])
    workspace, _controller, bridge, _preview = scene_bridge_factory(onvif=onvif)
    workspace.upsert_camera(SourceDefinition(
        id="rtsp-hall", kind=SourceKind.RTSP_CAMERA, name="Hall camera",
        configuration=RtspCameraConfig(uri="rtsp://192.168.1.20:554/stream"),
    ))
    assert bridge.onvifDiscoveryStatus == ""

    bridge.discoverOnvifCameras()

    assert bridge.onvifDiscoveryRunning
    assert bridge.onvifDiscoveryStatus == "Searching the network…"
    _wait_for(lambda: not bridge.onvifDiscoveryRunning)
    assert bridge.onvifDiscoveryStatus == "Cameras found: 2"
    assert bridge.onvifCameras == [
        {
            "id": "onvif:urn:uuid:192.168.1.20", "name": "Acme PTZ-200", "host": "192.168.1.20",
            "port": 80, "location": "Hall",
            "deviceService": "http://192.168.1.20/onvif/device_service",
            "savedId": "rtsp-hall", "savedName": "Hall camera",
        },
        {
            "id": "onvif:urn:uuid:192.168.1.30", "name": "192.168.1.30", "host": "192.168.1.30",
            "port": 80, "location": "",
            "deviceService": "http://192.168.1.30/onvif/device_service",
            "savedId": "", "savedName": "",
        },
    ]


def test_onvif_search_without_answers_explains_the_manual_path(scene_bridge_factory) -> None:
    _workspace, _controller, bridge, _preview = scene_bridge_factory(onvif=_Onvif())

    bridge.discoverOnvifCameras()
    _wait_for(lambda: not bridge.onvifDiscoveryRunning)

    assert bridge.onvifCameras == []
    assert "No cameras answered" in bridge.onvifDiscoveryStatus


def test_onvif_search_failure_is_reported(scene_bridge_factory) -> None:
    class _Broken(_Onvif):
        def discover(self, cancellation=None):
            raise OnvifDiscoveryError("onvif_discovery_unavailable")

    _workspace, _controller, bridge, _preview = scene_bridge_factory(onvif=_Broken())

    bridge.discoverOnvifCameras()
    _wait_for(lambda: not bridge.onvifDiscoveryRunning)

    assert bridge.onvifDiscoveryStatus == "The network could not be searched."


def test_onvif_query_reports_streams_and_ptz(scene_bridge_factory) -> None:
    onvif = _Onvif(details=OnvifDeviceDetails(
        device_service="http://192.168.1.20/onvif/device_service",
        ptz_endpoint="http://192.168.1.20/onvif/ptz_service",
        profiles=(
            OnvifMediaProfile(
                "main", "MainStream", "rtsp://192.168.1.20/main", True,
                stream_username="viewer", stream_password="stream-secret",
            ),
            OnvifMediaProfile("sub", "SubStream", "rtsp://192.168.1.20/sub", False),
        ),
    ))
    _workspace, _controller, bridge, _preview = scene_bridge_factory(onvif=onvif)
    spy = QSignalSpy(bridge.onvifCameraQueried)

    request_id = bridge.queryOnvifCamera(" http://192.168.1.20/onvif/device_service ", "admin", "secret")

    assert request_id > 0
    _wait_for(lambda: spy.count() == 1)
    assert spy.at(0)[0] == request_id
    assert spy.at(0)[1] == {
        "ok": True, "errorCode": "", "message": "",
        "deviceService": "http://192.168.1.20/onvif/device_service",
        "ptzEndpoint": "http://192.168.1.20/onvif/ptz_service",
        "profiles": [
            {"token": "main", "name": "MainStream", "uri": "rtsp://192.168.1.20/main", "ptz": True,
             "username": "viewer", "password": "stream-secret"},
            {"token": "sub", "name": "SubStream", "uri": "rtsp://192.168.1.20/sub", "ptz": False,
             "username": "", "password": ""},
        ],
    }
    assert onvif.queries == [
        ("http://192.168.1.20/onvif/device_service", PtzCredentials("admin", "secret")),
    ]


def test_onvif_query_without_a_full_login_asks_anonymously(scene_bridge_factory) -> None:
    onvif = _Onvif(error=OnvifDiscoveryError("onvif_authentication_failed"))
    _workspace, _controller, bridge, _preview = scene_bridge_factory(onvif=onvif)
    spy = QSignalSpy(bridge.onvifCameraQueried)

    bridge.queryOnvifCamera("http://192.168.1.20/onvif/device_service", "admin", "")
    _wait_for(lambda: spy.count() == 1)

    assert onvif.queries[0][1] is None
    assert spy.at(0)[1]["ok"] is False
    assert spy.at(0)[1]["errorCode"] == "onvif_authentication_failed"
    assert spy.at(0)[1]["message"] == "The camera did not accept this user name and password."


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("rtsp://admin@192.168.1.20:554/stream", "http://192.168.1.20/onvif/device_service"),
        ("192.168.1.20", "http://192.168.1.20/onvif/device_service"),
        ("camera.local", "http://camera.local/onvif/device_service"),
        ("", ""),
        ("rtsp:///no-host", ""),
    ],
)
def test_onvif_device_service_for_an_address(scene_bridge_factory, address: str, expected: str) -> None:
    _workspace, _controller, bridge, _preview = scene_bridge_factory(onvif=_Onvif())

    assert bridge.onvifDeviceServiceForAddress(address) == expected


def test_closed_bridge_starts_no_onvif_work(scene_bridge_factory) -> None:
    onvif = _Onvif()
    _workspace, _controller, bridge, _preview = scene_bridge_factory(onvif=onvif)
    bridge.close()

    bridge.discoverOnvifCameras()

    assert not bridge.onvifDiscoveryRunning
    assert bridge.queryOnvifCamera("http://192.168.1.20/onvif/device_service", "", "") == 0
    assert onvif.queries == []


def test_onvif_address_search_adds_to_the_found_cameras(scene_bridge_factory) -> None:
    onvif = _Onvif(cameras=[_onvif_camera("192.168.1.20")])
    onvif.found_by_address = (_onvif_camera("192.168.0.100"), _onvif_camera("192.168.1.20"))
    _workspace, _controller, bridge, _preview = scene_bridge_factory(onvif=onvif)
    bridge.discoverOnvifCameras()
    _wait_for(lambda: not bridge.onvifDiscoveryRunning)

    bridge.searchOnvifAddress(" 192.168.0.0/24 ")
    _wait_for(lambda: not bridge.onvifDiscoveryRunning)

    assert onvif.searches == ["192.168.0.0/24"]
    assert [camera["host"] for camera in bridge.onvifCameras] == ["192.168.1.20", "192.168.0.100"]
    assert bridge.onvifDiscoveryStatus == "Cameras found: 2"


def test_onvif_address_search_without_answers_says_so(scene_bridge_factory) -> None:
    _workspace, _controller, bridge, _preview = scene_bridge_factory(onvif=_Onvif())

    bridge.searchOnvifAddress("192.168.0.100")
    _wait_for(lambda: not bridge.onvifDiscoveryRunning)

    assert bridge.onvifDiscoveryStatus == "No ONVIF camera answered at that address."


@pytest.mark.parametrize(
    ("target", "status"),
    [
        ("bad", "Enter an address such as 192.168.0.100"),
        ("huge", "Search at most 1024 addresses at a time"),
    ],
)
def test_onvif_address_search_explains_an_unusable_target(scene_bridge_factory, target, status) -> None:
    _workspace, _controller, bridge, _preview = scene_bridge_factory(onvif=_Onvif())

    bridge.searchOnvifAddress(target)
    _wait_for(lambda: not bridge.onvifDiscoveryRunning)

    assert bridge.onvifDiscoveryStatus.startswith(status)
