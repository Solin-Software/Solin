from __future__ import annotations

from solin.core.playlists.jwl_import import playlist_items_from_jwl_document_items


def test_jwl_import_maps_jworg_and_local_items_with_metadata():
    result = playlist_items_from_jwl_document_items(
        [
            {
                "source": "jworg",
                "title": "JW Video",
                "type": "video",
                "jworg_url": "https://cdn.example/video.mp4",
                "key_symbol": "mwb",
                "track": 2,
                "issue_tag": 20260500,
                "doc_id": 123,
                "language": 99,
                "start_trim_ticks": 10_000_000,
                "end_trim_ticks": 20_000_000,
                "base_duration_ticks": 90_000_000,
                "accuracy": 2,
                "end_action": 3,
            },
            {
                "source": "local",
                "title": "Local Audio",
                "type": "audio",
                "url": "song.mp3",
            },
        ],
        source_name="playlist.jwlplaylist",
        save_embedded=lambda *_args: "unused",
    )

    assert result.skipped_titles == []
    assert [item["url"] for item in result.items] == [
        "https://cdn.example/video.mp4",
        "song.mp3",
    ]
    assert result.items[0]["key_symbol"] == "mwb"
    assert result.items[0]["track"] == 2
    assert result.items[0]["issue_tag"] == 20260500
    assert result.items[0]["doc_id"] == 123
    assert result.items[0]["meps_language"] == 99
    assert result.items[0]["start_trim_ticks"] == 10_000_000
    assert result.items[0]["end_trim_ticks"] == 20_000_000
    assert result.items[0]["base_duration_ticks"] == 90_000_000
    assert result.items[0]["accuracy"] == 2
    assert result.items[0]["end_action"] == 3


def test_jwl_import_ignores_malformed_trim_as_one_atomic_value():
    result = playlist_items_from_jwl_document_items(
        [{
            "source": "local",
            "title": "Corrupt",
            "type": "video",
            "url": "clip.mp4",
            "start_trim_ticks": "not-an-integer",
            "end_trim_ticks": 20_000_000,
            "base_duration_ticks": 90_000_000,
        }],
        source_name="playlist.jwlplaylist",
        save_embedded=lambda *_args: "unused",
    )

    item = result.items[0]
    assert "start_trim_ticks" not in item
    assert "end_trim_ticks" not in item
    assert "base_duration_ticks" not in item


def test_jwl_import_saves_embedded_media_with_item_identifier():
    calls = []

    def save_embedded(data, filename, identifier, default_suffix):
        calls.append((data, filename, identifier, default_suffix))
        return f"embedded/{identifier}{default_suffix}"

    result = playlist_items_from_jwl_document_items(
        [
            {
                "source": "embedded",
                "title": "Embedded clip",
                "type": "video",
                "data": b"video",
                "filename": "clip.mp4",
                "mime_type": "video/mp4",
                "start_trim_ticks": 5_000_000,
                "end_trim_ticks": 15_000_000,
                "base_duration_ticks": 60_000_000,
            }
        ],
        source_name="playlist.jwlplaylist",
        save_embedded=save_embedded,
        mark_embedded_tmp=True,
    )

    assert result.skipped_titles == []
    item = result.items[0]
    assert calls == [(b"video", "clip.mp4", item["id"], ".mp4")]
    assert item["url"] == f"embedded/{item['id']}.mp4"
    assert item["type"] == "video"
    assert item["_tmp"] is True
    assert item["original_filename"] == "clip.mp4"
    assert item["start_trim_ticks"] == 5_000_000
    assert item["end_trim_ticks"] == 15_000_000
    assert item["base_duration_ticks"] == 60_000_000


def test_jwl_import_skips_unresolved_items_and_failed_embedded_write():
    def fail_embedded(*_args):
        raise OSError("disk full")

    result = playlist_items_from_jwl_document_items(
        [
            {
                "source": "jworg",
                "title": "Offline JW video",
                "type": "video",
            },
            {
                "source": "embedded",
                "title": "Broken embedded",
                "type": "image",
                "data": b"image",
                "mime_type": "image/png",
            },
        ],
        source_name="playlist.jwlplaylist",
        save_embedded=fail_embedded,
    )

    assert result.items == []
    assert result.skipped_titles == ["Offline JW video", "Broken embedded"]
