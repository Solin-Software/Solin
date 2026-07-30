from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Slot
from PySide6.QtGui import QImage

from ..core.integrations.camera_options import CameraOption
from ..core.foundation.constants import MEMORIZE_PRE_MEDIA_SCENE

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LiveIntegrationContext:
    """Stable services and presentation ports for live integrations."""

    projection_session: Any
    obs_scene_session: Any
    obs_settings: Any
    camera_settings: Any
    obs_service: Any
    ndi_service: Any
    camera_service: Any
    zoom_service: Any
    notifications: Any
    projection_bar: Any
    media_controller: Any
    quick_toolbar: Callable[[], Any | None]
    projection_windows: Callable[[], list[Any]]
    translate: Callable[[str], str]
    playback_protection: Any
    platform: str = sys.platform


@dataclass(frozen=True, slots=True)
class LiveIntegrationHandlers:
    """Shell actions invoked by live integration workflows."""

    stop_projection: Callable[[], None]
    stop_browser_tab_projection: Callable[[], None]
    update_projection_status: Callable[..., None]


class LiveIntegrationController:
    """Owns OBS, NDI, camera, Zoom, and quick-toolbar integration callbacks."""

    def __init__(
        self,
        context: LiveIntegrationContext,
        handlers: LiveIntegrationHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers
        self._session = context.projection_session
        self._camera_tap_connected = False  # program-preview tap wiring (obs camera)

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
        self.refresh_obs_camera_stream_availability()

    def set_obs_stream_active(self, active: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_obs_stream_active(active)

    def selected_camera_option(self) -> CameraOption | None:
        context = self._context
        toolbar = context.quick_toolbar()
        if toolbar is not None:
            opt = toolbar.current_camera_option()
            if opt is not None:
                return opt
        backend = context.camera_settings.backend()
        name = context.camera_settings.device_name()
        return context.camera_service.find_saved(backend, name)

    def refresh_obs_camera_stream_availability(self) -> None:
        context = self._context
        toolbar = context.quick_toolbar()
        if toolbar is None:
            return
        ndi_enabled = context.obs_settings.ndi_enabled()
        camera_enabled = context.camera_settings.is_enabled()
        if not camera_enabled or ndi_enabled:
            toolbar.set_obs_camera_stream_available(False)
            return
        opt = self.selected_camera_option()
        toolbar.set_obs_camera_stream_available(bool(opt and opt.is_virtual))

    def set_camera_stream_active(self, active: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_camera_stream_active(active)
        if not active:
            # Stop feeding the operator preview once the camera stream ends (the
            # v4l2 source is released by the program's crossfade-away).
            self._stop_camera_preview_tap()

    def on_camera_settings_enabled_toggled(self, enabled: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_camera_enabled(enabled)
        self.refresh_obs_camera_stream_availability()
        if not enabled and self._session.state_type == "camera_stream":
            self._handlers.stop_projection()

    def on_camera_selection_changed(self, _option) -> None:
        self.refresh_obs_camera_stream_availability()

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
        context.camera_service.stop()
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

    def project_camera_stream(self) -> None:
        context = self._context
        if self._session.state_type == "camera_stream":
            self._handlers.stop_projection()
            return
        if not context.playback_protection.allow_manual_projection_change():
            return

        if not context.camera_settings.is_enabled():
            context.notifications.warning(context.translate("Camera is not enabled."))
            return

        option = self.selected_camera_option()
        if option is None:
            context.notifications.warning(context.translate("No camera selected."))
            return

        self._session.set_tab_projection_active(False)
        try:
            self._handlers.stop_browser_tab_projection()
        except Exception:  # noqa: BLE001 - native browser projection cleanup boundary
            log.debug("Failed to stop browser tab projection before camera stream", exc_info=True)
        context.media_controller.stop()
        context.ndi_service.stop()
        context.camera_service.stop()
        context.projection_bar.set_playlist([])
        for win in context.projection_windows():
            win.clear()
        title = context.translate("Camera")
        context.projection_bar.activate_live_stream(
            title,
            keep_expanded=context.projection_bar.is_expanded(),
        )
        self._session.set_state({"type": "camera_stream", "title": title})
        self._handlers.update_projection_status(
            True,
            title,
            auto_keys_media=False,
            sync_obs=False,
        )
        self.set_camera_stream_active(True)

        # Native libobs camera: a v4l2_input source libobs owns, decodes and
        # crossfades — no QImage round-trip, no Qt QCamera on the device. The
        # operator preview is fed from a channel-0 tap (libobs holds the device,
        # so the Qt camera service can't). Falls back to the Qt camera path if the
        # source can't be created (non-obs engine, or no v4l2 device path).
        from ..projection.window import obs_media_engine_active

        if obs_media_engine_active() and option.device_path:
            shown = False
            for win in context.projection_windows():
                if hasattr(win, "show_camera") and win.show_camera(
                    option.device_path, option.name
                ):
                    shown = True
            if shown:
                self._start_camera_preview_tap()
                return

        context.camera_service.start(option)

    def _start_camera_preview_tap(self) -> None:
        from ..projection.program_preview import program_preview_tap

        tap = program_preview_tap()
        if not self._camera_tap_connected:
            tap.frame_ready.connect(self._on_camera_preview_frame)
            self._camera_tap_connected = True
        tap.set_enabled(True)

    def _stop_camera_preview_tap(self) -> None:
        from ..projection.program_preview import program_preview_tap

        program_preview_tap().set_enabled(False)

    def _on_camera_preview_frame(self, image) -> None:
        # Only feed the preview while the camera is the projected content (the tap
        # is a shared channel-0 grab; state gates it so it never fights another
        # source's preview).
        if self._session.state_type == "camera_stream":
            self._context.projection_bar.update_tab_live_preview(image)

    @Slot(QImage)
    def on_camera_frame(self, frame: QImage) -> None:
        if self._session.state_type != "camera_stream":
            return
        context = self._context
        for win in context.projection_windows():
            if hasattr(win, "show_image_from_qimage"):
                win.show_image_from_qimage(frame, cache_pixmap=False)
        context.projection_bar.update_tab_live_preview(frame)

    def on_camera_error(self, message: str) -> None:
        if self._session.state_type == "camera_stream":
            context = self._context
            context.notifications.error(
                message,
                title=context.translate("Camera error"),
                dedupe_key=f"camera:{message}",
            )
            self._handlers.stop_projection()

    def on_camera_stopped(self) -> None:
        self.set_camera_stream_active(False)

    @Slot(QImage)
    def on_obs_ndi_frame(self, frame: QImage) -> None:
        if self._session.state_type != "obs_stream":
            return
        context = self._context
        for win in context.projection_windows():
            # Push the ctypes-decoded NDI frame into a libobs async source so the
            # program composites + crossfades it (vcam/recording-capturable),
            # instead of painting the QImage on the Qt page with the surface
            # hidden. Falls back to the Qt per-frame path when unavailable.
            if hasattr(win, "show_ndi_frame"):
                try:
                    if win.show_ndi_frame(frame):
                        continue
                except Exception:  # noqa: BLE001 - libobs/render boundary
                    log.debug("libobs NDI frame failed; using Qt frames", exc_info=True)
            if hasattr(win, "show_image_from_qimage"):
                win.show_image_from_qimage(frame, cache_pixmap=False)
        # The operator preview keeps the raw frame (already available in Python;
        # no device-exclusivity constraint like the camera).
        context.projection_bar.update_tab_live_preview(frame)

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
