from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent

from solin.core.media.placement import END_OF_LIST_INDEX
from solin.core.playlists.tree_editing import flat_media_insert_index, insert_playlist_media
from solin.widgets.playlist import widget as playlist_widget
from solin.widgets.playlist.drag_drop import PlaylistDragDropMixin
from solin.widgets.playlist.import_export import PlaylistEditImportMixin


def test_playlist_edit_view_uses_drag_drop_mixin():
    assert playlist_widget.PlaylistDragDropMixin is PlaylistDragDropMixin
    assert issubclass(playlist_widget.PlaylistEditView, PlaylistDragDropMixin)
    assert playlist_widget.PlaylistEditView.dropEvent is PlaylistDragDropMixin.dropEvent
    assert playlist_widget.PlaylistEditView.get_drop_context is (
        PlaylistDragDropMixin.get_drop_context
    )


class _PlaylistDropAdapter(PlaylistDragDropMixin, PlaylistEditImportMixin):
    def __init__(self, tmp_path, root):
        self._pl = {
            "id": "source",
            "items": [{"id": "existing", "url": "existing.jpg", "type": "image"}],
            "sections": [],
            "markers": [],
        }
        self.lang = SimpleNamespace(jw_lang_service=None)
        self._jwpub_origin_generation = 1
        self._jwpub_selection_factory = SimpleNamespace(select=Mock())
        self._profile_paths = SimpleNamespace(images_dir=tmp_path / "images")
        self._is_watched = False
        self._watched_path = ""
        self._media_tree_runtime = SimpleNamespace(operations=object())
        self._document_conversion_service = SimpleNamespace(
            office_conversion_available=lambda: False,
        )
        self._tree_session = SimpleNamespace(
            owner_id="playlist:source",
            tree_id="playlist:source",
            generation=1,
            structure_revision=1,
            insert_media=lambda target, index, items: insert_playlist_media(
                self._pl, target, index, items,
            ),
            flat_insert_index=lambda target, index: flat_media_insert_index(
                self._pl, target, index,
            ),
        )
        self.qml_widget = SimpleNamespace(
            mapFrom=lambda _source, point: point,
            rootObject=lambda: root,
        )
        self._save = Mock()
        self._reconcile_playlist = Mock()


def _drop_publications(tmp_path, *, insert_index=-1, root_available=True, **target_overrides):
    properties = {
        "externalDropListId": "root",
        "externalDropIndex": insert_index,
        "externalDropTreeId": "playlist:source",
        "externalDropStructureRevision": 1,
        **target_overrides,
    }
    root = SimpleNamespace(
        getIndexAt=lambda _y: insert_index,
        property=properties.get,
        clearExternalDropPreview=Mock(),
    )
    adapter = _PlaylistDropAdapter(tmp_path, root if root_available else None)
    mime = QMimeData()
    paths = [str(tmp_path / "one.jwpub"), str(tmp_path / "two.jwpub")]
    mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
    event = QDropEvent(
        QPointF(0, 100), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    adapter.dropEvent(event)
    return adapter, root, event


@pytest.mark.parametrize(("insert_index", "expected_index"), [
    pytest.param(-1, END_OF_LIST_INDEX, id="no-row"),
    pytest.param(-7, END_OF_LIST_INDEX, id="negative"),
    pytest.param(None, END_OF_LIST_INDEX, id="missing"),
    pytest.param("0", END_OF_LIST_INDEX, id="non-integer"),
    pytest.param(0, 0, id="top"),
    pytest.param(1, 1, id="finite-end"),
    pytest.param(END_OF_LIST_INDEX, END_OF_LIST_INDEX, id="append"),
])
def test_multi_publication_drop_preserves_order_with_existing_item(
    tmp_path, insert_index, expected_index,
):
    adapter, root, event = _drop_publications(tmp_path, insert_index=insert_index)

    assert event.isAccepted()
    root.clearExternalDropPreview.assert_called_once_with()
    paths = adapter._jwpub_selection_factory.select.call_args.args[0]
    assert [Path(path).name for path in paths] == ["one.jwpub", "two.jwpub"]
    options = adapter._jwpub_selection_factory.select.call_args.kwargs
    outcomes = []
    for path in paths:
        publication = Path(path).stem
        options["insert"](
            [{"id": publication, "url": f"{publication}.jpg", "type": "image"}],
            options["list_id"], options["insert_index"], outcomes.append,
        )

    expected_order = ["one", "two", "existing"] if expected_index == 0 else [
        "existing", "one", "two",
    ]
    assert [item["id"] for item in adapter._pl["items"]] == expected_order
    assert options["list_id"] == "root"
    assert options["insert_index"] == expected_index
    assert len(outcomes) == 2
    assert all(result.target_valid and result.added_count == 1 for result in outcomes)
    assert adapter._save.call_count == 2


def test_publication_drop_without_qml_root_targets_end_of_list(tmp_path):
    adapter, _root, event = _drop_publications(tmp_path, root_available=False)

    assert event.isAccepted()
    options = adapter._jwpub_selection_factory.select.call_args.kwargs
    assert (options["list_id"], options["insert_index"]) == ("root", END_OF_LIST_INDEX)


@pytest.mark.parametrize("stale_target", [
    {"externalDropTreeId": "playlist:other"},
    {"externalDropStructureRevision": 0},
])
def test_publication_drop_rejects_stale_tree_target(tmp_path, stale_target):
    adapter, root, event = _drop_publications(tmp_path, **stale_target)

    assert not event.isAccepted()
    root.clearExternalDropPreview.assert_called_once_with()
    adapter._jwpub_selection_factory.select.assert_not_called()
    adapter._save.assert_not_called()
    assert [item["id"] for item in adapter._pl["items"]] == ["existing"]
