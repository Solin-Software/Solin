from __future__ import annotations

from solin.core.jw import metadata
from solin.core.jw.publication_links import PubMediaFile


def test_resolve_jworg_meta_builds_typed_audio_request(monkeypatch) -> None:
    requests = []

    def resolve(request):
        requests.append(request)
        return PubMediaFile(
            url="https://example.test/song.mp3",
            title="Song",
            thumbnail_url="https://example.test/song.jpg",
            duration_ticks=120_000_000,
        )

    monkeypatch.setattr(metadata._RESOLVER, "resolve_media", resolve)

    assert metadata.resolve_jworg_meta(
        "rr",
        None,
        43,
        None,
        5,
        major_multimedia_type=0,
    ) == {
        "url": "https://example.test/song.mp3",
        "title": "Song",
        "thumbnail_url": "https://example.test/song.jpg",
        "duration_ticks": 120_000_000,
    }
    assert requests[0].media_type == "audio"
    assert requests[0].language == "T"
    assert requests[0].track == 43


def test_transient_resolution_failures_are_not_cached(monkeypatch) -> None:
    calls = []

    def resolve(request):
        calls.append(request)
        return None

    monkeypatch.setattr(metadata._RESOLVER, "resolve_media", resolve)

    assert metadata.resolve_jworg_meta("rr", None, 1, None, 5) is None
    assert metadata.resolve_jworg_meta("rr", None, 1, None, 5) is None
    assert len(calls) == 2
