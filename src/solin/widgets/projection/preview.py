from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from solin.core.projection.image_framing import (
    IDENTITY_IMAGE_TRANSFORM,
    ImageTransform,
    clamp_transform_to_frame,
    frame_for_aspect,
    initial_transform_for_frame,
    normalize_image_transform,
    snap_zoom_to_frame_cover,
)
from solin.styles.icons import ICON_ASPECT_MATCH, ICON_BOUNDS, make_icon
from solin.styles.theme import PALETTE, current_theme_scheme, qss_rgba


class ImagePreviewWidget(QWidget):
    """
    Preview widget that replaces the plain QLabel inside the expanded overlay.

    Normal mode draws the stored pixmap scaled-to-fit. Image mode adds
    interactive zoom and pan, then emits transforms for the projection windows.
    """

    apply_transform = Signal(float, float, float)
    reset_transform = Signal()
    match_projection_aspect_changed = Signal(bool)
    constrain_to_frame_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._zoom: float = 1.0
        self._norm_x: float = 0.0
        self._norm_y: float = 0.0
        self._drag_start: QPointF | None = None
        self._drag_norm_start: tuple[float, float] = (0.0, 0.0)
        self._drag_frame_size: tuple[float, float] = (1.0, 1.0)
        self._image_mode: bool = False
        self._match_projection_aspect: bool = False
        self._requested_constrain_to_frame: bool = False
        self._aspect_ratio: float = 16.0 / 9.0
        self._aspect_ratio_label: str = "16:9"

        self.setStyleSheet(f"background: {PALETTE.bg0};")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._action_bar = self._build_action_bar()
        self._action_bar.setVisible(False)
        self._sync_tool_buttons()

    @staticmethod
    def _action_bar_stylesheet() -> str:
        alpha = 0.94 if current_theme_scheme() == "dark" else 0.96
        return (
            "QWidget#ImageToolPill{"
            f"background:{qss_rgba(PALETTE.bg0, alpha)};"
            f"border:1px solid {qss_rgba(PALETTE.border, 0.86)};"
            "border-radius:19px;"
            "}"
        )

    @staticmethod
    def _tool_button_stylesheet() -> str:
        return (
            "QPushButton{background:transparent;border:none;border-radius:13px;"
            "padding:0;}"
            f"QPushButton:hover{{background:{qss_rgba(PALETTE.text_primary, 0.08)};}}"
            f"QPushButton:checked{{background:{qss_rgba(PALETTE.accent, 0.20)};}}"
            f"QPushButton:checked:hover{{background:{qss_rgba(PALETTE.accent, 0.28)};}}"
            "QPushButton:disabled{background:transparent;}"
        )

    @staticmethod
    def _reset_button_stylesheet() -> str:
        return (
            f"QPushButton{{background:transparent;color:{PALETTE.text_muted};"
            f"border:1px solid {qss_rgba(PALETTE.border, 0.95)};border-radius:13px;"
            "font-size:11px;font-weight:500;padding:0 10px;}"
            f"QPushButton:hover{{background:{PALETTE.surface_hover};color:{PALETTE.text_secondary};"
            f"border-color:{PALETTE.border_strong};}}"
            f"QPushButton:pressed{{background:{PALETTE.surface_hover_strong};}}"
        )

    @staticmethod
    def _apply_button_stylesheet() -> str:
        return (
            f"QPushButton{{background:{PALETTE.accent_selection};color:{PALETTE.text_on_accent};"
            "border:none;border-radius:13px;"
            "font-size:11px;font-weight:600;padding:0 13px;}"
            f"QPushButton:hover{{background:{PALETTE.accent_hover};}}"
            f"QPushButton:pressed{{background:{PALETTE.accent_pressed};}}"
        )

    @staticmethod
    def _zoom_label_stylesheet() -> str:
        return (
            f"background:transparent;color:{PALETTE.text_muted};"
            "font-size:11px;font-weight:600;min-width:44px;"
        )

    def _build_action_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("ImageToolPill")
        bar.setStyleSheet(self._action_bar_stylesheet())
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        self._aspect_btn = self._tool_button(
            ICON_ASPECT_MATCH,
            self.tr("Match projection aspect"),
        )
        self._aspect_btn.setCheckable(True)
        self._aspect_btn.clicked.connect(
            lambda checked: self.match_projection_aspect_changed.emit(bool(checked))
        )

        self._bounds_btn = self._tool_button(
            ICON_BOUNDS,
            self.tr("Keep image covering the frame"),
        )
        self._bounds_btn.setCheckable(True)
        self._bounds_btn.clicked.connect(
            lambda checked: self.constrain_to_frame_changed.emit(bool(checked))
        )

        self._zoom_lbl = QLabel("100%")
        self._zoom_lbl.setStyleSheet(self._zoom_label_stylesheet())
        self._zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._reset_btn = QPushButton(self.tr("Reset"))
        self._reset_btn.setFixedHeight(26)
        self._reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_btn.setStyleSheet(self._reset_button_stylesheet())
        self._reset_btn.clicked.connect(self._on_reset)

        self._apply_btn = QPushButton(self.tr("Apply to Projector"))
        self._apply_btn.setFixedHeight(26)
        self._apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_btn.setStyleSheet(self._apply_button_stylesheet())
        self._apply_btn.clicked.connect(self._on_apply)

        lay.addWidget(self._aspect_btn)
        lay.addWidget(self._bounds_btn)
        lay.addWidget(self._zoom_lbl)
        lay.addWidget(self._reset_btn)
        lay.addWidget(self._apply_btn)
        bar.adjustSize()
        return bar

    def _tool_button(self, icon: str, tooltip: str) -> QPushButton:
        button = QPushButton()
        button.setFixedSize(26, 26)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tooltip)
        button._solin_icon = icon  # type: ignore[attr-defined]
        button.setStyleSheet(self._tool_button_stylesheet())
        return button

    def apply_theme(self) -> None:
        self.setStyleSheet(f"background: {PALETTE.bg0};")
        self._action_bar.setStyleSheet(self._action_bar_stylesheet())
        self._zoom_lbl.setStyleSheet(self._zoom_label_stylesheet())
        self._reset_btn.setStyleSheet(self._reset_button_stylesheet())
        self._apply_btn.setStyleSheet(self._apply_button_stylesheet())
        self._aspect_btn.setStyleSheet(self._tool_button_stylesheet())
        self._bounds_btn.setStyleSheet(self._tool_button_stylesheet())
        self._sync_tool_buttons()
        self._check_action_bar()
        self.update()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self._reset_btn.setText(self.tr("Reset"))
            self._apply_btn.setText(self.tr("Apply to Projector"))
            self._sync_tool_buttons()
        super().changeEvent(event)

    def configure_framing(
        self,
        *,
        match_projection_aspect: bool,
        constrain_to_frame: bool,
        aspect_ratio: float,
        aspect_ratio_label: str,
        reset: bool = False,
    ) -> ImageTransform:
        self._match_projection_aspect = bool(match_projection_aspect)
        self._requested_constrain_to_frame = bool(constrain_to_frame)
        self._aspect_ratio = aspect_ratio if aspect_ratio > 0 else 16.0 / 9.0
        self._aspect_ratio_label = aspect_ratio_label or "16:9"
        self._sync_tool_buttons()
        if reset:
            self._set_initial_transform()
        else:
            self._clamp_current_transform()
        self._update_zoom_label()
        self._check_action_bar()
        self.update()
        return self.current_transform()

    def current_transform(self) -> ImageTransform:
        return ImageTransform(self._zoom, self._norm_x, self._norm_y)

    def set_current_transform(self, transform: ImageTransform) -> ImageTransform:
        normalized = normalize_image_transform(transform)
        self._zoom = normalized.zoom
        self._norm_x = normalized.norm_x
        self._norm_y = normalized.norm_y
        self._clamp_current_transform()
        self._update_zoom_label()
        self._check_action_bar()
        self.update()
        return self.current_transform()

    def set_image_mode(self, active: bool):
        self._image_mode = active
        if active:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.setFocus()
        else:
            self.unsetCursor()
            self._zoom = 1.0
            self._norm_x = 0.0
            self._norm_y = 0.0
            self._update_zoom_label()
            self._action_bar.setVisible(False)
        self._check_action_bar()

    def set_image_pixmap_fresh(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self._set_initial_transform()
        self._update_zoom_label()
        self._check_action_bar()
        self.update()

    def setPixmap(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self._clamp_current_transform()
        self._check_action_bar()
        self.update()

    def setText(self, text: str):
        pass

    def setAlignment(self, *args):
        pass

    def _sync_tool_buttons(self) -> None:
        self._sync_icon_button(self._aspect_btn, self._match_projection_aspect)
        self._aspect_btn.setChecked(self._match_projection_aspect)
        self._aspect_btn.setToolTip(
            self.tr("Match projection aspect ({ratio})").format(
                ratio=self._aspect_ratio_label
            )
        )

        self._bounds_btn.setEnabled(self._match_projection_aspect)
        self._bounds_btn.setChecked(
            self._match_projection_aspect and self._requested_constrain_to_frame
        )
        self._sync_icon_button(
            self._bounds_btn,
            self._match_projection_aspect and self._requested_constrain_to_frame,
        )
        if self._match_projection_aspect:
            self._bounds_btn.setToolTip(self.tr("Keep image covering the frame"))
        else:
            self._bounds_btn.setToolTip(self.tr("Enable projection aspect first"))

    def _sync_icon_button(self, button: QPushButton, active: bool) -> None:
        icon = getattr(button, "_solin_icon", "")
        color = PALETTE.accent_hover if active else PALETTE.text_muted
        if not button.isEnabled():
            color = PALETTE.text_dim
        button.setIcon(make_icon(icon, 15, color))

    def _position_action_bar(self):
        bar = self._action_bar
        self._prepare_action_bar_geometry()
        bw, bh = bar.width(), bar.height()
        x = (self.width() - bw) // 2
        y = self.height() - bh - 18
        bar.move(max(0, x), max(0, y))

    def _prepare_action_bar_geometry(self) -> None:
        bar = self._action_bar
        bar.ensurePolished()
        for child in bar.findChildren(QWidget):
            child.ensurePolished()
        layout = bar.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()
            size = layout.sizeHint()
        else:
            size = bar.sizeHint()
        bar.setFixedSize(size)

    def _check_action_bar(self):
        show = self._image_mode and self._pixmap is not None and not self._pixmap.isNull()
        if show and not self._action_bar.isVisible():
            self._action_bar.setVisible(True)
            self._action_bar.raise_()
            self._position_action_bar()
        elif not show and self._action_bar.isVisible():
            self._action_bar.setVisible(False)
        elif show:
            self._position_action_bar()

    def _update_zoom_label(self):
        self._zoom_lbl.setText(f"{int(round(self._zoom * 100))}%")

    def _on_reset(self):
        transform = self.reset_to_initial_transform()
        if transform == IDENTITY_IMAGE_TRANSFORM:
            self.reset_transform.emit()
        else:
            self.apply_transform.emit(transform.zoom, transform.norm_x, transform.norm_y)

    def reset_to_initial_transform(self) -> ImageTransform:
        self._set_initial_transform()
        self._update_zoom_label()
        self._check_action_bar()
        self.update()
        return self.current_transform()

    def _on_apply(self):
        transform = self.current_transform()
        self.apply_transform.emit(transform.zoom, transform.norm_x, transform.norm_y)

    def wheelEvent(self, event):
        if self._image_mode and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            delta = event.angleDelta().y()
            factor = 1.12 if delta > 0 else (1.0 / 1.12)
            self._apply_zoom_factor(factor)
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event):
        if self._image_mode:
            if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self._apply_zoom_factor(1.15)
                event.accept()
                return
            if event.key() == Qt.Key.Key_Minus:
                self._apply_zoom_factor(1.0 / 1.15)
                event.accept()
                return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if self._image_mode and event.button() == Qt.MouseButton.LeftButton:
            frame = self._active_frame()
            self._drag_start = event.position()
            self._drag_norm_start = (self._norm_x, self._norm_y)
            self._drag_frame_size = (
                max(1.0, frame.width()),
                max(1.0, frame.height()),
            )
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._image_mode and self._drag_start is not None:
            delta = event.position() - self._drag_start
            frame_w, frame_h = self._drag_frame_size
            start_x, start_y = self._drag_norm_start
            self._norm_x = start_x + (delta.x() / frame_w)
            self._norm_y = start_y + (delta.y() / frame_h)
            self._clamp_current_transform()
            self._check_action_bar()
            self.update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            self._drag_start = None
            if self._image_mode:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp_current_transform()
        if self._action_bar.isVisible():
            self._position_action_bar()

    def _constraint_active(self) -> bool:
        return self._match_projection_aspect and self._requested_constrain_to_frame

    def _active_frame(self) -> QRectF:
        if self._match_projection_aspect:
            frame = frame_for_aspect(self.width(), self.height(), self._aspect_ratio)
            return QRectF(frame.x, frame.y, frame.width, frame.height)
        return QRectF(0.0, 0.0, float(self.width()), float(self.height()))

    def _set_initial_transform(self) -> None:
        pix = self._pixmap
        frame = self._active_frame()
        if pix is None or pix.isNull():
            transform = IDENTITY_IMAGE_TRANSFORM
        else:
            transform = initial_transform_for_frame(
                pix.width(),
                pix.height(),
                frame.width(),
                frame.height(),
                constrain_to_frame=self._constraint_active(),
            )
        self._zoom = transform.zoom
        self._norm_x = transform.norm_x
        self._norm_y = transform.norm_y

    def _clamp_current_transform(self) -> None:
        self._zoom = max(0.1, min(10.0, self._zoom))
        if not self._constraint_active():
            return
        pix = self._pixmap
        if pix is None or pix.isNull():
            return
        frame = self._active_frame()
        transform = clamp_transform_to_frame(
            pix.width(),
            pix.height(),
            frame.width(),
            frame.height(),
            self.current_transform(),
        )
        self._zoom = transform.zoom
        self._norm_x = transform.norm_x
        self._norm_y = transform.norm_y

    def _apply_zoom_factor(self, factor: float) -> None:
        previous_zoom = self._zoom
        requested_zoom = max(0.1, min(10.0, previous_zoom * factor))
        if self._constraint_active():
            pix = self._pixmap
            if pix is not None and not pix.isNull():
                frame = self._active_frame()
                requested_zoom = snap_zoom_to_frame_cover(
                    previous_zoom,
                    requested_zoom,
                    pix.width(),
                    pix.height(),
                    frame.width(),
                    frame.height(),
                )
        self._zoom = requested_zoom
        self._clamp_current_transform()
        self._update_zoom_label()
        self._check_action_bar()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(PALETTE.bg0))

        pix = self._pixmap
        if pix is None or pix.isNull():
            painter.end()
            return

        frame = self._active_frame()
        pw, ph = pix.width(), pix.height()
        if pw <= 0 or ph <= 0 or frame.width() <= 0 or frame.height() <= 0:
            painter.end()
            return

        base_scale = min(frame.width() / pw, frame.height() / ph)

        if self._image_mode:
            scale = base_scale * self._zoom
            dw = pw * scale
            dh = ph * scale
            dx = frame.x() + (frame.width() - dw) / 2.0 + self._norm_x * frame.width()
            dy = frame.y() + (frame.height() - dh) / 2.0 + self._norm_y * frame.height()
        else:
            scale = base_scale
            dw = pw * scale
            dh = ph * scale
            dx = frame.x() + (frame.width() - dw) / 2.0
            dy = frame.y() + (frame.height() - dh) / 2.0

        if self._match_projection_aspect:
            painter.save()
            painter.setClipRect(frame)
        painter.drawPixmap(QRectF(dx, dy, dw, dh), pix, QRectF(0, 0, pw, ph))
        if self._match_projection_aspect:
            painter.restore()
        if self._match_projection_aspect:
            painter.setPen(QPen(QColor(139, 148, 158, 92), 1))
            painter.drawRoundedRect(frame.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        painter.end()
