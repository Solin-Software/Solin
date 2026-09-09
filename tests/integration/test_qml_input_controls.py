from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt, QUrl
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from solin.ui.qml.host import configure_qml_host


def _controls_widget(source):
    widget = QQuickWidget()
    configure_qml_host(
        widget, type_name="AppTextField", clear_color="#000000",
        mouse_tracking=True, dismiss_text_focus_on_pointer_press=True, defer_load=True,
    )
    url = QUrl.fromLocalFile(str(Path("src/solin/qml/ControlsTest.qml").resolve()))
    component = QQmlComponent(widget.engine())
    component.setParent(widget)
    component.setData(source, url)
    root = component.create()
    assert root is not None, [item.toString() for item in component.errors()]
    widget.setContent(url, component, root)
    widget.resize(360, 300)
    widget.show()
    QTest.qWait(50)
    return widget, root


def test_shared_text_field_cursor_icon_click_clear_and_outside_focus():
    widget, root = _controls_widget(b'''
        import QtQuick
        Item {
            AppTextField {
                objectName: "input"
                x: 20; y: 20; width: 280
                searchIcon: true
                clearActionVisible: text.length > 0
                onClearRequested: { text = ""; forceActiveFocus() }
            }
        }
    ''')
    try:
        field = root.findChild(QObject, "input")
        QTest.mouseMove(widget.quickWindow(), QPoint(38, 42))
        QTest.qWait(30)
        pointer = field.findChild(QObject, "textFieldPointer")
        assert pointer.property("hovered") is True
        assert pointer.property("cursorShape") == Qt.CursorShape.IBeamCursor
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(38, 42))
        assert field.property("activeFocus") is True
        QTest.keyClicks(widget, "hello")
        assert field.property("text") == "hello"
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(278, 42))
        assert field.property("text") == ""
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(330, 220))
        assert field.property("activeFocus") is False
    finally:
        widget.close()
        widget.deleteLater()
        QApplication.processEvents()


def test_shared_selection_list_reserves_scrollbar_space_and_preserves_elastic_bounds():
    widget, root = _controls_widget(b'''
        import QtQuick
        import QtQuick.Controls
        Item {
            AppSelectionList {
                id: list
                objectName: "list"
                property bool elasticBounds: boundsBehavior === Flickable.DragAndOvershootBounds
                width: 280; height: 120
                model: 12
                delegate: ItemDelegate {
                    objectName: "choice"
                    width: list.delegateWidth; height: 44
                    text: "A choice"
                }
            }
        }
    ''')
    try:
        listing = root.findChild(QObject, "list")
        assert listing.property("delegateWidth") == 266
        assert listing.property("elasticBounds") is True
        assert listing.property("contentHeight") > listing.property("height")
    finally:
        widget.close()
        widget.deleteLater()
        QApplication.processEvents()
