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


def test_jwlplaylist_document_helpers_forward_transfer_callbacks(monkeypatch):
    read_calls = []
    write_calls = []
    progress = lambda *_args: None
    should_cancel = lambda: False
    monkeypatch.setattr(
        jwl_files,
        "read_jwlplaylist",
        lambda *args, **kwargs: read_calls.append((args, kwargs))
        or {"name": "Playlist", "items": []},
    )
    monkeypatch.setattr(
        writer,
        "write_jwlplaylist",
        lambda *args, **kwargs: write_calls.append((args, kwargs)),
    )

    jwl_files.read_jwlplaylist_document(
        "playlist.jwlplaylist",
        progress_callback=progress,
        should_cancel=should_cancel,
    )
    jwl_files.write_jwlplaylist_document(
        "Playlist",
        [],
        "playlist.jwlplaylist",
        "cache/media",
        progress_callback=progress,
        should_cancel=should_cancel,
    )

    assert read_calls[0][1]["progress_callback"] is progress
    assert read_calls[0][1]["should_cancel"] is should_cancel
    assert write_calls[0][1]["progress_callback"] is progress
    assert write_calls[0][1]["should_cancel"] is should_cancel
