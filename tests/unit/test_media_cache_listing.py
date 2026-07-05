from __future__ import annotations

from pathlib import Path

from solin.core.media.cache_listing import scan_cached_media_items
from solin.core.media.download_storage import cached_path_for


def test_scan_cached_media_items_reads_complete_media_entries(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    media.with_name("clip.mp4.done").write_text(
        "https://example.test/clip.mp4",
        encoding="utf-8",
    )
    (tmp_path / "partial.mp4").write_bytes(b"partial")
    (tmp_path / "ignored.tmp").write_bytes(b"tmp")
    (tmp_path / "document.txt").write_text("text", encoding="utf-8")
    (tmp_path / "document.txt.done").write_text(
        "https://example.test/document.txt",
        encoding="utf-8",
    )

    items = scan_cached_media_items(tmp_path)

    assert len(items) == 1
    assert items[0].path == str(media)
    assert items[0].filename == "clip.mp4"
    assert items[0].display_title == "clip"
    assert items[0].size == 5
    assert items[0].media_type == "video"
    assert items[0].original_url == "https://example.test/clip.mp4"


def test_scan_cached_media_items_honors_cancellation(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    media.with_name("clip.mp4.done").write_text(
        "https://example.test/clip.mp4",
        encoding="utf-8",
    )

    assert scan_cached_media_items(tmp_path, is_cancelled=lambda: True) == []


def test_scan_hashed_cache_entry_displays_original_filename(tmp_path):
    url = "https://example.test/media/My%20Clip.mp4?quality=720"
    media = cached_path_for(url, tmp_path)
    with open(media, "wb") as handle:
        handle.write(b"video")
    with open(f"{media}.done", "w", encoding="utf-8") as marker:
        marker.write(url)

    items = scan_cached_media_items(tmp_path)

    assert len(items) == 1
    assert items[0].path == media
    assert items[0].filename == "My Clip.mp4"
    assert items[0].display_title == "My Clip"
    assert items[0].original_url == url


def test_scan_does_not_normalize_corrupt_origin_marker(tmp_path):
    url = "https://example.test/media/clip.mp4"
    media = cached_path_for(url, tmp_path)
    with open(media, "wb") as handle:
        handle.write(b"video")
    with open(f"{media}.done", "w", encoding="utf-8") as marker:
        marker.write(f"{url}\n")

    items = scan_cached_media_items(tmp_path)

    assert len(items) == 1
    assert items[0].original_url == ""
    assert items[0].filename == Path(media).name
