"""Shared in-memory store for JW song media.

The blocking HTTP/cache code lives in :mod:`app.core.jw.media_api`.  This module
adds a small Qt-facing coordination layer so every UI surface observes the same
song load state and concurrent requests for the same language/mode are coalesced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from .media_api import fetch_songs, fetch_songs_audio


@dataclass(frozen=True)
class JWSongsRequest:
    """Identity for a song media request."""

    api_code: str
    fallback_code: str
    is_sign_language: bool
    audio_mode: bool = False

    @property
    def key(self) -> str:
        sign = "sl" if self.is_sign_language else "regular"
        mode = "audio" if self.audio_mode else "video"
        return f"{self.api_code or 'E'}|{self.fallback_code or ''}|{sign}|{mode}"


@dataclass
class JWSongsSnapshot:
    """Read-only-ish snapshot of the current store state for one request."""

    items: list[dict[str, Any]]
    pub_name: str = ""
    fetched_at: float = 0.0
    from_cache: bool = False
    is_loading: bool = False
    error: str = ""


class _FetchSignals(QObject):
    succeeded = Signal(str, list, str, float, bool)
    failed = Signal(str, str)


class _FetchWorker(QRunnable):
    def __init__(self, request: JWSongsRequest, *, force: bool) -> None:
        super().__init__()
        self.request = request
        self.force = force
        self.signals = _FetchSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            if self.request.audio_mode:
                items, pub_name, fetched_at, from_cache = fetch_songs_audio(
                    self.request.api_code,
                    self.force,
                    fallback_code=self.request.fallback_code,
                    is_sign_language=self.request.is_sign_language,
                )
            else:
                items, pub_name, fetched_at, from_cache = fetch_songs(
                    self.request.api_code,
                    self.force,
                    fallback_code=self.request.fallback_code,
                    is_sign_language=self.request.is_sign_language,
                )
            self.signals.succeeded.emit(
                self.request.key,
                list(items or []),
                pub_name,
                float(fetched_at or 0.0),
                bool(from_cache),
            )
        except Exception as exc:
            self.signals.failed.emit(self.request.key, str(exc))


class JWSongsStore(QObject):
    """Process-wide JW song state shared by Songs tab and add-song modals."""

    songs_ready = Signal(str, list, str, float, bool)
    songs_failed = Signal(str, str)
    loading_changed = Signal(str, bool)

    _instance: "JWSongsStore | None" = None

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._states: dict[str, JWSongsSnapshot] = {}
        self._workers: dict[str, _FetchWorker] = {}

    @classmethod
    def instance(cls) -> "JWSongsStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def request_for(
        self,
        *,
        api_code: str,
        fallback_code: str = "",
        is_sign_language: bool = False,
        audio_mode: bool = False,
    ) -> JWSongsRequest:
        return JWSongsRequest(
            api_code=api_code or "E",
            fallback_code=fallback_code or "",
            is_sign_language=bool(is_sign_language),
            audio_mode=bool(audio_mode),
        )

    def snapshot(self, key: str) -> JWSongsSnapshot:
        state = self._states.get(key)
        if state is None:
            return JWSongsSnapshot(items=[])
        return JWSongsSnapshot(
            items=[dict(item) for item in state.items],
            pub_name=state.pub_name,
            fetched_at=state.fetched_at,
            from_cache=state.from_cache,
            is_loading=state.is_loading,
            error=state.error,
        )

    def ensure_loaded(self, request: JWSongsRequest, *, force: bool = False) -> str:
        """Ensure *request* is being loaded and return its stable key."""
        key = request.key
        state = self._states.setdefault(key, JWSongsSnapshot(items=[]))

        if state.is_loading:
            QTimer.singleShot(0, lambda k=key: self.loading_changed.emit(k, True))
            return key

        if state.items and not force:
            self._emit_ready_later(key, state)
            return key

        state.items = []
        state.pub_name = ""
        state.fetched_at = 0.0
        state.from_cache = False
        state.error = ""
        state.is_loading = True
        self.loading_changed.emit(key, True)

        worker = _FetchWorker(request, force=force)
        worker.signals.succeeded.connect(self._on_success)
        worker.signals.failed.connect(self._on_failed)
        self._workers[key] = worker
        QThreadPool.globalInstance().start(worker)
        return key

    def _emit_ready_later(self, key: str, state: JWSongsSnapshot) -> None:
        items = [dict(item) for item in state.items]
        pub_name = state.pub_name
        fetched_at = state.fetched_at
        from_cache = state.from_cache
        QTimer.singleShot(
            0,
            lambda: self.songs_ready.emit(key, items, pub_name, fetched_at, from_cache),
        )

    def _on_success(
        self,
        key: str,
        items: list,
        pub_name: str,
        fetched_at: float,
        from_cache: bool,
    ) -> None:
        self._workers.pop(key, None)
        state = self._states.setdefault(key, JWSongsSnapshot(items=[]))
        state.items = [dict(item) for item in items or []]
        state.pub_name = pub_name
        state.fetched_at = float(fetched_at or 0.0)
        state.from_cache = bool(from_cache)
        state.error = ""
        state.is_loading = False
        self.loading_changed.emit(key, False)
        self.songs_ready.emit(
            key,
            [dict(item) for item in state.items],
            state.pub_name,
            state.fetched_at,
            state.from_cache,
        )

    def _on_failed(self, key: str, error: str) -> None:
        self._workers.pop(key, None)
        state = self._states.setdefault(key, JWSongsSnapshot(items=[]))
        state.items = []
        state.error = error
        state.is_loading = False
        self.loading_changed.emit(key, False)
        self.songs_failed.emit(key, error)


__all__ = ["JWSongsRequest", "JWSongsSnapshot", "JWSongsStore"]
