"""OBS scene popup used by the quick access toolbar."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QSize,
    Qt,
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
from solin.styles.theme import PALETTE, qss_rgba
from solin.ui.themed_tooltip import install_themed_tooltip
from solin.widgets.common.button_feedback import ButtonSuccessFlash
from solin.widgets.common.flow_container import FlowContainer


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
        self._scenes: list[str] = []
        self._idle_scene = ""
        self._media_scene = ""
        self._return_scene_override_enabled = False
        self._success_flash = ButtonSuccessFlash(self)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("OBSSceneCard")
        self._card.setStyleSheet(self._card_style())
        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        self._hdr = QWidget()
        self._hdr.setStyleSheet("background: transparent;")
        h_lay = QHBoxLayout(self._hdr)
        h_lay.setContentsMargins(18, 14, 18, 6)
        h_lay.setSpacing(8)

        self._obs_icon = QLabel()
        self._obs_icon.setPixmap(make_icon(ICON_OBS, 14, PALETTE.text_faint).pixmap(14, 14))
        self._obs_icon.setFixedSize(14, 14)
        self._obs_icon.setStyleSheet("background: transparent;")

        self._title_lbl = QLabel(self.tr("OBS Scenes"))
        self._title_lbl.setStyleSheet(self._title_style())

        self._active_lbl = QLabel()
        self._active_lbl.setStyleSheet(self._active_label_style())

        h_lay.addWidget(self._obs_icon)
        h_lay.addWidget(self._title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(self._active_lbl)
        card_lay.addWidget(self._hdr)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(self._scroll_style())

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

        self._footer_sep = QFrame()
        self._footer_sep.setFixedHeight(1)
        self._footer_sep.setStyleSheet(self._divider_style())

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
        install_themed_tooltip(self._stream_btn)
        self._update_stream_button()
        f_lay.addWidget(self._stream_btn)
        f_lay.addStretch()

        card_lay.addWidget(self._footer_sep)
        card_lay.addWidget(self._footer)

        root.addWidget(self._card)

    @staticmethod
    def _card_style() -> str:
        return (
            "QFrame#OBSSceneCard {"
            f"  background: {PALETTE.surface};"
            f"  border: 1px solid {qss_rgba(PALETTE.border, 0.85)};"
            "  border-radius: 16px;"
            "}"
        )

    @staticmethod
    def _title_style() -> str:
        return (
            f"color: {PALETTE.text_faint}; font-size: 10px; font-weight: 700;"
            " letter-spacing: 0.6px; background: transparent;"
        )

    @staticmethod
    def _active_label_style() -> str:
        return (
            f"color: {PALETTE.accent_hover}; font-size: 10px; font-weight: 600;"
            f" background: {qss_rgba(PALETTE.accent, 0.08)}; border-radius: 4px;"
            " padding: 2px 8px; border: none;"
        )

    @staticmethod
    def _scroll_style() -> str:
        return (
            "QScrollArea { border: none; background: transparent; }"
            "QScrollBar:vertical {"
            "  width: 3px; background: transparent; margin: 4px 3px 4px 0;"
            "}"
            "QScrollBar::handle:vertical {"
            f"  background: {qss_rgba(PALETTE.text_faint, 0.30)};"
            "  border-radius: 1px; min-height: 20px;"
            "}"
            f"QScrollBar::handle:vertical:hover {{ background: {qss_rgba(PALETTE.text_faint, 0.50)}; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )

    @staticmethod
    def _divider_style() -> str:
        return f"background: {qss_rgba(PALETTE.border, 0.6)}; border: none;"

    @staticmethod
    def _section_label_style() -> str:
        return (
            f"color: {PALETTE.text_dim}; font-size: 9px; font-weight: 700;"
            " letter-spacing: 0.5px; background: transparent; padding: 0 4px;"
        )

    @staticmethod
    def _empty_label_style() -> str:
        return (
            f"color: {PALETTE.text_dim}; font-size: 12px; padding: 20px;"
            " background: transparent;"
        )

    def apply_theme(self) -> None:
        self._card.setStyleSheet(self._card_style())
        self._obs_icon.setPixmap(make_icon(ICON_OBS, 14, PALETTE.text_faint).pixmap(14, 14))
        self._title_lbl.setStyleSheet(self._title_style())
        self._active_lbl.setStyleSheet(self._active_label_style())
        self._scroll.setStyleSheet(self._scroll_style())
        self._footer_sep.setStyleSheet(self._divider_style())
        self._update_rec_button()
        self._update_stream_button()
        self.populate(
            list(self._scenes),
            self._current_scene,
            self._idle_scene,
            self._media_scene,
        )

    def populate(
        self,
        scenes: list[str],
        current_scene: str,
        idle_scene: str,
        media_scene: str,
    ) -> None:
        self._scenes = list(scenes)
        self._current_scene = current_scene
        self._idle_scene = idle_scene
        self._media_scene = media_scene
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
            empty.setStyleSheet(self._empty_label_style())
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
            sec_lbl.setStyleSheet(self._section_label_style())
            self._content_lay.addWidget(sec_lbl)

            flow_p = FlowContainer(horizontal_spacing=6, vertical_spacing=6)
            flow_p.setStyleSheet("background: transparent;")
            for name, badge in pinned:
                flow_p.add_widget(self._make_chip(name, name == current_scene, badge))
            self._content_lay.addWidget(flow_p)

        if rest:
            if pinned:
                sec_lbl2 = QLabel(self.tr("All scenes"))
                sec_lbl2.setStyleSheet(self._section_label_style())
                self._content_lay.addWidget(sec_lbl2)

            flow_r = FlowContainer(horizontal_spacing=6, vertical_spacing=6)
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
            if isinstance(widget, FlowContainer):
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
                f"  background: {qss_rgba(PALETTE.accent, 0.12)};"
                f"  border: 1px solid {qss_rgba(PALETTE.accent, 0.40)};"
                "  border-radius: 8px;"
                f"  color: {PALETTE.accent_hover};"
                "  font-size: 12px; font-weight: 600;"
                "  padding: 0 10px;"
                "  text-align: center;"
                "}"
                f"QPushButton#{uid}:hover {{"
                f"  background: {qss_rgba(PALETTE.accent, 0.20)};"
                f"  border-color: {qss_rgba(PALETTE.accent, 0.55)};"
                "}"
                f"QPushButton#{uid}:pressed {{"
                f"  background: {qss_rgba(PALETTE.accent, 0.28)};"
                "}"
            )
        else:
            if badge == self.tr("idle"):
                border_c = qss_rgba(PALETTE.success, 0.25)
                bg_hover = qss_rgba(PALETTE.success, 0.08)
                text_c = PALETTE.text_secondary
            elif badge == self.tr("media"):
                border_c = qss_rgba(PALETTE.accent, 0.20)
                bg_hover = qss_rgba(PALETTE.accent, 0.06)
                text_c = PALETTE.text_secondary
            else:
                border_c = qss_rgba(PALETTE.border, 0.60)
                bg_hover = PALETTE.surface_hover
                text_c = PALETTE.text_muted

            chip.setStyleSheet(
                f"QPushButton#{uid} {{"
                f"  background: {qss_rgba(PALETTE.surface, 0.60)};"
                f"  border: 1px solid {border_c};"
                "  border-radius: 8px;"
                f"  color: {text_c};"
                "  font-size: 12px; font-weight: 400;"
                "  padding: 0 10px;"
                "  text-align: center;"
                "}"
                f"QPushButton#{uid}:hover {{"
                f"  background: {bg_hover};"
                f"  border-color: {qss_rgba(PALETTE.accent, 0.35)};"
                f"  color: {PALETTE.text_secondary};"
                "}"
                f"QPushButton#{uid}:pressed {{"
                f"  background: {qss_rgba(PALETTE.accent, 0.10)};"
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
        self._success_flash.flash(chip)

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
            btn.setIcon(make_icon(ICON_REC_STOP, 14, PALETTE.danger))
            btn.setText(self.tr("Stop Recording"))
            text_w = btn.fontMetrics().horizontalAdvance(self.tr("Stop Recording"))
            btn.setFixedWidth(text_w + 48)
            btn.setStyleSheet(
                "QPushButton#OBSRecBtn {"
                f"  background: {qss_rgba(PALETTE.danger, 0.10)};"
                f"  border: 1px solid {qss_rgba(PALETTE.danger, 0.35)};"
                "  border-radius: 8px;"
                f"  color: {PALETTE.danger};"
                "  font-size: 12px; font-weight: 600;"
                "  padding: 0 14px;"
                "  text-align: center;"
                "}"
                "QPushButton#OBSRecBtn:hover {"
                f"  background: {qss_rgba(PALETTE.danger, 0.18)};"
                f"  border-color: {qss_rgba(PALETTE.danger, 0.50)};"
                "}"
                "QPushButton#OBSRecBtn:pressed {"
                f"  background: {qss_rgba(PALETTE.danger, 0.26)};"
                "}"
            )
        else:
            btn.setIcon(make_icon(ICON_REC_CIRCLE, 14, PALETTE.danger))
            btn.setText(self.tr("Record"))
            text_w = btn.fontMetrics().horizontalAdvance(self.tr("Record"))
            btn.setFixedWidth(text_w + 48)
            btn.setStyleSheet(
                "QPushButton#OBSRecBtn {"
                f"  background: {qss_rgba(PALETTE.danger, 0.06)};"
                f"  border: 1px solid {qss_rgba(PALETTE.border, 0.60)};"
                "  border-radius: 8px;"
                f"  color: {PALETTE.text_secondary};"
                "  font-size: 12px; font-weight: 500;"
                "  padding: 0 14px;"
                "  text-align: center;"
                "}"
                "QPushButton#OBSRecBtn:hover {"
                f"  background: {qss_rgba(PALETTE.danger, 0.12)};"
                f"  border-color: {qss_rgba(PALETTE.danger, 0.35)};"
                f"  color: {PALETTE.danger};"
                "}"
                "QPushButton#OBSRecBtn:pressed {"
                f"  background: {qss_rgba(PALETTE.danger, 0.20)};"
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
            "obs" if self._stream_available or self._stream_active else "camera"
        )
        self._stream_btn.setVisible(
            self._stream_available
            or self._stream_active
            or self._camera_stream_available
            or self._camera_stream_active
        )

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
            if self._camera_stream_active:
                icon_color = PALETTE.danger
                text = self.tr("Stop Stream")
                tooltip = self.tr("Stop OBS virtual camera")
                bg = qss_rgba(PALETTE.danger, 0.08)
                border = qss_rgba(PALETTE.danger, 0.30)
                hover_bg = qss_rgba(PALETTE.danger, 0.15)
                hover_border = qss_rgba(PALETTE.danger, 0.45)
                hover_color = PALETTE.danger
                press_bg = qss_rgba(PALETTE.danger, 0.24)
            else:
                icon_color = PALETTE.accent_hover
                text = self.tr("Show Stream")
                tooltip = self.tr("Project OBS virtual camera")
                bg = qss_rgba(PALETTE.accent, 0.08)
                border = qss_rgba(PALETTE.accent, 0.28)
                hover_bg = qss_rgba(PALETTE.accent, 0.15)
                hover_border = qss_rgba(PALETTE.accent, 0.45)
                hover_color = PALETTE.accent_hover
                press_bg = qss_rgba(PALETTE.accent, 0.24)
        elif self._stream_active:
            icon_color = PALETTE.danger
            text = self.tr("Stop Stream")
            tooltip = self.tr("Stop OBS program stream")
            bg = qss_rgba(PALETTE.danger, 0.08)
            border = qss_rgba(PALETTE.danger, 0.30)
            hover_bg = qss_rgba(PALETTE.danger, 0.15)
            hover_border = qss_rgba(PALETTE.danger, 0.45)
            hover_color = PALETTE.danger
            press_bg = qss_rgba(PALETTE.danger, 0.24)
        else:
            icon_color = PALETTE.accent_hover
            text = self.tr("Show Stream")
            tooltip = self.tr("Project OBS program stream")
            bg = qss_rgba(PALETTE.accent, 0.08)
            border = qss_rgba(PALETTE.accent, 0.28)
            hover_bg = qss_rgba(PALETTE.accent, 0.15)
            hover_border = qss_rgba(PALETTE.accent, 0.45)
            hover_color = PALETTE.accent_hover
            press_bg = qss_rgba(PALETTE.accent, 0.24)

        btn.setIcon(make_icon(ICON_CAST, 14, icon_color))
        btn.setText(text)
        btn.setToolTip(tooltip)
        btn.setFixedWidth(btn.fontMetrics().horizontalAdvance(text) + 48)
        btn.setStyleSheet(
            "QPushButton#OBSStreamBtn {"
            f"  background: {bg};"
            f"  border: 1px solid {border};"
            "  border-radius: 8px;"
            f"  color: {PALETTE.text_secondary};"
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
