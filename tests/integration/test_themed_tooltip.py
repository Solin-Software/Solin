from __future__ import annotations

import pytest
from types import SimpleNamespace

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QHelpEvent
from PySide6.QtWidgets import QApplication, QFrame, QVBoxLayout, QWidget

from solin.ui import themed_tooltip
from solin.widgets.common.popup_hover_button import PopupHoverButton
from solin.widgets.quick_access_toolbar import QuickAccessToolbar
from tests._qt import dispose_widget, show_and_activate, wait_until


@pytest.fixture
def tooltip(monkeypatch, request):
    popup = themed_tooltip._ThemedTooltipPopup()
    request.addfinalizer(lambda: dispose_widget(popup))
    monkeypatch.setattr(themed_tooltip, "_POPUP", popup)
    original_cursor = QCursor.pos()
    request.addfinalizer(lambda: QCursor.setPos(original_cursor))
    return popup


@pytest.fixture
def button(request):
    widget = PopupHoverButton()
    request.addfinalizer(lambda: dispose_widget(widget))
    # This standalone control models a button in frameless application chrome;
    # an OS title bar would put its client area beyond the requested screen edge.
    widget.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
    return widget


def _show_button(button, position):
    button.move(position)
    show_and_activate(button)
    button.move(position)
    wait_until(
        lambda: button.mapToGlobal(QPoint()) == position,
        description="native tooltip anchor geometry",
    )
    cursor = button.mapToGlobal(button.rect().center())
    QCursor.setPos(cursor)
    wait_until(lambda: QCursor.pos() == cursor, description="pointer over tooltip anchor")
    return cursor


def _wait_tooltip(tooltip):
    wait_until(
        lambda: tooltip.isVisible()
        and tooltip.windowHandle() is not None
        and tooltip.windowHandle().isExposed(),
        description="native tooltip exposure",
    )


def test_tooltip_filter_is_owned_by_widget_and_installed_once(button):
    themed_tooltip.install_themed_tooltip(button)
    themed_tooltip.install_themed_tooltip(button)
    filters = button.findChildren(
        themed_tooltip.ThemedTooltipFilter, options=Qt.FindChildOption.FindDirectChildrenOnly
    )
    assert len(filters) == 1
    assert filters[0].parent() is button


@pytest.mark.parametrize("edge", ["left", "right", "top", "bottom"])
def test_native_button_tooltip_stays_inside_work_area(tooltip, button, edge):
    available = QGuiApplication.primaryScreen().availableGeometry()
    center = available.center()
    x = (
        available.left()
        if edge == "left"
        else available.right() - 28
        if edge == "right"
        else center.x()
    )
    y = (
        available.top()
        if edge == "top"
        else available.bottom() - 28
        if edge == "bottom"
        else center.y()
    )
    cursor = _show_button(button, QPoint(x, y))
    event = QHelpEvent(QEvent.Type.ToolTip, button.rect().center(), cursor)
    QApplication.sendEvent(button, event)
    _wait_tooltip(tooltip)
    assert tooltip.isVisible()
    assert available.contains(tooltip.geometry()), (available, tooltip.geometry())
    assert not tooltip.geometry().contains(cursor)


def test_long_unbroken_media_title_fits_tooltip(tooltip, button):
    available = QGuiApplication.primaryScreen().availableGeometry()
    button.setToolTip("A" * 250 + ".mp4")
    cursor = _show_button(button, available.center())
    QApplication.sendEvent(button, QHelpEvent(QEvent.Type.ToolTip, button.rect().center(), cursor))
    _wait_tooltip(tooltip)
    assert available.contains(tooltip.geometry()), (available, tooltip.geometry())
    assert tooltip.height() > 50


@pytest.mark.parametrize("origin", [QPoint(0, 0), QPoint(-1280, -720), QPoint(1920, 120)])
@pytest.mark.parametrize("corner", ["top_left", "top_right", "bottom_left", "bottom_right"])
def test_tooltip_uses_anchor_monitor_and_fits_every_corner(tooltip, monkeypatch, origin, corner):
    available = QRect(origin, QGuiApplication.primaryScreen().availableGeometry().size())
    screen = SimpleNamespace(availableGeometry=lambda: available)
    anchors = []
    monkeypatch.setattr(QGuiApplication, "screenAt", lambda point: anchors.append(point) or screen)
    x = available.right() - 28 if corner.endswith("right") else available.left()
    y = available.bottom() - 28 if corner.startswith("bottom") else available.top()
    anchor = QRect(x, y, 28, 28)
    QCursor.setPos(anchor.center())
    themed_tooltip.show_themed_tooltip(anchor, "Long description with enough words to wrap. " * 4)
    assert anchors == [anchor.center()]
    assert available.adjusted(8, 8, -8, -8).contains(tooltip.geometry())
    assert not tooltip.geometry().contains(anchor.center())


def test_text_wraps_without_losing_newlines_or_interpreting_markup(tooltip, monkeypatch):
    available = QRect(0, 0, 640, 480)
    monkeypatch.setattr(
        QGuiApplication, "screenAt", lambda _: SimpleNamespace(availableGeometry=lambda: available)
    )
    text = "<media & title>\n" + "日本語👩🏽‍💻é" * 12
    anchor = QRect(200, 440, 28, 28)
    QCursor.setPos(anchor.center())
    themed_tooltip.show_themed_tooltip(anchor, text)
    assert tooltip._document.toPlainText() == text
    assert tooltip._document.size().height() + 26 <= tooltip.height()
    assert available.contains(tooltip.geometry())
    assert tooltip.geometry().bottom() < anchor.top()


def test_overlong_text_uses_whole_lines_and_ellipsis_on_short_screen(tooltip, monkeypatch):
    available = QRect(-320, 0, 320, 180)
    monkeypatch.setattr(
        QGuiApplication, "screenAt", lambda _: SimpleNamespace(availableGeometry=lambda: available)
    )
    text = "A long description 👨‍👩‍👧‍👦 with several lines. " * 80
    themed_tooltip.show_themed_tooltip(QRect(-150, 140, 28, 28), text)
    assert available.adjusted(8, 8, -8, -8).contains(tooltip.geometry())
    assert tooltip._document.toPlainText().endswith("…")
    assert tooltip._document.size().height() + 26 <= tooltip.height()
    assert tooltip.accessibleName() == text


def test_short_tooltip_shrinks_after_long_tooltip(tooltip):
    anchor = QRect(200, 200, 28, 28)
    themed_tooltip.show_themed_tooltip(anchor, "Long description. " * 20)
    size = tooltip.size()
    themed_tooltip.show_themed_tooltip(anchor, "Open")
    assert tooltip.width() < size.width()
    assert tooltip.height() < size.height()
    themed_tooltip.show_themed_tooltip(anchor, "")
    assert not tooltip.isVisible()


@pytest.mark.parametrize("first_route", ["widget", "toolbar"])
def test_toolbar_and_widget_tooltips_share_anchor_and_measured_size(tooltip, button, first_route):
    assert tooltip.windowHandle() is None
    available = QGuiApplication.primaryScreen().availableGeometry()
    cursor = _show_button(button, available.center())
    anchor = QRect(button.mapToGlobal(QPoint()), button.size())
    toolbar = SimpleNamespace(_active_surface=lambda: button)
    other_route = "toolbar" if first_route == "widget" else "widget"
    first_geometry = None
    first_document_size = None
    for index, route in enumerate((first_route, other_route, first_route)):
        assert QRect(button.mapToGlobal(QPoint()), button.size()) == anchor
        assert QCursor.pos() == cursor
        if route == "widget":
            QApplication.sendEvent(
                button, QHelpEvent(QEvent.Type.ToolTip, button.rect().center(), cursor)
            )
        else:
            QuickAccessToolbar._show_native_tooltip(
                toolbar, button.toolTip(), 0, 0, button.width(), button.height()
            )
        _wait_tooltip(tooltip)
        position = themed_tooltip._tooltip_position(
            anchor, tooltip.size(), available.adjusted(8, 8, -8, -8), cursor
        )
        assert position is not None
        expected = QRect(position, tooltip.size())
        wait_until(
            lambda expected=expected: (
                tooltip.geometry() == expected and tooltip.windowHandle().geometry() == expected
            ),
            description=lambda index=index, route=route, expected=expected: (
                f"tooltip placement on show {index + 1} via {route}: expected={expected}, "
                f"widget={tooltip.geometry()}, window={tooltip.windowHandle().geometry()}, "
                f"anchor={anchor}, cursor={QCursor.pos()}, screen={available}, "
                f"margins={tooltip.windowHandle().frameMargins()}, dpr={tooltip.devicePixelRatioF()}"
            ),
        )
        if index == 0:
            first_geometry = tooltip.geometry()
            first_document_size = tooltip._document.size()
        assert tooltip.geometry() == first_geometry
        assert tooltip._document.size() == first_document_size
        themed_tooltip.hide_themed_tooltip()
        assert not tooltip.isVisible()
    assert tooltip.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus


def test_tooltip_does_not_activate_or_dismiss_its_parent_popup(tooltip, button, request):
    host = QWidget()
    request.addfinalizer(lambda: dispose_widget(host))
    show_and_activate(host, size=QSize(320, 240))
    popup = QFrame(host, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
    request.addfinalizer(lambda: dispose_widget(popup))
    layout = QVBoxLayout(popup)
    button.setParent(popup, Qt.WindowType.Widget)
    layout.addWidget(button)
    button.show()
    popup.move(host.mapToGlobal(QPoint(40, 40)))
    popup.show()
    wait_until(
        lambda: (
            popup.windowHandle() is not None
            and popup.windowHandle().isExposed()
            and QApplication.activePopupWidget() is popup
        ),
        description="owned native popup exposure",
    )
    # Cocoa popups remain non-key panels; preserve the active window chosen by
    # the backend rather than requiring the popup itself to activate.
    active_window = QApplication.activeWindow()
    focus_window = QGuiApplication.focusWindow()
    cursor = button.mapToGlobal(button.rect().center())
    QCursor.setPos(cursor)
    wait_until(lambda: QCursor.pos() == cursor, description="pointer over popup tooltip anchor")
    QApplication.sendEvent(button, QHelpEvent(QEvent.Type.ToolTip, button.rect().center(), cursor))
    _wait_tooltip(tooltip)
    assert tooltip.isVisible()
    assert button.isVisible()
    assert popup.isVisible()
    assert QApplication.activePopupWidget() is popup
    assert QApplication.activeWindow() is active_window
    assert QGuiApplication.focusWindow() is focus_window
    assert not tooltip.windowHandle().isActive()
    QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
    assert not tooltip.isVisible()


def test_long_tooltip_on_short_screen_does_not_cover_cursor(tooltip, monkeypatch):
    available = QRect(0, 0, 320, 180)
    monkeypatch.setattr(
        QGuiApplication, "screenAt", lambda _: SimpleNamespace(availableGeometry=lambda: available)
    )
    anchor = QRect(146, 76, 28, 28)
    QCursor.setPos(anchor.center())
    themed_tooltip.show_themed_tooltip(anchor, "A long description with several lines. " * 80)
    assert tooltip.isVisible()
    assert available.contains(tooltip.geometry())
    assert not tooltip.geometry().contains(anchor.center())


def test_wide_anchor_crossing_monitors_uses_cursor_monitor(tooltip, monkeypatch):
    screen = QGuiApplication.primaryScreen()
    available = screen.availableGeometry()
    cursor = available.center()
    QCursor.setPos(cursor)
    wait_until(lambda: QCursor.pos() == cursor, description="pointer on the real anchor monitor")
    references = []
    screen_at = QGuiApplication.screenAt

    def record_screen_at(point):
        references.append(point)
        return screen_at(point)

    monkeypatch.setattr(QGuiApplication, "screenAt", record_screen_at)
    # The anchor spans beyond this monitor, but the pointer is on a real screen.
    anchor = QRect(cursor.x() - 50, cursor.y() - 14, available.width() * 2, 28)
    assert not screen.geometry().contains(anchor.center())
    themed_tooltip.show_themed_tooltip(anchor, "A wide media title")
    _wait_tooltip(tooltip)
    assert references == [cursor]
    assert available.contains(tooltip.geometry())


@pytest.mark.parametrize(("available", "cursor"), [
    (QRect(-1280, 0, 1280, 720), QPoint(-650, 560)),
    (QRect(0, 0, 1280, 720), QPoint(650, 560)),
])
def test_wide_anchor_position_tracks_cursor_on_each_monitor(available, cursor):
    anchor = QRect(-700, 550, 1600, 28)
    size = QSize(180, 40)
    assert anchor.contains(cursor)
    assert QRect(0, 0, 1280, 720).contains(anchor.center())

    position = themed_tooltip._tooltip_position(
        anchor, size, available.adjusted(8, 8, -8, -8), cursor
    )

    assert position is not None
    rect = QRect(position, size)
    assert available.adjusted(8, 8, -8, -8).contains(rect)
    assert not rect.contains(cursor)
    assert rect.left() == cursor.x() - size.width() // 2
