"""Top-level window hosting the Live meeting console (hosted meetings).

The console is a ``QQuickView`` in a window container rather than a ``QQuickWidget``:
a QQuickWidget renders into an offscreen buffer on the UI thread and reallocates it on
every resize step, which made resizing this window lag. The view renders on its own
thread like any Qt Quick window.

The console is also the main window's right panel: docking moves the body holding the
view into the host's strip and hides this window, and the QML switches to its vertical
layout when it is that thin. Only the body moves: reparenting an already shown top-level
leaves its old native window on screen as an empty phantom.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QSurfaceFormat
from PySide6.QtQuick import QQuickView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from solin.core.hosted_meeting.service import HostedMeetingService
from solin.styles.theme import PALETTE
from solin.ui.qml.host import configure_qml_controls_style, current_qml_theme
from solin.ui.qml.loader import QmlLoadHandle
from solin.ui.qml.meeting_console_bridge import MeetingConsoleBridge


class MeetingConsoleWindow(QWidget):
    docked_changed = Signal(bool)

    def __init__(
        self,
        service: HostedMeetingService,
        parent: QWidget | None = None,
        *,
        dock_host: Callable[[], QWidget] | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._dock_host = dock_host
        self._docked = False
        self.setWindowTitle(self.tr("Live meeting"))
        self.resize(1080, 680)
        self.bridge = MeetingConsoleBridge(service, self, dockable=dock_host is not None)
        # Queued: the request comes from a QML click, and docking reparents that QML's window.
        self.bridge.dockRequested.connect(self.set_docked, Qt.ConnectionType.QueuedConnection)
        configure_qml_controls_style()
        self._view = QQuickView()
        surface_format = QSurfaceFormat()
        surface_format.setAlphaBufferSize(8)
        self._view.setFormat(surface_format)
        self._view.setColor(QColor(PALETTE.bg0))
        self._view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        context = self._view.rootContext()
        context.setContextProperty("appTheme", current_qml_theme())
        context.setContextProperty("meeting", self.bridge)
        self.qml_load_handle = QmlLoadHandle(self._view, "MeetingConsoleView", asynchronous=False)
        self._body = QWidget(self)
        container = QWidget.createWindowContainer(self._view, self._body)
        container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        container.setMinimumSize(300, 200)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(container)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._body)

    @property
    def view(self) -> QQuickView:
        return self._view

    @property
    def docked(self) -> bool:
        return self._docked

    def set_docked(self, docked: bool) -> None:
        """Attach to the main window's right strip, or return to a floating window."""
        docked = bool(docked)
        if docked == self._docked or (docked and self._dock_host is None):
            return
        if docked:
            assert self._dock_host is not None
            host = self._dock_host()
            host.layout().addWidget(self._body)
            host.show()
            self.hide()
        else:
            host = self._body.parentWidget()
            self.layout().addWidget(self._body)
            host.hide()
            self.show()
        self._body.show()
        self._docked = docked
        self.bridge.set_docked(docked)
        self.qml_load_handle.start()
        self.docked_changed.emit(docked)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.qml_load_handle.start()

    def apply_theme(self) -> None:
        configure_qml_controls_style()
        self._view.rootContext().setContextProperty("appTheme", current_qml_theme())
        self._view.setColor(QColor(PALETTE.bg0))

    def present(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
