"""Non-modal toolbar panels with dismissal scoped to their host window."""

from __future__ import annotations

from weakref import ReferenceType, WeakKeyDictionary, ref

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QGuiApplication, QHideEvent, QKeyEvent, QMouseEvent, QWindow
from PySide6.QtQuick import QQuickItem
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from solin.ui.native_input import NATIVE_POINTER_PRESS

_PRESS_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.NonClientAreaMouseButtonPress,
    NATIVE_POINTER_PRESS,
)


class ToolbarPanel(QWidget):
    _open_panels: WeakKeyDictionary[QWidget, ReferenceType[ToolbarPanel]] = WeakKeyDictionary()

    def __init__(self, parent: QWidget | None = None) -> None:
        # Wayland delegates the position of top-level windows to the compositor.
        # Host an anchored panel inside the window on that platform.
        self._embedded = parent is not None and QGuiApplication.platformName().startswith("wayland")
        self._floating_parent = parent.window() if self._embedded and parent is not None else parent
        self._floating_flags = (
            Qt.WindowType.Widget
            if self._embedded
            else Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        super().__init__(self._floating_parent, self._floating_flags)
        if parent is not None and parent is not self._floating_parent:
            parent.destroyed.connect(self.deleteLater)
        self._host: QWidget | None = None
        self._anchor: QWidget | None = None
        self._trigger: QQuickItem | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @classmethod
    def open_for(cls, host: QWidget) -> ToolbarPanel | None:
        panel_ref = cls._open_panels.get(host)
        panel = panel_ref() if panel_ref is not None else None
        return panel if panel is not None and isValid(panel) else None

    def _available_geometry(self, anchor: QWidget) -> QRect:
        if self._embedded and self._floating_parent is not None:
            host = self._floating_parent
            return QRect(host.mapToGlobal(QPoint()), host.size())
        screen = QGuiApplication.screenAt(anchor.mapToGlobal(anchor.rect().center()))
        return (screen or anchor.screen()).availableGeometry()

    def _panel_position(self, x: int, y: int) -> QPoint:
        position = QPoint(x, y)
        if self._embedded and self._floating_parent is not None:
            return self._floating_parent.mapFromGlobal(position)
        return position

    def _show_anchored(self, anchor: QWidget, *, trigger: QQuickItem | None = None) -> None:
        parent = self.parentWidget()
        host = parent.window() if parent is not None else anchor.window()
        previous = self.open_for(host)
        if previous is not None and previous is not self:
            previous.hide()
        self.show()
        if not self.isVisible():
            return
        self._host = host
        self._anchor = anchor
        self._trigger = trigger
        self._open_panels[host] = ref(self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.raise_()
        if self.isWindow():
            self.activateWindow()
        else:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        if event.spontaneous():
            return
        if self._host is not None and self.open_for(self._host) is self:
            del self._open_panels[self._host]
        self._host = self._anchor = self._trigger = None
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind not in (QEvent.Type.Hide, QEvent.Type.WindowActivate) and kind not in _PRESS_EVENTS:
            return False
        host, anchor = self._host, self._anchor
        if host is None or anchor is None or not isValid(host) or not isValid(anchor):
            return False
        if kind == QEvent.Type.Hide:
            if not event.spontaneous() and (watched is host or watched is anchor):
                self.hide()
        elif kind == QEvent.Type.WindowActivate:
            if watched is host:
                self.raise_()
        elif isinstance(watched, QWidget):
            in_host = watched.window() in (host, anchor.window())
            in_panel = watched is self or self.isAncestorOf(watched)
            if in_host and not in_panel and not self._is_trigger_press(watched, event):
                self.hide()
        elif (
            kind == QEvent.Type.NonClientAreaMouseButtonPress
            and isinstance(watched, QWindow)
            and watched is host.windowHandle()
        ):
            self.hide()
        return False

    def _is_trigger_press(self, watched: QWidget, event: QEvent) -> bool:
        # The toolbar button handles its own toggle on release.
        trigger = self._trigger
        return (
            watched is self._anchor
            and trigger is not None
            and isValid(trigger)
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
            and trigger.isVisible()
            and trigger.contains(trigger.mapFromScene(event.position()))
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and (self.isWindow() or self._host is not None):
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)
