from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from solin.core.media.cache_listing import CachedMediaItem
from solin.ui.qml.library import DownloadedMediaModel, DownloadedMediaProxyModel
from solin.widgets.library_widget import LibraryWidget


def _item(path: str, title: str, media_type: str = "video") -> CachedMediaItem:
    return CachedMediaItem(
        path=path,
        filename=f"{title}.mp4",
        display_title=title,
        size=5,
        media_type=media_type,
        original_url=f"https://example.test/{title}.mp4",
    )


def test_downloaded_actions_resolve_stable_path_after_filter_changes():
    QCoreApplication.instance() or QCoreApplication([])
    source = DownloadedMediaModel()
    proxy = DownloadedMediaProxyModel()
    proxy.setSourceModel(source)
    source.set_items(
        [_item("C:/media/alpha.mp4", "Alpha"), _item("C:/media/beta.mp4", "Beta")],
        set(),
    )

    proxy.set_query("beta")

    index = proxy.index(0, 0)
    assert proxy.data(index, DownloadedMediaModel.PathRole) == "C:/media/beta.mp4"
    assert source.item_for_path("C:/media/beta.mp4").display_title == "Beta"


def test_selection_updates_only_selection_role_without_model_reset():
    QCoreApplication.instance() or QCoreApplication([])
    source = DownloadedMediaModel()
    source.set_items(
        [_item("C:/media/alpha.mp4", "Alpha"), _item("C:/media/beta.mp4", "Beta")],
        set(),
    )
    resets: list[bool] = []
    changes: list[tuple[int, int, list[int]]] = []
    source.modelReset.connect(lambda: resets.append(True))
    source.dataChanged.connect(
        lambda first, last, roles: changes.append((first.row(), last.row(), roles))
    )

    source.set_selected_paths({"C:/media/beta.mp4"})

    assert resets == []
    assert changes == [(1, 1, [DownloadedMediaModel.SelectedRole])]
    assert source.data(source.index(1, 0), DownloadedMediaModel.SelectedRole) is True


def test_scan_reset_preserves_enriched_title_for_stable_path():
    QCoreApplication.instance() or QCoreApplication([])
    source = DownloadedMediaModel()
    path = "C:/media/opaque-cache-name.mp4"
    source.set_items([_item(path, "opaque-cache-name")], set())
    source.update_title(path, "Meaningful metadata title")

    source.set_items([_item(path, "opaque-cache-name")], set())

    assert source.item_for_path(path).display_title == "Meaningful metadata title"


def test_filter_and_search_changes_clear_hidden_selection():
    QCoreApplication.instance() or QCoreApplication([])
    source = DownloadedMediaModel()
    item = _item("C:/media/alpha.mp4", "Alpha")
    source.set_items([item], {item.path})
    starts: list[bool] = []
    filters: list[bool] = []
    library = SimpleNamespace(
        _downloads_deletion=None,
        _downloads_query="",
        _downloads_filter="all",
        _downloads_selected={item.path},
        downloads_source_model=source,
        _downloads_search_timer=SimpleNamespace(start=lambda: starts.append(True)),
    )
    library._reset_downloads_selection = lambda: LibraryWidget._reset_downloads_selection(
        library
    )
    library._apply_downloads_filter = lambda: filters.append(True)
    library._publish_downloads_state = lambda: None

    LibraryWidget._set_downloads_query(library, "alpha")

    assert library._downloads_selected == set()
    assert source.data(source.index(0, 0), source.SelectedRole) is False
    assert starts == [True]

    library._downloads_selected.add(item.path)
    source.set_selected_paths(library._downloads_selected)
    LibraryWidget._set_downloads_filter(library, "video")

    assert library._downloads_selected == set()
    assert source.data(source.index(0, 0), source.SelectedRole) is False
    assert filters == [True]


def test_download_filters_remain_stable_while_deletion_runs():
    source = DownloadedMediaModel()
    library = SimpleNamespace(
        _downloads_deletion=object(),
        _downloads_query="before",
        _downloads_filter="all",
        _downloads_selected={"C:/media/alpha.mp4"},
        downloads_source_model=source,
    )

    LibraryWidget._set_downloads_query(library, "after")
    LibraryWidget._set_downloads_filter(library, "video")

    assert library._downloads_query == "before"
    assert library._downloads_filter == "all"
    assert library._downloads_selected == {"C:/media/alpha.mp4"}


def test_deletion_targets_only_selected_rows_that_are_still_visible():
    source = DownloadedMediaModel()
    proxy = DownloadedMediaProxyModel()
    proxy.setSourceModel(source)
    alpha = _item("C:/media/alpha.mp4", "Alpha")
    beta = _item("C:/media/beta.mp4", "Beta")
    source.set_items([alpha, beta], {alpha.path, beta.path})
    proxy.set_query("beta")
    library = SimpleNamespace(
        _downloads_selected={alpha.path, beta.path},
        downloads_model=proxy,
    )

    targets = LibraryWidget._visible_selected_download_paths(library)

    assert targets == [beta.path]
