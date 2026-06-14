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
