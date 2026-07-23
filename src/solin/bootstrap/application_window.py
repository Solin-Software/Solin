from __future__ import annotations

from enum import Enum
import logging
from typing import Any

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QProgressBar,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from solin.styles.theme import PALETTE


log = logging.getLogger(__name__)


class ApplicationWindowState(str, Enum):
    LOADING = "loading"
    HYDRATING = "hydrating"
    READY = "ready"
    CLOSING = "closing"


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


class ApplicationWindow(QMainWindow):
    """The single native window used from the loading frame until shutdown."""

    loading_frame_presented = Signal()
    application_frame_presented = Signal()
    switch_profile_requested = Signal()

    def __init__(
        self,
        *,
        width: int,
        height: int,
        pending_files: list[str],
    ) -> None:
        super().__init__()
        self._state = ApplicationWindowState.LOADING
        self._loading_frame_presented = False
        self._application_frame_ready = False
        self._loading_native_id: int | None = None
        self._load_handle: Any = None
        self._runtime: Any = None
        self._runtime_candidate: Any = None
        self._pending_files = pending_files

        self.setWindowTitle("Solin")
        self.setMinimumSize(635, 600)
        self.resize(max(635, width), max(600, height))

        self._content_host = QWidget(self)
        # The first QQuickWidget changes the native surface requirements of its
        # top-level window. If it is created after show(), Qt must recreate the
        # visible HWND on Windows. A source-less 1×1 child lets Qt select and
        # own the correct RHI surface before the first native frame, without
        # manual backend assumptions or a full-size extra render target.
        self._quick_surface_anchor = QQuickWidget(self._content_host)
        self._quick_surface_anchor.setClearColor(QColor(PALETTE.bg0))
        self._quick_surface_anchor.setFixedSize(1, 1)
        self._quick_surface_anchor.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
        )
        self._content_stack = QStackedLayout(self._content_host)
        self._content_stack.setContentsMargins(0, 0, 0, 0)
        self._content_stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._loading_canvas = _StartupCanvas(self._content_host)
        self._content_stack.addWidget(self._loading_canvas)
        self._content_stack.setCurrentWidget(self._loading_canvas)
        self.setCentralWidget(self._content_host)
        self._center_on_primary_screen()
        self.loading_frame_presented.connect(
            self._apply_titlebar_color,
            Qt.ConnectionType.QueuedConnection,
        )

    @property
    def state(self) -> ApplicationWindowState:
        return self._state

    @property
    def runtime(self) -> Any:
        return self._runtime

    def content_parent(self) -> QWidget:
        """Return the stable parent used while constructing application content."""

        return self._content_host

    def set_load_handle(self, handle: Any) -> None:
        self._load_handle = handle

    def begin_hydration(self) -> bool:
        if self._state is not ApplicationWindowState.LOADING:
            return False
        self._state = ApplicationWindowState.HYDRATING
        return True

    def register_runtime_candidate(self, runtime: Any) -> None:
        if self._state is not ApplicationWindowState.HYDRATING:
            raise RuntimeError("Application runtime construction requires hydration state.")
        if self._runtime_candidate is not None:
            raise RuntimeError("An application runtime is already being constructed.")
        self._runtime_candidate = runtime

    def abort_runtime_construction(self) -> None:
        runtime = self._runtime_candidate
        self._runtime_candidate = None
        if runtime is None:
            return
        runtime.abort_construction()
        runtime.deleteLater()

    def install_runtime(self, runtime: Any) -> None:
        if self._state is not ApplicationWindowState.HYDRATING:
            raise RuntimeError(
                f"Cannot install application content while window is {self._state.value}."
            )
        if runtime.parentWidget() is not self._content_host:
            raise RuntimeError("Application content must be built for the stable window host.")
        if self._runtime_candidate is not runtime:
            raise RuntimeError("Application content is not the registered runtime candidate.")

        runtime.commit_construction()
        self._runtime_candidate = None
        self._runtime = runtime
        runtime.first_frame_presented.connect(self._on_application_frame)
        runtime.switch_profile_requested.connect(self.switch_profile_requested)
        self._content_stack.addWidget(runtime)
        self._content_stack.setCurrentWidget(self._loading_canvas)
        runtime.show()

    def complete_startup_handoff(self) -> None:
        if (
            self._state is not ApplicationWindowState.HYDRATING
            or not self._application_frame_ready
            or self._runtime is None
        ):
            return
        self._runtime.complete_startup_handoff()
        self._state = ApplicationWindowState.READY
        self._flush_pending_files()

    def _flush_pending_files(self) -> None:
        if not self._pending_files:
            return
        paths = list(self._pending_files)
        self._pending_files.clear()
        self._runtime.open_media_files(paths)

    def open_media_files(self, paths: list[str]) -> None:
        if self._state is ApplicationWindowState.READY and self._runtime is not None:
            self._runtime.open_media_files(paths)
            return
        for path in paths:
            if path not in self._pending_files:
                self._pending_files.append(path)

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

    def _apply_titlebar_color(self) -> None:
        from solin.ui.titlebar import apply_titlebar_color

        apply_titlebar_color(self, PALETTE.titlebar)

    def _on_application_frame(self) -> None:
        if self._state is not ApplicationWindowState.HYDRATING:
            return
        current_native_id = int(self.winId())
        if (
            self._loading_native_id is not None
            and current_native_id != self._loading_native_id
        ):
            log.error(
                "Application window native identity changed during hydration "
                "(loading=%s, application=%s).",
                self._loading_native_id,
                current_native_id,
                stack_info=True,
            )
        if not self.isVisible():
            log.error(
                "Application window is hidden at the application handoff.",
                stack_info=True,
            )
        self._application_frame_ready = True
        self._load_handle = None
        self._content_stack.setCurrentWidget(self._runtime)
        self._content_stack.removeWidget(self._loading_canvas)
        self._loading_canvas.deleteLater()
        self.application_frame_presented.emit()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._loading_frame_presented:
            return
        self._loading_frame_presented = True
        self._loading_native_id = int(self.winId())
        from solin.bootstrap.startup_timeline import startup_timeline

        startup_timeline().mark("loading_first_paint")
        self.loading_frame_presented.emit()

    def hideEvent(self, event) -> None:
        if self._state is not ApplicationWindowState.CLOSING:
            log.error(
                "Application window was hidden unexpectedly during %s.",
                self._state.value,
                stack_info=True,
            )
        super().hideEvent(event)

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        if (
            event.type() == QEvent.Type.WinIdChange
            and self._state is not ApplicationWindowState.CLOSING
            and self._loading_native_id is not None
            and int(self.effectiveWinId()) != self._loading_native_id
        ):
            log.error(
                "Application window received WinIdChange during %s.",
                self._state.value,
                stack_info=True,
            )
        return handled

    def closeEvent(self, event) -> None:
        if self._state is ApplicationWindowState.CLOSING:
            super().closeEvent(event)
            return
        self._state = ApplicationWindowState.CLOSING
        if self._load_handle is not None:
            self._load_handle.cancel()
            self._load_handle = None
        self.abort_runtime_construction()
        if self._runtime is not None:
            self._runtime.shutdown()
        super().closeEvent(event)


__all__ = ["ApplicationWindow", "ApplicationWindowState"]
