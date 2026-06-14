from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProjectionStopContext:
    """Dependencies required to stop projection output cleanly."""

    projection_session: Any
    projection_bar: Any
    media_controller: Any
    ndi_service: Any
    camera_service: Any
    projection_windows: Callable[[], list[Any]]
    auto_share_configured: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ProjectionStopHandlers:
    """Shell actions affected by stopping projection."""

    stop_browser_tab_projection: Callable[[], None]
    update_projection_status: Callable[..., None]
    set_obs_stream_active: Callable[[bool], None]
    set_camera_stream_active: Callable[[bool], None]


class ProjectionStopController:
    """Stops active projection sessions while preserving OBS/camera edge cases."""

    _VISUAL_PROJECTION_TYPES = {
        "image",
        "timer",
        "sermon_theme",
        "obs_stream",
        "camera_stream",
    }

    def __init__(
        self,
        context: ProjectionStopContext,
        handlers: ProjectionStopHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers
        self._session = context.projection_session

    def stop_any(self) -> None:
        self._stop(
            tolerate_navigation_errors=False,
            stop_ndi_later_for_obs=False,
        )

    def stop_projection(self) -> None:
        self._stop(
            tolerate_navigation_errors=True,
            stop_ndi_later_for_obs=True,
        )

    def _stop(
        self,
        *,
        tolerate_navigation_errors: bool,
        stop_ndi_later_for_obs: bool,
    ) -> None:
        context = self._context
        was_obs_stream = self._session.state_type == "obs_stream"
        was_camera_stream = self._session.state_type == "camera_stream"
        was_visual = self._was_visual_projection()

        self._session.set_tab_projection_active(False)
        if tolerate_navigation_errors:
            try:
                self._handlers.stop_browser_tab_projection()
            except Exception:  # noqa: BLE001 - native browser projection cleanup boundary
                log.debug("Failed to stop browser tab projection during projection stop", exc_info=True)
        else:
            self._handlers.stop_browser_tab_projection()

        context.media_controller.stop()
        if was_obs_stream and stop_ndi_later_for_obs:
            context.ndi_service.stop_later()
        else:
            context.ndi_service.stop()
        context.camera_service.stop()

        for projection_window in context.projection_windows():
            projection_window.clear()

        context.projection_bar.deactivate()
        self._handlers.update_projection_status(
            False,
            sync_obs=not (was_obs_stream or was_camera_stream),
        )
        self._session.reset_state()
        self._handlers.set_obs_stream_active(False)
        self._handlers.set_camera_stream_active(False)

        share_handling = context.auto_share_configured()
        floating_preview = self._session.floating_preview_window
        if (
            was_visual
            and floating_preview is not None
            and not share_handling
        ):
            floating_preview.trigger_zoom_break()

    def _was_visual_projection(self) -> bool:
        is_visual_media_active = getattr(
            self._context.projection_bar,
            "is_visual_media_active",
            lambda: False,
        )
        return (
            self._session.tab_projection_active
            or is_visual_media_active()
            or self._session.state_type in self._VISUAL_PROJECTION_TYPES
        )
