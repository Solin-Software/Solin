from __future__ import annotations

import math
import time

from solin.core.media.duration import normalize_duration_ticks
from solin.core.media.insertion import MediaInsertPayload


def test_media_insert_payload_normalizes_jw_metadata_once() -> None:
    payload = MediaInsertPayload.from_mapping(
        {
            "title": "Video",
            "download_url": "https://cdn.example/video.mp4",
            "duration_seconds": 12.5,
            "thumbnail_url": "https://cdn.example/video.jpg",
            "pub": "sjjm",
            "track": "2",
            "language": "t",
        }
    )

    assert payload.base_duration_ticks == 125_000_000
    assert payload.thumbnail_url == "https://cdn.example/video.jpg"
    assert payload.key_symbol == "sjjm"
    assert payload.track == 2
    assert payload.language == "T"
    assert payload.jw_identity_authoritative is True


def test_duration_ticks_take_precedence_and_invalid_values_are_ignored() -> None:
    assert normalize_duration_ticks(ticks=42, seconds=10) == 42
    assert normalize_duration_ticks(ticks=0, seconds=1.25) == 12_500_000
    for invalid in (True, -1, 0, "bad", math.nan, math.inf):
        assert normalize_duration_ticks(seconds=invalid) == 0


def test_media_insert_normalization_p95_for_ten_thousand_items() -> None:
    samples: list[float] = []
    source = {
        "title": "Video",
        "download_url": "https://cdn.example/video.mp4",
        "duration_seconds": 12.5,
        "thumbnail_url": "https://cdn.example/video.jpg",
        "pub": "sjjm",
        "track": 2,
        "language": "T",
    }
    for _ in range(7):
        started = time.perf_counter()
        for _index in range(10_000):
            MediaInsertPayload.from_mapping(source)
        samples.append((time.perf_counter() - started) * 1_000)

    samples.sort()
    p95 = samples[math.ceil(len(samples) * 0.95) - 1]
    assert p95 < 125
