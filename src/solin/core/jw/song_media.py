"""Framework-free JW song media request and fetch policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solin.core.jw import media_api


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


@dataclass(frozen=True, slots=True)
class JWSongsFetchResult:
    items: list[dict[str, Any]]
    pub_name: str
    fetched_at: float
    from_cache: bool


def fetch_song_media(
    request: JWSongsRequest,
    *,
    cache_dir: str | Path,
    force: bool,
) -> JWSongsFetchResult:
    if request.audio_mode:
        items, pub_name, fetched_at, from_cache = media_api.fetch_songs_audio(
            request.api_code,
            force,
            fallback_code=request.fallback_code,
            is_sign_language=request.is_sign_language,
            cache_dir=cache_dir,
        )
    else:
        items, pub_name, fetched_at, from_cache = media_api.fetch_songs(
            request.api_code,
            force,
            fallback_code=request.fallback_code,
            is_sign_language=request.is_sign_language,
            cache_dir=cache_dir,
        )
    return JWSongsFetchResult(
        items=[dict(item) for item in items or []],
        pub_name=str(pub_name or ""),
        fetched_at=float(fetched_at or 0.0),
        from_cache=bool(from_cache),
    )
