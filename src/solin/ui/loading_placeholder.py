from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QHideEvent, QPainter, QPaintEvent, QShowEvent
from PySide6.QtWidgets import QWidget

from solin.styles.theme import PALETTE


_FRAME_INTERVAL_MS = 16
_ROUND_TRIP_MS = 1_600
_TRACK_WIDTH = 220.0
_TRACK_MARGIN = 24.0
_TRACK_HEIGHT = 3.0
_CHUNK_WIDTH = 72.0


class DeferredLoadingPlaceholder(QWidget):
    """Lightweight animated placeholder for incrementally hydrated pages."""

    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._text = text
        self._active = True
        self._elapsed = QElapsedTimer()
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(_FRAME_INTERVAL_MS)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.timeout.connect(self.update)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName(text)
        self.setGeometry(parent.rect())
        parent.installEventFilter(self)
        self.show()

    @property
    def active(self) -> bool:
        return self._active

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self.setAccessibleName(text)
        self.update()

    def refresh_theme(self) -> None:
        self.update()

    def finish(self) -> None:
        if not self._active:
            return
        self._active = False
        self._frame_timer.stop()
        parent = self.parentWidget()
        if parent is not None:
            parent.removeEventFilter(self)
        self.hide()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        parent = self.parentWidget()
        if parent is not None and watched is parent:
            if event.type() == QEvent.Type.Resize:
                self.setGeometry(parent.rect())
            elif event.type() == QEvent.Type.Show and self._active:
                self.show()
                self.raise_()
            elif event.type() == QEvent.Type.ChildAdded and self._active:
                # Deferred builders add real page children one unit at a time.
                # Keep the placeholder above them until the atomic handoff.
                self.raise_()
            elif event.type() == QEvent.Type.Hide:
                self._frame_timer.stop()
        return super().eventFilter(watched, event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._active:
            return
        self._elapsed.restart()
        self._frame_timer.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self._frame_timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if not self._active:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor(str(PALETTE.text_muted)))
        painter.drawText(
            self.rect().adjusted(0, 0, 0, -18),
            Qt.AlignmentFlag.AlignCenter,
            self._text,
        )

        track_width = min(_TRACK_WIDTH, max(0.0, self.width() - (2 * _TRACK_MARGIN)))
        if track_width <= 0:
            return
        chunk_width = min(_CHUNK_WIDTH, track_width)
        track = QRectF(
            (self.width() - track_width) / 2,
            (self.height() / 2) + 16,
            track_width,
            _TRACK_HEIGHT,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(str(PALETTE.surface)))
        painter.drawRoundedRect(track, 1, 1)

        travel = max(0.0, track_width - chunk_width)
        elapsed_ms = self._elapsed.elapsed() if self._elapsed.isValid() else 0
        chunk = QRectF(
            track.x() + self._chunk_offset(elapsed_ms, travel),
            track.y(),
            chunk_width,
            track.height(),
        )
        painter.setBrush(QColor(str(PALETTE.accent)))
        painter.drawRoundedRect(chunk, 1, 1)

    @staticmethod
    def _chunk_offset(elapsed_ms: int, travel: float) -> float:
        phase = (elapsed_ms % _ROUND_TRIP_MS) / _ROUND_TRIP_MS
        linear = phase * 2 if phase <= 0.5 else (1 - phase) * 2
        eased = linear * linear * (3 - (2 * linear))
        return travel * eased


__all__ = ["DeferredLoadingPlaceholder"]
