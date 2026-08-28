from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from concurrent.futures import Future

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QSignalSpy

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.engine import (
    LocalCameraDevice,
    LocalCameraDiscovery,
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
from solin.core.scenes.presets import CAMERA_SCENE_ID, CONTENT_SCENE_ID, SceneSeedNames
from solin.core.scenes.ptz import PtzControlKind, PtzControlResult, PtzRecallStatus
from solin.core.scenes.workspace import SceneWorkspaceService
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
        notifications=notifications,
        credentials=credentials,
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


def test_camera_source_health_surfaces_an_actionable_layer_warning(tmp_path: Path) -> None:
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)
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
    camera_id, layer_id = _enable_default_camera_ptz(workspace, controller)
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
    assert bridge.scenesModel.rowCount() == 3
    assert len(scene.layers) == 1
    assert virtual.mode is OutputMode.AUTO
    assert virtual.manual_scene_id == scene.id
    assert bridge.selectedSceneDesired
    assert not bridge.selectedSceneLive
    assert bridge.engineStatus == "Scene engine unavailable"
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
    bridge.selectScene(CAMERA_SCENE_ID)
    assert bridge.selectedSceneRequiresReplacement
    assert not bridge.selectedSceneLive

    bridge.deleteScene(CAMERA_SCENE_ID, "")
    assert controller.document.scene(CAMERA_SCENE_ID).id == CAMERA_SCENE_ID
    assert notifications.errors[-1][1]["dedupe_key"] == (
        "scenes-live-delete-replacement-required"
    )

    bridge.deleteScene(CAMERA_SCENE_ID, CONTENT_SCENE_ID)

    assert all(scene.id != CAMERA_SCENE_ID for scene in controller.document.scenes)
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


def test_bridge_invalidates_preview_on_scene_and_profile_changes(tmp_path: Path) -> None:
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
    bridge.selectScene(other_scene_id)
    assert not bridge.previewAvailable
    controller.preview_frame_changed.emit(scene_id, frame)
    assert not bridge.previewAvailable

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


def test_bridge_uses_exact_local_camera_format(tmp_path: Path) -> None:
    workspace, controller, bridge, _preview_store = _bridge(tmp_path)
    video_format = LocalVideoFormat(
        media_type=CameraMediaType.JPEG,
        pixel_format="JPEG",
        width=1920,
        height=1080,
        fps_numerator=30_000,
        fps_denominator=1_001,
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
    assert configuration.fps_numerator == 30_000
    assert configuration.fps_denominator == 1_001
    assert configuration.keep_active
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
