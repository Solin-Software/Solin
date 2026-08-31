from __future__ import annotations

import pytest
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QHelpEvent
from PySide6.QtWidgets import QApplication

from solin.ui import themed_tooltip
from solin.widgets.common.popup_hover_button import PopupHoverButton
from solin.widgets.quick_access_toolbar import QuickAccessToolbar


@pytest.fixture
def tooltip(monkeypatch):
    popup = themed_tooltip._ThemedTooltipPopup()
    monkeypatch.setattr(themed_tooltip, "_POPUP", popup)
    yield popup
    popup.close()
    popup.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def button():
    widget = PopupHoverButton()
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


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
    button.move(x, y)
    button.show()
    cursor = button.mapToGlobal(button.rect().center())
    QCursor.setPos(cursor)
    event = QHelpEvent(QEvent.Type.ToolTip, button.rect().center(), cursor)
    QApplication.sendEvent(button, event)
    assert tooltip.isVisible()
    assert available.contains(tooltip.geometry()), (available, tooltip.geometry())
    assert not tooltip.geometry().contains(cursor)


def test_long_unbroken_media_title_fits_tooltip(tooltip, button):
    available = QGuiApplication.primaryScreen().availableGeometry()
    button.setToolTip("A" * 250 + ".mp4")
    button.move(available.center())
    button.show()
    cursor = button.mapToGlobal(button.rect().center())
    QCursor.setPos(cursor)
    QApplication.sendEvent(button, QHelpEvent(QEvent.Type.ToolTip, button.rect().center(), cursor))
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


def test_toolbar_and_widget_tooltips_share_anchor_and_measured_size(tooltip, button):
    button.move(350, 350)
    button.show()
    cursor = button.mapToGlobal(button.rect().center())
    QCursor.setPos(cursor)
    QApplication.sendEvent(button, QHelpEvent(QEvent.Type.ToolTip, button.rect().center(), cursor))
    native_geometry = tooltip.geometry()
    themed_tooltip.hide_themed_tooltip()
    toolbar = SimpleNamespace(_active_surface=lambda: button)
    QuickAccessToolbar._show_native_tooltip(
        toolbar, button.toolTip(), 0, 0, button.width(), button.height()
    )
    assert tooltip.geometry() == native_geometry
    assert tooltip.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus


def test_tooltip_does_not_activate_or_dismiss_its_parent_popup(tooltip, button):
    button.setWindowFlags(Qt.WindowType.Popup)
    button.move(350, 350)
    button.show()
    cursor = button.mapToGlobal(button.rect().center())
    QCursor.setPos(cursor)
    QApplication.sendEvent(button, QHelpEvent(QEvent.Type.ToolTip, button.rect().center(), cursor))
    assert tooltip.isVisible()
    assert button.isVisible()
    assert QApplication.activePopupWidget() is button
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
    left = QRect(-1280, 0, 1280, 720)
    right = QRect(0, 0, 1280, 720)
    monkeypatch.setattr(
        QGuiApplication,
        "screenAt",
        lambda point: SimpleNamespace(
            availableGeometry=lambda: left if left.contains(point) else right
        ),
    )
    QCursor.setPos(QPoint(-650, 560))
    themed_tooltip.show_themed_tooltip(QRect(-700, 550, 1600, 28), "A wide media title")
    assert left.contains(tooltip.geometry())
