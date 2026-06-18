from __future__ import annotations

import pytest

from solin.core.jw import publication_archive
from solin.core.jw.publication_archive import (
    JwpubArchiveDownloadError,
    download_jwpub_archive,
    resolve_meeting_video,
)
from solin.core.jw.publication_links import PubMediaFile
from solin.core.network.http import HttpError


class _Stream:
    headers = {"Content-Length": "6"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def iter_bytes(self, chunk_size: int):
        assert chunk_size == 2
        yield b"ab"
        yield b"cd"
        yield b"ef"


def test_download_jwpub_archive_writes_file_and_reports_progress(tmp_path):
    calls = []
    progress = []

    def stream_factory(url: str, *, timeout: int, headers: dict[str, str]):
        calls.append((url, timeout, headers))
        return _Stream()

    destination = tmp_path / "mwb_T" / "mwb_T_20260600.jwpub"

    download_jwpub_archive(
        "https://example.test/mwb.jwpub",
        destination,
        progress=progress.append,
        stream_factory=stream_factory,
        timeout=12,
        chunk_size=2,
    )

    assert destination.read_bytes() == b"abcdef"
    assert progress == [33, 66, 100]
    assert calls == [
        (
            "https://example.test/mwb.jwpub",
            12,
            {"User-Agent": "Mozilla/5.0"},
        )
    ]


def test_download_jwpub_archive_raises_transport_independent_error(tmp_path):
    def stream_factory(url: str, *, timeout: int, headers: dict[str, str]):
        raise HttpError("offline")

    with pytest.raises(JwpubArchiveDownloadError, match="offline"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            tmp_path / "archive.jwpub",
            stream_factory=stream_factory,
        )


def test_resolve_meeting_video_returns_simple_result(monkeypatch):
    class Resolver:
        def resolve_video(self, request):
            assert request.key_symbol == "sjjm"
            assert request.is_sign_language is True
            return PubMediaFile(
                url="https://example.test/song.mp4",
                title="Song",
                thumbnail_url="https://example.test/thumb.jpg",
            )

    monkeypatch.setattr(publication_archive, "_PUBLICATION_MEDIA_RESOLVER", Resolver())

    assert resolve_meeting_video("sjjm", 1, 0, 0, "T", is_sign_language=True) == {
        "url": "https://example.test/song.mp4",
        "title": "Song",
        "thumbnail": "https://example.test/thumb.jpg",
    }
