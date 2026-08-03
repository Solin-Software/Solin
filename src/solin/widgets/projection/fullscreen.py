from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRectF,
    QSignalBlocker,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QWidget,
)

from solin.core.foundation.constants import ORDER_NEXT, ORDER_OFF, ORDER_RANDOM
from solin.styles.icons import (
    ICON_CLOSE,
    ICON_FULLSCREEN_EXIT,
    ICON_MORE_VERT,
    ICON_PAUSE,
    ICON_PLAY,
    ICON_SKIP_NEXT,
    ICON_SKIP_PREV,
    ICON_VOLUME_HIGH,
    ICON_VOLUME_LOW,
    ICON_VOLUME_MUTE,
    make_icon,
)
from solin.styles.theme import PALETTE, qss_rgba
from solin.ui.themed_tooltip import install_themed_tooltip
from solin.widgets.common.themed_slider import ThemedHorizontalSlider
from solin.widgets.songs_widget import BufferedSlider

from .controls import SPEED_CHOICES, icon_button, projection_menu_style
from ...core.media.playback_state import PlaybackState


class FullscreenVideoSurface(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self.setMouseTracking(True)
        self.setStyleSheet(f"background: {PALETTE.black};")

    def set_frame(self, frame) -> None:
        image = frame.toImage()
        if image.isNull():
            return
        self._pixmap = QPixmap.fromImage(image)
        self.update()

    def clear(self) -> None:
        self._pixmap = None
        self.update()

    def apply_theme(self) -> None:
        self.setStyleSheet(f"background: {PALETTE.black};")
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor(PALETTE.black))

        pixmap = self._pixmap
        if pixmap is None or pixmap.isNull():
            painter.end()
            return

        w, h = self.width(), self.height()
        pw, ph = pixmap.width(), pixmap.height()
        if w <= 0 or h <= 0 or pw <= 0 or ph <= 0:
            painter.end()
            return

        scale = min(w / pw, h / ph)
        draw_w = pw * scale
        draw_h = ph * scale
        painter.drawPixmap(
            QRectF((w - draw_w) / 2.0, (h - draw_h) / 2.0, draw_w, draw_h),
            pixmap,
            QRectF(0, 0, pw, ph),
        )
        painter.end()


class FullscreenVideoOverlay(QWidget):
    exit_requested = Signal()
    seek_requested = Signal(int)
    toggle_requested = Signal()
    volume_changed = Signal(float)
    stop_requested = Signal()
    previous_requested = Signal()
    next_requested = Signal()
    speed_selected = Signal(float)
    loop_toggled = Signal()
    playback_order_selected = Signal(str)

    _HIDE_CHROME_MS = 2500

    def __init__(
        self,
        *,
        source_widget: QWidget | None,
        translate: Callable[[str], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint,
        )
        self._source_widget = source_widget
        self._translate = translate or self.tr
        self._speed = 1.0
        self._loop_enabled = False
        self._playback_order = ORDER_OFF
        self._playback_options_active = False
        self._muted = False
        self._pre_mute_volume = 0.8
        self._playback_state = PlaybackState.StoppedState
        self._navigation_state = (False, False, False)

        self.setObjectName("AppFullscreenVideoOverlay")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(
            f"QWidget#AppFullscreenVideoOverlay {{ background: {PALETTE.black}; }}"
        )

        self._surface = FullscreenVideoSurface(self)
        self._title_bar = self._build_title_bar()
        self._controls = self._build_controls()

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self._HIDE_CHROME_MS)
        self._hide_timer.timeout.connect(self._hide_chrome_if_idle)

        self._install_activity_filters()

    def _title_bar_stylesheet(self) -> str:
        return (
            "QFrame#FullscreenTitleBar {"
            f" background: {qss_rgba(PALETTE.bg0, 0.62)};"
            f" border: 1px solid {qss_rgba(PALETTE.border, 0.72)};"
            " border-radius: 8px;"
            "}"
        )

    def _controls_stylesheet(self) -> str:
        return (
            "QFrame#FullscreenPlaybackControls {"
            f" background: {qss_rgba(PALETTE.bg0, 0.72)};"
            f" border: 1px solid {qss_rgba(PALETTE.border, 0.78)};"
            " border-radius: 8px;"
            "}"
        )

    def _stop_button_stylesheet(self) -> str:
        return (
            "QPushButton{border:none;border-radius:17px;background:transparent;padding:0;}"
            f"QPushButton:hover{{background:{qss_rgba(PALETTE.danger, 0.20)};}}"
            f"QPushButton:pressed{{background:{qss_rgba(PALETTE.danger, 0.32)};}}"
        )

    def _icon_button_stylesheet(self, button: QWidget) -> str:
        radius = max(1, button.width() // 2)
        return (
            f"QPushButton{{border:none;border-radius:{radius}px;"
            "background:transparent;padding:0;}"
            f"QPushButton:hover{{background:{PALETTE.surface_hover};"
            f"border-radius:{radius}px;}}"
            f"QPushButton:pressed{{background:{PALETTE.surface_hover_strong};}}"
        )

    def _apply_icon_button_styles(self) -> None:
        for button in (
            self.exit_btn,
            self.play_btn,
            self.prev_btn,
            self.next_btn,
            self.vol_btn,
            self.more_btn,
        ):
            button.setStyleSheet(self._icon_button_stylesheet(button))

    def _build_title_bar(self) -> QFrame:
        bar = QFrame(self)
        bar.setObjectName("FullscreenTitleBar")
        bar.setStyleSheet(self._title_bar_stylesheet())
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 0, 8, 0)
        layout.setSpacing(8)

        self.title_label = QLabel()
        self.title_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_primary};"
            " font-size: 13px; font-weight: 600;"
        )
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        install_themed_tooltip(self.title_label)

        self.exit_btn = icon_button(
            ICON_FULLSCREEN_EXIT,
            32,
            15,
            PALETTE.text_secondary,
            self._tr("Exit fullscreen"),
        )
        self.exit_btn.clicked.connect(lambda _checked=False: self.exit_requested.emit())
        self.exit_btn.setStyleSheet(self._icon_button_stylesheet(self.exit_btn))

        layout.addWidget(self.title_label, stretch=1)
        layout.addWidget(self.exit_btn)
        return bar

    def _build_controls(self) -> QFrame:
        controls = QFrame(self)
        controls.setObjectName("FullscreenPlaybackControls")
        controls.setStyleSheet(self._controls_stylesheet())
        layout = QHBoxLayout(controls)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        self.play_btn = icon_button(
            ICON_PAUSE,
            34,
            16,
            PALETTE.text_primary,
            self._tr("Pause/Resume"),
        )
        self.play_btn.clicked.connect(lambda _checked=False: self.toggle_requested.emit())
        self.play_btn.setStyleSheet(self._icon_button_stylesheet(self.play_btn))

        self.prev_btn = icon_button(
            ICON_SKIP_PREV,
            34,
            16,
            PALETTE.text_faint,
            self._tr("Previous"),
        )
        self.prev_btn.clicked.connect(lambda _checked=False: self.previous_requested.emit())
        self.prev_btn.setStyleSheet(self._icon_button_stylesheet(self.prev_btn))
        self.prev_btn.setVisible(False)

        self.next_btn = icon_button(
            ICON_SKIP_NEXT,
            34,
            16,
            PALETTE.text_faint,
            self._tr("Next"),
        )
        self.next_btn.clicked.connect(lambda _checked=False: self.next_requested.emit())
        self.next_btn.setStyleSheet(self._icon_button_stylesheet(self.next_btn))
        self.next_btn.setVisible(False)

        self.seek_slider = BufferedSlider()
        self.seek_slider.setMinimumWidth(260)
        self.seek_slider.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.seek_slider.sliderMoved.connect(self.seek_requested.emit)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_secondary};"
            " font-size: 12px; font-weight: 500;"
        )
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_label.setSizePolicy(
            QSizePolicy.Policy.Minimum,
            QSizePolicy.Policy.Fixed,
        )

        self.vol_btn = icon_button(
            ICON_VOLUME_HIGH,
            34,
            16,
            PALETTE.text_secondary,
            self._tr("Volume"),
        )
        self.vol_btn.clicked.connect(self._toggle_mute)
        self.vol_btn.setStyleSheet(self._icon_button_stylesheet(self.vol_btn))

        self.vol_slider = ThemedHorizontalSlider(
            track_height=3,
            handle_diameter=12,
            dark_track=QColor(255, 255, 255, 61),
            dark_handle=QColor("#e6edf3"),
        )
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setFixedWidth(82)
        self.vol_slider.setFixedHeight(22)
        self.vol_slider.valueChanged.connect(self._on_volume_slider)

        self.more_btn = icon_button(
            ICON_MORE_VERT,
            34,
            16,
            PALETTE.text_secondary,
            self._tr("Playback options"),
        )
        self.more_btn.clicked.connect(self._show_more_menu)
        self.more_btn.setStyleSheet(self._icon_button_stylesheet(self.more_btn))

        self.stop_btn = icon_button(
            ICON_CLOSE,
            34,
            14,
            PALETTE.text_secondary,
            self._tr("Stop projection"),
        )
        self.stop_btn.setStyleSheet(self._stop_button_stylesheet())
        self.stop_btn.clicked.connect(lambda _checked=False: self.stop_requested.emit())

        layout.addWidget(self.play_btn)
        layout.addWidget(self.seek_slider, stretch=1)
        layout.addWidget(self.time_label)
        layout.addWidget(self.vol_btn)
        layout.addWidget(self.vol_slider)
        layout.addWidget(self.more_btn)
        layout.addWidget(self.prev_btn)
        layout.addWidget(self.next_btn)
        layout.addWidget(self.stop_btn)
        return controls

    def _install_activity_filters(self) -> None:
        widgets = [self._surface, self._title_bar, self._controls]
        widgets.extend(self._title_bar.findChildren(QWidget))
        widgets.extend(self._controls.findChildren(QWidget))
        for widget in widgets:
            widget.setMouseTracking(True)
            widget.installEventFilter(self)

    def show_fullscreen(self) -> None:
        self._place_on_source_screen()
        self._show_chrome()
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def hide_fullscreen(self, *, clear_frame: bool = False) -> None:
        self._hide_timer.stop()
        self.unsetCursor()
        self._surface.unsetCursor()
        self.hide()
        if clear_frame:
            self._surface.clear()

    def reset(self) -> None:
        self.hide_fullscreen(clear_frame=True)
        self.seek_slider.reset()
        self.time_label.setText("0:00 / 0:00")
        self.set_reconnect_active(False)

    def is_active(self) -> bool:
        return self.isVisible()

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)
        self.title_label.setToolTip(title)

    def set_frame(self, frame) -> None:
        if self.isVisible():
            self._surface.set_frame(frame)

    def clear_frame(self) -> None:
        self._surface.clear()

    def set_playback_state(self, state) -> None:
        self._playback_state = state
        playing = state == PlaybackState.PlayingState
        icon = ICON_PAUSE if playing else ICON_PLAY
        self.play_btn.setIcon(make_icon(icon, 16, PALETTE.text_primary))

    def set_duration(self, duration: int) -> None:
        self.seek_slider.setRange(0, max(0, int(duration)))
        self._update_time_label(self.seek_slider.value(), duration)

    def set_position(self, position: int, duration: int) -> None:
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(int(position))
        self._update_time_label(position, duration)

    def set_buffer_progress(self, downloaded: int, total: int) -> None:
        ratio = downloaded / total if total > 0 else 0.0
        self.seek_slider.setBufferedRatio(ratio)

    def set_reconnect_active(self, active: bool) -> None:
        self.seek_slider.setReconnectActive(active)

    def set_play_enabled(self, enabled: bool) -> None:
        self.play_btn.setEnabled(enabled)

    def set_seek_enabled(self, enabled: bool) -> None:
        self.seek_slider.setEnabled(enabled)

    def set_volume(self, value: float) -> None:
        volume = max(0.0, min(1.0, float(value)))
        if volume > 0:
            self._pre_mute_volume = volume
            self._muted = False
        else:
            self._muted = True
        blocker = QSignalBlocker(self.vol_slider)
        self.vol_slider.setValue(int(round(volume * 100)))
        del blocker
        self._refresh_volume_icon()

    def set_speed(self, speed: float) -> None:
        self._speed = float(speed)

    def set_loop_enabled(self, enabled: bool) -> None:
        self._loop_enabled = bool(enabled)

    def set_playback_order(self, order: str) -> None:
        self._playback_order = order

    def set_playback_options_active(self, active: bool) -> None:
        self._playback_options_active = bool(active)
        self._refresh_playback_options_icon()

    def set_navigation(self, *, show: bool, can_previous: bool, can_next: bool) -> None:
        self._navigation_state = (show, can_previous, can_next)
        self.prev_btn.setVisible(show)
        self.next_btn.setVisible(show)
        self.prev_btn.setEnabled(can_previous)
        self.next_btn.setEnabled(can_next)
        self.prev_btn.setIcon(
            make_icon(
                ICON_SKIP_PREV,
                16,
                PALETTE.text_primary if can_previous else PALETTE.text_dim,
            )
        )
        self.next_btn.setIcon(
            make_icon(
                ICON_SKIP_NEXT,
                16,
                PALETTE.text_primary if can_next else PALETTE.text_dim,
            )
        )

    def apply_theme(self) -> None:
        self.setStyleSheet(
            f"QWidget#AppFullscreenVideoOverlay {{ background: {PALETTE.black}; }}"
        )
        self._surface.apply_theme()
        self._title_bar.setStyleSheet(self._title_bar_stylesheet())
        self._controls.setStyleSheet(self._controls_stylesheet())
        self.title_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_primary};"
            " font-size: 13px; font-weight: 600;"
        )
        self.time_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_secondary};"
            " font-size: 12px; font-weight: 500;"
        )
        self.vol_slider.apply_theme()
        self.stop_btn.setStyleSheet(self._stop_button_stylesheet())
        self._apply_icon_button_styles()
        self.exit_btn.setIcon(make_icon(ICON_FULLSCREEN_EXIT, 15, PALETTE.text_secondary))
        self._refresh_playback_options_icon()
        self.set_playback_state(self._playback_state)
        show, can_previous, can_next = self._navigation_state
        self.set_navigation(
            show=show,
            can_previous=can_previous,
            can_next=can_next,
        )
        self._refresh_volume_icon()
        self.seek_slider.update()

    def retranslateUi(self) -> None:
        self.exit_btn.setToolTip(self._tr("Exit fullscreen"))
        self.play_btn.setToolTip(self._tr("Pause/Resume"))
        self.prev_btn.setToolTip(self._tr("Previous"))
        self.next_btn.setToolTip(self._tr("Next"))
        self.vol_btn.setToolTip(self._tr("Volume"))
        self.more_btn.setToolTip(self._tr("Playback options"))
        self.stop_btn.setToolTip(self._tr("Stop projection"))

    def _place_on_source_screen(self) -> None:
        screen = None
        if self._source_widget is not None:
            global_center = self._source_widget.mapToGlobal(
                self._source_widget.rect().center()
            )
            screen = QGuiApplication.screenAt(global_center)
            if screen is None:
                window = self._source_widget.window()
                handle = window.windowHandle() if window is not None else None
                screen = handle.screen() if handle is not None else None
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._surface.setGeometry(self.rect())
        margin = 26
        top_width = max(240, min(780, self.width() - margin * 2))
        self._title_bar.setGeometry(margin, margin, top_width, 44)

        controls_width = max(360, min(1040, self.width() - 40))
        controls_height = 66
        controls_x = (self.width() - controls_width) // 2
        controls_y = self.height() - controls_height - 30
        self._controls.setGeometry(controls_x, max(margin, controls_y), controls_width, controls_height)

    def eventFilter(self, obj, event) -> bool:
        event_type = event.type()
        if event_type in {
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.Wheel,
        }:
            self._show_chrome()

        if event_type == QEvent.Type.MouseButtonDblClick and obj is self._surface:
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                self.exit_requested.emit()
                return True

        if event_type == QEvent.Type.KeyPress and self._is_escape_event(event):
            self.exit_requested.emit()
            return True

        return super().eventFilter(obj, event)

    def mouseMoveEvent(self, event) -> None:
        self._show_chrome()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        self._show_chrome()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.exit_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._is_escape_event(event):
            self.exit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._show_chrome()

    def hideEvent(self, event) -> None:
        self._hide_timer.stop()
        self.unsetCursor()
        self._surface.unsetCursor()
        super().hideEvent(event)

    def _show_chrome(self) -> None:
        if not self.isVisible() and not self.isFullScreen():
            return
        self._title_bar.setVisible(True)
        self._controls.setVisible(True)
        self.unsetCursor()
        self._surface.unsetCursor()
        self._hide_timer.start()

    def _hide_chrome_if_idle(self) -> None:
        if self._cursor_over_chrome():
            self._hide_timer.start(700)
            return
        self._title_bar.setVisible(False)
        self._controls.setVisible(False)
        self.setCursor(Qt.CursorShape.BlankCursor)
        self._surface.setCursor(Qt.CursorShape.BlankCursor)

    def _cursor_over_chrome(self) -> bool:
        pos = self.mapFromGlobal(QCursor.pos())
        return (
            self._title_bar.geometry().contains(pos)
            or self._controls.geometry().contains(pos)
        )

    def _show_more_menu(self) -> None:
        self._hide_timer.stop()
        menu = QMenu(self)
        menu.setStyleSheet(projection_menu_style())

        speed_menu = menu.addMenu("  " + self._tr("Speed"))
        speed_menu.setStyleSheet(projection_menu_style())
        speed_group = QActionGroup(speed_menu)
        speed_group.setExclusive(True)
        for label, value in SPEED_CHOICES:
            action = QAction(label, speed_group)
            action.setCheckable(True)
            action.setChecked(abs(self._speed - value) < 0.01)
            action.triggered.connect(
                lambda _checked=False, selected=value: self.speed_selected.emit(selected)
            )
            speed_menu.addAction(action)

        menu.addSeparator()

        loop_action = QAction("  " + self._tr("Loop"), menu)
        loop_action.setCheckable(True)
        loop_action.setChecked(self._loop_enabled)
        loop_action.triggered.connect(lambda _checked=False: self.loop_toggled.emit())
        menu.addAction(loop_action)

        menu.addSeparator()

        order_menu = menu.addMenu("  " + self._tr("Playback Order"))
        order_menu.setStyleSheet(projection_menu_style())
        order_group = QActionGroup(order_menu)
        order_group.setExclusive(True)
        for label, value in (
            (self._tr("Off"), ORDER_OFF),
            (self._tr("Next"), ORDER_NEXT),
            (self._tr("Random"), ORDER_RANDOM),
        ):
            action = QAction("  " + label, order_group)
            action.setCheckable(True)
            action.setChecked(self._playback_order == value)
            action.triggered.connect(
                lambda _checked=False, selected=value: (
                    self.playback_order_selected.emit(selected)
                )
            )
            order_menu.addAction(action)

        menu.exec(self.more_btn.mapToGlobal(QPoint(0, -menu.sizeHint().height())))
        self._show_chrome()

    def _on_volume_slider(self, value: int) -> None:
        if value > 0:
            self._pre_mute_volume = value / 100.0
            self._muted = False
        else:
            self._muted = True
        self._refresh_volume_icon()
        self.volume_changed.emit(value / 100.0)

    def _toggle_mute(self) -> None:
        if self._muted:
            self.vol_slider.setValue(int(round(self._pre_mute_volume * 100)))
        else:
            self._pre_mute_volume = max(0.01, self.vol_slider.value() / 100.0)
            self.vol_slider.setValue(0)

    def _refresh_volume_icon(self) -> None:
        value = self.vol_slider.value()
        if value == 0:
            icon = ICON_VOLUME_MUTE
        elif value < 50:
            icon = ICON_VOLUME_LOW
        else:
            icon = ICON_VOLUME_HIGH
        self.vol_btn.setIcon(make_icon(icon, 16, PALETTE.text_secondary))

    def _refresh_playback_options_icon(self) -> None:
        color = PALETTE.warning if self._playback_options_active else PALETTE.text_secondary
        self.more_btn.setIcon(make_icon(ICON_MORE_VERT, 16, color))

    def _update_time_label(self, position: int, duration: int) -> None:
        self.time_label.setText(
            f"{self._format_ms(position)} / {self._format_ms(duration)}"
        )

    @staticmethod
    def _format_ms(ms: int) -> str:
        seconds = max(0, int(ms)) // 1000
        return f"{seconds // 60}:{seconds % 60:02d}"

    @staticmethod
    def _is_escape_event(event) -> bool:
        return isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Escape

    def _tr(self, text: str) -> str:
        return self._translate(text)
