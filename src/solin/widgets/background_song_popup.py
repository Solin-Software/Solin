"""Floating control panel for automatic background songs."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractSpinBox,
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

from solin.core.jw.background_song_service import BackgroundSongService
from solin.styles.icons import (
    ICON_MUSIC,
    ICON_PAUSE,
    ICON_PLAY,
    ICON_SKIP_NEXT,
    ICON_VOLUME_HIGH,
    ICON_VOLUME_LOW,
    ICON_VOLUME_MUTE,
    make_icon,
)

# ── Palette (GitHub-dark, shared with sibling popups) ─────────────────────────
_SURF = "#161b22"
_TEXT = "#e6edf3"
_SUBTLE = "#c9d1d9"
_MUTED = "#8b949e"
_DIM = "#6e7681"
_ACCENT = "#58a6ff"
_RED = "#f85149"


class BackgroundSongPopup(QWidget):
    """Small popup shown from the quick access toolbar."""

    _POP_W = 332
    # Width available to the song title once card padding is removed.
    _SONG_W = _POP_W - 36

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

    # ── Construction ──────────────────────────────────────────────────────────
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
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(16)

        layout.addLayout(self._build_header())
        layout.addLayout(self._build_now_playing())
        layout.addLayout(self._build_controls())
        layout.addLayout(self._build_volume())
        layout.addWidget(self._divider())
        layout.addLayout(self._build_timing())

        root.addWidget(card)
        self.setFixedWidth(self._POP_W)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(9)
        icon = QLabel()
        icon.setPixmap(make_icon(ICON_MUSIC, 14, _DIM).pixmap(14, 14))
        icon.setFixedSize(14, 14)
        icon.setStyleSheet("background: transparent;")
        self._title_lbl = QLabel(self.tr("Background Song"))
        self._title_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        header.addWidget(icon)
        header.addWidget(self._title_lbl)
        header.addStretch()
        return header

    def _build_now_playing(self) -> QVBoxLayout:
        block = QVBoxLayout()
        block.setSpacing(3)
        self._song_lbl = QLabel("")
        self._song_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 14px; font-weight: 600; background: transparent;"
        )
        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            f"color: {_MUTED}; font-size: 11px; background: transparent;"
        )
        block.addWidget(self._song_lbl)
        block.addWidget(self._status_lbl)
        return block

    def _build_controls(self) -> QHBoxLayout:
        actions = QHBoxLayout()
        actions.setSpacing(10)

        self._toggle_btn = QPushButton()
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setFixedHeight(38)
        self._toggle_btn.setIconSize(QSize(14, 14))
        self._toggle_btn.clicked.connect(self._service.toggle_playback)
        actions.addWidget(self._toggle_btn, stretch=1)

        self._next_btn = QPushButton()
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setFixedSize(48, 38)
        self._next_btn.setToolTip(self.tr("Next song"))
        self._next_btn.setIcon(make_icon(ICON_SKIP_NEXT, 14, _SUBTLE))
        self._next_btn.setIconSize(QSize(14, 14))
        self._next_btn.setStyleSheet(self._ghost_button_style())
        self._next_btn.clicked.connect(self._service.skip)
        actions.addWidget(self._next_btn)
        return actions

    def _build_volume(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(11)
        self._volume_icon = QLabel()
        self._volume_icon.setFixedSize(16, 16)
        self._volume_icon.setStyleSheet("background: transparent;")
        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self._volume_slider.setStyleSheet(self._slider_style())
        self._volume_value_lbl = QLabel("")
        self._volume_value_lbl.setFixedWidth(34)
        self._volume_value_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._volume_value_lbl.setStyleSheet(
            f"color: {_MUTED}; font-size: 11px; background: transparent;"
        )
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        row.addWidget(self._volume_icon)
        row.addWidget(self._volume_slider, stretch=1)
        row.addWidget(self._volume_value_lbl)
        return row

    def _build_timing(self) -> QVBoxLayout:
        block = QVBoxLayout()
        block.setSpacing(11)

        self._timing_title_lbl = QLabel(self.tr("Meeting timing"))
        self._timing_title_lbl.setStyleSheet(
            f"color: {_DIM}; font-size: 10px; font-weight: 700;"
            " letter-spacing: 0.6px; background: transparent;"
        )
        block.addWidget(self._timing_title_lbl)

        self._stop_before_spin = self._make_spin(maximum=300)
        self._stop_before_spin.setSpecialValueText(self.tr("At start"))
        self._stop_before_spin.valueChanged.connect(self._service.set_stop_before_seconds)
        self._stop_before_lbl = QLabel(self.tr("Stop before meeting"))
        block.addLayout(
            self._timing_row(
                self._stop_before_lbl,
                self._stop_before_spin,
                self.tr("Stops playback before the scheduled meeting time."),
            )
        )

        self._fade_spin = self._make_spin(maximum=30)
        self._fade_spin.valueChanged.connect(self._service.set_fade_seconds)
        self._fade_lbl = QLabel(self.tr("Fade duration"))
        block.addLayout(
            self._timing_row(
                self._fade_lbl,
                self._fade_spin,
                self.tr("Lowers the volume before playback stops."),
            )
        )
        return block

    def _make_spin(self, *, maximum: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, maximum)
        spin.setSuffix(" s")
        spin.setFixedSize(74, 30)
        spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        spin.setAccelerated(True)
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setStyleSheet(self._spin_style())
        return spin

    # ── Service wiring (unchanged behaviour) ──────────────────────────────────
    def _connect_service(self) -> None:
        self._service.playback_changed.connect(self._set_playing)
        self._service.current_song_changed.connect(self._set_current_song)
        self._service.status_changed.connect(self._status_lbl.setText)
        self._service.volume_changed.connect(self._set_volume)
        self._service.fade_seconds_changed.connect(self._set_fade_seconds)
        self._service.stop_before_seconds_changed.connect(self._set_stop_before_seconds)
        self._service.availability_changed.connect(lambda _available: self._sync_buttons())

    def _sync_all(self) -> None:
        self._set_playing(self._service.is_playing)
        self._set_current_song(self._service.current_title)
        self._status_lbl.setText(self._service.status_text)
        self._set_volume(self._service.volume_percent)
        self._set_fade_seconds(self._service.fade_seconds)
        self._set_stop_before_seconds(self._service.stop_before_seconds)
        self._sync_buttons()

    def _set_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        self._sync_buttons()

    def _set_current_song(self, title: str) -> None:
        text = title or self.tr("No song playing")
        self._song_lbl.setToolTip(title if title else "")
        elided = self._song_lbl.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, self._SONG_W
        )
        self._song_lbl.setText(elided)

    def _set_volume(self, value: int) -> None:
        value = int(value)
        self._volume_slider.blockSignals(True)
        self._volume_slider.setValue(value)
        self._volume_slider.blockSignals(False)
        self._volume_value_lbl.setText(f"{value}%")
        self._update_volume_icon(value)

    def _set_fade_seconds(self, value: int) -> None:
        self._fade_spin.blockSignals(True)
        self._fade_spin.setValue(int(value))
        self._fade_spin.blockSignals(False)

    def _set_stop_before_seconds(self, value: int) -> None:
        self._stop_before_spin.blockSignals(True)
        self._stop_before_spin.setValue(int(value))
        self._stop_before_spin.blockSignals(False)

    def _sync_buttons(self) -> None:
        if self._playing:
            self._toggle_btn.setText(self.tr("Stop"))
            self._toggle_btn.setIcon(make_icon(ICON_PAUSE, 14, _RED))
            self._toggle_btn.setStyleSheet(self._toggle_button_style(playing=True))
        else:
            self._toggle_btn.setText(self.tr("Start"))
            self._toggle_btn.setIcon(make_icon(ICON_PLAY, 14, _ACCENT))
            self._toggle_btn.setStyleSheet(self._toggle_button_style(playing=False))
        self._toggle_btn.setIconSize(QSize(14, 14))
        self._next_btn.setEnabled(self._service.has_songs)

    def _on_volume_changed(self, value: int) -> None:
        self._volume_value_lbl.setText(f"{value}%")
        self._update_volume_icon(value)
        self._service.set_volume_percent(value)

    def _update_volume_icon(self, value: int) -> None:
        if value <= 0:
            glyph, color = ICON_VOLUME_MUTE, _DIM
        elif value < 50:
            glyph, color = ICON_VOLUME_LOW, _MUTED
        else:
            glyph, color = ICON_VOLUME_HIGH, _MUTED
        self._volume_icon.setPixmap(make_icon(glyph, 15, color).pixmap(15, 15))

    # ── Placement / animation ─────────────────────────────────────────────────
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
            self._timing_title_lbl.setText(self.tr("Meeting timing"))
            self._stop_before_lbl.setText(self.tr("Stop before meeting"))
            self._stop_before_spin.setToolTip(
                self.tr("Stops playback before the scheduled meeting time.")
            )
            self._stop_before_spin.setSpecialValueText(self.tr("At start"))
            self._fade_lbl.setText(self.tr("Fade duration"))
            self._fade_spin.setToolTip(self.tr("Lowers the volume before playback stops."))
            self._set_current_song(self._service.current_title)
            self._sync_buttons()
        super().changeEvent(event)

    # ── Reusable pieces ───────────────────────────────────────────────────────
    @staticmethod
    def _timing_row(label: QLabel, spin: QSpinBox, tooltip: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        label.setStyleSheet(f"color: {_SUBTLE}; font-size: 12px; background: transparent;")
        # Explanation surfaces on hover of the seconds control only, not the label.
        spin.setToolTip(tooltip)
        row.addWidget(label, stretch=1)
        row.addWidget(spin, alignment=Qt.AlignmentFlag.AlignVCenter)
        return row

    @staticmethod
    def _divider() -> QFrame:
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: rgba(48,54,61,0.5); border: none;")
        return divider

    @staticmethod
    def _toggle_button_style(*, playing: bool) -> str:
        if playing:
            base, border = "rgba(248,81,73,0.08)", "rgba(248,81,73,0.30)"
            hover_bg, hover_border = "rgba(248,81,73,0.14)", "rgba(248,81,73,0.45)"
            color = _SUBTLE
        else:
            base, border = "rgba(56,139,253,0.10)", "rgba(56,139,253,0.30)"
            hover_bg, hover_border = "rgba(56,139,253,0.17)", "rgba(56,139,253,0.45)"
            color = _ACCENT
        return (
            "QPushButton {"
            f" background: {base}; border: 1px solid {border}; border-radius: 9px;"
            f" color: {color}; font-size: 12px; font-weight: 600; padding: 0 14px;"
            "}"
            f"QPushButton:hover {{ background: {hover_bg}; border-color: {hover_border}; }}"
        )

    @staticmethod
    def _ghost_button_style() -> str:
        return (
            "QPushButton { background: transparent; border: 1px solid #30363d;"
            " border-radius: 9px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.05);"
            " border-color: #484f58; }"
            "QPushButton:disabled { border-color: rgba(48,54,61,0.5); }"
        )

    @staticmethod
    def _slider_style() -> str:
        return (
            "QSlider::groove:horizontal{height:4px;background:#21262d;border-radius:2px;}"
            "QSlider::handle:horizontal{width:12px;height:12px;margin:-4px 0;"
            "background:#e6edf3;border-radius:6px;}"
            "QSlider::handle:horizontal:hover{background:#ffffff;}"
            "QSlider::sub-page:horizontal{background:#58a6ff;border-radius:2px;}"
        )

    @staticmethod
    def _spin_style() -> str:
        return (
            "QSpinBox { background: #0d1117; color: #e6edf3;"
            " border: 1px solid #30363d; border-radius: 8px;"
            " padding: 0 6px; font-size: 12px; font-weight: 600; }"
            "QSpinBox:hover { border-color: #484f58; }"
            "QSpinBox:focus { border-color: #58a6ff; }"
        )
