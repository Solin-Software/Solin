from __future__ import annotations

from dataclasses import replace
from concurrent.futures import Future
from pathlib import Path
import time
from contextlib import ExitStack

import pytest

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.engine import SceneSourcePreview
from solin.core.scenes.model import BusId, NormalizedRect, TransitionKind
from solin.core.scenes.presets import SceneSeedNames
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
    # First-run ships Default (year text) + Camera + Content.
    assert widget.bridge.scenesModel.rowCount() == 3
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
