from __future__ import annotations

import os

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QPixmap

from solin.core.foundation.constants import ORDER_NEXT, ORDER_OFF, ORDER_RANDOM
from solin.core.media.download_storage import completed_cached_path
from solin.styles.icons import ICON_PANEL_RIGHT, ICON_SKIP_NEXT, ICON_SKIP_PREV, make_icon
from solin.styles.theme import PALETTE
from .idle_dialog import SetAsIdleConfirmDialog


_ANIM_MS = 220


class ProjectionPlaylistMixin:
    def set_playlist(
        self,
        items: list,
        playback_order: str | None = None,
        from_saved_playlist: bool = False,
    ):
        self._playlist = list(items)
        self._playlist_index = 0
        self._played_indices = {0}
        self._is_from_saved_playlist = from_saved_playlist
        if playback_order and playback_order in (ORDER_OFF, ORDER_NEXT, ORDER_RANDOM):
            self._playback_order = playback_order
        else:
            self._playback_order = self._playback_settings.playback_order()

        self._live_thumb_timer.stop()
        self._thumb_queue.clear()
        self._panel_populated = False
        self._live_thumb_captured = False
        if self.playlist_panel.is_open():
            self.playlist_panel.close_panel()
            self.ov_panel_btn.setIcon(make_icon(ICON_PANEL_RIGHT, 14, PALETTE.text_muted))
        self._update_nav_buttons()

    def playlist_items(self) -> list:
        return list(self._playlist)

    def current_playlist_item(self) -> dict | None:
        if 0 <= self._playlist_index < len(self._playlist):
            return self._playlist[self._playlist_index]
        return None

    def set_playlist_index(self, index: int) -> None:
        if not 0 <= index < len(self._playlist):
            return
        self._playlist_index = index
        self._played_indices = {index}
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        n = len(self._playlist)
        show = n > 1
        navigation_unlocked = not self._playback_protection.locked

        self.prev_btn.setVisible(show)
        self.next_btn.setVisible(show)
        self.ov_panel_btn.setVisible(show)
        self.ov_send_temp_btn.setVisible(show and not self._is_from_saved_playlist)

        if show:
            can_prev = self._playlist_index > 0
            can_next = self._playlist_index < n - 1
            prev_col = PALETTE.text_secondary if can_prev else PALETTE.text_dim
            next_col = PALETTE.text_secondary if can_next else PALETTE.text_dim

            self.prev_btn.setEnabled(can_prev and navigation_unlocked)
            self.next_btn.setEnabled(can_next and navigation_unlocked)
            if not navigation_unlocked:
                prev_col = next_col = PALETTE.text_dim
            self.prev_btn.setIcon(make_icon(ICON_SKIP_PREV, 15, prev_col))
            self.next_btn.setIcon(make_icon(ICON_SKIP_NEXT, 15, next_col))

            if self._panel_populated:
                self.playlist_panel.set_active(self._playlist_index)
                if (
                    self.playlist_panel.is_open()
                    and not self._thumb_queue.has_real_thumb(self._playlist_index)
                    and self._playlist_index < len(self._playlist)
                ):
                    self._request_thumbnail(
                        self._playlist_index,
                        self._playlist[self._playlist_index],
                    )

        sync_fullscreen = getattr(self, "_sync_app_fullscreen_navigation", None)
        if sync_fullscreen is not None:
            sync_fullscreen()

    def _on_prev_clicked(self):
        if not self._playback_protection.allow_manual_projection_change():
            return
        if self._playlist_index > 0:
            self._playlist_index -= 1
            self._played_indices.add(self._playlist_index)
            self._update_nav_buttons()
            self.playlist_navigate.emit(self._playlist_index)

    def _on_next_clicked(self):
        if not self._playback_protection.allow_manual_projection_change():
            return
        if self._playlist_index < len(self._playlist) - 1:
            self._playlist_index += 1
            self._played_indices.add(self._playlist_index)
            self._update_nav_buttons()
            self.playlist_navigate.emit(self._playlist_index)

    def _toggle_playlist_panel(self):
        if self.playlist_panel.is_open():
            self.playlist_panel.close_panel()
            self.ov_panel_btn.setIcon(make_icon(ICON_PANEL_RIGHT, 14, PALETTE.text_muted))
        else:
            self._ensure_panel_populated()
            self.playlist_panel.open_panel()
            self.ov_panel_btn.setIcon(make_icon(ICON_PANEL_RIGHT, 14, PALETTE.accent))
            QTimer.singleShot(_ANIM_MS + 30, self._redraw_current_preview)

    def _ensure_panel_populated(self):
        if self._panel_populated and len(self.playlist_panel._items) == len(self._playlist):
            self.playlist_panel.set_active(self._playlist_index)
            return

        self.playlist_panel.populate(self._playlist, self._playlist_index)
        self._panel_populated = True

        for i, item in enumerate(self._playlist):
            cached = self._thumb_queue.get_cached_pixmap(i)
            if cached is not None:
                self.playlist_panel.set_thumbnail(i, cached)
                continue

            url = item.get("url", "")
            is_remote = url.startswith(("http://", "https://"))

            if i == self._playlist_index:
                self._request_thumbnail(i, item)
                continue

            if not is_remote:
                self._request_thumbnail(i, item)
                continue

            if completed_cached_path(url, self._media_cache_dir):
                self._request_thumbnail(i, item)
                continue

    def _on_panel_item_clicked(self, index: int):
        if index == self._playlist_index:
            return
        if not self._playback_protection.allow_manual_projection_change():
            return
        self._playlist_index = index
        self._played_indices.add(index)
        self._update_nav_buttons()
        self.playlist_navigate.emit(index)

    def _on_add_to_destination_clicked(self):
        url = ""
        title = ""
        meta = {}

        if self._playlist and self._playlist_index < len(self._playlist):
            item = self._playlist[self._playlist_index]
            url = item.get("url", "")
            title = item.get("title", "")
            meta = {
                k: item.get(k)
                for k in (
                    "key_symbol",
                    "track",
                    "issue_tag",
                    "doc_id",
                    "meps_language",
                    "major_multimedia_type",
                    "type",
                    "base_duration_ticks",
                    "start_trim_ticks",
                    "end_trim_ticks",
                    "image_framing",
                )
                if item.get(k) is not None
            }
            live_dur = self.media.duration
            if live_dur and live_dur > 0 and not meta.get("base_duration_ticks"):
                meta["base_duration_ticks"] = live_dur * 10_000
        elif self._mode == "image":
            title = self.ov_title.text()
            if not self._image_file_path or not os.path.isfile(self._image_file_path):
                return
            url = self._image_file_path
            meta = {"type": "image"}
        else:
            return

        if not title and not url:
            return

        self.add_to_destination_requested.emit(url, title, meta)

    def _on_send_to_temp_playlist(self):
        if not self._playlist or self._is_from_saved_playlist:
            return
        self.send_to_temp_playlist_requested.emit(list(self._playlist))

    def set_live_tab_mode(self, active: bool) -> None:
        self._is_live_tab = active
        if active:
            self.ov_set_idle_btn.setVisible(False)

    def _resolve_idle_path(self) -> str:
        if self._is_audio or self._is_live_tab or self._mode not in ("video", "image"):
            return ""

        if self._mode == "video":
            if not self._playlist or self._playlist_index >= len(self._playlist):
                return ""
            url = self._playlist[self._playlist_index].get("url", "")
            if not url or url.startswith(("http://", "https://")):
                return ""
            return url if os.path.isfile(url) else ""

        path = self._image_file_path
        return path if path and os.path.isfile(path) else ""

    def _refresh_idle_btn_visibility(self) -> None:
        if (
            self._is_audio
            or self._is_live_tab
            or self._mode in ("timer", "live_stream")
            or self._mode is None
        ):
            self.ov_set_idle_btn.setVisible(False)
            return
        if self._mode == "image":
            self.ov_set_idle_btn.setVisible(True)
            return
        if self._mode == "video":
            self.ov_set_idle_btn.setVisible(bool(self._resolve_idle_path()))

    def _on_set_as_idle_clicked(self) -> None:
        path = self._resolve_idle_path()
        if not path:
            return

        pixmap: QPixmap | None = None
        if self._mode == "image" and self._image_pixmap and not self._image_pixmap.isNull():
            pixmap = self._image_pixmap
        elif self._mode == "video":
            if (
                self._playlist
                and self._playlist_index < len(self._playlist)
                and self._thumb_queue.has_real_thumb(self._playlist_index)
            ):
                pixmap = self._thumb_queue._cache.get(self._playlist_index, (None,))[0]

        title = self.ov_title.text() or os.path.basename(path)
        dlg = SetAsIdleConfirmDialog(title, pixmap=pixmap, parent=self.window())
        dlg.exec()
        if dlg.confirmed():
            self.set_as_idle_requested.emit(path)

    def _request_thumbnail(self, index: int, item: dict):
        media_type = item.get("type", "video")
        url = item.get("url", "")
        if not url:
            return

        if (
            index == self._playlist_index
            and self._mode == "video"
            and self._is_audio
            and self._audio_cover_pixmap
            and not self._audio_cover_pixmap.isNull()
        ):
            self._thumb_queue._cache[index] = (self._audio_cover_pixmap, "")
            self.playlist_panel.set_thumbnail(index, self._audio_cover_pixmap)
            return

        self._thumb_queue.request(index, url, media_type)

    def _on_thumbnail_ready(self, index: int, pixmap: QPixmap, _title: str = ""):
        self.playlist_panel.set_thumbnail(index, pixmap)

    def _schedule_live_thumb(self):
        if self._live_thumb_captured:
            return
        if not self._thumb_queue.has_real_thumb(self._playlist_index):
            self._live_thumb_timer.start(3_000)

    def _do_live_thumb_capture(self):
        if self._live_thumb_captured:
            return
        if self._mode != "video":
            return

        idx = self._playlist_index

        from PySide6.QtMultimedia import QMediaMetaData

        meta = self.media.player.metaData()
        for key in (QMediaMetaData.Key.CoverArtImage, QMediaMetaData.Key.ThumbnailImage):
            value = meta.value(key)
            if value is None:
                continue
            pixmap = None
            if isinstance(value, QImage) and not value.isNull():
                pixmap = QPixmap.fromImage(value)
            elif isinstance(value, QPixmap) and not value.isNull():
                pixmap = value
            if pixmap:
                self._live_thumb_captured = True
                self._thumb_queue.invalidate(idx)
                self._thumb_queue.feed_live_cover(idx, pixmap)
                self.playlist_panel.set_thumbnail(idx, pixmap)
                return

        if self._is_audio:
            return

        frame = self.media.video_sink.videoFrame()
        if not frame.isValid():
            return
        img = frame.toImage()
        if img.isNull():
            return

        self._live_thumb_captured = True
        pixmap = QPixmap.fromImage(img)
        self._thumb_queue.invalidate(idx)
        if self._thumb_queue.feed_live_frame(idx, pixmap):
            self.playlist_panel.set_thumbnail(idx, pixmap)

    def _redraw_current_preview(self):
        if self._mode == "image" and self._image_pixmap:
            self._redraw_preview_image()
        elif self._mode == "video" and self._is_audio and self._expanded:
            self._refresh_audio_cover_in_overlay()
