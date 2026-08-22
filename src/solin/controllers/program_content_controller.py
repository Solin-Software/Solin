from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from PySide6.QtCore import QObject, QDateTime, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget

from solin.core.timer.models import MediaCountdownPresentation
from solin.projection.yearly_text import YearlyTextWidget
from solin.widgets.circular_timer import CircularTimerWidget


log = logging.getLogger(__name__)


class _ProjectionSession(Protocol):
    @property
    def state(self) -> Mapping[str, Any]: ...

    @property
    def state_type(self) -> str: ...

    @property
    def idle_media_path(self) -> str: ...

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...


class ProgramContentController(QObject):
    """Render every Solin projection mode into one canonical program source."""

    def __init__(
        self,
        session: _ProjectionSession,
        font_manager: Any,
        frame_sink: Callable[[object], None],
        yearly_text: Callable[[], tuple[str, str, str]],
        *,
        media_epoch_sink: Callable[[int], None],
        width: int,
        height: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if width <= 0 or height <= 0:
            raise ValueError("Program content dimensions must be positive")
        self._session = session
        self._frame_sink = frame_sink
        self._media_epoch_sink = media_epoch_sink
        self._yearly_text = yearly_text
        self._width = width
        self._height = height
        self._closed = False
        self._yearly_widget = YearlyTextWidget(font_manager)
        self._timer_widget = CircularTimerWidget()
        for widget in (self._yearly_widget, self._timer_widget):
            widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            widget.resize(width, height)
        self._unsubscribe = session.subscribe(self._on_projection_changed)
        QTimer.singleShot(0, self.refresh)

    @Slot(object)
    def submit_frame(self, frame: object) -> None:
        if self._closed:
            return
        self._media_epoch_sink(self._session.session_id)
        self._frame_sink(frame)

    @Slot(object)
    def submit_idle_frame(self, frame: object) -> None:
        if self._closed or self._session.state_type != "idle" or not self._session.idle_media_path:
            return
        self.submit_frame(frame)

    @Slot(str, str, str)
    def update_yearly_text(self, _quote: str, _reference: str, _api_code: str) -> None:
        if self._session.state_type == "idle" and not self._session.idle_media_path:
            self._render_yearly()

    @Slot(bool)
    def set_timer_blink(self, enabled: bool) -> None:
        self._timer_widget.set_blink(bool(enabled))
        state = self._session.state
        if state.get("type") == "timer" and state.get("presentation") == (
            MediaCountdownPresentation.CIRCULAR.value
        ):
            self._render_widget(self._timer_widget)

    def refresh(self) -> None:
        if self._closed:
            return
        state = self._session.state
        state_type = str(state.get("type", "idle"))
        if state_type == "idle":
            if not self._session.idle_media_path:
                self._render_yearly()
            return
        if state_type == "timer":
            self._render_timer(state)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()
        self._yearly_widget.close()
        self._timer_widget.close()

    def _on_projection_changed(self) -> None:
        self.refresh()

    def _render_yearly(self) -> None:
        try:
            quote, reference, api_code = self._yearly_text()
        except Exception:  # noqa: BLE001 - settings may still be loading
            log.debug("Yearly text is not ready for program rendering", exc_info=True)
            return
        self._yearly_widget.clear_countdown()
        self._yearly_widget.set_text(quote, reference, api_code)
        self._render_widget(self._yearly_widget)

    def _render_timer(self, state: Mapping[str, Any]) -> None:
        remaining = state.get("remaining")
        total = state.get("total", 1)
        if not isinstance(remaining, int) or isinstance(remaining, bool):
            target = state.get("target_dt")
            remaining = (
                max(0, QDateTime.currentDateTime().secsTo(target))
                if isinstance(target, QDateTime) and target.isValid()
                else 0
            )
        if not isinstance(total, int) or isinstance(total, bool):
            total = 1
        presentation = state.get("presentation", MediaCountdownPresentation.CIRCULAR.value)
        if presentation == MediaCountdownPresentation.YEARLY_TEXT.value:
            try:
                quote, reference, api_code = self._yearly_text()
            except Exception:  # noqa: BLE001 - settings may still be loading
                quote, reference, api_code = "", "", ""
            self._yearly_widget.set_text(quote, reference, api_code)
            self._yearly_widget.set_countdown(remaining, total)
            self._render_widget(self._yearly_widget)
            return
        self._timer_widget.update_data(remaining, total)
        self._render_widget(self._timer_widget)

    def _render_widget(self, widget: QWidget) -> None:
        image = QImage(self._width, self._height, QImage.Format.Format_ARGB32)
        image.fill(QColor(0, 0, 0))
        widget.ensurePolished()
        widget.render(image)
        self.submit_frame(image)


__all__ = ["ProgramContentController"]
