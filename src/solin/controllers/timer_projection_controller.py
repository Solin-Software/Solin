from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QCoreApplication, QDateTime, QT_TRANSLATE_NOOP

from ..core.foundation.time_utils import ceil_remaining_seconds
from ..core.timer.models import MediaCountdownPresentation


_TR_CONTEXT = "TimerProjection"
_TIMER_SOURCE = QT_TRANSLATE_NOOP("TimerProjection", "Timer")
_TIMER_STATUS_SOURCE = QT_TRANSLATE_NOOP(
    "TimerProjection",
    "Timer → {time}",
)


def _tr(source: str) -> str:
    return QCoreApplication.translate(_TR_CONTEXT, source)


@dataclass(frozen=True, slots=True)
class TimerProjectionContext:
    """Dependencies for countdown projection workflows."""

    projection_session: Any
    projection_bar: Any
    media_controller: Any
    ndi_service: Any
    projection_windows: Callable[[], list[Any]]
    playback_protection: Any
    program_content: Any | None
    camera_service: Any | None = None


@dataclass(frozen=True, slots=True)
class TimerProjectionHandlers:
    """Shell actions invoked before and after countdown projection."""

    stop_browser_tab_projection: Callable[[], None]
    update_projection_status: Callable[..., None]


class TimerProjectionController:
    """Projects countdown visuals to the media projection windows."""

    def __init__(
        self,
        context: TimerProjectionContext,
        handlers: TimerProjectionHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers
        self._session = context.projection_session

    def start_timer(self, target_dt: QDateTime, presentation_value: str) -> bool:
        if not self._context.playback_protection.allow_manual_projection_change():
            return False
        return self._start_timer(target_dt, presentation_value)

    def start_automatic_timer(
        self,
        target_dt: QDateTime,
        presentation_value: str,
        occurrence_id: str,
    ) -> bool:
        context = self._context
        if not target_dt.isValid() or target_dt <= QDateTime.currentDateTime():
            return False
        if self._session.has_active_projection():
            return False
        if not context.playback_protection.allow_automatic_projection_change(
            projection_active=False,
        ):
            return False
        return self._start_timer(
            target_dt,
            presentation_value,
            origin="automatic_countdown",
            occurrence_id=occurrence_id,
        )

    def _start_timer(
        self,
        target_dt: QDateTime,
        presentation_value: str,
        *,
        origin: str = "manual",
        occurrence_id: str = "",
    ) -> bool:
        context = self._context
        presentation = MediaCountdownPresentation(presentation_value)
        self._session.set_tab_projection_active(False)
        self._stop_active_sources()
        context.projection_bar.set_playlist([])
        context.projection_bar.activate_timer(target_dt, presentation)

        remaining = self._remaining_seconds(target_dt)
        total = max(1, remaining)
        for projection_window in context.projection_windows():
            projection_window.show_timer(remaining, total, presentation)

        self._handlers.update_projection_status(
            True,
            _tr(_TIMER_STATUS_SOURCE).format(
                time=target_dt.time().toString("HH:mm"),
            ),
            auto_keys_media=False,
        )
        state = {
            "type": "timer",
            "title": _tr(_TIMER_SOURCE),
            "target_dt": target_dt,
            "total": total,
            "presentation": presentation.value,
            "origin": origin,
        }
        if occurrence_id:
            state["occurrence_id"] = occurrence_id
        self._session.set_state(state)
        return True

    def on_timer_update_proj(self, remaining: int, total: int) -> None:
        if self._session.state_type == "timer":
            self._session.update_state(remaining=remaining, total=total)
        for projection_window in self._context.projection_windows():
            projection_window.update_timer(remaining, total)

    def on_timer_blink_proj(self, on: bool) -> None:
        if self._context.program_content is not None:
            self._context.program_content.set_timer_blink(on)
        for projection_window in self._context.projection_windows():
            projection_window.set_timer_blink(on)

    def _remaining_seconds(self, target_dt: QDateTime) -> int:
        return ceil_remaining_seconds(target_dt)

    def _stop_active_sources(self) -> None:
        context = self._context
        self._handlers.stop_browser_tab_projection()
        context.media_controller.stop()
        context.ndi_service.stop()
        if context.camera_service is not None:
            context.camera_service.stop()


__all__ = [
    "TimerProjectionContext",
    "TimerProjectionController",
    "TimerProjectionHandlers",
]
