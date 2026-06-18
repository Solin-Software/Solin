"""Camera selection popup used by the quick access toolbar."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QSize,
    QPropertyAnimation,
    Qt,
    Signal,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from solin.core.integrations.camera import CameraService
from solin.core.integrations.camera_options import CameraOption
from solin.core.integrations.automation.settings import CameraSettingsStore
from solin.styles.icons import ICON_CAMERA, ICON_CAST, make_icon


class CameraPopup(QWidget):
    """Floating panel for camera selection and live projection."""

    camera_changed = Signal(object)
    stream_requested = Signal()

    def __init__(
        self,
        camera_service: CameraService,
        settings: CameraSettingsStore,
        parent=None,
    ):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._camera = camera_service
        self._settings = settings
        self._active = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._opacity_eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_eff)
        self._fade = QPropertyAnimation(self._opacity_eff, b"opacity")
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._build_ui()
        self._camera.cameras_ready.connect(self.populate)
        self._camera.started.connect(lambda _name: self.set_stream_active(True))
        self._camera.stopped.connect(lambda: self.set_stream_active(False))
        self._camera.error.connect(self._show_error)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        card = QFrame()
        card.setObjectName("CameraCard")
        card.setStyleSheet(
            "QFrame#CameraCard {"
            "  background: #161b22;"
            "  border: 1px solid rgba(48,54,61,0.85);"
            "  border-radius: 16px;"
            "}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        icon = QLabel()
        icon.setPixmap(make_icon(ICON_CAMERA, 14, "#6e7681").pixmap(14, 14))
        icon.setFixedSize(14, 14)
        icon.setStyleSheet("background: transparent;")
        self._title = QLabel(self.tr("Camera"))
        self._title.setStyleSheet(
            "color: #6e7681; font-size: 10px; font-weight: 700;"
            " letter-spacing: 0.6px; background: transparent;"
        )
        header.addWidget(icon)
        header.addWidget(self._title)
        header.addStretch()
        layout.addLayout(header)

        self._combo = QComboBox()
        self._combo.setMinimumHeight(34)
        self._combo.setStyleSheet(
            "QComboBox { background: #0d1117; color: #e6edf3;"
            " border: 1px solid #30363d; border-radius: 8px;"
            " padding: 0 10px; font-size: 12px; }"
            "QComboBox::drop-down { border: none; width: 26px; }"
            "QComboBox QAbstractItemView { background: #161b22; color: #e6edf3;"
            " border: 1px solid #30363d; selection-background-color: #1f3a6e; }"
        )
        self._combo.currentIndexChanged.connect(self._on_selected)
        layout.addWidget(self._combo)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._refresh_btn = QPushButton(self.tr("Refresh"))
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setFixedHeight(32)
        self._refresh_btn.setStyleSheet(
            "QPushButton { background: #21262d; border: 1px solid #30363d;"
            " border-radius: 8px; color: #c9d1d9; font-size: 12px; padding: 0 12px; }"
            "QPushButton:hover { background: #2d333b; }"
        )
        self._refresh_btn.clicked.connect(self.refresh)

        self._stream_btn = QPushButton()
        self._stream_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stream_btn.setFixedHeight(32)
        self._stream_btn.clicked.connect(self.stream_requested)
        actions.addWidget(self._refresh_btn)
        actions.addWidget(self._stream_btn, stretch=1)
        layout.addLayout(actions)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(0)

        self._status = QLabel("")
        self._status.setWordWrap(False)
        self._status.setStyleSheet(
            "color: #6e7681; font-size: 10px; background: transparent;"
        )

        self._help_link = QLabel()
        self._help_link.setOpenExternalLinks(True)
        self._help_link.setStyleSheet(
            "color: #58a6ff; font-size: 10px; background: transparent;"
        )
        self._help_link.setVisible(False)
        self._update_help_link()

        status_row.addWidget(self._status)
        status_row.addStretch()
        status_row.addWidget(self._help_link)
        layout.addLayout(status_row)

        root.addWidget(card)
        self.setFixedWidth(340)
        self._update_stream_button()

    def refresh(self) -> None:
        self._status.setStyleSheet(
            "color: #6e7681; font-size: 10px; background: transparent;"
        )
        self._status.setText(self.tr("Looking for cameras..."))
        self._help_link.setVisible(False)
        self._camera.refresh_cameras()
        self._refresh_triggered = True

    def populate(self, cameras: list[CameraOption]) -> None:
        saved_backend = self._settings.backend()
        saved_name = self._settings.device_name()
        self._combo.blockSignals(True)
        self._combo.clear()
        if not cameras:
            self._combo.addItem(self.tr("No cameras found"), None)
            self._status.setText(self.tr("No cameras found."))
        else:
            for opt in cameras:
                self._combo.addItem(opt.label, opt)
            selected = 0
            for idx, opt in enumerate(cameras):
                if opt.backend.value == saved_backend and opt.name == saved_name:
                    selected = idx
                    break
            self._combo.setCurrentIndex(selected)
            self._status.setText(self.tr("%n camera(s) found.", None, len(cameras)))
        self._combo.blockSignals(False)
        if getattr(self, "_refresh_triggered", False):
            self._help_link.setVisible(True)
        self.camera_changed.emit(self.selected_camera())

    def selected_camera(self) -> CameraOption | None:
        data = self._combo.currentData()
        return data if isinstance(data, CameraOption) else None

    def set_stream_active(self, active: bool) -> None:
        self._active = bool(active)
        self._update_stream_button()

    def _on_selected(self, _index: int) -> None:
        opt = self.selected_camera()
        if opt is None:
            return
        self._settings.set_device(opt.backend.value, opt.name)
        self.camera_changed.emit(opt)

    def _show_error(self, message: str) -> None:
        self._status.setStyleSheet(
            "color: #f85149; font-size: 10px; background: transparent;"
        )
        self._status.setText(message)
        self.set_stream_active(False)

    def _update_stream_button(self) -> None:
        if self._active:
            icon_color = "#f85149"
            text = self.tr("Stop Stream")
            bg = "rgba(248,81,73,0.08)"
            border = "rgba(248,81,73,0.30)"
            hover_bg = "rgba(248,81,73,0.15)"
            hover_border = "rgba(248,81,73,0.45)"
        else:
            icon_color = "#58a6ff"
            text = self.tr("Show Stream")
            bg = "rgba(56,139,253,0.08)"
            border = "rgba(56,139,253,0.28)"
            hover_bg = "rgba(56,139,253,0.15)"
            hover_border = "rgba(56,139,253,0.45)"
        self._stream_btn.setIcon(make_icon(ICON_CAST, 14, icon_color))
        self._stream_btn.setIconSize(QSize(14, 14))
        self._stream_btn.setText(text)
        self._stream_btn.setStyleSheet(
            "QPushButton {"
            f" background: {bg}; border: 1px solid {border}; border-radius: 8px;"
            " color: #c9d1d9; font-size: 12px; font-weight: 500; padding: 0 12px;"
            "}"
            f"QPushButton:hover {{ background: {hover_bg}; border-color: {hover_border}; }}"
        )

    def show_above(self, anchor: QWidget) -> None:
        if not self._camera.known_cameras():
            self.refresh()
        self.adjustSize()
        global_pos = anchor.mapToGlobal(anchor.rect().topLeft())
        x = global_pos.x() + (anchor.width() - self.width()) // 2
        y = global_pos.y() - self.height() - 10
        screen = anchor.screen() if hasattr(anchor, "screen") else None
        screen_geo = (
            screen.availableGeometry()
            if screen is not None
            else QGuiApplication.primaryScreen().availableGeometry()
        )
        if x < screen_geo.left() + 4:
            x = screen_geo.left() + 4
        if x + self.width() > screen_geo.right() - 4:
            x = screen_geo.right() - self.width() - 4
        if y < screen_geo.top() + 4:
            y = global_pos.y() + anchor.height() + 10
        self.move(x, y)
        self._opacity_eff.setOpacity(0.0)
        self.show()
        self.raise_()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self._title.setText(self.tr("Camera"))
            self._refresh_btn.setText(self.tr("Refresh"))
            self._update_stream_button()
            self._update_help_link()
        super().changeEvent(event)

    def _update_help_link(self) -> None:
        url = "https://solinav.vercel.app/faq#why-doesnt-obs-virtual-camera-appear"
        need_help = self.tr("Need help?")
        self._help_link.setText(
            f'<a href="{url}" style="color:#58a6ff; text-decoration:none;">'
            f"{need_help}</a>"
        )
