"""Shared placement remains modal and keyboard-operable inside embedded QML hosts."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QPointF, Property, QUrl, Signal, Slot, Qt
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickWidgets import QQuickWidget

from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.core.jw.jwpub_import import JwpubPlaylistImportService
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.jwpub_selection import JwpubSelectionBridge, JwpubSelectionDialog
from tests._jwpub_catalog_fixture import build_catalog_jwpub
from tests._qt import dispose_widget, key_click, mouse_click, show_and_activate, wait_until


class PlacementBridge(QObject):
    changed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.shown = False
        self.confirmed = []
        self.cancelled = 0
        self.options = [
            {"id": "top", "label": "Top", "type": "position", "color": ""},
            {"id": "section:a", "label": "Section", "type": "section", "color": ""},
        ]

    @Property(bool, notify=changed)
    def showPlacement(self):  # noqa: N802 - shared QML contract
        return self.shown

    @Property(list, notify=changed)
    def placementOptions(self):  # noqa: N802
        return self.options

    @Property(str, constant=True)
    def pendingItemTitle(self):  # noqa: N802
        return "Selected media"

    @Property(str, constant=True)
    def pendingItemThumb(self):  # noqa: N802
        return ""

    def show(self):
        self.shown = True
        self.changed.emit()

    @Slot(str)
    def confirmPlacement(self, placement_id):  # noqa: N802
        self.confirmed.append(placement_id)
        self.shown = False
        self.changed.emit()

    @Slot()
    def cancelSelection(self):  # noqa: N802
        self.cancelled += 1
        self.shown = False
        self.changed.emit()


@pytest.fixture
def placement(qt_object_owner, request, all_controls_tab_focus):
    bridge = PlacementBridge(qt_object_owner)
    widget = QQuickWidget()
    request.addfinalizer(lambda: dispose_widget(widget))
    configure_qml_host(
        widget,
        type_name="JwpubSelectionDialog",
        clear_color="#000000",
        context_properties={"testPlacementBridge": bridge},
        defer_load=True,
    )
    component = QQmlComponent(widget.engine(), widget)
    url = QUrl.fromLocalFile(str(Path("src/solin/qml/PlacementKeyboardTest.qml").resolve()))
    component.setData(
        b"""
        import QtQuick
        import QtQuick.Controls
        Rectangle {
            width: 520; height: 480
            property int backgroundClicks: 0
            Button {
                objectName: "backgroundAction"
                text: "Background action"
                onClicked: parent.backgroundClicks++
            }
            TextField { objectName: "backgroundSearch"; y: 60; width: 200 }
            MediaPlacementOverlay { placementBridge: testPlacementBridge }
        }
    """,
        url,
    )
    root = component.create()
    assert root is not None, [error.toString() for error in component.errors()]
    widget.setContent(url, component, root)
    show_and_activate(widget)
    background = root.findChild(QQuickItem, "backgroundAction")
    background.forceActiveFocus()
    return widget, root, bridge, background


def focused(widget):
    return widget.quickWindow().activeFocusItem()


@pytest.mark.parametrize("activation", [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space])
def test_placement_takes_focus_traps_tab_and_confirms_the_focused_destination(
    placement, activation
):
    widget, root, bridge, background = placement
    bridge.show()
    wait_until(lambda: focused(widget) is not background, description="placement focus")

    key_click(widget, Qt.Key.Key_Tab)
    key_click(widget, activation)

    assert bridge.confirmed == ["section:a"]
    assert root.property("backgroundClicks") == 0
    wait_until(lambda: focused(widget) is background, description="restored origin focus")


def test_placement_without_destinations_focuses_cancel_and_outside_click_does_not_activate_background(
    placement,
):
    widget, root, bridge, background = placement
    bridge.options = []
    bridge.show()
    wait_until(
        lambda: focused(widget).objectName() == "mediaPlacementCancel",
        description="cancel focus without destinations",
    )
    key_click(widget, Qt.Key.Key_Return)
    assert bridge.cancelled == 1

    bridge.show()
    wait_until(lambda: focused(widget) is not background, description="reopened placement focus")
    position = background.mapToScene(QPointF(background.width() / 2, background.height() / 2))
    mouse_click(widget, Qt.MouseButton.LeftButton, pos=position.toPoint())
    wait_until(lambda: bridge.cancelled == 2, description="outside placement cancellation")
    assert root.property("backgroundClicks") == 0


def test_publication_placement_escape_preserves_selection_and_restores_import_focus(
    tmp_path,
    qt_object_owner,
    request,
    all_controls_tab_focus,
):
    operations = MediaOperationCoordinator(parent=qt_object_owner)
    request.addfinalizer(operations.shutdown)
    bridge = JwpubSelectionBridge(
        service=JwpubPlaylistImportService(),
        settings=SimpleNamespace(),
        operations=operations,
        language="T",
        dest_images_dir=str(tmp_path / "images"),
        scope_id="publication-keyboard",
        is_current=lambda: True,
        placement_ref=lambda: {
            "items": [],
            "sections": [
                {"id": "a", "name": "A"},
                {"id": "b", "name": "B"},
            ],
        },
        insert=lambda *_args: None,
        parent=qt_object_owner,
    )
    request.addfinalizer(bridge.dispose)
    dialog = JwpubSelectionDialog(bridge)
    request.addfinalizer(dialog.dispose)
    show_and_activate(dialog)
    bridge.load_path(str(build_catalog_jwpub(tmp_path / "publication.jwpub")))
    wait_until(lambda: not bridge.busy, description="publication catalog")
    bridge.toggleDocument("10")
    selected = bridge.model.selected_ids
    import_button = dialog.qml_widget.rootObject().findChild(QQuickItem, "jwpubImport")
    import_button.forceActiveFocus()

    key_click(dialog, Qt.Key.Key_Return)
    wait_until(lambda: bridge.showPlacement, description="publication placement")
    key_click(dialog, Qt.Key.Key_Escape)

    assert not bridge.showPlacement
    assert bridge.model.selected_ids == selected
    assert dialog.isVisible()
    wait_until(
        lambda: focused(dialog.qml_widget) is import_button,
        description="import focus restoration",
    )


@pytest.mark.parametrize("backward", [False, True])
def test_many_destinations_stay_inside_host_and_keyboard_scrolls_the_focused_option(
    placement,
    backward,
):
    widget, root, bridge, background = placement
    bridge.options = [
        {"id": f"section:{number}", "label": f"Section {number}", "type": "section"}
        for number in range(32)
    ]
    bridge.show()
    wait_until(
        lambda: focused(widget).property("text") == bridge.options[0]["label"],
        description="first destination from the updated options",
    )
    popup = root.findChild(QObject, "mediaPlacementPopup")
    viewport = root.findChild(QQuickItem, "mediaPlacementOptions")
    wait_until(
        lambda: (
            viewport.property("contentHeight") > viewport.height()
            and popup.property("height")
            == min(popup.property("implicitHeight"), root.height() - 40)
        ),
        description="bounded destination list layout",
    )

    if backward:
        for _ in range(2):
            key_click(widget, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    else:
        for _ in range(len(bridge.options) - 1):
            key_click(widget, Qt.Key.Key_Tab)

    assert popup.property("height") <= root.height() - 40
    option = focused(widget)
    top = viewport.mapToScene(QPointF()).y()
    option_top = option.mapToScene(QPointF()).y()
    assert viewport.property("contentY") > 0
    assert option_top >= top - 1
    assert option_top + option.height() <= top + viewport.height() + 1
    key_click(widget, Qt.Key.Key_Return)
    assert bridge.confirmed == ["section:31"]
    assert root.property("backgroundClicks") == 0
    wait_until(lambda: focused(widget) is background, description="restored origin focus")


def test_placement_tab_wraps_in_both_directions_and_escape_restores_origin_focus(placement):
    widget, root, bridge, background = placement
    bridge.show()
    wait_until(lambda: focused(widget) is not background, description="placement focus")
    first = focused(widget)
    for _ in range(3):
        key_click(widget, Qt.Key.Key_Tab)
        assert focused(widget) not in (
            background,
            root.findChild(QQuickItem, "backgroundSearch"),
        )
    assert focused(widget) is first
    key_click(widget, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    key_click(widget, Qt.Key.Key_Return)
    assert bridge.cancelled == 1
    assert not bridge.confirmed

    bridge.show()
    wait_until(lambda: focused(widget) is not background, description="reopened placement focus")
    key_click(widget, Qt.Key.Key_Escape)
    assert bridge.cancelled == 2
    wait_until(lambda: focused(widget) is background, description="restored origin focus")
    assert root.property("backgroundClicks") == 0
