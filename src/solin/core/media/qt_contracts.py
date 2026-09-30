"""Qt adapter ports shared by media cache and playback services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from PySide6.QtCore import QObject


class PrefetchDownloader(Protocol):
    progress: Any
    finished: Any
    error: Any

    def start(self, url: str, persist: bool = True) -> int | None: ...

    def cancel(self) -> None: ...


class PlaybackDownloader(PrefetchDownloader, Protocol):
    def get_cached_path(self, url: str) -> str | None: ...

    def cleanup_temp(self) -> None: ...


PrefetchDownloaderFactory = Callable[[QObject], PrefetchDownloader]
PlaybackDownloaderFactory = Callable[[QObject], PlaybackDownloader]


class MediaMetadataExtractor(Protocol):
    metadata_ready: Any

    def request(self, session_id: int, path: str) -> None: ...

    def cancel(self) -> None: ...


class MediaPlaybackRoute(Protocol):
    def open(
        self, path: str, *, is_local_file: bool, autoplay: bool,
        volume_percent: int, speed_percent: int, trim_start_ms: int, trim_end_ms: int,
    ) -> None: ...

    def play(self) -> None: ...

    def pause(self) -> None: ...

    def close(self) -> None: ...

    def restart(self) -> None: ...

    def seek(self, position_ms: int) -> None: ...

    def set_properties(self, *, volume_percent: int, speed_percent: int) -> None: ...
