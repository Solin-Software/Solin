from __future__ import annotations

from solin.core.playlists import jwl_files
from solin.core.playlists import writer


def test_read_jwlplaylist_document_wraps_reader_result(monkeypatch):
    monkeypatch.setattr(
        jwl_files,
        "read_jwlplaylist",
        lambda path, *, fallback_lang_code: {
            "name": "",
            "items": [{"title": "Item"}],
        },
    )

    document = jwl_files.read_jwlplaylist_document(
        "meeting-media.jwlplaylist",
        fallback_lang_code="T",
    )

    assert document.name == "meeting-media"
    assert document.items == [{"title": "Item"}]


def test_write_jwlplaylist_document_wraps_writer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        writer,
        "write_jwlplaylist",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    jwl_files.write_jwlplaylist_document(
        "Playlist",
        [{"title": "Item"}],
        "playlist.jwlplaylist",
        "cache/media",
        fallback_lang_code="T",
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
