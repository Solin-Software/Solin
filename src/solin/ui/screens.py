"""Screen enumeration adapter for presentation controllers and widgets."""

from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtCore import QObject, Signal


class ScreenManager(QObject):
    screens_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        app = QGuiApplication.instance()
        app.screenAdded.connect(self._on_screens_changed)
        app.screenRemoved.connect(self._on_screens_changed)

    def _on_screens_changed(self, _screen=None):
        self.screens_changed.emit()

    @staticmethod
    def primary_screen() -> QScreen:
        return QGuiApplication.primaryScreen()

    @staticmethod
    def secondary_screens() -> list[QScreen]:
        primary = QGuiApplication.primaryScreen()
        return [s for s in QGuiApplication.screens() if s != primary]

    @staticmethod
    def all_screens() -> list[QScreen]:
        return QGuiApplication.screens()

    @staticmethod
    def has_secondary() -> bool:
        return len(QGuiApplication.screens()) > 1
