from __future__ import annotations

import os
from collections.abc import Mapping

from PySide6.QtCore import QTimer
from PySide6.QtGui import QPixmap

from solin.core.media.download_storage import completed_cached_path
from solin.core.projection.idle_media import existing_idle_media_path
from solin.styles.icons import ICON_PANEL_RIGHT, ICON_SKIP_NEXT, ICON_SKIP_PREV, make_icon
from solin.styles.theme import PALETTE
from .idle_dialog import confirm_set_as_idle


_ANIM_MS = 220


class ProjectionPlaylistMixin:
    def _on_queue_replaced(self) -> None:
        self._live_thumb_timer.stop()
        self._thumb_queue.clear()
        self._thumbnail_sources.clear()
        self._panel_populated = False
        self._live_thumb_captured = False
        if self._overlay_ready and self.playlist_panel.is_open():
            self.playlist_panel.close_panel()
            self.ov_panel_btn.setIcon(make_icon(ICON_PANEL_RIGHT, 14, PALETTE.text_muted))

    def _update_nav_buttons(self):
        n = len(self._playback.queue.items)
        show = n > 1

        self.prev_btn.setVisible(show)
        self.next_btn.setVisible(show)

        if show:
            can_prev = self._playback.can_navigate(-1)
            can_next = self._playback.can_navigate(1)
            prev_col = PALETTE.text_secondary if can_prev else PALETTE.text_dim
            next_col = PALETTE.text_secondary if can_next else PALETTE.text_dim

            self.prev_btn.setEnabled(can_prev)
            self.next_btn.setEnabled(can_next)
            self.prev_btn.setIcon(make_icon(ICON_SKIP_PREV, 15, prev_col))
            self.next_btn.setIcon(make_icon(ICON_SKIP_NEXT, 15, next_col))

        if self._overlay_ready:
            self.ov_panel_btn.setVisible(show)
            self.ov_send_temp_btn.setVisible(show and not self._playback.queue.from_saved_playlist)

            if show and self._panel_populated:
                self.playlist_panel.set_active(self._playback.queue.index)
                if (
                    self.playlist_panel.is_open()
                    and not self._thumb_queue.has_real_thumb(self._playback.queue.index)
                    and self._playback.queue.index < len(self._playback.queue.items)
                ):
                    self._request_thumbnail(
                        self._playback.queue.index,
                        self._playback.queue.items[self._playback.queue.index],
                    )

        self._sync_app_fullscreen_navigation()
        self._refresh_playback_options_indicator()

    def _on_prev_clicked(self) -> None:
        self.playlist_navigate.emit(self._playback.queue.index - 1)

    def _on_next_clicked(self) -> None:
        self.playlist_navigate.emit(self._playback.queue.index + 1)

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
        if self._panel_populated and len(self.playlist_panel._items) == len(self._playback.queue.items):
            self.playlist_panel.set_active(self._playback.queue.index)
            return

        self.playlist_panel.populate(self._playback.queue.items, self._playback.queue.index)
        self._panel_populated = True

        for i, item in enumerate(self._playback.queue.items):
            cached = self._thumb_queue.get_cached_pixmap(i)
            if cached is not None:
                self.playlist_panel.set_thumbnail(i, cached)
                continue

            url = item.get("url", "")
            is_remote = url.startswith(("http://", "https://"))

            if i == self._playback.queue.index:
                self._request_thumbnail(i, item)
                continue

            if not is_remote:
                self._request_thumbnail(i, item)
                continue

            if completed_cached_path(url, self._media_cache_dir):
                self._request_thumbnail(i, item)
                continue

    def _on_panel_item_clicked(self, index: int):
        self.playlist_navigate.emit(index)

    def _on_add_to_destination_clicked(self):
        url = ""
        title = ""
        meta = {}

        if self._playback.queue.items and self._playback.queue.index < len(self._playback.queue.items):
            item = self._playback.queue.items[self._playback.queue.index]
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
        if not self._playback.queue.items or self._playback.queue.from_saved_playlist:
            return
        self.send_to_temp_playlist_requested.emit([dict(item) for item in self._playback.queue.items])

    def set_live_tab_mode(self, active: bool) -> None:
        self._is_live_tab = active
        if active:
            self.ov_set_idle_btn.setVisible(False)

    def _resolve_idle_path(self) -> str:
        if self._is_audio or self._is_live_tab or self._mode not in ("video", "image"):
            return ""

        if self._mode == "video":
            if not self._playback.queue.items or self._playback.queue.index >= len(self._playback.queue.items):
                return ""
            source = self._playback.queue.items[self._playback.queue.index].get("url", "")
        else:
            source = self._image_file_path

        return existing_idle_media_path(self._mode, source)

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
                self._playback.queue.items
                and self._playback.queue.index < len(self._playback.queue.items)
                and self._thumb_queue.has_real_thumb(self._playback.queue.index)
            ):
                pixmap = self._thumb_queue._cache.get(self._playback.queue.index, (None,))[0]

        title = self.ov_title.text() or os.path.basename(path)
        if confirm_set_as_idle(title, pixmap=pixmap, parent=self.window()):
            self.set_as_idle_requested.emit(path)

    def _request_thumbnail(self, index: int, item: Mapping):
        media_type = item.get("type", "video")
        url = item.get("url", "")
        if not url:
            return

        source = (url, media_type)
        if self._thumbnail_sources.get(index) == source:
            return
        self._thumbnail_sources[index] = source
        self._thumb_queue.request(index, url, media_type)

    def _on_thumbnail_ready(self, index: int, pixmap: QPixmap, _title: str = ""):
        self.playlist_panel.set_thumbnail(index, pixmap)

    def _schedule_live_thumb(self):
        if self._live_thumb_captured:
            return
        if not self._thumb_queue.has_real_thumb(self._playback.queue.index):
            self._live_thumb_timer.start(3_000)

    def _do_live_thumb_capture(self):
        """Ask the thumbnail queue to resolve missing artwork asynchronously."""
        if self._live_thumb_captured or self._mode != "video":
            return
        idx = self._playback.queue.index
        if idx < 0 or idx >= len(self._playback.queue.items):
            return
        if self._thumb_queue.has_real_thumb(idx):
            self._live_thumb_captured = True
            return
        path = self.media.local_path
        if not path or path.startswith(("http://", "https://", "rtsp://", "rtmp://")):
            return
        item = self._playback.queue.items[idx]
        # The original source keeps pending work, terminal absence and retry
        # ownership intact, including the completed-cache fallback for HTTP.
        self._request_thumbnail(
            idx,
            {
                "url": item.get("url") or path,
                "type": "audio" if self._is_audio else item.get("type", "video"),
            },
        )

    def _redraw_current_preview(self):
        if self._mode == "image" and self._image_pixmap:
            self._redraw_preview_image()
        elif self._mode == "video" and self._is_audio and self._expanded:
            self._refresh_audio_cover_in_overlay()
