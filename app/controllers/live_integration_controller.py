from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Slot
from PySide6.QtGui import QImage

from ..core.integrations.camera import CameraOption
from ..core.foundation.constants import MEMORIZE_PRE_MEDIA_SCENE as _MEMORIZE_PRE_MEDIA_SCENE
from ..core.foundation.settings_keys import SettingsKey

log = logging.getLogger(__name__)


class LiveIntegrationController:
    """Owns OBS, NDI, camera, Zoom, and quick-toolbar integration callbacks."""

    def __init__(self, window) -> None:
        self._window = window

    @Slot(bool)
    def on_zoom_settings_enabled_toggled(self, enabled: bool) -> None:
        window = self._window
        if sys.platform != "win32":
            return
        if enabled:
            window._zoom_service.start()
        else:
            window._zoom_service.stop()
            window._quick_toolbar.set_zoom_connected(False)

    @Slot(bool)
    def on_zoom_settings_parts_toggled(self, show: bool) -> None:
        window = self._window
        if sys.platform != "win32":
            return
        if show:
            window._zoom_service.start_participant_polling()
        else:
            window._zoom_service.stop_participant_polling()
            self.on_zoom_participants_updated(0, [])

    def on_zoom_connection_changed(self, connected: bool) -> None:
        self._window._quick_toolbar.set_zoom_connected(connected)

    def on_zoom_participants_updated(self, count: int, names: list) -> None:
        toolbar = self._window._quick_toolbar
        if hasattr(toolbar, "_zoom_panel"):
            toolbar._zoom_panel.set_participants(count, names)

    def on_zoom_sharing_state_changed(self, sharing: bool) -> None:
        toolbar = self._window._quick_toolbar
        if hasattr(toolbar, "_zoom_panel"):
            toolbar._zoom_panel.set_sharing(sharing)

    def on_zoom_share_error(self, _message: str) -> None:
        pass
        # self._window._success_toast.show_message(message)

    def on_obs_state_changed(self, _state, _message: str) -> None:
        self.refresh_obs_btn_availability()
        self._window._quick_toolbar.set_obs_connected(self._window._obs_service.is_connected)

    def on_obs_scene_changed(self, scene_name: str) -> None:
        window = self._window
        window._quick_toolbar.set_obs_current_scene(scene_name)
        media_scene = window._obs_prefs.value(SettingsKey.OBS_MEDIA_WINDOW_SCENE, "", str)
        if not media_scene or media_scene.startswith("—"):
            return
        window.proj_bar.set_obs_scene_is_media(scene_name == media_scene)

    def on_obs_scenes_updated(self, scenes: list) -> None:
        self._window._quick_toolbar.set_obs_scenes(scenes)

    def refresh_obs_btn_availability(self) -> None:
        window = self._window
        connected = window._obs_service.is_connected
        media_scene = window._obs_prefs.value(SettingsKey.OBS_MEDIA_WINDOW_SCENE, "", str)
        available = connected and bool(media_scene) and not media_scene.startswith("—")
        window.proj_bar.set_obs_btn_available(available)

    def refresh_obs_stream_availability(self) -> None:
        window = self._window
        enabled = window._obs_prefs.value(SettingsKey.OBS_NDI_ENABLED, False, bool)
        source = window._obs_prefs.value(SettingsKey.OBS_NDI_SOURCE, "", str).strip()
        available = bool(enabled and source)
        if hasattr(window, "_quick_toolbar"):
            window._quick_toolbar.set_obs_stream_available(available)
            window._quick_toolbar.set_obs_stream_active(
                getattr(window, "_proj_state", {}).get("type") == "obs_stream"
            )
            self.refresh_obs_camera_stream_availability()

    def set_obs_stream_active(self, active: bool) -> None:
        if hasattr(self._window, "_quick_toolbar"):
            self._window._quick_toolbar.set_obs_stream_active(active)

    def selected_camera_option(self) -> CameraOption | None:
        window = self._window
        if hasattr(window, "_quick_toolbar"):
            opt = window._quick_toolbar.current_camera_option()
            if opt is not None:
                return opt
        backend = window._obs_prefs.value(SettingsKey.CAMERA_BACKEND, "", str)
        name = window._obs_prefs.value(SettingsKey.CAMERA_DEVICE_NAME, "", str)
        return window._camera_service.find_saved(backend, name)

    def refresh_obs_camera_stream_availability(self) -> None:
        window = self._window
        if not hasattr(window, "_quick_toolbar"):
            return
        ndi_enabled = window._obs_prefs.value(SettingsKey.OBS_NDI_ENABLED, False, bool)
        camera_enabled = window._obs_prefs.value(SettingsKey.CAMERA_ENABLED, False, bool)
        if not camera_enabled or ndi_enabled:
            window._quick_toolbar.set_obs_camera_stream_available(False)
            return
        try:
            opt = self.selected_camera_option()
        except Exception:
            opt = None
        window._quick_toolbar.set_obs_camera_stream_available(bool(opt and opt.is_virtual))

    def set_camera_stream_active(self, active: bool) -> None:
        if hasattr(self._window, "_quick_toolbar"):
            self._window._quick_toolbar.set_camera_stream_active(active)

    def on_camera_settings_enabled_toggled(self, enabled: bool) -> None:
        window = self._window
        window._quick_toolbar.set_camera_enabled(enabled)
        self.refresh_obs_camera_stream_availability()
        if not enabled and window._proj_state.get("type") == "camera_stream":
            window._stop_projection()

    def on_camera_selection_changed(self, _option) -> None:
        self.refresh_obs_camera_stream_availability()

    def on_quick_obs_scene_change(self, scene_name: str) -> None:
        self._window._obs_service.request_scene_change(scene_name)

    def on_quick_obs_return_scene_change(self, scene_name: str) -> None:
        window = self._window
        scene_name = (scene_name or "").strip()
        media_scene = window._obs_prefs.value(SettingsKey.OBS_MEDIA_WINDOW_SCENE, "", str)
        current = window._obs_service.current_scene or ""
        if (
            not scene_name
            or scene_name == current
            or not media_scene
            or media_scene.startswith("—")
            or current != media_scene
        ):
            return
        window._obs_pre_media_scene = scene_name

    def project_obs_ndi_stream(self) -> None:
        window = self._window
        if window._proj_state.get("type") == "obs_stream":
            window._stop_projection()
            return

        source = window._obs_prefs.value(SettingsKey.OBS_NDI_SOURCE, "", str).strip()
        if not window._obs_prefs.value(SettingsKey.OBS_NDI_ENABLED, False, bool) or not source:
            window._success_toast.show_message(window.tr("OBS stream is not configured."))
            self.refresh_obs_stream_availability()
            return

        window._tab_proj_active = False
        try:
            window._navigation.stop_browser_tab_projection()
        except Exception:
            log.debug("Failed to stop browser tab projection before OBS stream", exc_info=True)
        window.media_ctrl.stop()
        window._camera_service.stop()
        window.proj_bar.set_playlist([])
        for win in window._all_windows():
            win.clear()
        title = window.tr("OBS Program Stream")
        window.proj_bar.activate_live_stream(title, keep_expanded=window.proj_bar.is_expanded())
        window._proj_state = {"type": "obs_stream", "title": title}
        window._projection_integrations.update_status(
            True,
            title,
            auto_keys_media=False,
            sync_obs=False,
        )
        self.set_obs_stream_active(True)
        window._ndi_service.start(source, max_fps=30)

    def project_camera_stream(self) -> None:
        window = self._window
        if window._proj_state.get("type") == "camera_stream":
            window._stop_projection()
            return

        if not window._obs_prefs.value(SettingsKey.CAMERA_ENABLED, False, bool):
            window._success_toast.show_message(window.tr("Camera is not enabled."))
            return

        option = self.selected_camera_option()
        if option is None:
            window._success_toast.show_message(window.tr("No camera selected."))
            return

        window._tab_proj_active = False
        try:
            window._navigation.stop_browser_tab_projection()
        except Exception:
            log.debug("Failed to stop browser tab projection before camera stream", exc_info=True)
        window.media_ctrl.stop()
        window._ndi_service.stop()
        window._camera_service.stop()
        window.proj_bar.set_playlist([])
        for win in window._all_windows():
            win.clear()
        title = window.tr("Camera")
        window.proj_bar.activate_live_stream(title, keep_expanded=window.proj_bar.is_expanded())
        window._proj_state = {"type": "camera_stream", "title": title}
        window._projection_integrations.update_status(
            True,
            title,
            auto_keys_media=False,
            sync_obs=False,
        )
        self.set_camera_stream_active(True)
        window._camera_service.start(option)

    @Slot(QImage)
    def on_camera_frame(self, frame: QImage) -> None:
        window = self._window
        if window._proj_state.get("type") != "camera_stream":
            return
        for win in window._all_windows():
            if hasattr(win, "show_image_from_qimage"):
                win.show_image_from_qimage(frame, cache_pixmap=False)
        window.proj_bar.update_tab_live_preview(frame)

    def on_camera_error(self, message: str) -> None:
        window = self._window
        if window._proj_state.get("type") == "camera_stream":
            window._success_toast.show_message(message, duration_ms=3500)
            window._stop_projection()

    def on_camera_stopped(self) -> None:
        self.set_camera_stream_active(False)

    @Slot(QImage)
    def on_obs_ndi_frame(self, frame: QImage) -> None:
        window = self._window
        if window._proj_state.get("type") != "obs_stream":
            return
        for win in window._all_windows():
            if hasattr(win, "show_image_from_qimage"):
                win.show_image_from_qimage(frame, cache_pixmap=False)
        window.proj_bar.update_tab_live_preview(frame)

    def on_obs_ndi_error(self, message: str) -> None:
        window = self._window
        if window._proj_state.get("type") == "obs_stream":
            window._success_toast.show_message(message, duration_ms=3500)
            window._stop_projection()

    def on_obs_ndi_stopped(self) -> None:
        if not self._window._ndi_service.is_running:
            self.set_obs_stream_active(False)

    def on_obs_scene_toggle(self) -> None:
        window = self._window
        if not window._obs_service.is_connected:
            return

        media_scene = window._obs_prefs.value(SettingsKey.OBS_MEDIA_WINDOW_SCENE, "", str)
        if not media_scene or media_scene.startswith("—"):
            return

        if window.proj_bar.is_obs_scene_media():
            if _MEMORIZE_PRE_MEDIA_SCENE:
                target = window._obs_pre_media_scene or window._obs_prefs.value(
                    SettingsKey.OBS_DEFAULT_SCENE,
                    "",
                    str,
                )
            else:
                target = window._obs_prefs.value(SettingsKey.OBS_DEFAULT_SCENE, "", str)
            if target and not target.startswith("—"):
                window._obs_service.request_scene_change(target)
                window.proj_bar.set_obs_scene_is_media(False)
        else:
            if _MEMORIZE_PRE_MEDIA_SCENE:
                current = window._obs_service.current_scene or ""
                if current and current != media_scene:
                    window._obs_pre_media_scene = current
            window._obs_service.request_scene_change(media_scene)
            window.proj_bar.set_obs_scene_is_media(True)
