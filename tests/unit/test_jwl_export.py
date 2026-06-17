from __future__ import annotations

from solin.core.playlists import jwl_export
from solin.core.playlists.jwl_export import (
    JwlPlaylistExportRequest,
    export_jwlplaylist_document,
)


def test_export_jwlplaylist_document_uses_explicit_request(monkeypatch):
    calls = []
    monkeypatch.setattr(
        jwl_export,
        "write_jwlplaylist_document",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    export_jwlplaylist_document(
        JwlPlaylistExportRequest(
            name="Playlist",
            items=[{"title": "Item"}],
            output_path="playlist.jwlplaylist",
            media_cache_dir="cache/media",
            fallback_lang_code="T",
        )
    )

    assert calls == [
        (
            (
                "Playlist",
                [{"title": "Item"}],
                "playlist.jwlplaylist",
                "cache/media",
            ),
            {"fallback_lang_code": "T"},
        )
    ]
