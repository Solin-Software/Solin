from __future__ import annotations

from types import SimpleNamespace

import pytest

from solin.widgets.playlist.widget import PlaylistWidget


class _Signal:
    def __init__(self) -> None:
        self.emitted: list[tuple[object, ...]] = []

    def emit(self, *values: object) -> None:
        self.emitted.append(values)


@pytest.mark.parametrize(
    ("is_temporary", "is_linked", "expected_kind", "expected_container"),
    (
        (False, False, "playlist", "playlist-1"),
        (False, True, "linked_folder", "linked-1"),
        (True, False, "temporary", "temporary-1"),
    ),
)
def test_projected_playlist_items_use_public_origin_ids(
    is_temporary: bool,
    is_linked: bool,
    expected_kind: str,
    expected_container: str,
) -> None:
    signal = _Signal()
    collection_id = "linked-1" if is_linked else ("temporary-1" if is_temporary else "playlist-1")
    widget = SimpleNamespace(
        _edit_view=SimpleNamespace(
            _pl={"id": collection_id},
            _is_temp=is_temporary,
            _is_watched=is_linked,
            _watched_path=r"C:\Private\Linked folder",
        ),
        project_video_signal=signal,
    )
    items = [{"id": "media-1", "title": "Opening", "url": "opening.mp4"}]

    PlaylistWidget._on_project_items(widget, items, 0, "sequential")

    _url, _title, projected, _order = signal.emitted[0]
    assert projected[0]["origin_kind"] == expected_kind
    assert projected[0]["origin_container_id"] == expected_container
    assert projected[0]["origin_item_id"] == "media-1"
    assert "Private" not in str(projected)
