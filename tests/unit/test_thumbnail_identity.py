from solin.core.media.thumbnail_identity import (
    thumbnail_source_fingerprint,
    thumbnail_storage_id,
)


def test_thumbnail_storage_identity_changes_with_media_source() -> None:
    first = thumbnail_storage_id("media-1", "C:/media/first.mp4")
    second = thumbnail_storage_id("media-1", "C:/media/second.mp4")

    assert first != second
    assert first.startswith("media-1-")
    assert thumbnail_source_fingerprint("C:/media/first.mp4")


def test_thumbnail_storage_identity_is_stable_for_equivalent_local_paths() -> None:
    first = thumbnail_storage_id("media-1", "C:/media/../media/video.mp4")
    second = thumbnail_storage_id("media-1", "C:/media/video.mp4")

    assert first == second
