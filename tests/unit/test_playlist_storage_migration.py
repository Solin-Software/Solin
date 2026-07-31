from __future__ import annotations

import json

import pytest

from solin.core.playlists.storage import (
    PLAYLIST_STORE_VERSION,
    PlaylistRepository,
    migrate_playlist_metadata,
)


def test_playlist_metadata_migration_is_idempotent() -> None:
    playlists = [
        {
            "id": "playlist",
            "items": [
                {"id": "seconds", "duration_seconds": 2.5},
                {
                    "id": "ticks",
                    "duration_seconds": 9,
                    "base_duration_ticks": 123,
                },
                {"id": "invalid", "duration_seconds": "bad"},
            ],
        }
    ]

    assert migrate_playlist_metadata(playlists) is True
    assert playlists[0]["items"] == [
        {"id": "seconds", "base_duration_ticks": 25_000_000},
        {"id": "ticks", "base_duration_ticks": 123},
        {"id": "invalid"},
    ]
    assert migrate_playlist_metadata(playlists) is False


def test_playlist_repository_migrates_once_and_preserves_root_metadata(tmp_path) -> None:
    path = tmp_path / "playlists.json"
    path.write_text(
        json.dumps(
            {
                "profile": "main",
                "playlists": [
                    {
                        "id": "playlist",
                        "items": [{"id": "video", "duration_seconds": 3}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    repository = PlaylistRepository(path)

    assert repository.migrate_strict() is True
    assert repository.migrate_strict() is False
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == PLAYLIST_STORE_VERSION
    assert persisted["profile"] == "main"
    assert persisted["playlists"][0]["items"][0] == {
        "id": "video",
        "base_duration_ticks": 30_000_000,
    }


def test_playlist_repository_migration_fails_closed_on_invalid_storage(tmp_path) -> None:
    path = tmp_path / "playlists.json"
    original = '{"playlists": "invalid"}'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError):
        PlaylistRepository(path).migrate_strict()

    assert path.read_text(encoding="utf-8") == original
