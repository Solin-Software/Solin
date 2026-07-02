from __future__ import annotations

import urllib.parse
from pathlib import Path

from PySide6.QtCore import Slot

from ...core.foundation.constants import JWPUB_EXTS, PDF_EXTS, PLAYLIST_EXTS
from ...core.media.formats import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS


__all__ = ("BrowserDownloadsMixin",)


class BrowserDownloadsMixin:
    def play_destination_media(self, url: str, kind: str) -> None:
        if kind == "image":
            self._on_project_image(url)
        else:
            self._on_project_video(url)

    @Slot(str)
    def _on_download_requested(self, url: str):
        """Forward native WebView downloads to the unified route workflow."""
        parsed_path = urllib.parse.urlparse(url).path
        decoded_path = urllib.parse.unquote(parsed_path)
        ext = Path(decoded_path).suffix.lower()
        title = self._download_title_from_url(url)

        if ext in IMAGE_EXTS or ext in VIDEO_EXTS or ext in AUDIO_EXTS:
            if ext in IMAGE_EXTS:
                media_type = "image"
            elif ext in AUDIO_EXTS:
                media_type = "audio"
            else:
                media_type = "video"
            self.media_destination_signal.emit(url, title, media_type, True)
            return

        if ext in PDF_EXTS:
            self.media_destination_signal.emit(url, title, "pdf", False)
        elif ext in JWPUB_EXTS:
            self.media_destination_signal.emit(url, title, "jwpub", False)
        elif ext in PLAYLIST_EXTS:
            self.media_destination_signal.emit(url, title, "jwlplaylist", False)

    def _download_title_from_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        name = urllib.parse.unquote(Path(parsed.path).name or "").strip()
        if name:
            return name
        tab = self._current_tab()
        tab_title = tab.view.title().strip() if tab else ""
        return tab_title or self.tr("Downloaded file")

    @Slot(str, str)
    def _on_save_media(self, url: str, media_type: str):
        del media_type
        self._download_service.cache_media(url)

    @Slot(str, str, str)
    def _on_add_to_destination(self, url: str, title: str, media_type: str):
        if not url:
            return
        self.media_destination_signal.emit(url, title, media_type, False)

    def prepare_destination_media(
        self,
        url: str,
        title: str,
        kind: str,
        *,
        on_ready,
        on_failed,
    ) -> None:
        if kind == "image" and url.startswith(("http://", "https://")):
            self._download_service.download_image_for_destination(
                url,
                title,
                on_ready=on_ready,
                on_failed=on_failed,
            )
            return
        if kind in {"pdf", "jwpub", "jwlplaylist"}:
            self._download_service.download_file_for_destination(
                url,
                title,
                kind,
                on_ready=on_ready,
                on_failed=on_failed,
            )
            return
        on_ready(url, title, kind)
