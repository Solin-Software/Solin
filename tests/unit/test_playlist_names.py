from __future__ import annotations

import pytest

from solin.core.playlists.names import (
    PlaylistNameConflictError,
    PlaylistNameError,
    PlaylistNameRegistry,
    ensure_unique_playlist_name,
    playlist_name_key,
)


def test_playlist_names_are_trimmed_and_compared_case_insensitively() -> None:
    playlists = [{"id": "one", "name": "  Reunião  ", "items": []}]

    assert playlist_name_key("ＲＥＵＮＩÃＯ") == playlist_name_key("reunião")
    with pytest.raises(PlaylistNameConflictError, match="REUNIÃO"):
        ensure_unique_playlist_name("REUNIÃO", playlists)


def test_rename_excludes_only_the_current_playlist() -> None:
    playlists = [
        {"id": "one", "name": "First", "items": []},
        {"id": "two", "name": "Second", "items": []},
    ]

    assert (
        ensure_unique_playlist_name(" first ", playlists, excluding_id="one")
        == "first"
    )
    with pytest.raises(PlaylistNameConflictError):
        ensure_unique_playlist_name("SECOND", playlists, excluding_id="one")


def test_batch_registry_rejects_collisions_between_new_playlists() -> None:
    registry = PlaylistNameRegistry([{"id": "existing", "name": "Saved"}])

    assert registry.reserve(" Imported ") == "Imported"
    with pytest.raises(PlaylistNameConflictError):
        registry.reserve("ｉｍｐｏｒｔｅｄ")
    with pytest.raises(PlaylistNameConflictError):
        registry.reserve("SAVED")


@pytest.mark.parametrize("value", [None, 12, "", " \n\t "])
def test_invalid_playlist_names_are_rejected(value: object) -> None:
    with pytest.raises(PlaylistNameError):
        ensure_unique_playlist_name(value, [])
