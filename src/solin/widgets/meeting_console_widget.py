"""Host for the Solin-hosted meeting console (``MeetingConsoleView.qml``)."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QUrl
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QVBoxLayout, QWidget

from solin.styles.theme import PALETTE
from solin.ui.qml.host import apply_qml_theme, configure_qml_host
from solin.ui.qml.meeting_console_bridge import MeetingConsoleBridge

from ..core.foundation.exception_logging import log_ignored_exception


class MeetingConsoleWidget(QWidget):
    """The operator's view of the meeting: lobby, attendees and raised hands."""

    def __init__(self, *, bridge: MeetingConsoleBridge, defer_qml: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.bridge.setParent(self)
        self._qml = QQuickWidget(self)
        self.qml_load_handle = configure_qml_host(
            self._qml,
            type_name="MeetingConsoleView",
            clear_color=PALETTE.bg0,
            context_properties={"meetingConsole": self.bridge},
            mouse_tracking=True,
            defer_load=defer_qml,
            dismiss_text_focus_on_pointer_press=True,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._qml)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.qml_load_handle.start()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        try:
            self._qml.engine().retranslate()
        except Exception:  # noqa: BLE001 - QML engine lifecycle boundary
            log_ignored_exception(__name__, "Could not retranslate the meeting console")

    def apply_theme(self) -> None:
        apply_qml_theme(self._qml, clear_color=PALETTE.bg0)

    def cleanup(self) -> None:
        """Clear the QML scene before the bridge goes away (ShutdownController)."""
        self.qml_load_handle.cancel()
        try:
            self._qml.setSource(QUrl())
        except Exception:  # noqa: BLE001 - QML engine lifecycle boundary
            log_ignored_exception(__name__, "Could not clear the meeting console QML source")
