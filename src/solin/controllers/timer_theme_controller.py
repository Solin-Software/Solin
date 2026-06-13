from __future__ import annotations

from PySide6.QtCore import QDateTime

from ..core.foundation.time_utils import ceil_remaining_seconds


class TimerThemeController:
    """Projects timer and sermon-theme visuals to projection windows."""

    def __init__(self, window) -> None:
        self._window = window

    def start_timer(self, target_dt: QDateTime) -> None:
        window = self._window
        window._tab_proj_active = False
        window._navigation.stop_browser_tab_projection()
        window.media_ctrl.stop()
        window._ndi_service.stop()
        window._camera_service.stop()
        window.proj_bar.set_playlist([])
        window.proj_bar.activate_timer(target_dt)

        remaining = self._remaining_seconds(target_dt)
        total = max(1, remaining)
        for projection_window in window._all_windows():
            projection_window.show_timer(remaining, total)

        window._projection_integrations.update_status(
            True,
            f"Cronômetro → {target_dt.time().toString('HH:mm')}",
            auto_keys_media=False,
        )
        window._proj_state = {"type": "timer", "target_dt": target_dt, "total": total}

    def on_timer_update_proj(self, remaining: int, total: int) -> None:
        for projection_window in self._window._all_windows():
            projection_window.update_timer(remaining, total)

    def on_timer_blink_proj(self, on: bool) -> None:
        for projection_window in self._window._all_windows():
            projection_window.set_timer_blink(on)

    def project_sermon_theme(self, text: str, subtitle: str = "") -> None:
        window = self._window
        window._tab_proj_active = False
        window._navigation.stop_browser_tab_projection()
        window.media_ctrl.stop()
        window._ndi_service.stop()
        window._camera_service.stop()
        window.proj_bar.set_playlist([])

        subtitle = subtitle or window.tr("PUBLIC TALK")
        for projection_window in window._all_windows():
            projection_window.show_sermon_theme(text, subtitle)
        short = (text[:28] + "…") if len(text) > 28 else text

        image_bytes = self._render_sermon_theme_preview(text, subtitle)
        window.proj_bar.activate_image(short, image_data=image_bytes)
        window._projection_integrations.update_status(
            True,
            short,
            auto_keys_media=False,
        )
        window._proj_state = {
            "type": "sermon_theme",
            "text": text,
            "subtitle": subtitle,
            "transform": (1.0, 0.0, 0.0),
        }

    def _remaining_seconds(self, target_dt: QDateTime) -> int:
        return ceil_remaining_seconds(target_dt)

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
        image_bytes = bytes(buffer.data())
        buffer.close()
        return image_bytes
