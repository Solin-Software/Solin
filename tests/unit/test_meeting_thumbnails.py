from solin.core.meetings.thumbnails import (
    meeting_thumb_cache_key,
    meeting_thumb_dir,
    meeting_thumb_path,
    meeting_thumb_storage_id,
)


def test_meeting_thumb_path_uses_explicit_cache_root(tmp_path) -> None:
    assert meeting_thumb_cache_key("item-1") == "item-1.jpg"
    assert meeting_thumb_dir(meeting_thumb_cache_dir=tmp_path) == tmp_path
    assert meeting_thumb_path("item-1", meeting_thumb_cache_dir=tmp_path) == (
        tmp_path / "item-1.jpg"
    )


def test_generated_meeting_thumbnails_are_scoped_to_the_tree() -> None:
    item_id = "same-generated-node"

    july = meeting_thumb_storage_id("wt:2026-07-06:T:20260500", item_id)
    august = meeting_thumb_storage_id("wt:2026-08-10:T:20260600", item_id)

    assert july != august
    assert meeting_thumb_cache_key(july) != meeting_thumb_cache_key(august)
