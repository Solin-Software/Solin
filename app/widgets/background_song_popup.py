"""Floating control panel for automatic background songs."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QSize, QPropertyAnimation, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.jw.background_song_service import BackgroundSongService
from app.styles.icons import (
    ICON_MUSIC,
    ICON_PAUSE,
    ICON_PLAY,
    ICON_SKIP_NEXT,
    ICON_VOLUME_HIGH,
    make_icon,
)

_SURF = "#161b22"
_BORDER = "#30363d"
_TEXT = "#e6edf3"
_MUTED = "#8b949e"
_DIM = "#6e7681"
_ACCENT = "#58a6ff"
_RED = "#f85149"


class BackgroundSongPopup(QWidget):
    """Small popup shown from the quick access toolbar."""

    _POP_W = 360

    def __init__(self, service: BackgroundSongService, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._service = service
        self._playing = service.is_playing
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._opacity_eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_eff)
        self._fade = QPropertyAnimation(self._opacity_eff, b"opacity")
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._build_ui()
        self._connect_service()
        self._sync_all()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        card = QFrame()
        card.setObjectName("BackgroundSongCard")
        card.setStyleSheet(
            "QFrame#BackgroundSongCard {"
            f" background: {_SURF};"
            " border: 1px solid rgba(48,54,61,0.85);"
            " border-radius: 16px;"
            "}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        icon = QLabel()
        icon.setPixmap(make_icon(ICON_MUSIC, 15, _ACCENT).pixmap(15, 15))
        icon.setFixedSize(15, 15)
        icon.setStyleSheet("background: transparent;")
        self._title_lbl = QLabel(self.tr("Background Song"))
        self._title_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 13px; font-weight: 600;"
            " background: transparent;"
        )
        header.addWidget(icon)
        header.addWidget(self._title_lbl)
        header.addStretch()
        layout.addLayout(header)

        self._song_lbl = QLabel("")
        self._song_lbl.setWordWrap(True)
        self._song_lbl.setMinimumHeight(34)
        self._song_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 12px; font-weight: 500;"
            " background: transparent;"
        )
        layout.addWidget(self._song_lbl)

        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            f"color: {_DIM}; font-size: 10px; background: transparent;"
        )
        layout.addWidget(self._status_lbl)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._toggle_btn = QPushButton()
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setFixedHeight(36)
        self._toggle_btn.clicked.connect(self._service.toggle_playback)
        actions.addWidget(self._toggle_btn, stretch=1)

        self._next_btn = QPushButton()
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setFixedSize(42, 36)
        self._next_btn.setToolTip(self.tr("Next song"))
        self._next_btn.setIcon(make_icon(ICON_SKIP_NEXT, 14, _MUTED))
        self._next_btn.setIconSize(QSize(14, 14))
        self._next_btn.clicked.connect(self._service.skip)
        self._next_btn.setStyleSheet(self._secondary_button_style())
        actions.addWidget(self._next_btn)
        layout.addLayout(actions)

        layout.addWidget(self._divider())

        volume_row = QHBoxLayout()
        volume_row.setSpacing(10)
        self._volume_icon = QLabel()
        self._volume_icon.setPixmap(make_icon(ICON_VOLUME_HIGH, 15, _MUTED).pixmap(15, 15))
        self._volume_icon.setFixedSize(16, 16)
        self._volume_icon.setStyleSheet("background: transparent;")
        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setStyleSheet(self._slider_style())
        self._volume_value_lbl = QLabel("")
        self._volume_value_lbl.setFixedWidth(42)
        self._volume_value_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._volume_value_lbl.setStyleSheet(
            f"color: {_MUTED}; font-size: 11px; background: transparent;"
        )
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        volume_row.addWidget(self._volume_icon)
        volume_row.addWidget(self._volume_slider, stretch=1)
        volume_row.addWidget(self._volume_value_lbl)
        layout.addLayout(volume_row)

        fade_row = QHBoxLayout()
        fade_row.setSpacing(10)
        self._fade_lbl = QLabel(self.tr("Fade"))
        self._fade_lbl.setStyleSheet(
            f"color: {_MUTED}; font-size: 11px; background: transparent;"
        )
        self._fade_spin = QSpinBox()
        self._fade_spin.setRange(0, 30)
        self._fade_spin.setSuffix(" s")
        self._fade_spin.setMinimumHeight(32)
        self._fade_spin.setFixedWidth(82)
        self._fade_spin.setStyleSheet(self._spin_style())
        self._fade_spin.valueChanged.connect(self._service.set_fade_seconds)
        self._fade_hint_lbl = QLabel(self.tr("Fade finishes before meeting start."))
        self._fade_hint_lbl.setWordWrap(True)
        self._fade_hint_lbl.setStyleSheet(
            f"color: {_DIM}; font-size: 10px; background: transparent;"
        )
        fade_row.addWidget(self._fade_lbl)
        fade_row.addWidget(self._fade_spin)
        fade_row.addWidget(self._fade_hint_lbl, stretch=1)
        layout.addLayout(fade_row)

        root.addWidget(card)
        self.setFixedWidth(self._POP_W)

    def _connect_service(self) -> None:
        self._service.playback_changed.connect(self._set_playing)
        self._service.current_song_changed.connect(self._set_current_song)
        self._service.status_changed.connect(self._status_lbl.setText)
        self._service.volume_changed.connect(self._set_volume)
        self._service.fade_seconds_changed.connect(self._set_fade_seconds)
        self._service.availability_changed.connect(lambda _available: self._sync_buttons())

    def _sync_all(self) -> None:
        self._set_playing(self._service.is_playing)
        self._set_current_song(self._service.current_title)
        self._status_lbl.setText(self._service.status_text)
        self._set_volume(self._service.volume_percent)
        self._set_fade_seconds(self._service.fade_seconds)
        self._sync_buttons()

    def _set_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        self._sync_buttons()

    def _set_current_song(self, title: str) -> None:
        self._song_lbl.setText(title or self.tr("No song playing"))

    def _set_volume(self, value: int) -> None:
        self._volume_slider.blockSignals(True)
        self._volume_slider.setValue(int(value))
        self._volume_slider.blockSignals(False)
        self._volume_value_lbl.setText(f"{int(value)}%")

    def _set_fade_seconds(self, value: int) -> None:
        self._fade_spin.blockSignals(True)
        self._fade_spin.setValue(int(value))
        self._fade_spin.blockSignals(False)

    def _sync_buttons(self) -> None:
        if self._playing:
            self._toggle_btn.setText(self.tr("Stop"))
            self._toggle_btn.setIcon(make_icon(ICON_PAUSE, 14, _RED))
            self._toggle_btn.setStyleSheet(self._danger_button_style())
        else:
            self._toggle_btn.setText(self.tr("Start"))
            self._toggle_btn.setIcon(make_icon(ICON_PLAY, 14, _ACCENT))
            self._toggle_btn.setStyleSheet(self._primary_button_style())
        self._toggle_btn.setIconSize(QSize(14, 14))
        self._next_btn.setEnabled(self._service.has_songs)

    def _on_volume_changed(self, value: int) -> None:
        self._volume_value_lbl.setText(f"{value}%")
        self._service.set_volume_percent(value)

    def show_above(self, anchor: QWidget) -> None:
        self._sync_all()
        self.adjustSize()
        global_pos = anchor.mapToGlobal(anchor.rect().topLeft())
        x = global_pos.x() + (anchor.width() - self.width()) // 2
        y = global_pos.y() - self.height() - 10
        screen_geo = (
            anchor.screen().availableGeometry()
            if hasattr(anchor, "screen") and anchor.screen() is not None
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
            self._title_lbl.setText(self.tr("Background Song"))
            self._next_btn.setToolTip(self.tr("Next song"))
            self._fade_lbl.setText(self.tr("Fade"))
            self._fade_hint_lbl.setText(self.tr("Fade finishes before meeting start."))
            self._set_current_song(self._service.current_title)
            self._sync_buttons()
        super().changeEvent(event)

    @staticmethod
    def _divider() -> QFrame:
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: rgba(48,54,61,0.55); border: none;")
        return divider

    @staticmethod
    def _primary_button_style() -> str:
        return (
            "QPushButton { background: rgba(56,139,253,0.10);"
            " border: 1px solid rgba(56,139,253,0.28); border-radius: 10px;"
            f" color: {_ACCENT}; font-size: 12px; font-weight: 600; padding: 0 14px;"
            "}"
            "QPushButton:hover { background: rgba(56,139,253,0.18);"
            " border-color: rgba(56,139,253,0.45); }"
        )

    @staticmethod
    def _danger_button_style() -> str:
        return (
            "QPushButton { background: rgba(248,81,73,0.08);"
            " border: 1px solid rgba(248,81,73,0.30); border-radius: 10px;"
            f" color: {_TEXT}; font-size: 12px; font-weight: 600; padding: 0 14px;"
            "}"
            "QPushButton:hover { background: rgba(248,81,73,0.15);"
            " border-color: rgba(248,81,73,0.45); }"
        )

    @staticmethod
    def _secondary_button_style() -> str:
        return (
            "QPushButton { background: #21262d; border: 1px solid #30363d;"
            " border-radius: 10px; color: #c9d1d9; }"
            "QPushButton:hover { background: #2d333b; }"
            "QPushButton:disabled { color: #484f58; background: transparent; }"
        )

    @staticmethod
    def _slider_style() -> str:
        return (
            "QSlider::groove:horizontal{height:4px;background:#30363d;border-radius:2px;}"
            "QSlider::handle:horizontal{width:12px;height:12px;margin:-4px 0;"
            "background:#c9d1d9;border-radius:6px;}"
            "QSlider::sub-page:horizontal{background:#58a6ff;border-radius:2px;}"
        )

    @staticmethod
    def _spin_style() -> str:
        return (
            "QSpinBox { background: #0d1117; color: #e6edf3;"
            " border: 1px solid #30363d; border-radius: 8px;"
            " padding: 0 8px; font-size: 12px; }"
            "QSpinBox:focus { border-color: #58a6ff; }"
            "QSpinBox::up-button, QSpinBox::down-button { width: 0px; border: none; }"
        )
