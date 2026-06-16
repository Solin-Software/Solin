from __future__ import annotations

from pathlib import Path

import pytest

from solin.core.jw import media_api
from solin.core.jw.media_api import parse_clips_osg, parse_songs, pick_quality
from solin.core.network.http import HttpTransportError


def testpick_quality_prefers_lower_resolution_before_higher_fallback():
    items = [{"label": "1080p"}, {"label": "480p"}, {"label": "360p"}]

    assert pick_quality(items, preferred="720p", fallback_dir="below") == "480p"


def testpick_quality_can_prefer_higher_resolution_when_configured():
    items = [{"label": "1080p"}, {"label": "480p"}]

    assert pick_quality(items, preferred="720p", fallback_dir="above") == "1080p"


def testparse_songs_filters_video_quality_subtitles_and_track_mismatch():
    data = {
        "pubName": "Songs",
        "files": {
            "T": {
                "MP4": [
                    {
                        "title": "2. Good Song",
                        "track": 2,
                        "label": "1080p",
                        "subtitled": False,
                        "file": {"url": "https://example.test/high.mp4"},
                    },
                    {
                        "title": "2. Good Song",
                        "track": 2,
                        "label": "480p",
                        "subtitled": False,
                        "file": {"url": "https://example.test/selected.mp4"},
                    },
                    {
                        "title": "2. Good Song",
                        "track": 2,
                        "label": "480p",
                        "subtitled": True,
                        "file": {"url": "https://example.test/subtitled.mp4"},
                    },
                    {
                        "title": "2. Audio Description",
                        "track": 502,
                        "label": "480p",
                        "subtitled": False,
                        "file": {"url": "https://example.test/described.mp4"},
                    },
                ]
            }
        },
    }

    songs, pub_name = parse_songs(data, "T", "MP4")

    assert pub_name == "Songs"
    assert songs == [
        {
            "number": 2,
            "title": "Good Song",
            "url": "https://example.test/selected.mp4",
            "duration": 0,
        }
    ]


def testparse_clips_osg_selects_best_quality_and_newest_first():
    data = {
        "files": {
            "T": {
                "MP4": [
                    {
                        "title": "Older",
                        "label": "720p",
                        "duration": 10,
                        "file": {"url": "https://example.test/older.mp4"},
                    },
                    {
                        "title": "Newer",
                        "label": "720p",
                        "duration": 12,
                        "file": {"url": "https://example.test/newer.mp4"},
                    },
                    {
                        "title": "Ignored Quality",
                        "label": "360p",
                        "file": {"url": "https://example.test/low.mp4"},
                    },
                ]
            }
        }
    }

    clips = parse_clips_osg(data, "T")

    assert [clip["title"] for clip in clips] == ["Newer", "Older"]
    assert clips[0]["url"] == "https://example.test/newer.mp4"


def test_corrupt_cache_is_treated_as_a_cache_miss(tmp_path):
    cache_path = Path(media_api._cache_path("T", False, tmp_path))
    cache_path.write_text("{invalid", encoding="utf-8")

    assert media_api._is_cache_valid("T", False, tmp_path) is False
    assert media_api._load_cache("T", False, tmp_path) == (None, "", 0)
    assert media_api.get_cache_date("T", cache_dir=tmp_path) is None


def test_unexpected_cache_reader_error_is_not_silenced(tmp_path, monkeypatch):
    Path(media_api._cache_path("T", False, tmp_path)).write_text("{}", encoding="utf-8")

    def fail_unexpectedly(_file):
        raise RuntimeError("programming error")

    monkeypatch.setattr(media_api.json, "load", fail_unexpectedly)

    with pytest.raises(RuntimeError, match="programming error"):
        media_api._load_cache("T", False, tmp_path)


def test_fetch_songs_falls_back_only_for_request_failures(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_get_json(url, *, timeout):
        assert timeout == 15
        calls.append(url)
        if "langwritten=X" in url:
            raise HttpTransportError("offline")
        return {
            "pubName": "Songs",
            "files": {
                "T": {
                    "MP4": [
                        {
                            "title": "1. Song",
                            "track": 1,
                            "label": "720p",
                            "subtitled": False,
                            "file": {"url": "https://example.test/song.mp4"},
                        }
                    ]
                }
            },
        }

    monkeypatch.setattr(media_api, "http_get_json", fake_get_json)
    monkeypatch.setattr(media_api, "_save_cache", lambda *_args: None)

    songs, pub_name, _fetched_at, from_cache = media_api.fetch_songs(
        "X",
        force=True,
        fallback_code="T",
        cache_dir=tmp_path,
    )

    assert [song["number"] for song in songs] == [1]
    assert pub_name == "Songs"
    assert from_cache is False
    assert len(calls) == 2


def test_fetch_songs_does_not_hide_unexpected_request_code_errors(tmp_path, monkeypatch):
    def fail_unexpectedly(*_args, **_kwargs):
        raise RuntimeError("programming error")

    monkeypatch.setattr(media_api, "http_get_json", fail_unexpectedly)

    with pytest.raises(RuntimeError, match="programming error"):
        media_api.fetch_songs(
            "X",
            force=True,
            fallback_code="T",
            cache_dir=tmp_path,
        )
