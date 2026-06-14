from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from solin.core.jw import metadata
from solin.core.jw.metadata import _extract_best_meta


def test_extract_best_meta_prefers_matching_unsubtitled_track():
    data = {
        "files": {
            "T": {
                "MP4": [
                    {
                        "track": 1,
                        "title": "Wrong Track",
                        "duration": 10,
                        "file": {"url": "https://example.test/wrong.mp4"},
                    },
                    {
                        "track": 2,
                        "title": "720p",
                        "duration": 12.5,
                        "subtitled": False,
                        "file": {"url": "https://example.test/right.mp4"},
                    },
                ]
            }
        }
    }

    assert _extract_best_meta(data, "T", "MP4", target_track=2) == {
        "title": None,
        "duration_ticks": 125_000_000,
    }


def test_resolve_jworg_meta_builds_typed_audio_request(monkeypatch):
    metadata._RESOLVE_CACHE.clear()
    requests: list[str] = []

    def fake_get_json(url: str, **_kwargs):
        requests.append(url)
        return {
            "files": {
                "T": {
                    "MP3": [
                        {
                            "track": 43,
                            "title": "Song",
                            "duration": 12,
                            "file": {"url": "https://example.test/song.mp3"},
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(metadata, "get_json", fake_get_json)

    assert metadata.resolve_jworg_meta(
        "rr",
        None,
        43,
        None,
        5,
        major_multimedia_type=0,
    ) == {
        "title": "Song",
        "duration_ticks": 120_000_000,
    }
    params = parse_qs(urlsplit(requests[0]).query)
    assert params == {
        "langwritten": ["T"],
        "fileformat": ["MP3"],
        "pub": ["rr"],
        "track": ["43"],
    }


def test_resolve_cache_separates_audio_and_video_formats(monkeypatch):
    metadata._RESOLVE_CACHE.clear()
    requested_formats: list[str] = []

    def fake_get_json(url: str, **_kwargs):
        file_format = parse_qs(urlsplit(url).query)["fileformat"][0]
        requested_formats.append(file_format)
        extension = file_format.lower()
        return {
            "files": {
                "T": {
                    file_format: [
                        {
                            "title": file_format,
                            "file": {
                                "url": f"https://example.test/media.{extension}"
                            },
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(metadata, "get_json", fake_get_json)

    audio = metadata.resolve_jworg_meta(
        "rr",
        None,
        1,
        None,
        5,
        major_multimedia_type=0,
    )
    video = metadata.resolve_jworg_meta(
        "rr",
        None,
        1,
        None,
        5,
        major_multimedia_type=1,
    )

    assert audio == {"title": "MP3", "duration_ticks": None}
    assert video == {"title": "MP4", "duration_ticks": None}
    assert requested_formats == ["MP3", "MP4"]
