from __future__ import annotations

from solin.core.media.identity import (
    contains_media,
    media_identity,
    partition_media_items,
    same_media,
)


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


def test_empty_records_never_create_false_duplicates() -> None:
    assert media_identity({}) is None
    assert contains_media([{}], {}) is False
