from solin.core.meetings.thumbnails import (
    meeting_thumb_cache_key,
    meeting_thumb_dir,
    meeting_thumb_path,
)


def test_meeting_thumb_path_uses_explicit_cache_root(tmp_path) -> None:
    assert meeting_thumb_cache_key("item-1") == "item-1.jpg"
    assert meeting_thumb_dir(meeting_thumb_cache_dir=tmp_path) == tmp_path
    assert meeting_thumb_path("item-1", meeting_thumb_cache_dir=tmp_path) == (
        tmp_path / "item-1.jpg"
    )
