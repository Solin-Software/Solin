from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, QPointF, Qt, QTranslator, QUrl, Slot
from PySide6.QtGui import QDesktopServices, QKeySequence
from PySide6.QtQml import QQmlComponent
from PySide6.QtTest import QTest
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication

from solin.styles.theme import get_theme
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.playlist.visuals import PlaylistIconProvider
from tests._paths import REPO_ROOT
from tests._qt import dispose_widget, mouse_click, show_and_activate, wait_until


@pytest.fixture
def meeting_folder_ui(request):
    widget = QQuickWidget()
    request.addfinalizer(lambda: dispose_widget(widget))
    configure_qml_host(
        widget,
        type_name="MeetingDetailView",
        clear_color="#000000",
        defer_load=True,
        context_properties={"meetingPill": "MIDWEEK", "meetingDate": "October 19–25"},
        image_providers={"playlisticons": PlaylistIconProvider()},
    )
    warnings = []
    widget.engine().warnings.connect(lambda items: warnings.extend(error.toString() for error in items))

    def verify_no_qml_warnings():
        assert warnings == [], "\n".join(warnings)

    request.addfinalizer(verify_no_qml_warnings)
    component = QQmlComponent(widget.engine())
    component.setParent(widget)
    component.setData(b'''
        import QtQuick
        QtObject {
            signal markerEditRequested(string nodeId)
            property bool folderManagementAvailable: true
            property string folderState: "present"
            property string folderPath: "/shared/2026-10-19-midweek"
            property bool syncEnabled: false
            property bool syncBusy: false
            property bool syncPending: false
            property string syncBusyMessage: ""
            property bool modalCancellable: true
            property bool hasItems: false
            property var treeSource: null
            property bool canonicalRestoreAvailable: false
            property int creates: 0
            property int opens: 0
            property int deletes: 0
            property int syncRequests: 0
            property bool requestedSync: false
            property bool acceptSync: true
            function createMeetingFolder() { creates++ }
            function openMeetingFolder() { opens++ }
            function deleteMeetingFolder() { deletes++ }
            function setSyncEnabled(value) {
                syncRequests++
                requestedSync = value
                if (acceptSync) syncEnabled = value
            }
            function pointerEnter() {}
            function pointerExit() {}
            function pointerCursorEnter(source, shape) {}
            function pointerCursorChange(source, shape) {}
            function pointerCursorExit(source) {}
        }
    ''', QUrl())
    bridge = component.create()
    assert bridge is not None, [error.toString() for error in component.errors()]
    bridge.setParent(widget)
    widget.rootContext().setContextProperty("controller", bridge)
    widget.setSource(QUrl.fromLocalFile(str(REPO_ROOT / "src/solin/qml/MeetingDetailView.qml")))
    assert widget.errors() == []
    assert widget.rootObject() is not None
    widget.resize(820, 680)
    show_and_activate(widget)

    def item(name):
        result = widget.rootObject().findChild(QObject, name)
        assert result is not None, name
        return result

    def click(name):
        control = item(name)
        point = control.mapToScene(QPointF(control.width() / 2, control.height() / 2)).toPoint()
        mouse_click(widget, Qt.MouseButton.LeftButton, pos=point)

    return widget, bridge, item, click


def _open_popup(ui):
    _, _, item, click = ui
    click("meetingFolderControl")
    wait_until(
        lambda: item("meetingFolderPopup").property("visible")
        and item("meetingFolderPath").width() > 0,
        description="folder popup layout",
    )


@pytest.mark.parametrize("state", ["present", "missing", "unavailable", "checking"])
@pytest.mark.parametrize("enabled", [False, True])
def test_compact_folder_control_preserves_independent_state_details(meeting_folder_ui, state, enabled):
    widget, bridge, item, _ = meeting_folder_ui
    bridge.setProperty("folderState", state)
    bridge.setProperty("syncEnabled", enabled)
    control = item("meetingFolderControl")
    wait_until(
        lambda: control.property("folderState") == state and control.property("syncEnabled") == enabled,
        description="independent folder and sync state",
    )
    assert control.width() == control.height() == 32
    assert control.property("folderLabel") in control.property("toolTipText")
    assert control.property("syncLabel") in control.property("toolTipText")
    icon_name = "folder_link" if enabled else "folder"
    assert f"playlisticons/{icon_name}/" in item("meetingFolderIcon").property("source").toString()
    color_role = "success" if state == "present" else "textMuted"
    expected_color = widget.rootObject().property(color_role).name()
    assert item("meetingFolderIcon").property("source").toString().endswith(expected_color.lstrip("#"))
    _open_popup(meeting_folder_ui)
    assert item("meetingFolderCreate").property("visible") == (state == "missing")
    assert item("meetingFolderOpen").property("visible") == (state == "present")
    assert item("meetingFolderSync").property("enabled") == (state in {"present", "missing"})


def test_create_is_independent_and_enable_sync_can_start_with_missing_folder(meeting_folder_ui):
    _, bridge, item, click = meeting_folder_ui
    bridge.setProperty("folderState", "missing")
    _open_popup(meeting_folder_ui)
    click("meetingFolderCreate")
    assert bridge.property("creates") == 1
    assert bridge.property("syncRequests") == 0
    assert not bridge.property("syncEnabled")
    click("meetingFolderSync")
    assert bridge.property("syncRequests") == 1
    assert bridge.property("requestedSync")
    assert item("meetingFolderSync").property("checked")


def test_folder_delete_requires_disabled_sync_and_calls_backend_confirmation(meeting_folder_ui):
    _, bridge, item, click = meeting_folder_ui
    bridge.setProperty("syncEnabled", True)
    _open_popup(meeting_folder_ui)
    assert not item("meetingFolderDelete").property("enabled")
    assert item("meetingFolderDeleteReason").property("visible")
    click("meetingFolderDelete")
    assert bridge.property("deletes") == 0
    click("meetingFolderSync")
    assert not bridge.property("requestedSync")
    assert item("meetingFolderDelete").property("enabled")
    click("meetingFolderDelete")
    assert bridge.property("deletes") == 1
    assert not item("meetingFolderPopup").property("visible")


@pytest.mark.parametrize("operation", ["syncPending", "syncBusy"])
def test_popup_stays_inspectable_and_mutations_block_during_operations(meeting_folder_ui, operation):
    _, bridge, item, click = meeting_folder_ui
    bridge.setProperty(operation, True)
    _open_popup(meeting_folder_ui)
    assert item("meetingFolderControl").property("enabled")
    assert not item("meetingFolderSync").property("enabled")
    assert not item("meetingFolderDelete").property("enabled")
    assert item("meetingFolderOperation").property("visible")
    assert item("meetingFolderOperation").property("text") == "Finishing…"
    click("meetingFolderOpen")
    assert bridge.property("opens") == 1
    click("meetingFolderSync")
    click("meetingFolderDelete")
    assert bridge.property("syncRequests") == bridge.property("deletes") == 0


def test_switch_reflects_backend_when_confirmation_is_cancelled(meeting_folder_ui):
    _, bridge, item, click = meeting_folder_ui
    bridge.setProperty("syncEnabled", True)
    bridge.setProperty("acceptSync", False)
    _open_popup(meeting_folder_ui)
    click("meetingFolderSync")
    assert bridge.property("syncRequests") == 1
    assert item("meetingFolderSync").property("checked")
    assert not item("meetingFolderDelete").property("enabled")
    bridge.setProperty("syncEnabled", False)
    wait_until(lambda: not item("meetingFolderSync").property("checked"), description="switch binding")
    assert item("meetingFolderDelete").property("enabled")


def test_folder_popup_supports_keyboard_and_selectable_path(meeting_folder_ui):
    widget, bridge, item, _ = meeting_folder_ui
    control = item("meetingFolderControl")
    control.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(widget, Qt.Key.Key_Space)
    wait_until(lambda: item("meetingFolderPopup").property("visible"), description="keyboard popup open")
    assert item("meetingFolderOpen").property("activeFocus")
    QTest.keyClick(widget, Qt.Key.Key_Space)
    assert bridge.property("opens") == 1
    path = item("meetingFolderPath")
    path.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    QTest.keySequence(widget, QKeySequence(QKeySequence.StandardKey.SelectAll))
    assert path.property("selectedText") == bridge.property("folderPath")
    QTest.keyClick(widget, Qt.Key.Key_Escape)
    wait_until(lambda: not item("meetingFolderPopup").property("visible"), description="keyboard popup close")
    assert control.property("activeFocus")


@pytest.mark.parametrize("width", [360, 520, 1100])
def test_compact_header_and_popup_fit_with_long_folder_paths(meeting_folder_ui, width):
    widget, bridge, item, _ = meeting_folder_ui
    bridge.setProperty("folderPath", "/shared/" + "long directory name/" * 12 + "meeting")
    widget.resize(width, 680)
    control = item("meetingFolderControl")
    header = item("meetingDetailHeader")
    wait_until(lambda: widget.rootObject().width() == width, description="resized meeting detail")
    assert control.x() >= 0
    assert control.mapToScene(QPointF(control.width(), 0)).x() <= width
    assert header.height() == 48
    add_media = item("meetingAddMedia")
    assert control.parentItem() == add_media.parentItem()
    assert control.x() + control.width() + 10 == add_media.x()
    _open_popup(meeting_folder_ui)
    popup = item("meetingFolderPopup")
    assert popup.property("width") <= width - 24
    assert popup.property("height") <= 680 - 24
    assert item("meetingFolderPath").property("text") == bridge.property("folderPath")


def test_folder_control_is_hidden_only_when_management_is_not_configured(meeting_folder_ui):
    _, bridge, item, _ = meeting_folder_ui
    bridge.setProperty("folderState", "unavailable")
    assert item("meetingFolderControl").property("visible")
    bridge.setProperty("folderManagementAvailable", False)
    wait_until(lambda: not item("meetingFolderControl").property("visible"), description="unconfigured folder control")


def test_folder_control_closes_popup_when_clicked_again(meeting_folder_ui):
    _, _, item, click = meeting_folder_ui
    _open_popup(meeting_folder_ui)
    click("meetingFolderControl")
    assert not item("meetingFolderPopup").property("visible")


@pytest.mark.parametrize("keyboard", [False, True])
def test_folder_help_opens_the_meeting_sync_guide(meeting_folder_ui, keyboard):
    class UrlReceiver(QObject):
        @Slot(QUrl)
        def capture(self, url):
            opened.append(url.toString())

    widget, _, item, click = meeting_folder_ui
    opened = []
    receiver = UrlReceiver()
    QDesktopServices.setUrlHandler("https", receiver, "capture")
    try:
        _open_popup(meeting_folder_ui)
        if keyboard:
            item("meetingFolderHelp").forceActiveFocus(Qt.FocusReason.TabFocusReason)
            QTest.keyClick(widget, Qt.Key.Key_Return)
        else:
            click("meetingFolderHelp")
        assert opened == ["https://solinav.vercel.app/guide/#linked-folder-meeting-sync"]
    finally:
        QDesktopServices.unsetUrlHandler("https")


@pytest.fixture
def all_controls_tab_focus():
    """Enable full keyboard navigation for this test and restore the host preference."""
    hints = QApplication.styleHints()
    previous = hints.tabFocusBehavior()
    hints.setTabFocusBehavior(Qt.TabFocusBehavior.TabFocusAllControls)
    try:
        yield
    finally:
        hints.setTabFocusBehavior(previous)


@pytest.mark.usefixtures("all_controls_tab_focus")
def test_keyboard_focus_reveals_actions_in_short_popup(meeting_folder_ui):
    widget, _, item, _ = meeting_folder_ui
    widget.resize(520, 300)
    _open_popup(meeting_folder_ui)
    QTest.keyClick(widget, Qt.Key.Key_Tab)
    wait_until(lambda: item("meetingFolderSync").property("activeFocus"), description="sync keyboard focus")
    QTest.keyClick(widget, Qt.Key.Key_Tab)
    delete = item("meetingFolderDelete")
    wait_until(lambda: delete.property("activeFocus"), description="delete keyboard focus")
    viewport = item("meetingFolderViewport")
    top = delete.mapToItem(viewport, QPointF()).y()
    assert 0 <= top
    assert top + delete.height() <= viewport.height() + 1
    mouse_click(widget, Qt.MouseButton.LeftButton, pos=QPointF(10, 280).toPoint())
    assert not item("meetingFolderPopup").property("visible")


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_expanded_translated_labels_wrap_inside_themed_popup(meeting_folder_ui, theme):
    class ExpandedTranslator(QTranslator):
        def translate(self, context, source, disambiguation=None, n=-1):
            return source + " " + source if context == "MeetingSync" else ""

    widget, _, item, _ = meeting_folder_ui
    widget.rootContext().setContextProperty("appTheme", get_theme(theme).qml_palette())
    translator = ExpandedTranslator()
    app = QApplication.instance()
    app.installTranslator(translator)
    try:
        widget.engine().retranslate()
        widget.resize(360, 680)
        _open_popup(meeting_folder_ui)
        viewport = item("meetingFolderViewport")
        assert item("meetingDetailHeader").height() == 48
        for name in ("meetingFolderHelp", "meetingFolderSync"):
            label = item(name)
            assert label.property("text")
            assert label.width() <= 360 - 24
            assert label.height() > 0
        assert item("meetingFolderSync").width() <= viewport.width()
        assert item("meetingFolderControl").mapToScene(QPointF()).x() >= 0
    finally:
        app.removeTranslator(translator)
        widget.engine().retranslate()
