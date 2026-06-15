"""Qt worker adapter for loading the JW original-song catalog."""

from __future__ import annotations

import os

from PySide6.QtCore import QThread, Signal

from .media_api import fetch_clips


class ClipFetchThread(QThread):
    items_ready = Signal(int, list, float, bool)
    failed = Signal(int, str)

    def __init__(
        self,
        generation: int,
        *,
        api_code: str,
        fallback_code: str,
        is_sign_language: bool,
        force: bool,
        cache_dir: str | os.PathLike[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._generation = generation
        self._api_code = api_code
        self._fallback_code = fallback_code
        self._is_sign_language = is_sign_language
        self._force = force
        self._cache_dir = cache_dir

    def run(self) -> None:
        try:
            items, fetched_at, from_cache = fetch_clips(
                self._api_code,
                self._force,
                fallback_code=self._fallback_code,
                is_sign_language=self._is_sign_language,
                cache_dir=self._cache_dir,
            )
            if not self.isInterruptionRequested():
                self.items_ready.emit(
                    self._generation,
                    items,
                    fetched_at,
                    from_cache,
                )
        except Exception as exc:  # noqa: BLE001 - worker signal boundary
            if not self.isInterruptionRequested():
                self.failed.emit(self._generation, str(exc))


class ClipFetchThreadFactory:
    """Create generation-aware clip catalog workers."""

    def create(
        self,
        generation: int,
        *,
        api_code: str,
        fallback_code: str,
        is_sign_language: bool,
        force: bool,
        cache_dir: str | os.PathLike[str],
        parent=None,
    ) -> ClipFetchThread:
        return ClipFetchThread(
            generation,
            api_code=api_code,
            fallback_code=fallback_code,
            is_sign_language=is_sign_language,
            force=force,
            cache_dir=cache_dir,
            parent=parent,
        )
