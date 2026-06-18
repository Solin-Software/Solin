"""OBS scene popup used by the quick access toolbar."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QRect,
    QSize,
    QPropertyAnimation,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from solin.styles.icons import (
    ICON_CAST,
    ICON_OBS,
    ICON_REC_CIRCLE,
    ICON_REC_STOP,
    make_icon,
)


class _FlowContainer(QWidget):
    """Lightweight flow layout that wraps child widgets into rows."""

    def __init__(self, h_spacing=6, v_spacing=6, parent=None):
        super().__init__(parent)
        self._hs = h_spacing
        self._vs = v_spacing
        self._items: list[QWidget] = []

    def add_widget(self, widget: QWidget) -> None:
        widget.setParent(self)
        self._items.append(widget)

    def clear_items(self) -> None:
        for widget in self._items:
            widget.setParent(None)
            widget.deleteLater()
        self._items.clear()

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), dry_run=True)

    def minimumSize(self):
        return QSize(100, 40)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.width() > 0:
            self._do_layout(QRect(0, 0, self.width(), self.height()))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.width() > 0:
            self._do_layout(QRect(0, 0, self.width(), self.height()))

    def _do_layout(self, rect: QRect, dry_run: bool = False) -> int:
        x = rect.x()
        y = rect.y()
        row_h = 0
        for widget in self._items:
            if not dry_run and not widget.isVisible():
                continue
            width = (
                widget.minimumWidth()
                if widget.minimumWidth() == widget.maximumWidth()
                else widget.sizeHint().width()
            )
            height = (
                widget.minimumHeight()
                if widget.minimumHeight() == widget.maximumHeight()
                else widget.sizeHint().height()
            )
            if width <= 0 or height <= 0:
                continue
            if x + width > rect.x() + rect.width() and x > rect.x():
                x = rect.x()
                y += row_h + self._vs
                row_h = 0
            if not dry_run:
                widget.setGeometry(x, y, width, height)
            x += width + self._hs
            row_h = max(row_h, height)
        return y + row_h


class _SceneChipButton(QPushButton):
    right_clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class OBSScenePopup(QWidget):
    """Floating popup with a flow grid of OBS scene chips."""

    scene_change_requested = Signal(str)
    return_scene_requested = Signal(str)
    stream_requested = Signal()
    camera_stream_requested = Signal()

    _POP_MIN_W = 320
    _POP_MAX_W = 460

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._opacity_eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_eff)
        self._fade = QPropertyAnimation(self._opacity_eff, b"opacity")
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._current_scene = ""
        self._is_recording = False
        self._stream_available = False
        self._stream_active = False
        self._camera_stream_available = False
        self._camera_stream_active = False
        self._stream_kind = "obs"
        self._obs_service = None
        self._return_scene_override_enabled = False
        self._chip_feedback_anims: list[QVariantAnimation] = []
        self._chip_feedback_by_id: dict[int, tuple[QVariantAnimation, str]] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("OBSSceneCard")
        self._card.setStyleSheet(
            "QFrame#OBSSceneCard {"
            "  background: #161b22;"
            "  border: 1px solid rgba(48,54,61,0.85);"
            "  border-radius: 16px;"
            "}"
        )
        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        self._hdr = QWidget()
        self._hdr.setStyleSheet("background: transparent;")
        h_lay = QHBoxLayout(self._hdr)
        h_lay.setContentsMargins(18, 14, 18, 6)
        h_lay.setSpacing(8)

        obs_px = QLabel()
        obs_px.setPixmap(make_icon(ICON_OBS, 14, "#6e7681").pixmap(14, 14))
        obs_px.setFixedSize(14, 14)
        obs_px.setStyleSheet("background: transparent;")

        self._title_lbl = QLabel(self.tr("OBS Scenes"))
        self._title_lbl.setStyleSheet(
            "color: #6e7681; font-size: 10px; font-weight: 700;"
            " letter-spacing: 0.6px; background: transparent;"
        )

        self._active_lbl = QLabel()
        self._active_lbl.setStyleSheet(
            "color: #58a6ff; font-size: 10px; font-weight: 600;"
            " background: rgba(56,139,253,0.08); border-radius: 4px;"
            " padding: 2px 8px; border: none;"
        )

        h_lay.addWidget(obs_px)
        h_lay.addWidget(self._title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(self._active_lbl)
        card_lay.addWidget(self._hdr)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollBar:vertical {"
            "  width: 3px; background: transparent; margin: 4px 3px 4px 0;"
            "}"
            "QScrollBar::handle:vertical {"
            "  background: rgba(110,118,129,0.3); border-radius: 1px; min-height: 20px;"
            "}"
            "QScrollBar::handle:vertical:hover { background: rgba(110,118,129,0.5); }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )

        self._content = QWidget()
        self._content.setStyleSheet("background: transparent;")
        self._content_lay = QVBoxLayout(self._content)
        self._content_lay.setContentsMargins(14, 6, 14, 14)
        self._content_lay.setSpacing(10)

        self._scroll.setWidget(self._content)
        card_lay.addWidget(self._scroll)

        self._footer = QWidget()
        self._footer.setStyleSheet("background: transparent;")
        f_lay = QHBoxLayout(self._footer)
        f_lay.setContentsMargins(14, 6, 14, 12)
        f_lay.setSpacing(8)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(48,54,61,0.6); border: none;")

        self._rec_btn = QPushButton()
        self._rec_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._rec_btn.setFixedHeight(32)
        self._rec_btn.setObjectName("OBSRecBtn")
        self._rec_btn.clicked.connect(self._on_record_clicked)
        self._update_rec_button()

        f_lay.addStretch()
        f_lay.addWidget(self._rec_btn)

        self._stream_btn = QPushButton()
        self._stream_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stream_btn.setFixedHeight(32)
        self._stream_btn.setObjectName("OBSStreamBtn")
        self._stream_btn.setVisible(False)
        self._stream_btn.clicked.connect(self._on_stream_clicked)
        self._update_stream_button()
        f_lay.addWidget(self._stream_btn)
        f_lay.addStretch()

        card_lay.addWidget(sep)
        card_lay.addWidget(self._footer)

        root.addWidget(self._card)

    def populate(
        self,
        scenes: list[str],
        current_scene: str,
        idle_scene: str,
        media_scene: str,
    ) -> None:
        self._current_scene = current_scene
        self._return_scene_override_enabled = bool(
            media_scene
            and current_scene == media_scene
            and not media_scene.startswith("—")
        )
        self._active_lbl.setText(current_scene or "—")

        while self._content_lay.count():
            item = self._content_lay.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()

        if not scenes:
            empty = QLabel(self.tr("No scenes available"))
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(
                "color: #484f58; font-size: 12px; padding: 20px; background: transparent;"
            )
            self._content_lay.addWidget(empty)
            self._finalize_size(scenes)
            return

        pinned = []
        if idle_scene and idle_scene in scenes and not idle_scene.startswith("—"):
            pinned.append((idle_scene, ""))
        if (
            media_scene
            and media_scene in scenes
            and not media_scene.startswith("—")
            and media_scene != idle_scene
        ):
            pinned.append((media_scene, ""))
        pinned_names = {p[0] for p in pinned}
        rest = [scene for scene in scenes if scene not in pinned_names]

        if pinned:
            sec_lbl = QLabel(self.tr("Configured"))
            sec_lbl.setStyleSheet(
                "color: #484f58; font-size: 9px; font-weight: 700;"
                " letter-spacing: 0.5px; background: transparent; padding: 0 4px;"
            )
            self._content_lay.addWidget(sec_lbl)

            flow_p = _FlowContainer(h_spacing=6, v_spacing=6)
            flow_p.setStyleSheet("background: transparent;")
            for name, badge in pinned:
                flow_p.add_widget(self._make_chip(name, name == current_scene, badge))
            self._content_lay.addWidget(flow_p)

        if rest:
            if pinned:
                sec_lbl2 = QLabel(self.tr("All scenes"))
                sec_lbl2.setStyleSheet(
                    "color: #484f58; font-size: 9px; font-weight: 700;"
                    " letter-spacing: 0.5px; background: transparent; padding: 0 4px;"
                )
                self._content_lay.addWidget(sec_lbl2)

            flow_r = _FlowContainer(h_spacing=6, v_spacing=6)
            flow_r.setStyleSheet("background: transparent;")
            for name in rest:
                flow_r.add_widget(self._make_chip(name, name == current_scene, ""))
            self._content_lay.addWidget(flow_r)

        self._finalize_size(scenes)

    def _finalize_size(self, scenes: list[str]) -> None:
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)

        if not scenes:
            width = self._POP_MIN_W
        else:
            max_len = max(len(scene) for scene in scenes)
            chip_w = max_len * 7 + 30
            width = min(
                self._POP_MAX_W,
                max(self._POP_MIN_W, chip_w * 3 + 14 * 2 + 12 * 2),
            )

        margins = self._content_lay.contentsMargins()
        content_w = int(width) - margins.left() - margins.right() - 6

        total_h = 0
        n_widgets = 0
        for index in range(self._content_lay.count()):
            item = self._content_lay.itemAt(index)
            widget = item.widget()
            if widget is None:
                continue
            n_widgets += 1
            if isinstance(widget, _FlowContainer):
                height = max(widget.heightForWidth(content_w), 36)
                widget.setFixedSize(content_w, height)
                total_h += height
            elif isinstance(widget, QLabel):
                total_h += widget.sizeHint().height()

        spacing_total = max(n_widgets - 1, 0) * self._content_lay.spacing()
        needed_h = total_h + spacing_total + margins.top() + margins.bottom()

        viewport_w = int(width) - 2
        self._content.setFixedSize(viewport_w, needed_h)

        scroll_h = min(needed_h, 360)
        self._scroll.setFixedHeight(max(scroll_h, 60))

        hdr_h = self._hdr.sizeHint().height()
        footer_h = self._footer.sizeHint().height() + 1
        self.setFixedSize(int(width), hdr_h + scroll_h + footer_h + 2)

    def _make_chip(self, name: str, active: bool, badge: str) -> QPushButton:
        chip = _SceneChipButton()
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.setFixedHeight(32)

        display = name
        if badge:
            display = f"{name}  ·  {badge}"
        chip.setText(display)

        text_w = chip.fontMetrics().horizontalAdvance(display)
        chip.setFixedWidth(max(text_w + 24, 60))

        uid = f"SC_{id(chip)}"
        chip.setObjectName(uid)

        if active:
            chip.setStyleSheet(
                f"QPushButton#{uid} {{"
                "  background: rgba(56,139,253,0.12);"
                "  border: 1px solid rgba(56,139,253,0.40);"
                "  border-radius: 8px;"
                "  color: #58a6ff;"
                "  font-size: 12px; font-weight: 600;"
                "  padding: 0 10px;"
                "  text-align: center;"
                "}"
                f"QPushButton#{uid}:hover {{"
                "  background: rgba(56,139,253,0.20);"
                "  border-color: rgba(56,139,253,0.55);"
                "}"
                f"QPushButton#{uid}:pressed {{"
                "  background: rgba(56,139,253,0.28);"
                "}"
            )
        else:
            if badge == self.tr("idle"):
                border_c = "rgba(63,185,80,0.25)"
                bg_hover = "rgba(63,185,80,0.08)"
                text_c = "#b1bac4"
            elif badge == self.tr("media"):
                border_c = "rgba(56,139,253,0.20)"
                bg_hover = "rgba(56,139,253,0.06)"
                text_c = "#b1bac4"
            else:
                border_c = "rgba(48,54,61,0.6)"
                bg_hover = "rgba(255,255,255,0.04)"
                text_c = "#8b949e"

            chip.setStyleSheet(
                f"QPushButton#{uid} {{"
                "  background: rgba(22,27,34,0.6);"
                f"  border: 1px solid {border_c};"
                "  border-radius: 8px;"
                f"  color: {text_c};"
                "  font-size: 12px; font-weight: 400;"
                "  padding: 0 10px;"
                "  text-align: center;"
                "}"
                f"QPushButton#{uid}:hover {{"
                f"  background: {bg_hover};"
                "  border-color: rgba(56,139,253,0.35);"
                "  color: #c9d1d9;"
                "}"
                f"QPushButton#{uid}:pressed {{"
                "  background: rgba(56,139,253,0.10);"
                "}"
            )

        chip.clicked.connect(
            lambda checked=False, n=name: self.scene_change_requested.emit(n)
        )
        if not active and self._return_scene_override_enabled:
            chip.right_clicked.connect(
                lambda n=name, c=chip: self._on_return_scene_chip_clicked(n, c)
            )
        return chip

    def _on_return_scene_chip_clicked(self, scene_name: str, chip: QPushButton) -> None:
        self.return_scene_requested.emit(scene_name)
        self._flash_return_scene_chip(chip)

    def _flash_return_scene_chip(self, chip: QPushButton) -> None:
        chip_id = id(chip)
        previous = self._chip_feedback_by_id.pop(chip_id, None)
        if previous is not None:
            previous_anim, previous_style = previous
            previous_anim.stop()
            if previous_anim in self._chip_feedback_anims:
                self._chip_feedback_anims.remove(previous_anim)
            try:
                chip.setStyleSheet(previous_style)
            except RuntimeError:
                pass

        original_style = chip.styleSheet()
        uid = chip.objectName()

        anim = QVariantAnimation(self)
        anim.setDuration(360)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._chip_feedback_anims.append(anim)

        def _rgba(r: int, g: int, b: int, alpha: int) -> str:
            return f"rgba({r},{g},{b},{max(0, min(alpha, 255))})"

        def _apply(value) -> None:
            t = float(value)
            bg_alpha = int(18 + (56 * t))
            border_alpha = int(70 + (120 * t))
            hover_bg_alpha = int(30 + (70 * t))
            pressed_bg_alpha = int(42 + (82 * t))
            border = _rgba(63, 185, 80, border_alpha)
            try:
                chip.setStyleSheet(
                    original_style
                    + f"QPushButton#{uid} {{"
                    f"  background: {_rgba(63, 185, 80, bg_alpha)};"
                    f"  border-color: {border};"
                    "  color: #3fb950;"
                    "}"
                    + f"QPushButton#{uid}:hover {{"
                    f"  background: {_rgba(63, 185, 80, hover_bg_alpha)};"
                    f"  border-color: {border};"
                    "  color: #3fb950;"
                    "}"
                    + f"QPushButton#{uid}:pressed {{"
                    f"  background: {_rgba(63, 185, 80, pressed_bg_alpha)};"
                    f"  border-color: {border};"
                    "  color: #c9d1d9;"
                    "}"
                )
            except RuntimeError:
                pass

        def _cleanup() -> None:
            try:
                chip.setStyleSheet(original_style)
            except RuntimeError:
                pass
            if anim in self._chip_feedback_anims:
                self._chip_feedback_anims.remove(anim)
            current = self._chip_feedback_by_id.get(chip_id)
            if current is not None and current[0] is anim:
                self._chip_feedback_by_id.pop(chip_id, None)

        anim.valueChanged.connect(_apply)
        anim.finished.connect(_cleanup)
        chip.destroyed.connect(
            lambda _obj=None, key=chip_id: self._chip_feedback_by_id.pop(key, None)
        )
        self._chip_feedback_by_id[chip_id] = (anim, original_style)
        _apply(1.0)
        anim.start()

    def show_above(self, anchor: QWidget) -> None:
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

    def set_obs_service(self, obs_service) -> None:
        self._obs_service = obs_service
        if obs_service:
            obs_service.recording_state_changed.connect(self._on_recording_state)

    def _on_recording_state(self, is_recording: bool) -> None:
        self._is_recording = is_recording
        self._update_rec_button()

    def _on_record_clicked(self) -> None:
        if not self._obs_service or not self._obs_service.is_connected:
            return
        self._obs_service.toggle_recording()

    def _update_rec_button(self) -> None:
        btn = self._rec_btn
        if self._is_recording:
            btn.setIcon(make_icon(ICON_REC_STOP, 14, "#f85149"))
            btn.setText(self.tr("Stop Recording"))
            text_w = btn.fontMetrics().horizontalAdvance(self.tr("Stop Recording"))
            btn.setFixedWidth(text_w + 48)
            btn.setStyleSheet(
                "QPushButton#OBSRecBtn {"
                "  background: rgba(248,81,73,0.10);"
                "  border: 1px solid rgba(248,81,73,0.35);"
                "  border-radius: 8px;"
                "  color: #f85149;"
                "  font-size: 12px; font-weight: 600;"
                "  padding: 0 14px;"
                "  text-align: center;"
                "}"
                "QPushButton#OBSRecBtn:hover {"
                "  background: rgba(248,81,73,0.18);"
                "  border-color: rgba(248,81,73,0.50);"
                "}"
                "QPushButton#OBSRecBtn:pressed {"
                "  background: rgba(248,81,73,0.26);"
                "}"
            )
        else:
            btn.setIcon(make_icon(ICON_REC_CIRCLE, 14, "#f85149"))
            btn.setText(self.tr("Record"))
            text_w = btn.fontMetrics().horizontalAdvance(self.tr("Record"))
            btn.setFixedWidth(text_w + 48)
            btn.setStyleSheet(
                "QPushButton#OBSRecBtn {"
                "  background: rgba(248,81,73,0.06);"
                "  border: 1px solid rgba(48,54,61,0.6);"
                "  border-radius: 8px;"
                "  color: #b1bac4;"
                "  font-size: 12px; font-weight: 500;"
                "  padding: 0 14px;"
                "  text-align: center;"
                "}"
                "QPushButton#OBSRecBtn:hover {"
                "  background: rgba(248,81,73,0.12);"
                "  border-color: rgba(248,81,73,0.35);"
                "  color: #f85149;"
                "}"
                "QPushButton#OBSRecBtn:pressed {"
                "  background: rgba(248,81,73,0.20);"
                "}"
            )
        btn.setIconSize(QSize(14, 14))

    def set_stream_available(self, available: bool) -> None:
        self._stream_available = bool(available)
        self._update_stream_kind()
        self._update_stream_button()

    def set_stream_active(self, active: bool) -> None:
        self._stream_active = bool(active)
        self._update_stream_kind()
        self._update_stream_button()

    def set_camera_stream_available(self, available: bool) -> None:
        self._camera_stream_available = bool(available)
        self._update_stream_kind()
        self._update_stream_button()

    def set_camera_stream_active(self, active: bool) -> None:
        self._camera_stream_active = bool(active)
        self._update_stream_kind()
        self._update_stream_button()

    def _update_stream_kind(self) -> None:
        self._stream_kind = (
            "obs"
            if self._stream_available or self._stream_active
            else "camera"
        )
        visible = (
            self._stream_available
            or self._stream_active
            or self._camera_stream_available
            or self._camera_stream_active
        )
        self._stream_btn.setVisible(visible)

    def _on_stream_clicked(self) -> None:
        if self._stream_kind == "camera":
            self.camera_stream_requested.emit()
        else:
            self.stream_requested.emit()

    def _update_stream_button(self) -> None:
        btn = getattr(self, "_stream_btn", None)
        if btn is None:
            return
        if self._stream_kind == "camera":
            camera_active = getattr(self, "_camera_stream_active", False)
            if camera_active:
                icon_color = "#f85149"
                text = self.tr("Stop Stream")
                tooltip = self.tr("Stop OBS virtual camera")
                bg = "rgba(248,81,73,0.08)"
                border = "rgba(248,81,73,0.30)"
                hover_bg = "rgba(248,81,73,0.15)"
                hover_border = "rgba(248,81,73,0.45)"
                hover_color = "#f85149"
                press_bg = "rgba(248,81,73,0.24)"
            else:
                icon_color = "#58a6ff"
                text = self.tr("Show Stream")
                tooltip = self.tr("Project OBS virtual camera")
                bg = "rgba(56,139,253,0.08)"
                border = "rgba(56,139,253,0.28)"
                hover_bg = "rgba(56,139,253,0.15)"
                hover_border = "rgba(56,139,253,0.45)"
                hover_color = "#58a6ff"
                press_bg = "rgba(56,139,253,0.24)"
        elif self._stream_active:
            icon_color = "#f85149"
            text = self.tr("Stop Stream")
            tooltip = self.tr("Stop OBS program stream")
            bg = "rgba(248,81,73,0.08)"
            border = "rgba(248,81,73,0.30)"
            hover_bg = "rgba(248,81,73,0.15)"
            hover_border = "rgba(248,81,73,0.45)"
            hover_color = "#f85149"
            press_bg = "rgba(248,81,73,0.24)"
        else:
            icon_color = "#58a6ff"
            text = self.tr("Show Stream")
            tooltip = self.tr("Project OBS program stream")
            bg = "rgba(56,139,253,0.08)"
            border = "rgba(56,139,253,0.28)"
            hover_bg = "rgba(56,139,253,0.15)"
            hover_border = "rgba(56,139,253,0.45)"
            hover_color = "#58a6ff"
            press_bg = "rgba(56,139,253,0.24)"

        btn.setIcon(make_icon(ICON_CAST, 14, icon_color))
        btn.setText(text)
        btn.setToolTip(tooltip)
        btn.setFixedWidth(btn.fontMetrics().horizontalAdvance(text) + 48)
        btn.setStyleSheet(
            "QPushButton#OBSStreamBtn {"
            f"  background: {bg};"
            f"  border: 1px solid {border};"
            "  border-radius: 8px;"
            "  color: #c9d1d9;"
            "  font-size: 12px; font-weight: 500;"
            "  padding: 0 14px;"
            "  text-align: center;"
            "}"
            "QPushButton#OBSStreamBtn:hover {"
            f"  background: {hover_bg};"
            f"  border-color: {hover_border};"
            f"  color: {hover_color};"
            "}"
            "QPushButton#OBSStreamBtn:pressed {"
            f"  background: {press_bg};"
            "}"
        )
        btn.setIconSize(QSize(14, 14))

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self._title_lbl.setText(self.tr("OBS Scenes"))
            self._update_rec_button()
            self._update_stream_button()
        super().changeEvent(event)
