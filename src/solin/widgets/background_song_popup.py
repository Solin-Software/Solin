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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from solin.core.jw.background_song_service import BackgroundSongService
from solin.ui.background_song_status import translate_background_song_status
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
from solin.styles.theme import PALETTE, qss_rgba
from solin.widgets.common.themed_slider import ThemedHorizontalSlider
from solin.ui.incremental_load import IncrementalLoadHandle


class _PaletteToken:
    def __init__(self, palette_attr: str) -> None:
        self._palette_attr = palette_attr

    def __str__(self) -> str:
        return getattr(PALETTE, self._palette_attr)

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)


_SURF = _PaletteToken("surface")
_TEXT = _PaletteToken("text_primary")
_SUBTLE = _PaletteToken("text_secondary")
_MUTED = _PaletteToken("text_muted")
_DIM = _PaletteToken("text_faint")
_ACCENT = _PaletteToken("accent_hover")
_RED = _PaletteToken("danger")


class BackgroundSongPopup(QWidget):
    """Small popup shown from the quick access toolbar."""

    _POP_W = 332
    # Width available to the song title once card padding is removed.
    _SONG_W = _POP_W - 36

    def __init__(
        self,
        service: BackgroundSongService,
        parent=None,
        *,
        defer_build: bool = False,
    ) -> None:
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
        self._ui_ready = False
        self.preparation_handle = IncrementalLoadHandle(
            (
                self._begin_ui,
                self._build_header_unit,
                self._build_now_playing_unit,
                self._build_controls_unit,
                self._build_volume_unit,
                self._build_timing_unit,
                self._finish_ui,
            ),
            self,
        )
        if not defer_build:
            self.preparation_handle.complete_now()

    # ── Construction ──────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.preparation_handle.complete_now()

    def _begin_ui(self) -> None:
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("BackgroundSongCard")
        self._card_layout = QVBoxLayout(self._card)
        self._card_layout.setContentsMargins(18, 16, 18, 18)
        self._card_layout.setSpacing(16)

    def _build_header_unit(self) -> None:
        self._card_layout.addLayout(self._build_header())

    def _build_now_playing_unit(self) -> None:
        self._card_layout.addLayout(self._build_now_playing())

    def _build_controls_unit(self) -> None:
        self._card_layout.addLayout(self._build_controls())

    def _build_volume_unit(self) -> None:
        self._card_layout.addLayout(self._build_volume())

    def _build_timing_unit(self) -> None:
        self._divider_frame = self._divider()
        self._card_layout.addWidget(self._divider_frame)
        self._card_layout.addLayout(self._build_timing())

    def _finish_ui(self) -> None:
        self._root_layout.addWidget(self._card)
        self.setFixedWidth(self._POP_W)
        self._connect_service()
        self._ui_ready = True
        self.apply_theme()
        self._sync_all()

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(9)
        self._header_icon = QLabel()
        self._header_icon.setFixedSize(14, 14)
        self._header_icon.setStyleSheet("background: transparent;")
        self._title_lbl = QLabel(self.tr("Background Song"))
        self._title_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        header.addWidget(self._header_icon)
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
        self._volume_slider = ThemedHorizontalSlider(track_height=4, handle_diameter=12)
        self._volume_slider.setRange(0, 100)
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
        self._service.status_changed.connect(self._set_status)
        self._service.volume_changed.connect(self._set_volume)
        self._service.fade_seconds_changed.connect(self._set_fade_seconds)
        self._service.stop_before_seconds_changed.connect(self._set_stop_before_seconds)
        self._service.availability_changed.connect(lambda _available: self._sync_buttons())

    def _sync_all(self) -> None:
        self._set_playing(self._service.is_playing)
        self._set_current_song(self._service.current_title)
        self._set_status(self._service.status_text)
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

    def _set_status(self, source: str) -> None:
        self._status_lbl.setText(translate_background_song_status(source))

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

    def apply_theme(self) -> None:
        if not self._ui_ready:
            return
        self._card.setStyleSheet(
            "QFrame#BackgroundSongCard {"
            f" background: {_SURF};"
            f" border: 1px solid {qss_rgba(PALETTE.border, 0.85)};"
            " border-radius: 16px;"
            "}"
        )
        self._title_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        self._header_icon.setPixmap(make_icon(ICON_MUSIC, 14, _DIM).pixmap(14, 14))
        self._song_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 14px; font-weight: 600; background: transparent;"
        )
        self._status_lbl.setStyleSheet(
            f"color: {_MUTED}; font-size: 11px; background: transparent;"
        )
        self._volume_slider.apply_theme()
        self._volume_value_lbl.setStyleSheet(
            f"color: {_MUTED}; font-size: 11px; background: transparent;"
        )
        self._divider_frame.setStyleSheet(
            f"background: {qss_rgba(PALETTE.border, 0.5)}; border: none;"
        )
        self._timing_title_lbl.setStyleSheet(
            f"color: {_DIM}; font-size: 10px; font-weight: 700;"
            " letter-spacing: 0.6px; background: transparent;"
        )
        for label in (self._stop_before_lbl, self._fade_lbl):
            label.setStyleSheet(f"color: {_SUBTLE}; font-size: 12px; background: transparent;")
        for spin in (self._stop_before_spin, self._fade_spin):
            spin.setStyleSheet(self._spin_style())
        self._next_btn.setIcon(make_icon(ICON_SKIP_NEXT, 14, _SUBTLE))
        self._next_btn.setStyleSheet(self._ghost_button_style())
        self._sync_buttons()
        self._update_volume_icon(self._volume_slider.value())

    # ── Placement / animation ─────────────────────────────────────────────────
    def show_above(self, anchor: QWidget) -> None:
        self.preparation_handle.complete_now()
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
        if event.type() == QEvent.Type.LanguageChange and self._ui_ready:
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
            self._set_status(self._service.status_text)
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
        divider.setStyleSheet(f"background: {qss_rgba(PALETTE.border, 0.5)}; border: none;")
        return divider

    @staticmethod
    def _toggle_button_style(*, playing: bool) -> str:
        if playing:
            base = qss_rgba(PALETTE.danger, 0.08)
            border = qss_rgba(PALETTE.danger, 0.30)
            hover_bg = qss_rgba(PALETTE.danger, 0.14)
            hover_border = qss_rgba(PALETTE.danger, 0.45)
            color = _SUBTLE
        else:
            base = qss_rgba(PALETTE.accent, 0.10)
            border = qss_rgba(PALETTE.accent, 0.30)
            hover_bg = qss_rgba(PALETTE.accent, 0.17)
            hover_border = qss_rgba(PALETTE.accent, 0.45)
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
            f"QPushButton {{ background: transparent; border: 1px solid {PALETTE.border};"
            " border-radius: 9px; }"
            f"QPushButton:hover {{ background: {qss_rgba(PALETTE.white, 0.05)};"
            f" border-color: {PALETTE.text_dim}; }}"
            f"QPushButton:disabled {{ border-color: {qss_rgba(PALETTE.border, 0.5)}; }}"
        )

    @staticmethod
    def _spin_style() -> str:
        return (
            f"QSpinBox {{ background: {PALETTE.bg0}; color: {PALETTE.text_primary};"
            f" border: 1px solid {PALETTE.border}; border-radius: 8px;"
            " padding: 0 6px; font-size: 12px; font-weight: 600; }"
            f"QSpinBox:hover {{ border-color: {PALETTE.text_dim}; }}"
            f"QSpinBox:focus {{ border-color: {_ACCENT}; }}"
        )
