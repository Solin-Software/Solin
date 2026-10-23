"""Top-level window hosting the Live meeting console (hosted meetings)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QVBoxLayout, QWidget

from solin.core.hosted_meeting.service import HostedMeetingService
from solin.styles.theme import PALETTE
from solin.ui.qml.host import apply_qml_theme, configure_qml_host
from solin.ui.qml.meeting_console_bridge import MeetingConsoleBridge


class MeetingConsoleWindow(QWidget):
    def __init__(self, service: HostedMeetingService, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(self.tr("Live meeting"))
        self.resize(1080, 680)
        self.bridge = MeetingConsoleBridge(service, self)
        self._qml = QQuickWidget(self)
        self.qml_load_handle = configure_qml_host(
            self._qml, type_name="MeetingConsoleView", clear_color=PALETTE.bg0,
            context_properties={"meeting": self.bridge}, mouse_tracking=True,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._qml)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        start = getattr(self.qml_load_handle, "start", None)
        if callable(start):
            start()

    def apply_theme(self) -> None:
        apply_qml_theme(self._qml)

    def present(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
