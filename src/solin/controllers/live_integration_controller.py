from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Slot
from PySide6.QtGui import QImage

from ..core.foundation.constants import MEMORIZE_PRE_MEDIA_SCENE

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LiveIntegrationContext:
    """Stable services and presentation ports for live integrations."""

    projection_session: Any
    obs_scene_session: Any
    obs_settings: Any
    obs_service: Any
    ndi_service: Any
    zoom_service: Any
    notifications: Any
    projection_bar: Any
    media_controller: Any
    quick_toolbar: Callable[[], Any | None]
    projection_windows: Callable[[], list[Any]]
    translate: Callable[[str], str]
    playback_protection: Any
    content_frame_sink: Callable[[object], None]
    platform: str = sys.platform


@dataclass(frozen=True, slots=True)
class LiveIntegrationHandlers:
    """Shell actions invoked by live integration workflows."""

    stop_projection: Callable[[], None]
    stop_browser_tab_projection: Callable[[], None]
    update_projection_status: Callable[..., None]


class LiveIntegrationController:
    """Owns OBS, NDI, Zoom, and quick-toolbar integration callbacks."""

    def __init__(
        self,
        context: LiveIntegrationContext,
        handlers: LiveIntegrationHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers
        self._session = context.projection_session

    @Slot(bool)
    def on_zoom_settings_enabled_toggled(self, enabled: bool) -> None:
        context = self._context
        if context.platform != "win32":
            return
        if enabled:
            context.zoom_service.start()
        else:
            context.zoom_service.stop()
            toolbar = context.quick_toolbar()
            if toolbar is not None:
                toolbar.set_zoom_connected(False)

    @Slot(bool)
    def on_zoom_settings_parts_toggled(self, show: bool) -> None:
        context = self._context
        if context.platform != "win32":
            return
        if show:
            context.zoom_service.start_participant_polling()
        else:
            context.zoom_service.stop_participant_polling()
            self.on_zoom_participants_updated(0, [])

    def on_zoom_connection_changed(self, connected: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_zoom_connected(connected)

    def on_zoom_participants_updated(self, count: int, names: list) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_zoom_participants(count, names)

    def on_zoom_sharing_state_changed(self, sharing: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_zoom_sharing(sharing)

    def on_zoom_share_error(self, message: str) -> None:
        context = self._context
        context.notifications.error(
            message,
            title=context.translate("Zoom sharing failed"),
            dedupe_key=f"zoom-share:{message}",
        )

    def on_obs_state_changed(self, _state, _message: str) -> None:
        self.refresh_obs_btn_availability()
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_obs_connected(self._context.obs_service.is_connected)

    def on_obs_scene_changed(self, scene_name: str) -> None:
        context = self._context
        toolbar = context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_obs_current_scene(scene_name)
        media_scene = context.obs_settings.media_window_scene()
        if not media_scene or media_scene.startswith("—"):
            return
        context.projection_bar.set_obs_scene_is_media(scene_name == media_scene)

    def on_obs_scenes_updated(self, scenes: list) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_obs_scenes(scenes)

    def refresh_obs_btn_availability(self) -> None:
        context = self._context
        connected = context.obs_service.is_connected
        available = connected and context.obs_settings.has_media_window_scene()
        context.projection_bar.set_obs_btn_available(available)

    def refresh_obs_stream_availability(self) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is None:
            return
        available = self._context.obs_settings.ndi_stream_configured()
        toolbar.set_obs_stream_available(available)
        toolbar.set_obs_stream_active(self._session.state_type == "obs_stream")

    def set_obs_stream_active(self, active: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_obs_stream_active(active)

    def on_quick_obs_scene_change(self, scene_name: str) -> None:
        self._context.obs_service.request_scene_change(scene_name)

    def on_quick_obs_return_scene_change(self, scene_name: str) -> None:
        context = self._context
        scene_name = (scene_name or "").strip()
        media_scene = context.obs_settings.media_window_scene()
        current = context.obs_service.current_scene or ""
        if (
            not scene_name
            or scene_name == current
            or not media_scene
            or media_scene.startswith("—")
            or current != media_scene
        ):
            return
        context.obs_scene_session.remember(scene_name)

    def project_obs_ndi_stream(self) -> None:
        context = self._context
        if self._session.state_type == "obs_stream":
            self._handlers.stop_projection()
            return
        if not context.playback_protection.allow_manual_projection_change():
            return

        source = context.obs_settings.ndi_source()
        if not context.obs_settings.ndi_stream_configured():
            context.notifications.warning(
                context.translate("OBS stream is not configured.")
            )
            self.refresh_obs_stream_availability()
            return

        self._session.set_tab_projection_active(False)
        try:
            self._handlers.stop_browser_tab_projection()
        except Exception:  # noqa: BLE001 - native browser projection cleanup boundary
            log.debug("Failed to stop browser tab projection before OBS stream", exc_info=True)
        context.media_controller.stop()
        context.projection_bar.set_playlist([])
        for win in context.projection_windows():
            win.clear()
        title = context.translate("OBS Program Stream")
        context.projection_bar.activate_live_stream(
            title,
            keep_expanded=context.projection_bar.is_expanded(),
        )
        self._session.set_state({"type": "obs_stream", "title": title})
        self._handlers.update_projection_status(
            True,
            title,
            auto_keys_media=False,
            sync_obs=False,
        )
        self.set_obs_stream_active(True)
        context.ndi_service.start(source, max_fps=30)

    @Slot(QImage)
    def on_obs_ndi_frame(self, frame: QImage) -> None:
        if self._session.state_type != "obs_stream":
            return
        context = self._context
        for win in context.projection_windows():
            if hasattr(win, "show_image_from_qimage"):
                win.show_image_from_qimage(frame, cache_pixmap=False)
        context.projection_bar.update_tab_live_preview(frame)
        context.content_frame_sink(frame)

    def on_obs_ndi_error(self, message: str) -> None:
        if self._session.state_type == "obs_stream":
            context = self._context
            context.notifications.error(
                message,
                title=context.translate("OBS stream error"),
                dedupe_key=f"obs-stream:{message}",
            )
            self._handlers.stop_projection()

    def on_obs_ndi_stopped(self) -> None:
        if not self._context.ndi_service.is_running:
            self.set_obs_stream_active(False)

    def on_obs_scene_toggle(self) -> None:
        context = self._context
        if not context.obs_service.is_connected:
            return

        media_scene = context.obs_settings.media_window_scene()
        if not media_scene or media_scene.startswith("—"):
            return

        if context.projection_bar.is_obs_scene_media():
            if MEMORIZE_PRE_MEDIA_SCENE:
                target = (
                    context.obs_scene_session.pre_media_scene
                    or context.obs_settings.default_scene()
                )
            else:
                target = context.obs_settings.default_scene()
            if target and not target.startswith("—"):
                context.obs_service.request_scene_change(target)
                context.projection_bar.set_obs_scene_is_media(False)
        else:
            if MEMORIZE_PRE_MEDIA_SCENE:
                current = context.obs_service.current_scene or ""
                if current and current != media_scene:
                    context.obs_scene_session.remember(current)
            context.obs_service.request_scene_change(media_scene)
            context.projection_bar.set_obs_scene_is_media(True)
