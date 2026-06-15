from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


__all__ = ("CropOverlay",)


class CropOverlay(QWidget):
    """
    Transparent native overlay shown above the webview while selecting a crop.
    Emits crop_confirmed(x, y, w, h) in physical pixels, or crop_cancelled().
    """

    crop_confirmed = Signal(float, float, float, float)
    crop_cancelled = Signal()

    _DARK = QColor(0, 0, 0, 140)
    _PEN_W = QPen(QColor(255, 255, 255, 230), 2, Qt.PenStyle.DashLine)
    _PEN_O = QPen(QColor(0, 0, 0, 100), 1, Qt.PenStyle.SolidLine)
    _MIN_DRAG = 8

    def __init__(self, parent: QWidget, toggle_btn: QWidget | None = None):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(None, flags)
        self._target = parent
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._start_x = self._start_y = 0
        self._cur_x = self._cur_y = 0
        self._dragging = False
        self._has_sel = False
        self._toggle_btn = toggle_btn
        self._sync_geometry()
        self.raise_()
        self.show()
        self.activateWindow()

    def resizeEvent(self, event):
        super().resizeEvent(event)

    def _sync_geometry(self):
        if self._target:
            top_left = self._target.mapToGlobal(self._target.rect().topLeft())
            self.setGeometry(
                top_left.x(),
                top_left.y(),
                self._target.width(),
                self._target.height(),
            )

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        width, height = self.width(), self.height()

        if not self._has_sel:
            painter.fillRect(0, 0, width, height, self._DARK)
        else:
            rx = min(self._start_x, self._cur_x)
            ry = min(self._start_y, self._cur_y)
            rw = abs(self._cur_x - self._start_x)
            rh = abs(self._cur_y - self._start_y)
            painter.fillRect(0, 0, width, ry, self._DARK)
            painter.fillRect(0, ry + rh, width, height - ry - rh, self._DARK)
            painter.fillRect(0, ry, rx, rh, self._DARK)
            painter.fillRect(rx + rw, ry, width - rx - rw, rh, self._DARK)
            painter.setPen(self._PEN_O)
            painter.drawRect(rx - 1, ry - 1, rw + 1, rh + 1)
            painter.setPen(self._PEN_W)
            painter.drawRect(rx, ry, rw, rh)

        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            self.crop_cancelled.emit()
            return
        self._start_x = event.position().x()
        self._start_y = event.position().y()
        self._cur_x = self._start_x
        self._cur_y = self._start_y
        self._dragging = True
        self._has_sel = False
        self.update()

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        self._cur_x = event.position().x()
        self._cur_y = event.position().y()
        dx = abs(self._cur_x - self._start_x)
        dy = abs(self._cur_y - self._start_y)
        self._has_sel = dx > self._MIN_DRAG and dy > self._MIN_DRAG
        self.update()

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        rx = min(self._start_x, event.position().x())
        ry = min(self._start_y, event.position().y())
        rw = abs(event.position().x() - self._start_x)
        rh = abs(event.position().y() - self._start_y)
        if rw < self._MIN_DRAG or rh < self._MIN_DRAG:
            self.crop_cancelled.emit()
            return
        dpr = self.devicePixelRatio()
        self.crop_confirmed.emit(rx * dpr, ry * dpr, rw * dpr, rh * dpr)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.crop_cancelled.emit()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)

    def wheelEvent(self, event):
        if self._dragging:
            event.accept()
            return

        if self._target:
            QApplication.sendEvent(self._target, event)
        else:
            event.ignore()
