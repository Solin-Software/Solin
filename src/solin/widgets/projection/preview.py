from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget


class ImagePreviewWidget(QWidget):
    """
    Preview widget that replaces the plain QLabel inside the expanded overlay.

    Normal mode draws the stored pixmap scaled-to-fit. Image mode adds
    interactive zoom and pan, then emits transforms for the projection windows.
    """

    apply_transform = Signal(float, float, float)
    reset_transform = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._zoom: float = 1.0
        self._offset: QPointF = QPointF(0.0, 0.0)
        self._drag_start: QPointF | None = None
        self._drag_offset_start: QPointF = QPointF(0.0, 0.0)
        self._image_mode: bool = False

        self.setStyleSheet("background: #0d1117;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._action_bar = self._build_action_bar()
        self._action_bar.setVisible(False)

    def _build_action_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("ZoomActionBar")
        bar.setStyleSheet(
            "QWidget#ZoomActionBar{"
            "background:rgba(13,17,23,0.93);"
            "border:1px solid #30363d;"
            "border-radius:20px;"
            "}"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 7, 14, 7)
        lay.setSpacing(10)

        self._zoom_lbl = QLabel("100%")
        self._zoom_lbl.setStyleSheet(
            "background:transparent;color:#8b949e;"
            "font-size:11px;font-weight:600;min-width:44px;"
        )
        self._zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("QFrame{color:#30363d;}")
        sep.setFixedHeight(16)

        self._reset_btn = QPushButton(self.tr("Reset"))
        self._reset_btn.setFixedHeight(26)
        self._reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_btn.setStyleSheet(
            "QPushButton{background:transparent;color:#8b949e;"
            "border:1px solid #30363d;border-radius:6px;"
            "font-size:11px;font-weight:500;padding:0 10px;}"
            "QPushButton:hover{background:#21262d;color:#c9d1d9;"
            "border-color:#484f58;}"
            "QPushButton:pressed{background:#161b22;}"
        )
        self._reset_btn.clicked.connect(self._on_reset)

        self._apply_btn = QPushButton(self.tr("Apply to Projector"))
        self._apply_btn.setFixedHeight(26)
        self._apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_btn.setStyleSheet(
            "QPushButton{background:#1f6feb;color:#ffffff;"
            "border:none;border-radius:6px;"
            "font-size:11px;font-weight:600;padding:0 14px;}"
            "QPushButton:hover{background:#388bfd;}"
            "QPushButton:pressed{background:#1158c7;}"
        )
        self._apply_btn.clicked.connect(self._on_apply)

        lay.addWidget(self._zoom_lbl)
        lay.addWidget(sep)
        lay.addWidget(self._reset_btn)
        lay.addWidget(self._apply_btn)
        bar.adjustSize()
        return bar

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self._reset_btn.setText(self.tr("Reset"))
            self._apply_btn.setText(self.tr("Apply to Projector"))
        super().changeEvent(event)

    def _position_action_bar(self):
        bar = self._action_bar
        bar.adjustSize()
        sh = bar.sizeHint()
        bw, bh = sh.width(), sh.height()
        x = (self.width() - bw) // 2
        y = self.height() - bh - 18
        bar.move(max(0, x), max(0, y))
        bar.resize(bw, bh)

    def _check_action_bar(self):
        show = (
            abs(self._zoom - 1.0) >= 0.05
            or abs(self._offset.x()) > 3.0
            or abs(self._offset.y()) > 3.0
        )
        if show and not self._action_bar.isVisible():
            self._action_bar.setVisible(True)
            self._action_bar.raise_()
            self._position_action_bar()
        elif not show and self._action_bar.isVisible():
            self._action_bar.setVisible(False)

    def _update_zoom_label(self):
        self._zoom_lbl.setText(f"{int(round(self._zoom * 100))}%")

    def _on_reset(self):
        self._zoom = 1.0
        self._offset = QPointF(0.0, 0.0)
        self._update_zoom_label()
        self._check_action_bar()
        self.update()
        self.reset_transform.emit()

    def _on_apply(self):
        w, h = self.width(), self.height()
        norm_x = self._offset.x() / w if w > 0 else 0.0
        norm_y = self._offset.y() / h if h > 0 else 0.0
        self.apply_transform.emit(self._zoom, norm_x, norm_y)

    def set_image_mode(self, active: bool):
        self._image_mode = active
        if active:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.setFocus()
        else:
            self.unsetCursor()
            self._zoom = 1.0
            self._offset = QPointF(0.0, 0.0)
            self._update_zoom_label()
            self._action_bar.setVisible(False)

    def set_image_pixmap_fresh(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self._zoom = 1.0
        self._offset = QPointF(0.0, 0.0)
        self._update_zoom_label()
        self._action_bar.setVisible(False)
        self.update()

    def setPixmap(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self.update()

    def setText(self, text: str):
        pass

    def setAlignment(self, *args):
        pass

    def wheelEvent(self, event):
        if self._image_mode and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            delta = event.angleDelta().y()
            factor = 1.12 if delta > 0 else (1.0 / 1.12)
            self._zoom = max(0.1, min(10.0, self._zoom * factor))
            self._update_zoom_label()
            self._check_action_bar()
            self.update()
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event):
        if self._image_mode:
            if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self._zoom = min(10.0, self._zoom * 1.15)
                self._update_zoom_label()
                self._check_action_bar()
                self.update()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Minus:
                self._zoom = max(0.1, self._zoom / 1.15)
                self._update_zoom_label()
                self._check_action_bar()
                self.update()
                event.accept()
                return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if self._image_mode and event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()
            self._drag_offset_start = QPointF(self._offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._image_mode and self._drag_start is not None:
            delta = event.position() - self._drag_start
            self._offset = self._drag_offset_start + delta
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
        if self._action_bar.isVisible():
            self._position_action_bar()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0d1117"))

        pix = self._pixmap
        if pix is None or pix.isNull():
            painter.end()
            return

        w, h = self.width(), self.height()
        pw, ph = pix.width(), pix.height()
        if pw <= 0 or ph <= 0 or w <= 0 or h <= 0:
            painter.end()
            return

        base_scale = min(w / pw, h / ph)

        if self._image_mode:
            scale = base_scale * self._zoom
            dw = pw * scale
            dh = ph * scale
            dx = (w - dw) / 2.0 + self._offset.x()
            dy = (h - dh) / 2.0 + self._offset.y()
        else:
            scale = base_scale
            dw = pw * scale
            dh = ph * scale
            dx = (w - dw) / 2.0
            dy = (h - dh) / 2.0

        painter.drawPixmap(
            QRectF(dx, dy, dw, dh),
            pix,
            QRectF(0, 0, pw, ph),
        )
        painter.end()
