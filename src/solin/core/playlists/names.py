"""Canonical playlist-name validation shared by every creation path."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import unicodedata
from typing import Any


class PlaylistNameError(ValueError):
    """Base error for invalid playlist names."""


class PlaylistNameConflictError(PlaylistNameError):
    """Raised when a profile already contains the requested playlist name."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f'A playlist named "{name}" already exists.')


def normalize_playlist_name(value: object) -> str:
    """Return the persisted display name or reject blank/non-string values."""
    if not isinstance(value, str):
        raise PlaylistNameError("Playlist name must be a string.")
    name = value.strip()
    if not name:
        raise PlaylistNameError("Playlist name cannot be empty.")
    return name


def playlist_name_key(value: object) -> str:
    """Build the profile-wide identity key for a playlist display name."""
    return unicodedata.normalize("NFKC", normalize_playlist_name(value)).casefold()


class PlaylistNameRegistry:
    """Reserve unique names against an immutable snapshot of a profile."""

    def __init__(
        self,
        playlists: Iterable[Mapping[str, Any]] = (),
        *,
        excluding_id: str | None = None,
    ) -> None:
        self._keys = {
            playlist_name_key(playlist.get("name"))
            for playlist in playlists
            if str(playlist.get("id") or "") != excluding_id
        }

    def reserve(self, value: object) -> str:
        """Normalize and reserve one name, raising on a collision."""
        name = normalize_playlist_name(value)
        key = playlist_name_key(name)
        if key in self._keys:
            raise PlaylistNameConflictError(name)
        self._keys.add(key)
        return name


def ensure_unique_playlist_name(
    value: object,
    playlists: Iterable[Mapping[str, Any]],
    *,
    excluding_id: str | None = None,
) -> str:
    """Normalize one name and verify profile-wide uniqueness."""
    return PlaylistNameRegistry(playlists, excluding_id=excluding_id).reserve(value)


__all__ = [
    "PlaylistNameConflictError",
    "PlaylistNameError",
    "PlaylistNameRegistry",
    "ensure_unique_playlist_name",
    "normalize_playlist_name",
    "playlist_name_key",
]
