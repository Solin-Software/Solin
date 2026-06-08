from __future__ import annotations

from app.core.jw.media_api import _parse_clips_osg, _parse_songs, _pick_quality


def test_pick_quality_prefers_lower_resolution_before_higher_fallback():
    items = [{"label": "1080p"}, {"label": "480p"}, {"label": "360p"}]

    assert _pick_quality(items, preferred="720p", fallback_dir="below") == "480p"


def test_pick_quality_can_prefer_higher_resolution_when_configured():
    items = [{"label": "1080p"}, {"label": "480p"}]

    assert _pick_quality(items, preferred="720p", fallback_dir="above") == "1080p"


def test_parse_songs_filters_video_quality_subtitles_and_track_mismatch():
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

    songs, pub_name = _parse_songs(data, "T", "MP4")

    assert pub_name == "Songs"
    assert songs == [
        {
            "number": 2,
            "title": "Good Song",
            "url": "https://example.test/selected.mp4",
            "duration": 0,
        }
    ]


def test_parse_clips_osg_selects_best_quality_and_newest_first():
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

    clips = _parse_clips_osg(data, "T")

    assert [clip["title"] for clip in clips] == ["Newer", "Older"]
    assert clips[0]["url"] == "https://example.test/newer.mp4"
