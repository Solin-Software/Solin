from __future__ import annotations

from dataclasses import replace
from concurrent.futures import Future
from pathlib import Path
import time
from contextlib import ExitStack

import pytest

from PySide6.QtCore import Q_ARG, QCoreApplication, QEvent, QMetaObject, QObject, QPoint, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.engine import LocalCameraDevice, LocalCameraDiscovery, LocalCameraProbe, LocalCameraProbeStatus, LocalVideoFormat, LocalVideoFrameInterval, LocalVideoFrameRateRange, LocalVideoResolutionRange, SceneSourcePreview, SourceHealthEvent, SourceHealthStatus
from solin.core.scenes.model import BusId, CameraMediaType, DEFAULT_CAMERA_SOURCE_ID, LocalCameraConfig, NormalizedRect, SceneLayer, SourceDefinition, SourceKind, TransitionKind
from solin.core.scenes.onvif_discovery import (
    OnvifDeviceDetails,
    OnvifDiscoveredCamera,
    OnvifDiscoveryError,
    OnvifMediaProfile,
)
from solin.core.scenes.presets import CAMERA_SCENE_ID, SceneSeedNames
from solin.core.scenes.workspace import LocalCameraConflictError
from solin.core.scenes.recording import (
    AudioDevice,
    AudioDeviceDirection,
    AudioDeviceDiscovery,
    AudioDeviceSelection,
    ProgramRecordingState,
    ProgramRecordingStatus,
    SceneRecordingConfig,
)
from solin.ui.qml.scenes import ScenesEditorWidget
from tests.fixtures.editor_source_preview import EditorSourcePreview
from tests._qt import mouse_move, mouse_press, mouse_release, show_and_activate, wait_until


class _Projection:
    state = {"type": "idle"}
    idle_media_path = ""

    def set_idle_media_path(self, path):
        self.idle_media_path = path

    def subscribe(self, _listener):
        return lambda: None


class _Recording(QObject):
    state_changed = Signal(object)
    audio_devices_changed = Signal(object)
    configuration_changed = Signal(object)
    busy_changed = Signal(bool)

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.supported = True
        self.state = ProgramRecordingState()
        self.configuration = SceneRecordingConfig()
        self.audio_devices = AudioDeviceDiscovery(
            supported=True,
            ready=True,
            generation=1,
            devices=(
                AudioDevice(
                    "mic",
                    "Room microphone",
                    AudioDeviceDirection.INPUT,
                    True,
                ),
                AudioDevice(
                    "speakers",
                    "Main speakers",
                    AudioDeviceDirection.OUTPUT,
                    True,
                ),
            ),
        )
        self.directory = directory
        self.refresh_count = 0

    @property
    def busy(self) -> bool:
        return self.state.busy

    def toggle(self) -> None:
        self.state = (
            ProgramRecordingState()
            if self.busy
            else ProgramRecordingState(
                status=ProgramRecordingStatus.RECORDING,
                started_at_monotonic=time.monotonic() - 12,
                output_path=self.directory / "recording.mp4",
                active_config=self.configuration,
            )
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
        return Path(self.configuration.output_directory) if self.configuration.output_directory else self.directory


def _profile_paths(tmp_path: Path) -> ProfilePaths:
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="qml-scenes-test",
    )
    paths.ensure_dirs()
    return paths


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
def scene_editor_factory(request, scene_workspace_factory):
    def create(paths: ProfilePaths, *, recording=None, engine_ready=False, source_preview=None):
        cleanup = ExitStack()
        request.addfinalizer(cleanup.close)
        workspace = scene_workspace_factory(paths, seed_names=_seed_names())
        if not workspace.configured_cameras:
            camera = SourceDefinition(
                id=DEFAULT_CAMERA_SOURCE_ID, kind=SourceKind.LOCAL_CAMERA,
                name="Default camera", configuration=LocalCameraConfig(),
            )
            workspace.upsert_camera(camera, add_to_active_document=True)
            workspace.documents.add_layer(CAMERA_SCENE_ID, SceneLayer(
                id="configured-camera-layer", source_id=camera.id, name="Camera",
            ))
        controller = SceneRuntimeController(workspace, _Projection())
        controller.set_editor_source_preview = source_preview or EditorSourcePreview()
        cleanup.callback(controller.close)
        if engine_ready:
            controller._set_engine_ready(True)
        widget = ScenesEditorWidget(controller, recording=recording)
        # The editor is a child viewport in production, without a native title
        # bar that Cocoa constrains to the available screen height.
        widget.setWindowFlag(Qt.WindowType.FramelessWindowHint)

        def close_widget() -> None:
            try:
                widget.cleanup()
            finally:
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                QCoreApplication.processEvents()

        cleanup.callback(close_widget)
        return workspace, controller, widget, cleanup.close

    return create


def _find_quick_item(root: QQuickItem, object_name: str) -> QQuickItem | None:
    if root.objectName() == object_name:
        return root
    for child in root.childItems():
        match = _find_quick_item(child, object_name)
        if match is not None:
            return match
    return None


def _wait_until(predicate, timeout_ms: int = 1000) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        QCoreApplication.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
        elapsed += 10
    return bool(predicate())


def test_scenes_qml_editor_loads_with_the_real_workspace(
    scene_editor_factory, tmp_path: Path
) -> None:
    workspace, controller, widget, _close_editor = scene_editor_factory(_profile_paths(tmp_path))
    widget.resize(1280, 760)
    widget.show()
    QCoreApplication.processEvents()

    assert widget._qml.status() is QQuickWidget.Status.Ready
    assert widget._qml.rootObject() is not None
    assert widget._qml.rootObject().objectName() == "scenesEditorView"
    assert widget._qml.rootObject().findChild(QQuickItem, "scenesPreviewImage") is not None
    # First-run ships Content + Camera.
    assert widget.bridge.scenesModel.rowCount() == 2
    assert widget.bridge.outputFormatLabel == "1920 × 1080"

    root = widget._qml.rootObject()
    beta_badge = root.findChild(QQuickItem, "scenesBetaBadge")
    assert beta_badge is not None
    assert beta_badge.property("label") == "Beta"
    assert beta_badge.isVisible()
    profile_button = root.findChild(QQuickItem, "scenesHeaderProfileButton")
    profile_menu = root.findChild(QObject, "scenesProfileMenu")
    assert profile_button is not None
    assert profile_menu is not None
    profile_button.clicked.emit()
    QCoreApplication.processEvents()
    assert profile_menu.property("visible") is True
    assert profile_menu.property("parent") == profile_button
    assert abs(float(profile_menu.property("x"))) < 2
    assert float(profile_menu.property("y")) >= profile_button.height()
    profile_menu.close()

    transition_button = root.findChild(QQuickItem, "scenesProgramTransitionButton")
    transition_popover = root.findChild(QObject, "scenesProgramTransitionPopover")
    assert transition_button is not None
    assert transition_popover is not None
    transition_button.clicked.emit()
    QCoreApplication.processEvents()
    assert transition_popover.property("visible") is True
    assert transition_popover.property("parent").objectName() == "scenesWorkspace"
    transition_popover.close()

    camera_scene_id = next(
        str(widget.bridge.scenesModel.get(row)["id"])
        for row in range(widget.bridge.scenesModel.rowCount())
        if widget.bridge.scenesModel.get(row)["name"] == "Camera"
    )
    widget.bridge.selectScene(camera_scene_id)
    QCoreApplication.processEvents()
    layer_id = str(widget.bridge.layersModel.get(0)["id"])
    layer_item = _find_quick_item(root, f"scenesCanvasLayer-{layer_id}")
    assert layer_item is not None
    widget.bridge.applyPreciseTransform(
        layer_id,
        {
            "x": 0.2,
            "y": 0.2,
            "width": 0.5,
            "height": 0.5,
            "cropLeft": 0.0,
            "cropTop": 0.0,
            "cropRight": 0.0,
            "cropBottom": 0.0,
            "rotation": 0.0,
        },
    )
    QCoreApplication.processEvents()
    layer_item.setProperty("displayX", 0.6)
    widget.bridge.resetLayerTransform(layer_id)
    QCoreApplication.processEvents()
    assert float(layer_item.property("displayX")) == 0.0
    assert float(layer_item.property("displayWidth")) == 1.0

    widget.resize(1000, 700)
    QCoreApplication.processEvents()
    canvas = widget._qml.rootObject().findChild(QObject, "scenesCanvas")
    assert canvas is not None
    assert canvas.property("width") > 300


def test_program_recording_control_and_settings_share_the_injected_state(
    scene_editor_factory,
    tmp_path: Path,
) -> None:
    recording = _Recording(tmp_path / "Videos" / "Solin")
    workspace, controller, widget, _close_editor = scene_editor_factory(
        _profile_paths(tmp_path), recording=recording, engine_ready=True,
    )
    show_and_activate(widget, size=QSize(1280, 760))
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)

    root = widget._qml.rootObject()
    assert root is not None
    wait_until(lambda: root.width() == 1280, description="wide recording layout")
    recording_button = root.findChild(QQuickItem, "scenesProgramRecordingButton")
    recording_popover = root.findChild(QObject, "scenesRecordingPopover")
    microphone = root.findChild(QQuickItem, "scenesRecordingMicrophone")
    system_audio = root.findChild(QQuickItem, "scenesRecordingSystemAudio")
    choose_folder = root.findChild(QQuickItem, "scenesRecordingChooseFolder")
    audio_warning = root.findChild(QQuickItem, "scenesRecordingAudioWarning")
    assert recording_button is not None and not recording_button.isVisible()
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    assert _wait_until(recording_button.isVisible)
    assert recording_button.property("expanded") is True
    assert recording_popover is not None
    assert microphone is not None
    assert system_audio is not None
    assert choose_folder is not None
    assert audio_warning is not None and not audio_warning.isVisible()

    recording_button.toggleRequested.emit()
    assert _wait_until(lambda: widget.bridge.recordingStatus == "recording")
    assert recording_button.property("active") is True
    assert widget.bridge.profileChangesBlocked

    recording_button.settingsRequested.emit()
    QCoreApplication.processEvents()
    assert recording_popover.property("visible") is True
    assert recording.refresh_count == 1
    assert microphone.property("count") == 3
    assert system_audio.property("count") == 3
    assert choose_folder.property("enabled") is False
    recording.state = replace(
        recording.state,
        system_audio_warning="System audio unavailable; recording silence.",
    )
    recording.state_changed.emit(recording.state)
    assert _wait_until(audio_warning.isVisible)
    recording_popover.close()

    widget.resize(450, 400)
    wait_until(
        lambda: root.width() == 450 and not recording_button.property("expanded"),
        description="compact recording layout",
    )
    assert recording_button.property("expanded") is False
    recording_button.settingsRequested.emit()
    QCoreApplication.processEvents()
    popover_content = recording_popover.property("contentItem")
    assert isinstance(popover_content, QQuickItem)
    assert popover_content.height() <= 296
    assert popover_content.property("contentHeight") > popover_content.height()
    recording_popover.close()

    recording_button.toggleRequested.emit()
    assert _wait_until(lambda: widget.bridge.recordingStatus == "idle")
    assert widget.bridge.profileChangesBlocked
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, False)
    assert not widget.bridge.profileChangesBlocked


def test_canvas_framing_shortcuts_overlay_and_responsive_actions(
    scene_editor_factory, tmp_path: Path
) -> None:
    workspace, controller, widget, _close_editor = scene_editor_factory(_profile_paths(tmp_path))
    widget.resize(1280, 760)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)

    root = widget._qml.rootObject()
    assert root is not None
    camera_scene_id = next(
        str(widget.bridge.scenesModel.get(row)["id"])
        for row in range(widget.bridge.scenesModel.rowCount())
        if widget.bridge.scenesModel.get(row)["name"] == "Camera"
    )
    widget.bridge.selectScene(camera_scene_id)
    layer_id = str(widget.bridge.layersModel.get(0)["id"])
    widget.bridge.selectLayer(layer_id)
    QCoreApplication.processEvents()

    canvas = root.findChild(QQuickItem, "scenesCanvas")
    toolbar = root.findChild(QQuickItem, "scenesFramingToolbar")
    begin_button = root.findChild(QQuickItem, "scenesBeginFramingButton")
    overlay = root.findChild(QQuickItem, "scenesFramingOverlay")
    fill_menu_item = root.findChild(QObject, "scenesFillCropMenuItem")
    mirror_menu_item = root.findChild(QObject, "scenesMirrorLayerMenuItem")
    drawer_toggle = root.findChild(QQuickItem, "scenesDrawerToggle")
    assert canvas is not None
    assert toolbar is not None and toolbar.isVisible()
    assert begin_button is not None and begin_button.property("actionEnabled") is True
    assert overlay is not None and not overlay.isVisible()
    assert fill_menu_item is not None
    assert fill_menu_item.property("text") == "Fill canvas from crop"
    assert mirror_menu_item is not None
    assert mirror_menu_item.property("text") == "Mirror horizontally"
    assert mirror_menu_item.property("checkable") is True
    assert mirror_menu_item.property("checked") is False
    assert drawer_toggle is not None

    widget.bridge.setLayerMirrored(layer_id, True)
    QCoreApplication.processEvents()
    assert mirror_menu_item.property("checked") is True
    widget.bridge.setLayerLocked(layer_id, True)
    QCoreApplication.processEvents()
    assert mirror_menu_item.property("enabled") is False
    widget.bridge.setLayerLocked(layer_id, False)
    widget.bridge.setLayerMirrored(layer_id, False)
    QCoreApplication.processEvents()

    revision = controller.document.revision
    drawer_toggle.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_F)
    QCoreApplication.processEvents()
    assert not widget.bridge.framingActive

    canvas.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_F)
    QCoreApplication.processEvents()
    assert widget.bridge.framingActive
    assert overlay.isVisible()

    QTest.keyClick(widget._qml, Qt.Key.Key_Escape)
    QCoreApplication.processEvents()
    assert not widget.bridge.framingActive
    assert not overlay.isVisible()
    assert controller.document.revision == revision

    canvas.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_F)
    widget.bridge.updateLayerFraming(
        {
            "operation": "scale",
            "scale": 0.5,
            "anchorX": 0.5,
            "anchorY": 0.5,
        }
    )
    QTest.keyClick(widget._qml, Qt.Key.Key_Return)
    QCoreApplication.processEvents()
    assert not widget.bridge.framingActive
    assert controller.document.revision == revision + 1

    widget.resize(450, 620)
    QCoreApplication.processEvents()
    assert begin_button.property("showLabel") is False


def test_framing_waits_for_full_source_and_reopens_the_applied_overlay(
    scene_editor_factory, tmp_path: Path,
) -> None:
    source_preview = EditorSourcePreview()
    pending: Future[SceneSourcePreview] = Future()
    source_preview.responses.append(pending)
    _, controller, widget, _ = scene_editor_factory(_profile_paths(tmp_path), source_preview=source_preview)
    widget.resize(1280, 760)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    layer = controller.document.scene(widget.bridge.selectedSceneId).layers[0]
    widget.bridge.selectLayer(layer.id)
    root = widget._qml.rootObject()
    canvas = root.findChild(QQuickItem, "scenesCanvas")
    overlay = root.findChild(QQuickItem, "scenesFramingOverlay")
    apply_button = root.findChild(QQuickItem, "scenesCommitFramingButton")
    layer_item = _find_quick_item(root, f"scenesCanvasLayer-{layer.id}")
    assert canvas is not None and overlay is not None and apply_button is not None and layer_item is not None
    canvas.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_F)
    assert widget.bridge.framingActive and not widget.bridge.framingReady
    assert not apply_button.property("actionEnabled")
    assert not overlay.isVisible()
    assert not layer_item.isVisible()
    center = canvas.mapToScene(QPointF(canvas.width() / 2, canvas.height() / 2))
    QTest.mouseClick(widget._qml, Qt.MouseButton.LeftButton, pos=center.toPoint())
    assert widget.bridge.framingActive

    pending.set_result(SceneSourcePreview(1920, 1080))
    assert _wait_until(lambda: widget.bridge.framingReady)
    assert overlay.isVisible()
    assert apply_button.property("actionEnabled")
    widget.bridge.updateLayerFraming({"operation": "scale", "scale": 0.5})
    QTest.keyClick(widget._qml, Qt.Key.Key_Return)
    assert not widget.bridge.framingActive
    revision = controller.document.revision
    QTest.keyClick(widget._qml, Qt.Key.Key_F)
    assert widget.bridge.framingActive
    assert canvas.property("framingDraft")["width"] == pytest.approx(0.5)
    assert canvas.property("framingDraft")["x"] == pytest.approx(0.25)
    widget.bridge.updateLayerFraming({"operation": "scale", "scale": 2.0})
    assert canvas.property("framingDraft")["width"] == pytest.approx(1.0)
    QTest.keyClick(widget._qml, Qt.Key.Key_Escape)
    assert not widget.bridge.framingActive
    assert controller.document.revision == revision
    assert layer_item.isVisible()


def test_alt_handle_drag_crops_and_framing_keyboard_commits_the_crop(
    scene_editor_factory, tmp_path: Path
) -> None:
    workspace, controller, widget, _close_editor = scene_editor_factory(_profile_paths(tmp_path))
    widget.resize(1280, 760)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    scene = next(scene for scene in controller.document.scenes if scene.name == "Camera")
    layer = scene.layers[0]
    controller.documents.update_layer(
        scene.id, layer.id,
        replace(layer, rect=NormalizedRect(x=0.2, y=0.2, width=0.6, height=0.6)),
    )
    widget.bridge.selectScene(scene.id)
    widget.bridge.selectLayer(layer.id)
    QCoreApplication.processEvents()
    root = widget._qml.rootObject()
    layer_item = _find_quick_item(root, f"scenesCanvasLayer-{layer.id}")
    assert layer_item is not None
    center = layer_item.mapToScene(QPointF(layer_item.width() / 2, layer_item.height() / 2))
    QTest.mouseClick(widget._qml, Qt.MouseButton.LeftButton, pos=center.toPoint())
    start = layer_item.mapToScene(QPointF(0, layer_item.height() / 2))
    end = start + QPointF(40, 0)
    for event_type, point, button, buttons in (
        (QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
    ):
        QCoreApplication.sendEvent(widget._qml, QMouseEvent(
            event_type, point, point, button, buttons, Qt.KeyboardModifier.AltModifier,
        ))
        QCoreApplication.processEvents()
    cropped = controller.document.scene(scene.id).layers[0]
    assert cropped.crop.left > 0
    assert cropped.crop.right == 0
    assert cropped.rect.x > 0.2
    assert cropped.rect.width < 0.6
    QTest.keyClick(widget._qml, Qt.Key.Key_F)
    assert widget.bridge.framingActive
    QTest.keyClick(widget._qml, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(widget._qml, Qt.Key.Key_Return)
    QCoreApplication.processEvents()
    assert not widget.bridge.framingActive
    framed = controller.document.scene(scene.id).layers[0]
    assert framed.rect == NormalizedRect()
    assert framed.crop != cropped.crop


def test_responsive_drawer_toggle_reflects_the_drawer_state(
    scene_editor_factory, tmp_path: Path
) -> None:
    workspace, controller, widget, _close_editor = scene_editor_factory(_profile_paths(tmp_path))
    widget.resize(1000, 700)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)

    root = widget._qml.rootObject()
    assert root is not None
    workspace_item = root.findChild(QObject, "scenesWorkspace")
    toggle = root.findChild(QObject, "scenesDrawerToggle")
    toggle_icon = root.findChild(QObject, "scenesDrawerToggleIcon")
    assert isinstance(workspace_item, QQuickItem)
    assert isinstance(toggle, QQuickItem)
    assert isinstance(toggle_icon, QQuickItem)
    assert toggle.isVisible()
    assert toggle.property("open") is False
    assert QColor(toggle.property("color")).alpha() == 0
    closed_icon_source = str(toggle_icon.property("source"))

    workspace_item.setProperty("drawerOpen", True)

    assert _wait_until(lambda: toggle.property("open") is True)
    assert _wait_until(lambda: QColor(toggle.property("color")).alpha() > 0)
    assert str(toggle_icon.property("source")) != closed_icon_source


def test_transition_duration_fields_apply_uncommitted_text_and_persist(
    scene_editor_factory,
    scene_workspace_factory,
    request,
    tmp_path: Path,
) -> None:
    paths = _profile_paths(tmp_path)
    workspace, controller, widget, close_editor = scene_editor_factory(paths)
    widget.resize(1280, 760)
    widget.show()
    QCoreApplication.processEvents()
    root = widget._qml.rootObject()
    assert root is not None

    transition_button = root.findChild(QQuickItem, "scenesProgramTransitionButton")
    transition_duration = root.findChild(
        QQuickItem, "scenesProgramTransitionDuration"
    )
    transition_apply = root.findChild(QQuickItem, "scenesProgramTransitionApply")
    assert transition_button is not None
    assert transition_duration is not None
    assert transition_apply is not None
    transition_button.clicked.emit()
    QCoreApplication.processEvents()

    global_editor = transition_duration.property("contentItem")
    global_unit = root.findChild(QQuickItem, "scenesProgramTransitionDuration-unit")
    assert isinstance(global_editor, QQuickItem)
    assert global_unit is not None
    assert global_editor.property("selectByMouse") is True
    assert global_editor.property("text") == "350"
    assert global_unit.property("text") == "ms"
    global_editor.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(widget._qml, "725")
    QCoreApplication.processEvents()
    assert transition_duration.property("value") == 350
    assert global_editor.property("text") == "725"

    transition_apply.clicked.emit()
    assert _wait_until(lambda: widget.bridge.programTransitionDurationMs == 725)

    scene_id = widget.bridge.selectedSceneId
    widget.bridge.setSceneTransitionOverride(
        scene_id,
        TransitionKind.DISSOLVE.value,
        -1,
    )
    duration_dialog = root.findChild(
        QObject, "scenesTransitionOverrideDurationDialog"
    )
    override_duration = root.findChild(
        QQuickItem, "scenesTransitionOverrideDuration"
    )
    override_apply = root.findChild(
        QQuickItem, "scenesTransitionOverrideDurationApply"
    )
    assert duration_dialog is not None
    assert override_duration is not None
    assert override_apply is not None
    duration_dialog.openFor(scene_id, TransitionKind.DISSOLVE.value, 725)
    QCoreApplication.processEvents()

    override_editor = override_duration.property("contentItem")
    assert isinstance(override_editor, QQuickItem)
    assert override_editor.property("selectByMouse") is True
    override_editor.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(widget._qml, "825")
    QCoreApplication.processEvents()
    assert override_duration.property("value") == 725
    assert override_editor.property("text") == "825"

    override_apply.clicked.emit()
    assert _wait_until(
        lambda: controller.documents.effective_transition(scene_id).duration_ms == 825
    )


    close_editor()

    reopened_workspace = scene_workspace_factory(paths, seed_names=_seed_names())
    reopened_controller = SceneRuntimeController(reopened_workspace, _Projection())
    request.addfinalizer(reopened_controller.close)
    assert reopened_controller.document.transition_policy.default.duration_ms == 725
    assert reopened_controller.documents.effective_transition(scene_id).duration_ms == 825


def test_scene_qml_sources_describe_both_reorder_ghosts_and_responsive_drawer() -> None:
    qml_dir = Path("src/solin/qml")
    scene_panel = (qml_dir / "ScenesScenePanel.qml").read_text(encoding="utf-8")
    source_panel = (qml_dir / "ScenesSourcePanel.qml").read_text(encoding="utf-8")
    workspace = (qml_dir / "ScenesWorkspace.qml").read_text(encoding="utf-8")
    menu_item = (qml_dir / "ScenesMenuItem.qml").read_text(encoding="utf-8")

    assert "dragTargetIndex" in scene_panel and "reorderScene" in scene_panel
    assert "dragTargetIndex" in source_panel and "reorderLayer" in source_panel
    assert "width >= 1180" in workspace
    assert "sceneRailVisible: width >= 1180" in workspace
    assert "sourceRailVisible: width >= 760" in workspace
    assert "width < 760" in workspace
    assert "drawerOpen" in workspace
    assert "replaceAll" not in source_panel
    assert "dragGhostY = boundedGhostY" in scene_panel
    assert "dragGhostY = boundedGhostY" in source_panel
    assert "enabled: root.dragActive" in scene_panel
    assert "enabled: root.dragActive" in source_panel
    assert "visible: !root.sourceRailVisible" in workspace
    assert "clip: true" in workspace
    assert "acceptedButtons: Qt.AllButtons" in workspace
    assert "border.color: outputButton.active ? root.success" not in workspace
    assert "border.color: root.borderStrong" in workspace
    assert "profileMenu.open()" not in workspace
    assert "sourceMenu.open()" not in workspace
    assert "transformMenu.open()" not in workspace
    assert "menu.popup(anchor, x, y)" in workspace
    assert "interactionDirty" in (qml_dir / "ScenesCanvas.qml").read_text(encoding="utf-8")
    assert "id: sceneMenu" in workspace
    assert "id: sceneMenu" not in scene_panel
    assert "image://sceneicons/grip" not in scene_panel
    assert "root.beginDrag(sceneRow" in scene_panel
    assert "beginPointerOverride(" in scene_panel
    assert "beginPointerOverride(" in source_panel
    assert "endPointerOverride(dragCursorSource)" in scene_panel
    assert "endPointerOverride(dragCursorSource)" in source_panel
    assert "Keys.onPressed" in scene_panel
    assert "Keys.onPressed" in source_panel
    assert "Qt.AltModifier" in scene_panel and "Qt.AltModifier" in source_panel
    assert "root.bridge.beginPointer(cursorSource, Number(cursorShape))" in scene_panel
    assert "root.bridge.beginPointer(cursorSource, Number(cursorShape))" in source_panel
    assert "root.bridge.updatePointer(cursorSource, Number(cursorShape))" in scene_panel
    assert "root.bridge.updatePointer(cursorSource, Number(cursorShape))" in source_panel
    assert "title: qsTr(\"Fit\")" in workspace
    assert 'objectName: "scenesProgramTransitionButton"' in workspace
    assert 'objectName: "scenesProgramTransitionPopover"' in workspace
    assert 'title: qsTr("Transition override")' in workspace
    assert 'text: qsTr("Use profile transition")' in workspace
    assert 'text: qsTr("Fade through black")' in workspace
    assert "setProgramTransition(" in workspace
    assert "setSceneTransitionOverride(" in workspace
    assert "indicator: Item { visible: false }" in menu_item
    assert menu_item.count('root.checked ? "✓  "') == 1
    assert "indicator: Item { visible: false }" in (
        qml_dir / "ScenesMenuItem.qml"
    ).read_text(encoding="utf-8")


def test_scene_and_source_drag_handles_update_the_native_cursor(
    scene_editor_factory, tmp_path: Path
) -> None:
    workspace, controller, widget, _close_editor = scene_editor_factory(_profile_paths(tmp_path))
    show_and_activate(widget, size=QSize(1280, 760))
    root = widget._qml.rootObject()
    assert root is not None
    wait_until(lambda: root.width() == 1280, description="wide scene editor layout")

    scene_id = str(widget.bridge.scenesModel.get(0)["id"])
    assert _wait_until(
        lambda: _find_quick_item(root, f"scenesSceneDrag-{scene_id}") is not None
    )
    scene_drag = _find_quick_item(root, f"scenesSceneDrag-{scene_id}")
    assert scene_drag is not None
    scene_point = scene_drag.mapToScene(
        QPointF(scene_drag.width() / 2, scene_drag.height() / 2)
    ).toPoint()
    mouse_move(widget._qml, scene_point)
    assert _wait_until(
        lambda: widget._qml.quickWindow().cursor().shape()
        == Qt.CursorShape.OpenHandCursor
    )
    scene_drag_point = scene_point + QPoint(0, int(scene_drag.height()) + 12)
    mouse_press(widget._qml, Qt.MouseButton.LeftButton, pos=scene_point)
    try:
        mouse_move(widget._qml, scene_drag_point)
        wait_until(
            lambda: QApplication.overrideCursor() is not None
            and QApplication.overrideCursor().shape() == Qt.CursorShape.ClosedHandCursor,
            description="scene drag cursor",
        )
    finally:
        mouse_release(widget._qml, Qt.MouseButton.LeftButton, pos=scene_drag_point)
    assert _wait_until(lambda: QApplication.overrideCursor() is None)

    layer_id = str(widget.bridge.layersModel.get(0)["id"])
    source_drag = _find_quick_item(root, f"scenesSourceDrag-{layer_id}")
    assert source_drag is not None
    source_point = source_drag.mapToScene(
        QPointF(source_drag.width() / 2, source_drag.height() / 2)
    ).toPoint()
    mouse_move(widget._qml, QPoint(1, 1))
    mouse_move(widget._qml, source_point)
    wait_until(
        lambda: widget._qml.quickWindow().cursor().shape() == Qt.CursorShape.OpenHandCursor,
        description="source drag hover cursor",
    )
    source_drag_point = source_point + QPoint(0, int(source_drag.height()) + 12)
    mouse_press(widget._qml, Qt.MouseButton.LeftButton, pos=source_point)
    try:
        mouse_move(widget._qml, source_drag_point)
        wait_until(
            lambda: QApplication.overrideCursor() is not None
            and QApplication.overrideCursor().shape() == Qt.CursorShape.ClosedHandCursor,
            description="source drag cursor",
        )
    finally:
        mouse_release(widget._qml, Qt.MouseButton.LeftButton, pos=source_drag_point)
    assert _wait_until(lambda: QApplication.overrideCursor() is None)


def test_scene_qml_attaches_ptz_keys_to_an_item() -> None:
    ptz_popup = Path("src/solin/qml/ScenesPtzPopup.qml").read_text(encoding="utf-8")

    assert "contentItem: FocusScope" in ptz_popup
    assert "Keys.onPressed" in ptz_popup
    assert ptz_popup.index("Keys.onPressed") > ptz_popup.index("contentItem: FocusScope")



def test_camera_dialog_keeps_dependent_modes_and_selection_after_refresh(
    scene_editor_factory, tmp_path, monkeypatch,
) -> None:
    _workspace, controller, widget, _close_editor = scene_editor_factory(_profile_paths(tmp_path))
    hd = LocalVideoFormat(CameraMediaType.JPEG, "MJPG", 1920, 1080, 30_000, 1_001)
    low_rate = replace(hd, fps_numerator=15, fps_denominator=1)
    raw = replace(hd, media_type=CameraMediaType.RAW, pixel_format="NV12")
    sd = LocalVideoFormat(CameraMediaType.RAW, "YUY2", 640, 480, 30, 1)
    camera = LocalCameraDevice("camera://usb", "USB camera", False,
        (hd, low_rate, raw, sd), LocalCameraProbe(LocalCameraProbeStatus.READY, "libobs"))
    discovery = LocalCameraDiscovery(supported=True, ready=True, generation=1, devices=(camera,))
    controller._set_local_cameras(discovery)
    refreshes = []
    monkeypatch.setattr(controller, "refresh_local_cameras", lambda force=True: refreshes.append(force))
    widget.resize(1000, 760)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    assert dialog is not None
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "local_camera"))
    QCoreApplication.processEvents()
    assert refreshes == [False]
    device_combo = dialog.findChild(QObject, "cameraDeviceCombo")
    resolution_combo = dialog.findChild(QObject, "cameraResolutionCombo")
    fps_combo = dialog.findChild(QObject, "cameraFrameRateCombo")
    format_combo = dialog.findChild(QObject, "cameraFormatCombo")
    assert all((device_combo, resolution_combo, fps_combo, format_combo))
    _choose_local_device(dialog, camera.device_id)
    assert QMetaObject.invokeMethod(dialog, "selectResolution", Q_ARG("QVariant", "1920x1080"))
    QCoreApplication.processEvents()
    assert resolution_combo.property("count") == 3  # Automatic, HD, SD.
    assert fps_combo.property("count") == 2  # Only rates supported at HD.
    assert format_combo.property("count") == 2  # Only formats supported at HD / 29.97.
    selected_id = dialog.property("formatId")
    assert not format_combo.property("visible")
    controller._set_local_cameras(replace(discovery, generation=2, devices=(replace(camera, formats=(sd, raw, low_rate, hd)),)))
    QCoreApplication.processEvents()
    assert dialog.property("formatId") == selected_id
    assert resolution_combo.property("currentText") == "1920 × 1080"
    assert fps_combo.property("currentText") == "29.97"
    controller._set_local_cameras(replace(discovery, generation=3, devices=()))
    QCoreApplication.processEvents()
    assert dialog.property("deviceId") == "camera://usb"
    assert dialog.property("formatId") == selected_id
    assert "Unavailable device" in device_combo.property("currentText")
    assert "1920 × 1080" in resolution_combo.property("currentText")
    assert not fps_combo.property("enabled")
    assert QMetaObject.invokeMethod(dialog, "close")



def test_camera_dialog_validates_custom_rate_and_retains_it_during_refresh(
    scene_editor_factory, tmp_path, monkeypatch,
) -> None:
    _workspace, controller, widget, _close_editor = scene_editor_factory(_profile_paths(tmp_path))
    mode = LocalVideoFormat(CameraMediaType.RAW, "NV12", 1280, 720, 30, 1)
    rates = LocalVideoFrameRateRange(
        mode.media_type, mode.pixel_format, mode.width, mode.height,
        LocalVideoFrameInterval(1, 30), LocalVideoFrameInterval(1, 10),
    )
    camera = LocalCameraDevice("camera://usb", "USB camera", False,
        (mode,), LocalCameraProbe(LocalCameraProbeStatus.READY, "libobs"), frame_rate_ranges=(rates,))
    discovery = LocalCameraDiscovery(supported=True, ready=True, generation=1, devices=(camera,))
    controller._set_local_cameras(discovery)
    monkeypatch.setattr(controller, "refresh_local_cameras", lambda force=True: None)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "local_camera"))
    _choose_local_device(dialog, camera.device_id)
    assert QMetaObject.invokeMethod(dialog, "selectResolution", Q_ARG("QVariant", "1280x720"))
    QCoreApplication.processEvents()
    field = dialog.findChild(QObject, "cameraCustomFrameRateField")
    assert not field.property("visible")
    dialog.setProperty("advancedExpanded", True)
    assert field.property("visible")
    assert QMetaObject.invokeMethod(dialog, "applyCustomFrameRate", Q_ARG("QVariant", "20"))
    QCoreApplication.processEvents()
    mode_id = dialog.property("formatId")
    fps_combo = dialog.findChild(QObject, "cameraFrameRateCombo")
    assert fps_combo.property("currentText") == "20"
    assert not dialog.property("customFrameRateInvalid")
    controller._set_local_cameras(replace(discovery, generation=2))
    QCoreApplication.processEvents()
    assert dialog.property("formatId") == mode_id
    assert fps_combo.property("currentText") == "20"
    message = dialog.findChild(QObject, "cameraCapabilitiesMessage")
    assert not message.property("visible")
    assert QMetaObject.invokeMethod(dialog, "applyCustomFrameRate", Q_ARG("QVariant", "31"))
    assert dialog.property("customFrameRateInvalid")
    assert dialog.property("formatId") == mode_id
    assert QMetaObject.invokeMethod(dialog, "close")



def test_camera_dialog_updates_capture_status_when_frames_recover(
    scene_editor_factory, tmp_path, monkeypatch,
) -> None:
    workspace, controller, widget, _close_editor = scene_editor_factory(_profile_paths(tmp_path))
    monkeypatch.setattr(controller, "refresh_local_cameras", lambda force=True: None)
    controller._set_local_cameras(LocalCameraDiscovery(supported=True, ready=True, generation=1, devices=()))
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "local_camera"))
    camera_id = workspace.configured_cameras[0].id
    dialog.setProperty("cameraId", camera_id)
    controller._consume_engine_event(SourceHealthEvent(
        source_id=camera_id, status=SourceHealthStatus.FAILED, error_code="camera_no_frames",
    ))
    QCoreApplication.processEvents()
    message = dialog.findChild(QObject, "cameraCapabilitiesMessage")
    assert "No frames received" in message.property("text")
    controller._consume_engine_event(SourceHealthEvent(source_id=camera_id, status=SourceHealthStatus.READY))
    QCoreApplication.processEvents()
    assert "No frames received" not in message.property("text")
    assert QMetaObject.invokeMethod(dialog, "close")


@pytest.mark.parametrize("query_succeeds", [True, False])
def test_camera_dialog_queries_custom_size_before_exposing_its_frame_rates(
    scene_editor_factory, tmp_path, monkeypatch, query_succeeds,
) -> None:
    _workspace, controller, widget, _close_editor = scene_editor_factory(_profile_paths(tmp_path))
    mode = LocalVideoFormat(CameraMediaType.RAW, "YUY2", 640, 480, 30, 1)
    camera = LocalCameraDevice("camera://usb", "Camera", False, (mode,),
        LocalCameraProbe(LocalCameraProbeStatus.READY, "v4l2"),
        resolution_ranges=(LocalVideoResolutionRange(
            mode.media_type, mode.pixel_format, 320, 1920, 16, 240, 1080, 16,
        ),))
    discovery = LocalCameraDiscovery(supported=True, ready=True, generation=1, devices=(camera,))
    controller._set_local_cameras(discovery)
    monkeypatch.setattr(controller, "refresh_local_cameras", lambda force=True: None)
    pending = Future()
    requests = []
    monkeypatch.setattr(controller, "query_local_camera_modes",
        lambda *args: requests.append(args) or pending, raising=False)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "local_camera"))
    _choose_local_device(dialog, camera.device_id)
    width = dialog.findChild(QObject, "cameraCustomWidthField")
    height = dialog.findChild(QObject, "cameraCustomHeightField")
    assert width is not None and height is not None
    width.setProperty("text", "1281")
    height.setProperty("text", "720")
    assert QMetaObject.invokeMethod(dialog, "requestCustomResolution")
    assert not requests
    assert dialog.property("customResolutionError")
    width.setProperty("text", "1280")
    assert QMetaObject.invokeMethod(dialog, "requestCustomResolution")
    assert dialog.property("customResolutionPending")
    assert requests == [(camera.device_id, 1280, 720)]
    assert dialog.property("formatId") == ""
    if not query_succeeds:
        pending.set_exception(RuntimeError("native query failed"))
        QCoreApplication.processEvents()
        assert not dialog.property("customResolutionPending")
        assert "Supported modes could not be retrieved" in dialog.property("customResolutionError")
        assert dialog.property("formatId") == ""
        assert QMetaObject.invokeMethod(dialog, "close")
        return
    queried = replace(camera, formats=(replace(mode, width=1280, height=720, fps_numerator=20),), resolution_ranges=())
    controller._set_local_cameras(replace(discovery, devices=(replace(camera, formats=(*camera.formats, *queried.formats)),)))
    pending.set_result(replace(discovery, devices=(queried,)))
    QCoreApplication.processEvents()
    QCoreApplication.processEvents()
    assert not dialog.property("customResolutionPending")
    assert not dialog.property("customResolutionError")
    fps = dialog.findChild(QObject, "cameraFrameRateCombo")
    assert fps.property("currentText") == "20"
    assert fps.property("count") == 1
    assert dialog.findChild(QObject, "cameraResolutionCombo").property("currentText") == "1280 × 720"
    assert QMetaObject.invokeMethod(dialog, "close")



def _qml_value(value):
    return value.toVariant() if hasattr(value, "toVariant") else value


def _choose_local_device(dialog, device_id: str) -> None:
    combo = dialog.findChild(QObject, "cameraDeviceCombo")
    devices = _qml_value(combo.property("model"))
    index = next(index for index, device in enumerate(devices) if device["id"] == device_id)
    combo.setProperty("currentIndex", index)
    assert QMetaObject.invokeMethod(combo, "activated", Q_ARG(int, index))
    QCoreApplication.processEvents()


def _set_edited_text(field, text: str) -> None:
    field.setProperty("text", text)
    assert QMetaObject.invokeMethod(field, "textEdited")
    QCoreApplication.processEvents()


@pytest.mark.parametrize("physical_device", [False, True])
def test_new_local_camera_requires_a_physical_device_and_preserves_legacy_edit(
    scene_editor_factory, tmp_path, monkeypatch, physical_device,
) -> None:
    workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    device = LocalCameraDevice(
        "camera://physical", "Lectern camera", False,
        (LocalVideoFormat(CameraMediaType.RAW, "NV12", 1280, 720, 30, 1),),
        LocalCameraProbe(LocalCameraProbeStatus.READY, "libobs"),
    )
    controller._set_local_cameras(LocalCameraDiscovery(
        supported=True, ready=True, generation=1,
        devices=(device,) if physical_device else (),
    ))
    monkeypatch.setattr(controller, "refresh_local_cameras", lambda force=True: None)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "local_camera"))
    devices = _qml_value(dialog.findChild(QObject, "cameraDeviceCombo").property("model"))
    assert all(device["id"] for device in devices)
    if physical_device:
        assert dialog.property("deviceId") == device.device_id
        assert dialog.findChild(QObject, "cameraLayerNameField").property("text") == device.display_name
    else:
        assert not dialog.findChild(QObject, "cameraSaveButton").property("enabled")
        cameras = workspace.configured_cameras
        assert QMetaObject.invokeMethod(dialog, "save")
        assert dialog.property("visible")
        assert workspace.configured_cameras == cameras
    assert QMetaObject.invokeMethod(dialog, "close")
    widget.bridge.selectScene(CAMERA_SCENE_ID)
    widget.bridge.selectLayer("configured-camera-layer")
    assert QMetaObject.invokeMethod(dialog, "openForSelectedCamera")
    assert dialog.property("deviceId") == ""
    assert dialog.property("cameraId") == DEFAULT_CAMERA_SOURCE_ID
    assert not dialog.findChild(QObject, "cameraConnectionNameField").property("visible")
    assert dialog.findChild(QObject, "cameraDeviceCombo").property("currentIndex") >= 0
    assert QMetaObject.invokeMethod(dialog, "save")
    assert not dialog.property("visible")
    assert workspace.configured_cameras[0].configuration.device_id == ""


def test_source_menu_has_four_stable_choices(scene_editor_factory, tmp_path) -> None:
    _workspace, _controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    root = widget._qml.rootObject()
    menu = root.findChild(QObject, "scenesAddSourceMenu")
    assert menu.property("count") == 4
    assert [root.findChild(QObject, name).property("text") for name in (
        "scenesAddCameraMenuItem", "scenesAddNetworkCameraMenuItem", "scenesAddSceneMenuItem",
    )] == ["Camera", "Network camera", "Scene"]
    assert root.findChild(QObject, "scenesRenameLayerMenuItem").property("text") == "Rename…"
    assert root.findChild(QObject, "scenesConfigureCameraMenuItem").property("text") == "Configure camera…"


def test_local_camera_reuses_disconnected_device_and_allows_repeated_layers(
    scene_editor_factory, tmp_path, monkeypatch,
) -> None:
    workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    monkeypatch.setattr(controller, "refresh_local_cameras", lambda force=True: None)
    camera = SourceDefinition(
        id="registered-usb", name="Room camera", kind=SourceKind.LOCAL_CAMERA,
        configuration=LocalCameraConfig(device_id="camera://offline"),
    )
    workspace.upsert_camera(camera, add_to_active_document=True)
    controller._set_local_cameras(LocalCameraDiscovery(supported=True, ready=True, generation=1, devices=()))
    widget.resize(1000, 760)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    for alias in ("Wide shot", "Close shot"):
        assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "local_camera"))
        _choose_local_device(dialog, camera.configuration.device_id)
        assert dialog.property("cameraId") == camera.id
        assert dialog.property("reusingCamera")
        assert dialog.findChild(QObject, "cameraReuseSummary").property("visible")
        assert not dialog.findChild(QObject, "cameraResolutionCombo").property("visible")
        alias_field = dialog.findChild(QObject, "cameraLayerNameField")
        assert alias_field.property("text") == camera.name
        _set_edited_text(alias_field, alias)
        _choose_local_device(dialog, camera.configuration.device_id)
        assert alias_field.property("text") == alias
        assert QMetaObject.invokeMethod(dialog, "save")
        assert not dialog.property("visible")
    scene = controller.document.scene(widget.bridge.selectedSceneId)
    layers = [layer for layer in scene.layers if layer.source_id == camera.id]
    assert [layer.name for layer in layers] == ["Wide shot", "Close shot"]
    assert sum(source.id == camera.id for source in workspace.configured_cameras) == 1


@pytest.mark.parametrize("stale_target", ["scene", "profile"])
def test_network_camera_names_errors_and_stale_scene_keep_the_draft(
    scene_editor_factory, tmp_path, stale_target,
) -> None:
    workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    widget.resize(1000, 760)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "rtsp_camera"))
    connection = dialog.findChild(QObject, "cameraConnectionNameField")
    alias = dialog.findChild(QObject, "cameraLayerNameField")
    uri = dialog.findChild(QObject, "cameraUriField")
    _set_edited_text(connection, "Shared room feed")
    assert alias.property("text") == "Shared room feed"
    _set_edited_text(alias, "Lectern")
    _set_edited_text(connection, "Main connection")
    assert alias.property("text") == "Lectern"
    uri.setProperty("text", "invalid-address")
    count = len(workspace.configured_cameras)
    assert QMetaObject.invokeMethod(dialog, "save")
    assert dialog.property("visible")
    assert dialog.findChild(QObject, "cameraSaveError").property("text")
    assert alias.property("text") == "Lectern"
    assert len(workspace.configured_cameras) == count
    uri.setProperty("text", "rtsp://192.0.2.10/live")
    context = _qml_value(dialog.property("editContext"))
    assert context["requestId"]
    if stale_target == "scene":
        other = next(scene.id for scene in controller.document.scenes if scene.id != context["sceneId"])
        widget.bridge.selectScene(other)
    else:
        widget.bridge.createSceneProfile("Other scene profile")
        profile = next(profile for profile in widget.bridge.sceneProfiles if not profile["active"])
        widget.bridge.activateSceneProfile(profile["id"])
    assert QMetaObject.invokeMethod(dialog, "save")
    assert dialog.property("visible")
    assert _qml_value(dialog.property("editContext")) == context
    assert alias.property("text") == "Lectern"
    assert len(workspace.configured_cameras) == count
    assert dialog.findChild(QObject, "cameraSaveError").property("text")
    assert QMetaObject.invokeMethod(dialog, "close")


def test_network_existing_choice_searches_reuses_and_does_not_edit_shared_name(
    scene_editor_factory, tmp_path,
) -> None:
    workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    camera_id = widget.bridge.addCamera({
        "kind": "rtsp_camera", "name": "Rear connection", "layerName": "Rear",
        "uri": "rtsp://192.0.2.20/live",
    }, widget.bridge.sourceEditContext())
    assert camera_id
    source_id = controller.document.scene(widget.bridge.selectedSceneId).layers[-1].source_id
    widget.resize(1000, 760)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "rtsp_camera"))
    assert dialog.property("connectionChoicePending")
    assert not dialog.findChild(QObject, "cameraConnectionNameField").property("visible")
    assert not dialog.findChild(QObject, "cameraSaveButton").property("enabled")
    selector = dialog.findChild(QObject, "cameraNetworkSelector")
    assert selector.property("selectedId") == ""
    search = selector.findChild(QObject, "cameraNetworkSelectorSearch")
    search.setProperty("text", "rear")
    assert len(_qml_value(selector.property("filteredOptions"))) == 1
    search.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_Return)
    assert dialog.property("cameraId") == source_id
    assert not dialog.property("connectionChoicePending")
    assert dialog.property("reusingCamera")
    _set_edited_text(dialog.findChild(QObject, "cameraLayerNameField"), "Rear detail")
    assert QMetaObject.invokeMethod(dialog, "save")
    assert not dialog.property("visible")
    assert next(camera for camera in workspace.configured_cameras if camera.id == source_id).name == "Rear connection"
    layers = [layer for layer in controller.document.scene(widget.bridge.selectedSceneId).layers if layer.source_id == source_id]
    assert [layer.name for layer in layers] == ["Rear", "Rear detail"]
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "rtsp_camera"))
    assert dialog.property("connectionChoicePending")
    selector.findChild(QObject, "cameraNetworkSelectorSearch").forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_Return)
    assert not dialog.property("connectionChoicePending")
    assert dialog.property("cameraId") == ""
    assert dialog.findChild(QObject, "cameraConnectionNameField").property("visible")
    assert QMetaObject.invokeMethod(dialog, "close")


@pytest.mark.parametrize("camera_scene", [True, False])
def test_layer_rename_f2_preserves_shared_camera_and_rejects_stale_context(
    scene_editor_factory, tmp_path, camera_scene,
) -> None:
    workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    scene_id = CAMERA_SCENE_ID if camera_scene else next(
        scene.id for scene in controller.document.scenes if scene.id != CAMERA_SCENE_ID
    )
    widget.bridge.selectScene(scene_id)
    layer_id = widget.bridge.layersModel.get(0)["id"]
    widget.bridge.selectLayer(layer_id)
    widget.resize(1000, 760)
    show_and_activate(widget)
    canvas = widget._qml.rootObject().findChild(QQuickItem, "scenesCanvas")
    canvas.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_F2)
    dialog = widget._qml.rootObject().findChild(QObject, "scenesRenameLayerDialog")
    assert _wait_until(lambda: dialog.property("visible"))
    name = dialog.findChild(QObject, "scenesRenameLayerNameField")
    name.setProperty("text", "  ")
    assert QMetaObject.invokeMethod(dialog, "save")
    assert dialog.property("visible")
    shared_names = [camera.name for camera in workspace.configured_cameras]
    name.setProperty("text", "Speaker")
    assert QMetaObject.invokeMethod(dialog, "save")
    assert not dialog.property("visible")
    assert widget.bridge.selectedLayer["name"] == "Speaker"
    assert [camera.name for camera in workspace.configured_cameras] == shared_names
    assert QMetaObject.invokeMethod(dialog, "openForLayer")
    name.setProperty("text", "Stale rename")
    other = next(scene.id for scene in controller.document.scenes if scene.id != scene_id)
    widget.bridge.selectScene(other)
    assert QMetaObject.invokeMethod(dialog, "save")
    assert dialog.property("visible")
    assert dialog.findChild(QObject, "scenesRenameLayerError").property("text")
    assert next(layer for layer in controller.document.scene(scene_id).layers if layer.id == layer_id).name == "Speaker"
    assert QMetaObject.invokeMethod(dialog, "close")


def test_nested_scene_search_disables_cycles_and_captures_target_context(
    scene_editor_factory, tmp_path,
) -> None:
    _workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    widget.bridge.createScene("Outer composition")
    outer_id = widget.bridge.selectedSceneId
    assert widget.bridge.addSceneReference(CAMERA_SCENE_ID, "Camera inset", widget.bridge.sourceEditContext())
    widget.bridge.selectScene(CAMERA_SCENE_ID)
    widget.resize(1000, 760)
    widget.show()
    QCoreApplication.processEvents()
    root = widget._qml.rootObject()
    dialog = root.findChild(QObject, "scenesReferenceDialog")
    assert QMetaObject.invokeMethod(dialog, "openForScene")
    choices = _qml_value(dialog.property("choices"))
    cyclic = next(choice for choice in choices if choice["id"] == outer_id)
    assert cyclic["available"] is False and cyclic["reason"]
    selector = dialog.findChild(QObject, "scenesReferenceSelector")
    search = selector.findChild(QObject, "scenesReferenceSelectorSearch")
    search.setProperty("text", "Outer")
    QCoreApplication.processEvents()
    assert len(_qml_value(selector.property("filteredOptions"))) == 1
    search.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_Return)
    assert dialog.property("sceneId") == ""
    assert not dialog.findChild(QObject, "scenesReferenceAddButton").property("enabled")
    search.setProperty("text", "Content")
    QCoreApplication.processEvents()
    QTest.keyClick(widget._qml, Qt.Key.Key_Return)
    assert dialog.property("canAdd")
    name = dialog.findChild(QObject, "scenesReferenceNameField")
    _set_edited_text(name, "Content inset")
    assert QMetaObject.invokeMethod(dialog, "save")
    assert not dialog.property("visible")
    assert controller.document.scene(CAMERA_SCENE_ID).layers[-1].name == "Content inset"



def test_camera_configuration_updates_shared_settings_and_keeps_layer_alias(
    scene_editor_factory, tmp_path,
) -> None:
    workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    widget.resize(1000, 760)
    widget.show()
    QCoreApplication.processEvents()
    root = widget._qml.rootObject()
    dialog = root.findChild(QObject, "scenesCameraDialog")
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "rtsp_camera"))
    _set_edited_text(dialog.findChild(QObject, "cameraConnectionNameField"), "Room connection")
    _set_edited_text(dialog.findChild(QObject, "cameraLayerNameField"), "Speaker inset")
    dialog.findChild(QObject, "cameraUriField").setProperty("text", "rtsp://192.0.2.30/live")
    assert QMetaObject.invokeMethod(dialog, "save")
    assert not dialog.property("visible")
    scene = controller.document.scene(widget.bridge.selectedSceneId)
    layer = next(layer for layer in scene.layers if layer.name == "Speaker inset")
    # Repeated Enter/click on the completed draft must not insert another occurrence.
    assert QMetaObject.invokeMethod(dialog, "save")
    assert controller.document.scene(scene.id).layers == scene.layers
    widget.bridge.selectLayer(layer.id)
    assert QMetaObject.invokeMethod(root.findChild(QObject, "scenesConfigureCameraMenuItem"), "triggered")
    assert dialog.property("visible")
    assert not dialog.property("addToScene")
    assert dialog.property("cameraId") == layer.source_id
    assert not dialog.findChild(QObject, "cameraLayerNameField").property("visible")
    _set_edited_text(dialog.findChild(QObject, "cameraConnectionNameField"), "Renamed connection")
    assert QMetaObject.invokeMethod(dialog, "save")
    assert not dialog.property("visible")
    assert next(camera for camera in workspace.configured_cameras if camera.id == layer.source_id).name == "Renamed connection"
    assert next(current for current in controller.document.scene(scene.id).layers if current.id == layer.id).name == "Speaker inset"


def test_ambiguous_local_device_blocks_creation_with_inline_explanation(
    scene_editor_factory, tmp_path, monkeypatch,
) -> None:
    workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    device = LocalCameraDevice(
        "camera://ambiguous", "Room camera", False,
        (LocalVideoFormat(CameraMediaType.RAW, "NV12", 1280, 720, 30, 1),),
        LocalCameraProbe(LocalCameraProbeStatus.READY, "libobs"),
    )
    controller._set_local_cameras(LocalCameraDiscovery(
        supported=True, ready=True, generation=1, devices=(device,),
    ))
    monkeypatch.setattr(controller, "refresh_local_cameras", lambda force=True: None)
    original_lookup = workspace.local_camera_for_device

    def lookup(device_id):
        if device_id == device.device_id:
            raise LocalCameraConflictError("Conflicting connections")
        return original_lookup(device_id)

    monkeypatch.setattr(workspace, "local_camera_for_device", lookup)
    widget.resize(1000, 760)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "local_camera"))
    _choose_local_device(dialog, device.device_id)
    assert dialog.property("deviceConflict")
    assert not dialog.property("editingSettings")
    assert not dialog.findChild(QObject, "cameraSaveButton").property("enabled")
    message = dialog.findChild(QObject, "cameraDeviceConflictMessage")
    assert message.property("visible")
    assert "conflicting camera connections" in message.property("text")
    cameras = workspace.configured_cameras
    assert QMetaObject.invokeMethod(dialog, "save")
    assert dialog.property("visible")
    assert workspace.configured_cameras == cameras
    monkeypatch.setattr(workspace, "local_camera_for_device", original_lookup)
    _choose_local_device(dialog, device.device_id)
    assert not dialog.property("deviceConflict")
    assert dialog.findChild(QObject, "cameraSaveButton").property("enabled")
    assert QMetaObject.invokeMethod(dialog, "close")


@pytest.mark.parametrize("viewport", [(420, 540), (760, 620)])
@pytest.mark.parametrize("kind", ["local_camera", "rtsp_camera"])
def test_source_dialog_keeps_actions_visible_while_advanced_body_scrolls(
    scene_editor_factory, tmp_path, monkeypatch, viewport, kind,
) -> None:
    _workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    monkeypatch.setattr(controller, "refresh_local_cameras", lambda force=True: None)
    controller._set_local_cameras(LocalCameraDiscovery(
        supported=True, ready=True, generation=1,
        devices=(LocalCameraDevice(
            "camera://physical", "Lectern camera", False,
            (LocalVideoFormat(CameraMediaType.RAW, "NV12", 1280, 720, 30, 1),),
            LocalCameraProbe(LocalCameraProbeStatus.READY, "libobs"),
        ),),
    ))
    widget.resize(*viewport)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", kind))
    dialog.setProperty("advancedExpanded", True)
    QCoreApplication.processEvents()
    header = dialog.property("header")
    footer = dialog.property("footer")
    scroll = dialog.property("contentItem")
    flickable = scroll.property("contentItem")
    save = dialog.findChild(QQuickItem, "cameraSaveButton")
    header_position = header.mapToScene(QPointF())
    footer_position = footer.mapToScene(QPointF())
    assert header_position.y() >= 0
    assert footer_position.y() + footer.height() <= widget._qml.height() + 1
    assert save.mapToScene(QPointF()).x() + save.width() <= widget._qml.width() + 1
    assert float(scroll.property("contentWidth")) <= scroll.width() + 1
    assert float(flickable.property("contentHeight")) > flickable.height()
    flickable.setProperty("contentY", float(flickable.property("contentHeight")) - flickable.height())
    QCoreApplication.processEvents()
    assert header.mapToScene(QPointF()) == header_position
    assert footer.mapToScene(QPointF()) == footer_position
    assert save.isVisible()
    assert QMetaObject.invokeMethod(dialog, "close")


def test_rename_footer_insets_actions_from_dialog_edges(scene_editor_factory, tmp_path) -> None:
    _workspace, _controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    widget.bridge.selectScene(CAMERA_SCENE_ID)
    widget.bridge.selectLayer("configured-camera-layer")
    widget.resize(420, 540)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesRenameLayerDialog")
    assert QMetaObject.invokeMethod(dialog, "openForLayer")
    QCoreApplication.processEvents()
    footer = dialog.property("footer")
    save = dialog.findChild(QQuickItem, "scenesRenameLayerSaveButton")
    position = save.mapToItem(footer, QPointF())
    assert footer.width() - position.x() - save.width() >= 18
    assert footer.height() - position.y() - save.height() >= 18
    assert not dialog.property("nameError")
    assert save.property("enabled")  # The layer's own unchanged name remains valid.
    assert QMetaObject.invokeMethod(dialog, "close")


@pytest.mark.parametrize("flow", [
    "rename", "new_camera", "reuse_camera", "new_local_camera", "reuse_local_camera", "scene_reference",
])
def test_source_names_validate_live_and_block_duplicate_submissions(
    scene_editor_factory, tmp_path, monkeypatch, flow,
) -> None:
    workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    bridge = widget.bridge
    scene_id = bridge.selectedSceneId
    original = controller.document.scene(scene_id).layers[0]
    if flow == "rename":
        added = bridge.addConfiguredCamera(DEFAULT_CAMERA_SOURCE_ID, "Inset", bridge.sourceEditContext())
        assert added
        bridge.selectLayer(added)
    elif flow == "reuse_camera":
        assert bridge.addCamera({
            "kind": "rtsp_camera", "name": "Room connection", "layerName": "Room inset",
            "uri": "rtsp://192.0.2.10/live",
        }, bridge.sourceEditContext())
    elif flow in ("new_local_camera", "reuse_local_camera"):
        device = LocalCameraDevice(
            "camera://physical", "Room camera", False,
            (LocalVideoFormat(CameraMediaType.RAW, "NV12", 1280, 720, 30, 1),),
            LocalCameraProbe(LocalCameraProbeStatus.READY, "libobs"),
        )
        controller._set_local_cameras(LocalCameraDiscovery(
            supported=True, ready=True, generation=1, devices=(device,),
        ))
        monkeypatch.setattr(controller, "refresh_local_cameras", lambda force=True: None)
        if flow == "reuse_local_camera":
            workspace.upsert_camera(SourceDefinition(
                id="registered-room", name="Room camera", kind=SourceKind.LOCAL_CAMERA,
                configuration=LocalCameraConfig(device_id=device.device_id),
            ), add_to_active_document=True)
    widget.resize(760, 620)
    widget.show()
    QCoreApplication.processEvents()
    root = widget._qml.rootObject()
    if flow == "rename":
        dialog = root.findChild(QObject, "scenesRenameLayerDialog")
        assert QMetaObject.invokeMethod(dialog, "openForLayer")
        name = dialog.findChild(QObject, "scenesRenameLayerNameField")
        error_name = "scenesRenameLayerNameError"
        save_name = "scenesRenameLayerSaveButton"
        submission_error_name = "scenesRenameLayerError"
    elif flow == "scene_reference":
        dialog = root.findChild(QObject, "scenesReferenceDialog")
        assert QMetaObject.invokeMethod(dialog, "openForScene")
        choice = next(choice for choice in _qml_value(dialog.property("choices")) if choice["available"])
        assert QMetaObject.invokeMethod(dialog, "selectScene", Q_ARG("QVariant", choice))
        name = dialog.findChild(QObject, "scenesReferenceNameField")
        error_name = "scenesReferenceNameError"
        save_name = "scenesReferenceAddButton"
        submission_error_name = "scenesReferenceError"
    else:
        dialog = root.findChild(QObject, "scenesCameraDialog")
        local = flow in ("new_local_camera", "reuse_local_camera")
        kind = "local_camera" if local else "rtsp_camera"
        assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", kind))
        if local:
            assert dialog.property("reusingCamera") == (flow == "reuse_local_camera")
        elif flow == "reuse_camera":
            selector = dialog.findChild(QObject, "cameraNetworkSelector")
            selector.findChild(QObject, "cameraNetworkSelectorSearch").setProperty("text", "Room")
            assert QMetaObject.invokeMethod(selector, "chooseCurrent")
            assert dialog.property("reusingCamera")
        else:
            _set_edited_text(dialog.findChild(QObject, "cameraConnectionNameField"), "Room connection")
            dialog.findChild(QObject, "cameraUriField").setProperty("text", "rtsp://192.0.2.10/live")
        name = dialog.findChild(QObject, "cameraLayerNameField")
        error_name = "cameraLayerNameError"
        save_name = "cameraSaveButton"
        submission_error_name = "cameraSaveError"
    _set_edited_text(name, "  " + original.name.swapcase() + "  ")
    error = dialog.findChild(QObject, error_name)
    assert error is not None
    assert error.property("visible")
    assert error.property("text") == "A source with this name already exists in this scene."
    save = dialog.findChild(QObject, save_name)
    assert not save.property("enabled")
    layers = controller.document.scene(scene_id).layers
    assert QMetaObject.invokeMethod(dialog, "save")
    name.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_Return)
    assert dialog.property("visible")
    assert controller.document.scene(scene_id).layers == layers
    # A rejected bridge submission must not leave its old error beside a corrected draft.
    assert not bridge.addConfiguredCamera(DEFAULT_CAMERA_SOURCE_ID, original.name, bridge.sourceEditContext())
    assert bridge.sourceEditError
    assert dialog.findChild(QObject, submission_error_name).property("text")
    # "Camera" already names a layer in another scene, which must not conflict.
    _set_edited_text(name, "Camera")
    assert not error.property("visible")
    assert not dialog.findChild(QObject, submission_error_name).property("text")
    assert save.property("enabled")
    # Revalidate a draft when another layer acquires and then releases its name.
    conflict = bridge.addConfiguredCamera(DEFAULT_CAMERA_SOURCE_ID, "Camera", bridge.sourceEditContext())
    assert conflict
    assert error.property("visible")
    assert not save.property("enabled")
    assert bridge.renameLayer(conflict, "Another inset", bridge.sourceEditContext())
    assert not error.property("visible")
    assert save.property("enabled")
    assert QMetaObject.invokeMethod(dialog, "save")
    assert not dialog.property("visible")
    assert any(layer.name == "Camera" for layer in controller.document.scene(scene_id).layers)


class _Onvif:
    def __init__(self, *, cameras=(), details=None, error=None) -> None:
        self.cameras = tuple(cameras)
        self.details = details
        self.error = error
        self.queries: list[str] = []

    def discover(self, cancellation=None):
        return self.cameras

    def query(self, device_service, credentials, cancellation=None):
        self.queries.append(device_service)
        if self.error is not None:
            raise self.error
        return self.details


_ONVIF_CAMERA = OnvifDiscoveredCamera(
    key="urn:uuid:acme",
    device_service="http://192.0.2.40/onvif/device_service",
    host="192.0.2.40",
    port=80,
    name="Acme",
    hardware="PTZ-200",
    location="Hall",
)
_ONVIF_DETAILS = OnvifDeviceDetails(
    device_service="http://192.0.2.40/onvif/device_service",
    ptz_endpoint="http://192.0.2.40/onvif/ptz_service",
    profiles=(
        OnvifMediaProfile("main", "MainStream", "rtsp://192.0.2.40/main", True),
        OnvifMediaProfile("sub", "SubStream", "rtsp://192.0.2.40/sub", False),
    ),
)


def _open_network_camera_dialog(widget, onvif: _Onvif):
    widget.bridge._onvif = onvif
    widget.resize(1000, 760)
    widget.show()
    QCoreApplication.processEvents()
    dialog = widget._qml.rootObject().findChild(QObject, "scenesCameraDialog")
    assert QMetaObject.invokeMethod(dialog, "openForCamera", Q_ARG("QVariant", "rtsp_camera"))
    return dialog


def _find_cameras_on_the_network(dialog) -> None:
    dialog.findChild(QObject, "cameraFindNetworkButton").clicked.emit()
    assert _wait_until(
        lambda: any(choice.get("discovered") for choice in _qml_value(dialog.property("networkChoices"))),
        timeout_ms=5000,
    )


def _choose_network_option(widget, dialog, search_text: str) -> None:
    selector = dialog.findChild(QObject, "cameraNetworkSelector")
    search = selector.findChild(QObject, "cameraNetworkSelectorSearch")
    search.setProperty("text", search_text)
    QCoreApplication.processEvents()
    search.forceActiveFocus()
    QTest.keyClick(widget._qml, Qt.Key.Key_Return)
    QCoreApplication.processEvents()


def test_found_network_camera_fills_its_stream_and_ptz_settings(
    scene_editor_factory, tmp_path,
) -> None:
    workspace, controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    onvif = _Onvif(cameras=[_ONVIF_CAMERA], details=_ONVIF_DETAILS)
    dialog = _open_network_camera_dialog(widget, onvif)

    _find_cameras_on_the_network(dialog)
    assert dialog.findChild(QObject, "cameraFindNetworkStatus").property("text") == "Cameras found: 1"
    discovered = next(
        choice for choice in _qml_value(dialog.property("networkChoices")) if choice.get("discovered")
    )
    assert discovered["name"] == "Acme PTZ-200"
    assert discovered["reason"] == "192.0.2.40 · Hall · Found on the network"

    _choose_network_option(widget, dialog, "Acme")
    uri = dialog.findChild(QObject, "cameraUriField")
    assert _wait_until(lambda: uri.property("text") == "rtsp://192.0.2.40/main", timeout_ms=5000)
    assert onvif.queries == ["http://192.0.2.40/onvif/device_service"]
    assert dialog.findChild(QObject, "cameraConnectionNameField").property("text") == "Acme PTZ-200"
    assert dialog.findChild(QObject, "cameraLayerNameField").property("text") == "Acme PTZ-200"
    assert dialog.findChild(QObject, "cameraStreamCombo").property("visible")
    assert dialog.property("ptzProtocol") == "onvif"
    assert dialog.findChild(QObject, "cameraDiscoveredStatus").property("text").startswith("Connected.")
    assert dialog.findChild(QObject, "cameraNetworkSelector").property("selectedId") == "onvif:urn:uuid:acme"

    assert QMetaObject.invokeMethod(dialog, "selectStream", Q_ARG("QVariant", 1))
    assert uri.property("text") == "rtsp://192.0.2.40/sub"
    assert QMetaObject.invokeMethod(dialog, "save")
    assert not dialog.property("visible")

    camera = next(camera for camera in workspace.configured_cameras if camera.name == "Acme PTZ-200")
    assert camera.configuration.uri == "rtsp://192.0.2.40/sub"
    # The sub stream cannot move, so PTZ keeps the profile that can.
    assert camera.configuration.ptz_binding.endpoint == "http://192.0.2.40/onvif/ptz_service"
    assert camera.configuration.ptz_binding.profile_token == "main"


def test_found_network_camera_asks_for_its_login_when_refused(
    scene_editor_factory, tmp_path,
) -> None:
    _workspace, _controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    onvif = _Onvif(cameras=[_ONVIF_CAMERA], error=OnvifDiscoveryError("onvif_authentication_failed"))
    dialog = _open_network_camera_dialog(widget, onvif)
    _find_cameras_on_the_network(dialog)

    _choose_network_option(widget, dialog, "Acme")

    status = dialog.findChild(QObject, "cameraDiscoveredStatus")
    assert _wait_until(lambda: "needs its login" in status.property("text"), timeout_ms=5000)
    assert dialog.findChild(QObject, "cameraDiscoveredConnectRow").property("visible")
    assert dialog.findChild(QObject, "cameraUriField").property("text") == ""
    assert dialog.property("ptzProtocol") == ""

    onvif.error = None
    onvif.details = _ONVIF_DETAILS
    dialog.findChild(QObject, "cameraDiscoveredConnectButton").clicked.emit()
    assert _wait_until(
        lambda: dialog.findChild(QObject, "cameraUriField").property("text") == "rtsp://192.0.2.40/main",
        timeout_ms=5000,
    )
    assert QMetaObject.invokeMethod(dialog, "close")


def test_ptz_finder_reads_onvif_settings_from_the_camera_address(
    scene_editor_factory, tmp_path,
) -> None:
    _workspace, _controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    onvif = _Onvif(details=_ONVIF_DETAILS)
    dialog = _open_network_camera_dialog(widget, onvif)
    _choose_network_option(widget, dialog, "New")
    dialog.findChild(QObject, "cameraUriField").setProperty("text", "rtsp://192.0.2.40:554/live")
    QCoreApplication.processEvents()

    dialog.findChild(QObject, "cameraPtzFindButton").clicked.emit()
    QCoreApplication.processEvents()
    choices = _qml_value(dialog.property("ptzFinderChoices"))
    assert choices[0]["id"] == "__address__"
    assert choices[0]["deviceService"] == "http://192.0.2.40/onvif/device_service"
    assert QMetaObject.invokeMethod(dialog, "queryPtz", Q_ARG("QVariant", choices[0]))

    status = dialog.findChild(QObject, "cameraPtzFinderStatus")
    assert _wait_until(
        lambda: status.property("text") == "PTZ settings filled in from the camera.", timeout_ms=5000,
    )
    assert not dialog.property("ptzFinderOpen")
    assert _qml_value(dialog.property("ptzProfiles")) == [
        {"token": "main", "name": "MainStream", "uri": "rtsp://192.0.2.40/main", "ptz": True,
         "username": "", "password": ""},
    ]
    assert QMetaObject.invokeMethod(dialog, "close")


def test_address_search_finds_a_camera_behind_a_router(scene_editor_factory, tmp_path) -> None:
    _workspace, _controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))

    class _AddressOnvif(_Onvif):
        def search(self, target, cancellation=None):
            self.queries.append(f"search:{target}")
            return self.cameras

    onvif = _AddressOnvif(cameras=[_ONVIF_CAMERA], details=_ONVIF_DETAILS)
    dialog = _open_network_camera_dialog(widget, onvif)
    field = dialog.findChild(QObject, "cameraSearchAddressField")
    button = dialog.findChild(QObject, "cameraSearchAddressButton")
    assert not button.property("enabled")

    field.setProperty("text", "192.0.2.0/24")
    QCoreApplication.processEvents()
    assert button.property("enabled")
    button.clicked.emit()

    assert _wait_until(
        lambda: any(choice.get("discovered") for choice in _qml_value(dialog.property("networkChoices"))),
        timeout_ms=5000,
    )
    assert onvif.queries == ["search:192.0.2.0/24"]
    assert dialog.findChild(QObject, "cameraFindNetworkStatus").property("text") == "Cameras found: 1"
    assert QMetaObject.invokeMethod(dialog, "close")


def test_choosing_a_found_camera_that_is_saved_reuses_its_connection(
    scene_editor_factory, tmp_path,
) -> None:
    workspace, _controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    saved_id = widget.bridge.addCamera({
        "kind": "rtsp_camera", "name": "Hall", "layerName": "Hall",
        "uri": "rtsp://192.0.2.40:554/live",
    }, widget.bridge.sourceEditContext())
    assert saved_id
    source_id = next(camera.id for camera in workspace.configured_cameras if camera.name == "Hall")
    onvif = _Onvif(cameras=[_ONVIF_CAMERA], details=_ONVIF_DETAILS)
    dialog = _open_network_camera_dialog(widget, onvif)
    _find_cameras_on_the_network(dialog)

    _choose_network_option(widget, dialog, "Acme")

    assert dialog.property("cameraId") == source_id
    assert dialog.property("reusingCamera")
    assert onvif.queries == []  # nothing to ask: the saved connection is reused
    count = len(workspace.configured_cameras)
    _set_edited_text(dialog.findChild(QObject, "cameraLayerNameField"), "Hall again")
    assert QMetaObject.invokeMethod(dialog, "save")
    assert len(workspace.configured_cameras) == count



def test_ptz_finder_keeps_the_stream_login_it_succeeded_with(scene_editor_factory, tmp_path) -> None:
    _workspace, _controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    onvif = _Onvif(details=_ONVIF_DETAILS)
    dialog = _open_network_camera_dialog(widget, onvif)
    _choose_network_option(widget, dialog, "New")
    dialog.findChild(QObject, "cameraUriField").setProperty("text", "rtsp://192.0.2.40:554/live")
    dialog.findChild(QObject, "cameraStreamUsernameField").setProperty("text", "operator")
    dialog.findChild(QObject, "cameraStreamPasswordField").setProperty("text", "stream-secret")
    QCoreApplication.processEvents()
    choices = _qml_value(dialog.property("ptzFinderChoices"))

    assert QMetaObject.invokeMethod(dialog, "queryPtz", Q_ARG("QVariant", choices[0]))
    status = dialog.findChild(QObject, "cameraPtzFinderStatus")
    assert _wait_until(
        lambda: status.property("text") == "PTZ settings filled in from the camera.", timeout_ms=5000,
    )

    # Saving must not drop the login the camera just accepted.
    assert dialog.findChild(QObject, "cameraPtzUsernameField").property("text") == "operator"
    assert dialog.findChild(QObject, "cameraPtzPasswordField").property("text") == "stream-secret"
    assert QMetaObject.invokeMethod(dialog, "close")


def test_found_camera_login_in_the_stream_address_is_kept_apart(scene_editor_factory, tmp_path) -> None:
    _workspace, _controller, widget, _close = scene_editor_factory(_profile_paths(tmp_path))
    details = OnvifDeviceDetails(
        device_service=_ONVIF_DETAILS.device_service,
        ptz_endpoint="",
        profiles=(OnvifMediaProfile(
            "main", "MainStream", "rtsp://192.0.2.40/main", False,
            stream_username="viewer", stream_password="stream-secret",
        ),),
    )
    onvif = _Onvif(cameras=[_ONVIF_CAMERA], details=details)
    dialog = _open_network_camera_dialog(widget, onvif)
    _find_cameras_on_the_network(dialog)

    _choose_network_option(widget, dialog, "Acme")

    uri = dialog.findChild(QObject, "cameraUriField")
    assert _wait_until(lambda: uri.property("text") == "rtsp://192.0.2.40/main", timeout_ms=5000)
    assert dialog.findChild(QObject, "cameraStreamUsernameField").property("text") == "viewer"
    assert dialog.findChild(QObject, "cameraStreamPasswordField").property("text") == "stream-secret"
    assert QMetaObject.invokeMethod(dialog, "close")
