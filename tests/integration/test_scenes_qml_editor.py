from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QColor
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.model import TransitionKind
from solin.core.scenes.presets import SceneSeedNames
from solin.core.scenes.workspace import SceneWorkspaceService
from solin.ui.qml.scenes import ScenesEditorWidget


class _Projection:
    state = {"type": "idle"}

    def subscribe(self, _listener):
        return lambda: None


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


def test_scenes_qml_editor_loads_with_the_real_workspace(tmp_path: Path) -> None:
    workspace = SceneWorkspaceService(_profile_paths(tmp_path), seed_names=_seed_names())
    controller = SceneRuntimeController(workspace, _Projection())
    widget = ScenesEditorWidget(controller)
    widget.resize(1280, 760)
    widget.show()
    QCoreApplication.processEvents()

    assert widget._qml.status() is QQuickWidget.Status.Ready
    assert widget._qml.rootObject() is not None
    assert widget._qml.rootObject().objectName() == "scenesEditorView"
    assert widget._qml.rootObject().findChild(QQuickItem, "scenesPreviewImage") is not None
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

    widget.cleanup()
    widget.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_canvas_framing_shortcuts_overlay_and_responsive_actions(tmp_path: Path) -> None:
    workspace = SceneWorkspaceService(_profile_paths(tmp_path), seed_names=_seed_names())
    controller = SceneRuntimeController(workspace, _Projection())
    widget = ScenesEditorWidget(controller)
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

    widget.cleanup()
    widget.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_responsive_drawer_toggle_reflects_the_drawer_state(tmp_path: Path) -> None:
    workspace = SceneWorkspaceService(_profile_paths(tmp_path), seed_names=_seed_names())
    controller = SceneRuntimeController(workspace, _Projection())
    widget = ScenesEditorWidget(controller)
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

    widget.cleanup()
    widget.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_transition_duration_fields_apply_uncommitted_text_and_persist(
    tmp_path: Path,
) -> None:
    paths = _profile_paths(tmp_path)
    workspace = SceneWorkspaceService(paths, seed_names=_seed_names())
    controller = SceneRuntimeController(workspace, _Projection())
    widget = ScenesEditorWidget(controller)
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

    widget.cleanup()
    widget.deleteLater()
    QCoreApplication.processEvents()
    controller.close()

    reopened_workspace = SceneWorkspaceService(paths, seed_names=_seed_names())
    reopened_controller = SceneRuntimeController(reopened_workspace, _Projection())
    assert reopened_controller.document.transition_policy.default.duration_ms == 725
    assert reopened_controller.documents.effective_transition(scene_id).duration_ms == 825
    reopened_controller.close()


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


def test_scene_and_source_drag_handles_update_the_native_cursor(tmp_path: Path) -> None:
    workspace = SceneWorkspaceService(_profile_paths(tmp_path), seed_names=_seed_names())
    controller = SceneRuntimeController(workspace, _Projection())
    widget = ScenesEditorWidget(controller)
    widget.resize(1280, 760)
    widget.show()
    QCoreApplication.processEvents()
    root = widget._qml.rootObject()
    assert root is not None

    scene_id = str(widget.bridge.scenesModel.get(0)["id"])
    assert _wait_until(
        lambda: _find_quick_item(root, f"scenesSceneDrag-{scene_id}") is not None
    )
    scene_drag = _find_quick_item(root, f"scenesSceneDrag-{scene_id}")
    assert scene_drag is not None
    scene_point = scene_drag.mapToScene(
        QPointF(scene_drag.width() / 2, scene_drag.height() / 2)
    ).toPoint()
    QTest.mouseMove(widget._qml, scene_point)
    assert _wait_until(
        lambda: widget._qml.quickWindow().cursor().shape()
        == Qt.CursorShape.OpenHandCursor
    )
    scene_drag_point = scene_point + QPoint(0, int(scene_drag.height()) + 12)
    QTest.mousePress(widget._qml, Qt.MouseButton.LeftButton, pos=scene_point)
    QTest.mouseMove(widget._qml, scene_drag_point)
    assert _wait_until(
        lambda: QApplication.overrideCursor() is not None
        and QApplication.overrideCursor().shape()
        == Qt.CursorShape.ClosedHandCursor
    )
    QTest.mouseRelease(
        widget._qml,
        Qt.MouseButton.LeftButton,
        pos=scene_drag_point,
    )
    assert _wait_until(lambda: QApplication.overrideCursor() is None)

    layer_id = str(widget.bridge.layersModel.get(0)["id"])
    source_drag = _find_quick_item(root, f"scenesSourceDrag-{layer_id}")
    assert source_drag is not None
    source_point = source_drag.mapToScene(
        QPointF(source_drag.width() / 2, source_drag.height() / 2)
    ).toPoint()
    QTest.mouseMove(widget._qml, source_point)
    assert _wait_until(
        lambda: widget._qml.quickWindow().cursor().shape()
        == Qt.CursorShape.OpenHandCursor
    )
    source_drag_point = source_point + QPoint(0, int(source_drag.height()) + 12)
    QTest.mousePress(widget._qml, Qt.MouseButton.LeftButton, pos=source_point)
    QTest.mouseMove(widget._qml, source_drag_point)
    assert _wait_until(
        lambda: QApplication.overrideCursor() is not None
        and QApplication.overrideCursor().shape()
        == Qt.CursorShape.ClosedHandCursor
    )
    QTest.mouseRelease(
        widget._qml,
        Qt.MouseButton.LeftButton,
        pos=source_drag_point,
    )
    assert _wait_until(lambda: QApplication.overrideCursor() is None)

    widget.cleanup()
    widget.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_qml_attaches_ptz_keys_to_an_item() -> None:
    ptz_popup = Path("src/solin/qml/ScenesPtzPopup.qml").read_text(encoding="utf-8")

    assert "contentItem: FocusScope" in ptz_popup
    assert "Keys.onPressed" in ptz_popup
    assert ptz_popup.index("Keys.onPressed") > ptz_popup.index("contentItem: FocusScope")
