from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import Slot

from ...core.foundation.constants import (
    JWPUB_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
)
from ...core.media.formats import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS
from ...core.network.http import HttpError, stream_get

log = logging.getLogger(__name__)


def _make_download_temp_path(dest_path: str) -> str:
    directory = os.path.dirname(dest_path)
    filename = os.path.basename(dest_path)
    fd, path = tempfile.mkstemp(
        prefix=f".{filename}.",
        suffix=".tmp",
        dir=directory,
    )
    os.close(fd)
    return path


class _BrowserDownloadsMixin:
    def _media_cache_dir(self) -> str:
        return os.fspath(self._media_cache_manager.media_cache_dir)

    @Slot(str)
    def _on_download_requested(self, url: str):
        """
        Intercept downloads initiated by the native WebView.

        Media files can be played/projected or sent to a playlist. Files that
        need local processing are downloaded to cache and forwarded to
        MainWindow through the existing signals.
        """
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
            return

        if ext in JWPUB_EXTS:
            self._download_file_then_playlist(url, title, "jwpub")
            return

        if ext in PLAYLIST_EXTS:
            self._download_file_then_playlist(url, title, "jwlplaylist")
            return

    def _download_title_from_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        name = urllib.parse.unquote(Path(parsed.path).name or "").strip()
        if name:
            return name
        tab = self._current_tab()
        tab_title = tab.view.title().strip() if tab else ""
        return tab_title or self.tr("Downloaded file")

    def _download_http_to_cache(
        self,
        url: str,
        dest_path: str,
        done_path: str,
        *,
        timeout: int,
        chunk_size: int,
    ) -> None:
        tmp_path = _make_download_temp_path(dest_path)
        try:
            with stream_get(
                url,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Solin/1.0)"},
            ) as resp:
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size):
                        fh.write(chunk)
            os.replace(tmp_path, dest_path)
            with open(done_path, "w", encoding="utf-8") as fh:
                fh.write(url)
            self._media_cache_manager.notify_cached_threadsafe(url)
        except (HttpError, OSError, ValueError):
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise

    def _download_file_then_playlist(self, url: str, title: str, kind: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            local_path = urllib.request.url2pathname(parsed.path)
            self.add_downloaded_file_to_playlist_signal.emit(local_path, title, kind)
            return

        if parsed.scheme not in ("http", "https"):
            return

        os.makedirs(self._media_cache_dir(), exist_ok=True)
        dest_path = self._browser_download_cache_path(url, title, kind)
        done_path = dest_path + ".done"

        if os.path.isfile(dest_path) and os.path.isfile(done_path):
            try:
                with open(done_path, "r", encoding="utf-8") as fh:
                    if fh.read().strip() == url:
                        self.add_downloaded_file_to_playlist_signal.emit(dest_path, title, kind)
                        return
            except OSError:
                pass

        ready = self.add_downloaded_file_to_playlist_signal
        failed = self.download_failed_signal

        def _worker():
            try:
                self._download_http_to_cache(
                    url,
                    timeout=45,
                    dest_path=dest_path,
                    done_path=done_path,
                    chunk_size=131_072,
                )
                ready.emit(dest_path, title, kind)
            except (HttpError, OSError, ValueError) as exc:
                log.warning('Download for playlist failed "%s": %s', title, exc)
                failed.emit(title, str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _browser_download_cache_path(self, url: str, title: str, kind: str) -> str:
        ext_by_kind = {
            "pdf": ".pdf",
            "jwpub": ".jwpub",
            "jwlplaylist": ".jwlplaylist",
        }
        parsed = urllib.parse.urlparse(url)
        raw_name = urllib.parse.unquote(Path(parsed.path).name or title or "download")
        suffix = Path(raw_name).suffix.lower() or ext_by_kind.get(kind, "")
        stem = Path(raw_name).stem or Path(title).stem or "download"
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" ._")
        if not stem:
            stem = "download"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        filename = f"{stem[:80]}-{digest}{suffix}"
        return os.path.join(self._media_cache_dir(), filename)

    @Slot(str, str)
    def _on_save_media(self, url: str, media_type: str):
        if not url or not url.startswith("http"):
            return

        os.makedirs(self._media_cache_dir(), exist_ok=True)
        filename = url.split("/")[-1].split("?")[0] or "media_file"
        dest_path = os.path.join(self._media_cache_dir(), filename)
        done_path = dest_path + ".done"

        if os.path.isfile(dest_path) and os.path.isfile(done_path):
            return

        def _worker():
            try:
                self._download_http_to_cache(
                    url,
                    timeout=30,
                    dest_path=dest_path,
                    done_path=done_path,
                    chunk_size=131_072,
                )
            except (HttpError, OSError, ValueError) as exc:
                log.warning('Cache save failed for "%s": %s', filename, exc)

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(str, str, str)
    def _on_add_to_playlist(self, url: str, title: str, media_type: str):
        if not url:
            return
        if media_type == "image" and url.startswith("http"):
            self._download_image_then_playlist(url, title)
        else:
            self.add_to_playlist_signal.emit(url, title, media_type)

    def _download_image_then_playlist(self, url: str, title: str):
        os.makedirs(self._media_cache_dir(), exist_ok=True)
        raw_name = url.split("/")[-1].split("?")[0] or "image"
        if not any(
            raw_name.lower().endswith(e)
            for e in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
        ):
            raw_name += ".jpg"
        dest_path = os.path.join(self._media_cache_dir(), raw_name)
        done_path = dest_path + ".done"

        if os.path.isfile(dest_path) and os.path.isfile(done_path):
            self.add_to_playlist_signal.emit(dest_path, title, "image")
            return

        sig = self.add_to_playlist_signal

        def _worker():
            try:
                self._download_http_to_cache(
                    url,
                    timeout=30,
                    dest_path=dest_path,
                    done_path=done_path,
                    chunk_size=65_536,
                )
                sig.emit(dest_path, title, "image")
            except (HttpError, OSError, ValueError) as exc:
                log.warning('Image download for playlist failed "%s": %s', raw_name, exc)
                sig.emit(url, title, "image")

        threading.Thread(target=_worker, daemon=True).start()
