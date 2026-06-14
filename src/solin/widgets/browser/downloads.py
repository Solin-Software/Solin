from __future__ import annotations

import urllib.parse
from pathlib import Path

from PySide6.QtCore import Slot

from ...core.foundation.constants import JWPUB_EXTS, PDF_EXTS, PLAYLIST_EXTS
from ...core.media.formats import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS


class _BrowserDownloadsMixin:
    @Slot(str)
    def _on_download_requested(self, url: str):
        """Route native WebView downloads to presentation actions."""
        parsed_path = urllib.parse.urlparse(url).path
        decoded_path = urllib.parse.unquote(parsed_path)
        ext = Path(decoded_path).suffix.lower()
        title = self._download_title_from_url(url)

        if ext in IMAGE_EXTS or ext in VIDEO_EXTS or ext in AUDIO_EXTS:
            from ..media_download_action_dialog import (
                MediaDownloadAction,
                choose_media_download_action,
            )

            action = choose_media_download_action(self, title)
            if action == MediaDownloadAction.PLAY:
                if ext in IMAGE_EXTS:
                    self._on_project_image(url)
                else:
                    self._on_project_video(url)
            elif action == MediaDownloadAction.ADD_TO_PLAYLIST:
                if ext in IMAGE_EXTS:
                    media_type = "image"
                elif ext in AUDIO_EXTS:
                    media_type = "audio"
                else:
                    media_type = "video"
                self._on_add_to_playlist(url, title, media_type)
            return

        if ext in PDF_EXTS:
            self._download_file_then_playlist(url, title, "pdf")
        elif ext in JWPUB_EXTS:
            self._download_file_then_playlist(url, title, "jwpub")
        elif ext in PLAYLIST_EXTS:
            self._download_file_then_playlist(url, title, "jwlplaylist")

    def _download_title_from_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        name = urllib.parse.unquote(Path(parsed.path).name or "").strip()
        if name:
            return name
        tab = self._current_tab()
        tab_title = tab.view.title().strip() if tab else ""
        return tab_title or self.tr("Downloaded file")

    def _download_file_then_playlist(self, url: str, title: str, kind: str) -> None:
        self._download_service.download_file_for_playlist(
            url,
            title,
            kind,
            on_ready=self.add_downloaded_file_to_playlist_signal.emit,
            on_failed=self.download_failed_signal.emit,
        )

    @Slot(str, str)
    def _on_save_media(self, url: str, media_type: str):
        del media_type
        self._download_service.cache_media(url)

    @Slot(str, str, str)
    def _on_add_to_playlist(self, url: str, title: str, media_type: str):
        if not url:
            return
        if media_type == "image" and url.startswith("http"):
            self._download_image_then_playlist(url, title)
        else:
            self.add_to_playlist_signal.emit(url, title, media_type)

    def _download_image_then_playlist(self, url: str, title: str):
        signal = self.add_to_playlist_signal
        self._download_service.download_image_for_playlist(
            url,
            title,
            on_ready=signal.emit,
            on_failed=lambda _title, _error: signal.emit(url, title, "image"),
        )
