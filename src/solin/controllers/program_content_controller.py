from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from PySide6.QtCore import QObject, QDateTime, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget

from solin.core.timer.models import MediaCountdownPresentation
from solin.core.projection.aspect_ratio import (
    DEFAULT_PROJECTION_ASPECT_RATIO,
    ProjectionAspectRatio,
)
from solin.core.projection.image_framing import (
    IDENTITY_IMAGE_TRANSFORM,
    ImageTransform,
    image_transform_from_values,
)
from solin.projection.yearly_text import YearlyTextWidget
from solin.widgets.circular_timer import CircularTimerWidget


log = logging.getLogger(__name__)


def _discard_image_transform(
    _transform: ImageTransform | None,
    **_options: object,
) -> None:
    return


def _default_projection_aspect_ratio() -> ProjectionAspectRatio:
    return DEFAULT_PROJECTION_ASPECT_RATIO


class _ProjectionSession(Protocol):
    @property
    def state(self) -> Mapping[str, Any]: ...

    @property
    def state_type(self) -> str: ...

    @property
    def idle_media_path(self) -> str: ...

    @property
    def image_transform_animate(self) -> bool: ...

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
        image_transform_sink: Callable[..., None] = _discard_image_transform,
        projection_aspect_ratio_provider: Callable[[], ProjectionAspectRatio] = (
            _default_projection_aspect_ratio
        ),
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
        self._image_transform_sink = image_transform_sink
        self._projection_aspect_ratio_provider = projection_aspect_ratio_provider
        self._yearly_text = yearly_text
        self._width = width
        self._height = height
        self._closed = False
        self._published_projection_session_id = -1
        self._published_image_transform_key: tuple[object, ...] | None = None
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
        media_epoch = self._session.session_id
        self._media_epoch_sink(media_epoch)
        self._publish_image_transform(media_epoch, animate=False, force=True)
        self._frame_sink(frame)
        self._published_projection_session_id = media_epoch

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
        if state_type == "image":
            if self._session.session_id == self._published_projection_session_id:
                self._publish_image_transform(
                    self._published_projection_session_id,
                    animate=False,
                )
            return
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
        if self._session.session_id == self._published_projection_session_id:
            self._publish_image_transform(
                self._published_projection_session_id,
                animate=self._session.image_transform_animate,
            )
        self.refresh()

    def _publish_image_transform(
        self,
        media_epoch: int,
        *,
        animate: bool,
        force: bool = False,
    ) -> None:
        state = self._session.state
        transform = (
            image_transform_from_values(state.get("transform"))
            if state.get("type") == "image"
            else None
        )
        if state.get("type") == "image" and transform is None:
            transform = IDENTITY_IMAGE_TRANSFORM
        try:
            ratio = self._projection_aspect_ratio_provider()
        except Exception:  # noqa: BLE001 - optional projection target boundary
            ratio = DEFAULT_PROJECTION_ASPECT_RATIO
        if not isinstance(ratio, ProjectionAspectRatio):
            ratio = DEFAULT_PROJECTION_ASPECT_RATIO
        canvas_width, canvas_height = _fit_aspect_inside(
            self._width,
            self._height,
            ratio.value,
        )
        key = (media_epoch, transform, canvas_width, canvas_height)
        if not force and key == self._published_image_transform_key:
            return
        self._image_transform_sink(
            transform,
            media_epoch=media_epoch,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            animate=animate,
        )
        self._published_image_transform_key = key

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


def _fit_aspect_inside(
    maximum_width: int,
    maximum_height: int,
    aspect_ratio: float,
) -> tuple[int, int]:
    ratio = aspect_ratio if aspect_ratio > 0.0 else 16.0 / 9.0
    if maximum_width / maximum_height > ratio:
        return max(1, round(maximum_height * ratio)), maximum_height
    return maximum_width, max(1, round(maximum_width / ratio))


__all__ = ["ProgramContentController"]
