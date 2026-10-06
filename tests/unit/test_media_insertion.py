from __future__ import annotations

import math
import sys
import time

import pytest

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


def test_media_insert_payload_preserves_all_fields_and_alias_precedence() -> None:
    payload = MediaInsertPayload.from_mapping(
        {
            "title": "  Selected media  ",
            "label": "Fallback title",
            "download_url": "https://cdn.example/selected.mp3",
            "url": "https://cdn.example/fallback.mp4",
            "jworg_url": "https://www.jw.org/fallback",
            "media_type": "AUDIO",
            "type": "image",
            "base_duration_ticks": 37,
            "duration_ticks": 42,
            "duration_seconds": 12.5,
            "thumbnail_url": "https://cdn.example/selected.jpg",
            "thumbnail_path": "cached/selected.jpg",
            "pub": "sjjm",
            "key_symbol": "fallback",
            "track": "3",
            "issue": "202610",
            "issue_tag": 202609,
            "docid": "502100025",
            "doc_id": 502100024,
            "meps_language": "6",
            "language": "t",
            "jw_media_id": "selected-id",
            "jw_identity_authoritative": False,
        }
    )

    assert payload == MediaInsertPayload(
        title="Selected media",
        source_url="https://cdn.example/selected.mp3",
        media_type="audio",
        base_duration_ticks=37,
        thumbnail_url="https://cdn.example/selected.jpg",
        thumbnail_path="cached/selected.jpg",
        key_symbol="sjjm",
        track=3,
        issue_tag=202610,
        doc_id=502100025,
        meps_language=6,
        language="T",
        jw_media_id="selected-id",
        jw_identity_authoritative=True,
    )


def test_duration_ticks_take_precedence_and_invalid_values_are_ignored() -> None:
    assert normalize_duration_ticks(ticks=42, seconds=10) == 42
    assert normalize_duration_ticks(ticks=0, seconds=1.25) == 12_500_000
    for invalid in (True, -1, 0, "bad", math.nan, math.inf):
        assert normalize_duration_ticks(seconds=invalid) == 0


def test_normalized_selection_is_immutable_and_projects_identity_independently() -> None:
    payload = MediaInsertPayload.from_mapping({"pub": "sjjm", "track": "2"})
    with pytest.raises(AttributeError):
        payload.track = 3
    with pytest.raises(AttributeError):
        del payload.key_symbol

    identity = payload.to_identity_mapping()
    assert identity["track"] == 2
    assert identity["pub"] == identity["key_symbol"] == "sjjm"
    identity["track"] = 4
    assert payload.track == 2
    assert payload.to_identity_mapping()["track"] == 2


@pytest.mark.parametrize(
    ("metadata", "authoritative"),
    [
        ({}, False),
        ({"source": "local"}, False),
        ({"source": "JWORG"}, True),
        ({"source": "PUB-MEDIA"}, True),
        ({"source": "CATEGORY:music"}, True),
        ({"jw_identity_authoritative": True}, True),
        ({"jw_identity_authoritative": False, "source": "JWORG"}, True),
        ({"jw_identity_authoritative": True, "source": "local"}, True),
        ({"key_symbol": "sjjm"}, True),
        ({"doc_id": "502100025"}, True),
        ({"jw_media_id": "selected-id"}, True),
        ({"doc_id": True}, False),
        ({"doc_id": "invalid"}, False),
    ],
)
def test_media_insert_identity_authority_uses_metadata_and_source(
    metadata: dict, authoritative: bool
) -> None:
    assert MediaInsertPayload.from_mapping(metadata).jw_identity_authoritative is authoritative


def test_media_insert_empty_aliases_and_invalid_metadata_use_valid_fallbacks() -> None:
    payload = MediaInsertPayload.from_mapping(
        {
            "title": "",
            "label": "  Fallback  ",
            "download_url": None,
            "url": "",
            "jworg_url": "https://www.jw.org/media",
            "media_type": "invalid",
            "pub": "",
            "key_symbol": "sjjm",
            "track": True,
            "issue": math.inf,
            "doc_id": "invalid",
            "docid": "invalid primary",
            "duration_seconds": 0,
            "duration": 10,
            "meps_language": True,
        }
    )
    assert payload.title == "Fallback"
    assert payload.source_url == "https://www.jw.org/media"
    assert payload.media_type == "video"
    assert payload.key_symbol == "sjjm"
    assert payload.track == payload.issue_tag == payload.doc_id == 0
    assert payload.meps_language == payload.base_duration_ticks == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Performance budget uses the Linux reference runner")
def test_media_insert_normalization_cpu_cost_p95_for_ten_thousand_items() -> None:
    """Bound steady-state calling-thread CPU cost on the Linux reference runner.

    Normalization is synchronous and starts no workers. Thread CPU time excludes
    scheduler waits and unrelated Qt background work. Warm-up removes interpreter
    specialization from the measured steady-state budget; twenty 10,000-item
    batches make the reported P95 distinct from the single slowest sample.
    """
    samples_ns: list[tuple[int, int]] = []
    source = {
        "title": "Video",
        "download_url": "https://cdn.example/video.mp4",
        "duration_seconds": 12.5,
        "thumbnail_url": "https://cdn.example/video.jpg",
        "pub": "sjjm",
        "track": 2,
        "language": "T",
    }
    for _index in range(2_000):
        MediaInsertPayload.from_mapping(source)

    for _ in range(20):
        wall_started = time.perf_counter_ns()
        cpu_started = time.thread_time_ns()
        for _index in range(10_000):
            MediaInsertPayload.from_mapping(source)
        cpu_ns = time.thread_time_ns() - cpu_started
        wall_ns = time.perf_counter_ns() - wall_started
        samples_ns.append((cpu_ns, wall_ns))

    cpu_samples_ns = sorted(cpu_ns for cpu_ns, _ in samples_ns)
    wall_samples_ns = sorted(wall_ns for _, wall_ns in samples_ns)
    p50_index = len(samples_ns) // 2
    p95_index = math.ceil(len(samples_ns) * 0.95) - 1
    cpu_p95_ns = cpu_samples_ns[p95_index]
    assert cpu_p95_ns < 125_000_000, (
        f"Calling-thread CPU P50/P95: {cpu_samples_ns[p50_index] / 1_000_000:.3f}/"
        f"{cpu_p95_ns / 1_000_000:.3f} ms; strict CPU P95 budget: <125 ms. "
        f"Diagnostic wall P50/P95: {wall_samples_ns[p50_index] / 1_000_000:.3f}/"
        f"{wall_samples_ns[p95_index] / 1_000_000:.3f} ms. "
        f"Paired (CPU, wall) samples in ns: {samples_ns}"
    )
