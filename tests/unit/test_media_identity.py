from __future__ import annotations

from itertools import product

from solin.core.media.identity import (
    PLAYLIST_MEDIA_OCCURRENCES,
    contains_media,
    media_identity,
    partition_media_items,
    same_media,
)
from solin.core.playlists.items import create_playlist_item


def test_stable_jw_id_matches_when_delivery_url_changes() -> None:
    existing = {"jw_media_id": "natural:video", "url": "old.mp4"}
    candidate = {"jw_media_id": "natural:video", "download_url": "new.mp4"}

    assert same_media(existing, candidate) is True


def test_stable_jw_id_still_respects_known_language() -> None:
    portuguese = {"jw_media_id": "natural:video", "language": "T"}
    english = {"jw_media_id": "natural:video", "language": "E"}

    assert same_media(portuguese, english) is False


def test_distinct_stable_jw_ids_do_not_fall_back_to_matching_metadata() -> None:
    first = {
        "jw_media_id": "natural:first",
        "key_symbol": "sjjm",
        "track": 2,
    }
    second = {
        "jw_media_id": "natural:second",
        "key_symbol": "sjjm",
        "track": 2,
    }

    assert same_media(first, second) is False


def test_jw_reference_ignores_quality_and_query_but_respects_language() -> None:
    portuguese_720p = {
        "url": "https://akamd1.jw-cdn.org/x/sjjm_T_002_r720P.mp4?token=old"
    }
    portuguese_480p = {
        "url": "https://akamd1.jw-cdn.org/y/sjjm_T_002_r480P.mp4?token=new"
    }
    english = {"url": "https://akamd1.jw-cdn.org/x/sjjm_E_002_r720P.mp4"}

    assert same_media(portuguese_720p, portuguese_480p) is True
    assert same_media(portuguese_720p, english) is False


def test_unknown_language_matches_equivalent_jw_reference() -> None:
    existing = {"key_symbol": "sjjm", "track": 2}
    candidate = {"pub": "sjjm", "track": 2, "meps_language": 5}

    assert same_media(existing, candidate) is True


def test_document_media_identity_includes_track() -> None:
    existing = {"meps_doc_id": 502100025, "track": 1}

    assert same_media(existing, {"docid": 502100025, "track": 1}) is True
    assert same_media(existing, {"docid": 502100025, "track": 2}) is False


def test_local_path_fallback_is_normalized(tmp_path) -> None:
    media_path = tmp_path / "video.mp4"

    assert same_media(
        {"url": str(media_path)},
        {"file_path": str(tmp_path / "." / "video.mp4")},
    ) is True


def test_local_jw_filename_variants_do_not_imply_duplicate_media(tmp_path) -> None:
    first_path = tmp_path / "sjjm_E_02_r720P.mp4"
    second_path = tmp_path / "sjjm_E_02_r720P (1).mp4"
    first = create_playlist_item(first_path.stem, str(first_path))
    second = create_playlist_item(second_path.stem, str(second_path))

    assert first["key_symbol"] is None
    assert second["key_symbol"] is None
    assert same_media(first, second) is False
    assert partition_media_items([], [first, second]).unique_items == (first, second)


def test_authoritative_local_jw_reference_matches_remote_delivery(tmp_path) -> None:
    local_copy = {
        "file_path": str(tmp_path / "cached.mp4"),
        "key_symbol": "sjjm",
        "track": 2,
        "meps_language": 5,
        "jw_identity_authoritative": True,
    }
    remote = {
        "url": "https://akamd1.jw-cdn.org/x/sjjm_T_002_r720P.mp4"
    }

    local_identity = media_identity(local_copy)
    assert local_identity is not None
    assert local_identity.language_code == ""
    assert same_media(local_copy, remote) is True


def test_copied_media_matches_its_origin_and_destination(tmp_path) -> None:
    source = tmp_path / "source" / "video.mp4"
    destination = tmp_path / "linked" / "video.mp4"
    copied = {"source_url": str(source), "url": str(destination)}

    assert same_media(copied, {"url": str(source)}) is True
    assert same_media(copied, {"url": str(destination)}) is True


def test_partition_preserves_order_and_deduplicates_inside_batch() -> None:
    existing = [{"url": "existing.mp4"}]
    candidates = [
        {"id": "duplicate-existing", "url": "existing.mp4"},
        {"id": "new", "url": "new.mp4"},
        {"id": "duplicate-batch", "url": "new.mp4"},
        {"id": "second", "url": "second.mp4"},
    ]

    result = partition_media_items(existing, candidates)

    assert [item["id"] for item in result.unique_items] == ["new", "second"]
    assert [item["id"] for item in result.duplicate_items] == [
        "duplicate-existing",
        "duplicate-batch",
    ]


def test_playlist_policy_accepts_repeated_videos_as_distinct_occurrences() -> None:
    existing = [{"id": "existing", "url": "video.mp4", "type": "video"}]
    candidates = [
        {"id": "first", "url": "video.mp4", "type": "video"},
        {"id": "second", "url": "video.mp4", "media_type": "video"},
    ]

    result = partition_media_items(
        existing,
        candidates,
        occurrence_policy=PLAYLIST_MEDIA_OCCURRENCES,
    )

    assert result.unique_items == tuple(candidates)
    assert result.duplicate_items == ()


def test_playlist_policy_keeps_audio_and_image_occurrences_unique() -> None:
    existing = [
        {"url": "song.mp3", "type": "audio"},
        {"url": "slide.png", "type": "image"},
    ]
    candidates = [
        {"id": "audio", "url": "song.mp3", "type": "audio"},
        {"id": "image", "url": "slide.png", "type": "image"},
    ]

    result = partition_media_items(
        existing,
        candidates,
        occurrence_policy=PLAYLIST_MEDIA_OCCURRENCES,
    )

    assert result.unique_items == ()
    assert result.duplicate_items == tuple(candidates)


def test_playlist_policy_infers_repeatable_video_from_location() -> None:
    candidate = {"id": "candidate", "url": "video.webm"}

    result = partition_media_items(
        [{"url": "video.webm"}],
        [candidate],
        occurrence_policy=PLAYLIST_MEDIA_OCCURRENCES,
    )

    assert result.unique_items == (candidate,)


def test_playlist_policy_does_not_repeat_audio_mislabeled_as_video() -> None:
    candidate = {"id": "candidate", "url": "song.mp3", "type": "video"}

    result = partition_media_items(
        [{"url": "song.mp3"}],
        [candidate],
        occurrence_policy=PLAYLIST_MEDIA_OCCURRENCES,
    )

    assert result.duplicate_items == (candidate,)


def test_empty_records_never_create_false_duplicates() -> None:
    assert media_identity({}) is None
    assert contains_media([{}], {}) is False


def test_partition_propagates_duplicate_aliases_transitively(tmp_path) -> None:
    first = tmp_path / "first.mp4"
    bridge = tmp_path / "bridge.mp4"
    last = tmp_path / "last.mp4"
    existing = [{"url": str(first), "source_url": str(bridge)}]
    candidates = [
        {"url": str(bridge), "source_url": str(last), "id": "bridge"},
        {"url": str(last), "id": "last"},
    ]

    result = partition_media_items(existing, candidates)

    assert result.unique_items == ()
    assert [item["id"] for item in result.duplicate_items] == ["bridge", "last"]


def test_distinct_source_ids_do_not_match_through_a_shared_location(tmp_path) -> None:
    location = str(tmp_path / "shared.mp4")

    assert contains_media(
        [{"jw_media_id": "first", "url": location}],
        {"jw_media_id": "second", "url": location},
    ) is False


def test_distinct_authoritative_ids_scale_despite_shared_delivery_url() -> None:
    shared_url = "https://cdn.example.test/shared.mp4"
    candidates = [
        {"jw_media_id": f"media-{index}", "url": shared_url}
        for index in range(2_000)
    ]

    result = partition_media_items([], candidates)

    assert len(result.unique_items) == len(candidates)
    assert result.duplicate_items == ()


def test_same_authoritative_id_respects_many_incompatible_languages() -> None:
    candidates = [
        {
            "jw_media_id": "shared-id",
            "language": f"L{index}",
            "meps_language": index + 1,
        }
        for index in range(2_000)
    ]

    result = partition_media_items([], candidates)

    assert len(result.unique_items) == len(candidates)
    assert result.duplicate_items == ()


def test_same_jw_reference_respects_many_incompatible_languages() -> None:
    candidates = [
        {
            "meps_doc_id": 502100025,
            "track": 2,
            "language": f"L{index}",
            "meps_language": index + 1,
        }
        for index in range(2_000)
    ]

    result = partition_media_items([], candidates)

    assert len(result.unique_items) == len(candidates)
    assert result.duplicate_items == ()


def test_same_jw_reference_with_shared_url_scales_without_false_duplicates() -> None:
    candidates = [
        {
            "meps_doc_id": 502100025,
            "track": 2,
            "language": f"L{index}",
            "meps_language": index + 1,
            "url": "https://cdn.example.test/shared.mp4",
        }
        for index in range(2_000)
    ]

    result = partition_media_items([], candidates)

    assert len(result.unique_items) == len(candidates)
    assert result.duplicate_items == ()


def test_identity_index_preserves_pairwise_matching_semantics() -> None:
    records = [
        {
            **({"jw_media_id": source_id} if source_id else {}),
            **({"meps_doc_id": doc_id, "track": 1} if doc_id else {}),
            **({"language": language} if language else {}),
            **({"meps_language": meps_language} if meps_language else {}),
            **({"url": url} if url else {}),
        }
        for source_id, doc_id, language, meps_language, url in product(
            ("", "source-a", "source-b"),
            (0, 100, 200),
            ("", "T", "E"),
            (0, 5, 6),
            ("", "https://cdn.example/shared.mp4", "local.mp4"),
        )
        if source_id or doc_id or url
    ]

    for existing, candidate in product(records, repeat=2):
        assert contains_media([existing], candidate) is same_media(existing, candidate), (
            existing,
            candidate,
        )
