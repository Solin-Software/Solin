from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from PySide6.QtCore import QObject, QDateTime, QIODevice, QSaveFile, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QImage, QImageWriter
from PySide6.QtWidgets import QWidget

from solin.core.timer.models import MediaCountdownPresentation
from solin.core.projection.image_framing import (
    IDENTITY_IMAGE_TRANSFORM,
    ImageTransform,
    image_transform_from_values,
)
from solin.core.projection.application import projection_presentation_type
from solin.projection.yearly_text import YearlyTextWidget
from solin.widgets.circular_timer import CircularTimerWidget


log = logging.getLogger(__name__)


def _discard_image_transform(
    _transform: ImageTransform | None,
    **_options: object,
) -> None:
    return


class _ProjectionSession(Protocol):
    @property
    def state(self) -> Mapping[str, Any]: ...

    @property
    def presentation_session_id(self) -> int: ...

    @property
    def image_transform_animate(self) -> bool: ...

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...


class ProgramContentController(QObject):
    """Render application content and the fallback PNG for the shared idle source."""

    def __init__(
        self,
        session: _ProjectionSession,
        font_manager: Any,
        frame_sink: Callable[[object], None],
        yearly_text: Callable[[], tuple[str, str, str]],
        *,
        media_epoch_sink: Callable[[int], None],
        image_transform_sink: Callable[..., None] = _discard_image_transform,
        yeartext_image_path: str = "",
        yeartext_reloaded: Callable[[str, int], None] = lambda _path, _revision: None,
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
        self._yeartext_image_path = yeartext_image_path
        self._yeartext_image_revision = 0
        self._yeartext_reloaded = yeartext_reloaded
        self._yearly_text = yearly_text
        self._width = width
        self._height = height
        self._closed = False
        self._published_presentation_session_id = -1
        self._published_image_transform_key: tuple[object, ...] | None = None
        self._blanked_media_epoch = -1
        self._yearly_widget = YearlyTextWidget(font_manager)
        self._timer_widget = CircularTimerWidget()
        for widget in (self._yearly_widget, self._timer_widget):
            widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            widget.resize(width, height)
        self._unsubscribe = session.subscribe(self._on_projection_changed)
        # Render the year-text source image on the next event-loop turn, not now:
        # the yearly-text provider reads the settings widget, which is constructed
        # after this controller. Deferring lets it exist; the on-disk PNG (from a
        # prior run) covers the first hydrate until then, and update_yearly_text
        # refreshes it live thereafter.
        QTimer.singleShot(0, self.render_yeartext_source_image)
        QTimer.singleShot(0, self.refresh)

    @Slot(object)
    def submit_frame(self, frame: object) -> None:
        if self._closed:
            return
        media_epoch = self._session.presentation_session_id
        self._publish_projection_identity(media_epoch)
        self._publish_image_transform(media_epoch, animate=False)
        # Real content on the channel retires the blank; the next idle re-blanks.
        self._blanked_media_epoch = -1
        self._frame_sink(frame)

    @Slot(str, str, str)
    def update_yearly_text(self, _quote: str, _reference: str, _api_code: str) -> None:
        self.render_yeartext_source_image()
        if self._session.state.get("presentation") == MediaCountdownPresentation.YEARLY_TEXT.value:
            self.refresh()

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
        state_type = projection_presentation_type(state)
        if state_type == "image":
            if (
                self._session.presentation_session_id
                == self._published_presentation_session_id
            ):
                self._publish_image_transform(
                    self._published_presentation_session_id,
                    animate=False,
                )
            return
        if state_type == "idle":
            self._blank_content()
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
        media_epoch = self._session.presentation_session_id
        self._publish_projection_identity(media_epoch)
        self._publish_image_transform(
            media_epoch,
            animate=self._session.image_transform_animate,
        )
        self.refresh()

    def _publish_projection_identity(self, media_epoch: int) -> None:
        if media_epoch == self._published_presentation_session_id:
            return
        self._media_epoch_sink(media_epoch)
        self._published_presentation_session_id = media_epoch

    def _publish_image_transform(
        self,
        media_epoch: int,
        *,
        animate: bool,
    ) -> None:
        state = self._session.state
        transform = (
            image_transform_from_values(state.get("transform"))
            if state.get("type") == "image"
            else None
        )
        if state.get("type") == "image" and transform is None:
            transform = IDENTITY_IMAGE_TRANSFORM
        canvas_width = self._width
        canvas_height = self._height
        key = (media_epoch, transform, canvas_width, canvas_height)
        if key == self._published_image_transform_key:
            return
        self._image_transform_sink(
            transform,
            media_epoch=media_epoch,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            animate=animate,
        )
        self._published_image_transform_key = key

    def _blank_content(self) -> None:
        """Publish an empty frame: idle means the content source has nothing to show.

        The content source carries what Solin is *presenting* — media, images,
        timers, the browser. Idle belongs to a separate shared scene source,
        regardless of whether it shows year text, an image, or a video.
        Transparent rather than black so the source composites away entirely.
        """
        if self._blanked_media_epoch == self._session.presentation_session_id:
            return
        image = QImage(self._width, self._height, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        self.submit_frame(image)
        self._blanked_media_epoch = self._session.presentation_session_id

    def render_yeartext_source_image(self) -> None:
        """Atomically render the fallback PNG and notify its committed revision.

        An empty injected path disables file rendering. Failed encoding or writes
        preserve the previous file and never announce a new engine revision.
        """
        if self._closed or not self._yeartext_image_path:
            return
        path = self._yeartext_image_path
        try:
            quote, reference, api_code = self._yearly_text()
        except Exception:  # noqa: BLE001 - settings may still be loading
            log.debug("Yearly text is not ready for the year-text source", exc_info=True)
            return
        self._yearly_widget.clear_countdown()
        self._yearly_widget.set_text(quote, reference, api_code)
        image = QImage(self._width, self._height, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        self._yearly_widget.ensurePolished()
        self._yearly_widget.render(image)
        output = QSaveFile(path)
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            log.warning("Could not open the year-text source image: %s", output.errorString())
            return
        writer = QImageWriter(output, b"PNG")
        if not writer.write(image):
            output.cancelWriting()
            log.warning("Could not encode the year-text source image: %s", writer.errorString())
            return
        if not output.commit():
            log.warning("Could not write the year-text source image: %s", output.errorString())
            return
        self._yeartext_image_revision += 1
        self._yeartext_reloaded(path, self._yeartext_image_revision)

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
