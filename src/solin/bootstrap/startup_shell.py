from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from solin.styles.theme import PALETTE


class _StartupCanvas(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background:{PALETTE.bg0};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)

        icon_label = QLabel(self)
        icon_label.setFixedSize(72, 72)
        icon_label.setPixmap(QApplication.windowIcon().pixmap(72, 72))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet("background:transparent;")
        layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(10)

        title = QLabel("Solin", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"background:transparent;color:{PALETTE.text_primary};"
            "font-size:18px;font-weight:600;"
        )
        layout.addWidget(title)
        layout.addSpacing(20)

        progress = QProgressBar(self)
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setFixedSize(180, 3)
        progress.setStyleSheet(
            "QProgressBar{border:none;"
            f"background:{PALETTE.surface_hover};border-radius:1px;}}"
            "QProgressBar::chunk{"
            f"background:{PALETTE.accent};border-radius:1px;width:54px;}}"
        )
        layout.addWidget(progress, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)


class StartupShellWindow(QMainWindow):
    """Lightweight definitive-size shell shown while the main view is prepared."""

    first_frame_presented = Signal()

    def __init__(self, *, width: int, height: int) -> None:
        super().__init__()
        self._first_frame_presented = False
        self._load_handle: Any = None
        self._target_window: Any = None
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowTitle("Solin")
        self.setMinimumSize(635, 600)
        self.resize(max(635, width), max(600, height))
        self.setCentralWidget(_StartupCanvas(self))
        self._center_on_primary_screen()

    def set_load_handle(self, handle: Any) -> None:
        self._load_handle = handle

    def set_target_window(self, window: Any) -> None:
        self._target_window = window

    def _center_on_primary_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        self.move(
            geometry.x() + (geometry.width() - self.width()) // 2,
            geometry.y() + (geometry.height() - self.height()) // 2,
        )

    def _bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        elif not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._first_frame_presented:
            return
        self._first_frame_presented = True
        from solin.bootstrap.startup_timeline import startup_timeline

        startup_timeline().mark("startup_shell_first_paint")
        self.first_frame_presented.emit()

    def closeEvent(self, event) -> None:
        if self._load_handle is not None:
            self._load_handle.cancel()
        if self._target_window is not None:
            self._target_window.close()
        super().closeEvent(event)


__all__ = ["StartupShellWindow"]
