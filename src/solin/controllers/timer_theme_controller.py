from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from PySide6.QtCore import QDateTime

from ..core.foundation.time_utils import ceil_remaining_seconds
from ..core.timer.models import MediaCountdownPresentation


@dataclass(frozen=True, slots=True)
class TimerThemeContext:
    """Dependencies for timer and sermon-theme projection workflows."""

    projection_session: Any
    projection_bar: Any
    media_controller: Any
    ndi_service: Any
    camera_service: Any
    projection_windows: Callable[[], list[Any]]
    translate: Callable[[str], str]


@dataclass(frozen=True, slots=True)
class TimerThemeHandlers:
    """Shell actions invoked before and after timer/theme projection."""

    stop_browser_tab_projection: Callable[[], None]
    update_projection_status: Callable[..., None]


class TimerThemeController:
    """Projects timer and sermon-theme visuals to projection windows."""

    def __init__(
        self,
        context: TimerThemeContext,
        handlers: TimerThemeHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers
        self._session = context.projection_session

    def start_timer(self, target_dt: QDateTime, presentation_value: str) -> None:
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
            f"Cronômetro → {target_dt.time().toString('HH:mm')}",
            auto_keys_media=False,
        )
        self._session.set_state({
            "type": "timer",
            "target_dt": target_dt,
            "total": total,
            "presentation": presentation.value,
        })

    def on_timer_update_proj(self, remaining: int, total: int) -> None:
        for projection_window in self._context.projection_windows():
            projection_window.update_timer(remaining, total)

    def on_timer_blink_proj(self, on: bool) -> None:
        for projection_window in self._context.projection_windows():
            projection_window.set_timer_blink(on)

    def project_sermon_theme(self, text: str, subtitle: str = "") -> None:
        context = self._context
        self._session.set_tab_projection_active(False)
        self._stop_active_sources()
        context.projection_bar.set_playlist([])

        subtitle = subtitle or context.translate("PUBLIC TALK")
        for projection_window in context.projection_windows():
            projection_window.show_sermon_theme(text, subtitle)
        short = (text[:28] + "…") if len(text) > 28 else text

        image_bytes = self._render_sermon_theme_preview(text, subtitle)
        context.projection_bar.activate_image(short, image_data=image_bytes)
        self._handlers.update_projection_status(
            True,
            short,
            auto_keys_media=False,
        )
        self._session.set_state({
            "type": "sermon_theme",
            "text": text,
            "subtitle": subtitle,
            "transform": (1.0, 0.0, 0.0),
        })

    def _remaining_seconds(self, target_dt: QDateTime) -> int:
        return ceil_remaining_seconds(target_dt)

    def _stop_active_sources(self) -> None:
        context = self._context
        self._handlers.stop_browser_tab_projection()
        context.media_controller.stop()
        context.ndi_service.stop()
        context.camera_service.stop()

    def _render_sermon_theme_preview(self, text: str, subtitle: str) -> bytes:
        from PySide6.QtCore import QBuffer, QIODevice

        from ..projection.sermon_theme import SermonThemeProjectionWidget

        preview = SermonThemeProjectionWidget()
        preview.set_theme(text, subtitle)
        preview.resize(1024, 576)
        pixmap = preview.grab()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buffer, "PNG")
        image_bytes = bytes(cast(bytes, buffer.data()))
        buffer.close()
        return image_bytes
