from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class ProjectionStopController:
    """Stops active projection sessions while preserving OBS/camera edge cases."""

    _VISUAL_PROJECTION_TYPES = {
        "image",
        "timer",
        "sermon_theme",
        "obs_stream",
        "camera_stream",
    }

    def __init__(self, window) -> None:
        self._window = window

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
        window = self._window
        was_obs_stream = window._proj_state.get("type") == "obs_stream"
        was_camera_stream = window._proj_state.get("type") == "camera_stream"
        was_visual = self._was_visual_projection()

        window._tab_proj_active = False
        if tolerate_navigation_errors:
            try:
                window._navigation.stop_browser_tab_projection()
            except Exception:  # noqa: BLE001 - native browser projection cleanup boundary
                log.debug("Failed to stop browser tab projection during projection stop", exc_info=True)
        else:
            window._navigation.stop_browser_tab_projection()

        window.media_ctrl.stop()
        if was_obs_stream and stop_ndi_later_for_obs:
            window._ndi_service.stop_later()
        else:
            window._ndi_service.stop()
        window._camera_service.stop()

        for projection_window in window._all_windows():
            projection_window.clear()

        window.proj_bar.deactivate()
        window._projection_integrations.update_status(
            False,
            sync_obs=not (was_obs_stream or was_camera_stream),
        )
        window._proj_state = {"type": "idle"}
        window._live_integrations.set_obs_stream_active(False)
        window._live_integrations.set_camera_stream_active(False)

        share_handling = window._projection_integrations.auto_share_configured()
        if (
            was_visual
            and window.floating_preview_window is not None
            and not share_handling
        ):
            window.floating_preview_window.trigger_zoom_break()

    def _was_visual_projection(self) -> bool:
        window = self._window
        is_visual_media_active = getattr(
            window.proj_bar,
            "is_visual_media_active",
            lambda: False,
        )
        return (
            window._tab_proj_active
            or is_visual_media_active()
            or window._proj_state.get("type") in self._VISUAL_PROJECTION_TYPES
        )
