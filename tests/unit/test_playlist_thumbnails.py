from solin.core.playlists.thumbnails import playlist_thumb_path


def test_playlist_thumb_path_uses_explicit_cache_root(tmp_path) -> None:
    assert playlist_thumb_path("item-1", thumb_cache_dir=tmp_path) == (
        tmp_path / "item-1.jpg"
    )
