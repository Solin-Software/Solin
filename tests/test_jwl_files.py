from __future__ import annotations

from solin.core.playlists import jwl_files


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
