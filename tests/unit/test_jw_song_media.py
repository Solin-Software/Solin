from __future__ import annotations

from solin.core.jw import song_media
from solin.core.jw.song_media import JWSongsRequest, fetch_song_media


def test_song_request_key_includes_language_sign_and_mode():
    assert (
        JWSongsRequest(
            api_code="T",
            fallback_code="E",
            is_sign_language=True,
            audio_mode=True,
        ).key
        == "T|E|sl|audio"
    )


def test_fetch_song_media_routes_video_requests(monkeypatch, tmp_path):
    calls = []

    def fetch_songs(api_code, force, **kwargs):
        calls.append((api_code, force, kwargs))
        return ([{"number": 1}], "Songs", 123.0, False)

    monkeypatch.setattr(song_media.media_api, "fetch_songs", fetch_songs)

    result = fetch_song_media(
        JWSongsRequest(
            api_code="T",
            fallback_code="E",
            is_sign_language=False,
            audio_mode=False,
        ),
        cache_dir=tmp_path,
        force=True,
    )

    assert result.items == [{"number": 1}]
    assert result.pub_name == "Songs"
    assert result.fetched_at == 123.0
    assert result.from_cache is False
    assert calls == [
        (
            "T",
            True,
            {
                "fallback_code": "E",
                "is_sign_language": False,
                "cache_dir": tmp_path,
            },
        )
    ]


def test_fetch_song_media_routes_audio_requests(monkeypatch, tmp_path):
    calls = []

    def fetch_songs_audio(api_code, force, **kwargs):
        calls.append((api_code, force, kwargs))
        return ([{"number": 2}], "Songs Audio", 456.0, True)

    monkeypatch.setattr(song_media.media_api, "fetch_songs_audio", fetch_songs_audio)

    result = fetch_song_media(
        JWSongsRequest(
            api_code="T",
            fallback_code="E",
            is_sign_language=False,
            audio_mode=True,
        ),
        cache_dir=tmp_path,
        force=False,
    )

    assert result.items == [{"number": 2}]
    assert result.pub_name == "Songs Audio"
    assert result.fetched_at == 456.0
    assert result.from_cache is True
    assert calls[0][0] == "T"
    assert calls[0][1] is False
    assert calls[0][2]["cache_dir"] == tmp_path
