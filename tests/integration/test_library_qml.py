from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from solin.core.media.cache import MediaCacheManager
from solin.core.media.cache_listing import CachedMediaItem
from solin.styles.theme import PALETTE
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.library import (
    DownloadedMediaModel,
    DownloadedMediaProxyModel,
    DownloadedMediaThumbnailProvider,
    LibraryBridge,
    LibraryIconProvider,
    LibrarySectionModel,
    MediaCatalogModel,
)


_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])
elif not isinstance(_APP, QApplication):
    pytest.skip("Library QML tests require QApplication.", allow_module_level=True)


def _visible_texts(item, *, parent_visible: bool = True) -> list[str]:
    visible = parent_visible and bool(item.property("visible"))
    values: list[str] = []
    text = item.property("text")
    if visible and isinstance(text, str) and text:
        values.append(text)
    for child in item.childItems():
        values.extend(_visible_texts(child, parent_visible=visible))
    return values


@pytest.mark.parametrize("size", [(635, 600), (1200, 760)])
def test_library_qml_loads_catalog_and_downloads_sections(tmp_path, size) -> None:
    cache_manager = MediaCacheManager(
        tmp_path,
        downloader_factory=lambda _parent: None,
    )
    catalog_model = MediaCatalogModel(cache_manager)
    catalog_model.set_items(
        [{"number": 1, "title": "Sample song", "duration": 125, "url": ""}],
        audio_mode=False,
    )
    downloads_source = DownloadedMediaModel()
    downloads_proxy = DownloadedMediaProxyModel()
    downloads_proxy.setSourceModel(downloads_source)
    sections = LibrarySectionModel()
    sections.set_sections(
        (
            {"id": "songs", "label": "Songs", "view_kind": "catalog"},
            {"id": "clips", "label": "Original Songs", "view_kind": "catalog"},
            {
                "id": "downloads",
                "label": "Downloads",
                "view_kind": "downloads",
            },
        )
    )
    bridge = LibraryBridge()
    downloads_thumbnail_provider = DownloadedMediaThumbnailProvider()
    bridge.update_state(
        page_title="Library",
        page_subtitle="One place for media",
        active_section="songs",
        section_title="Songs",
        search_placeholder="Search songs…",
        has_items=True,
        play_all_tooltip="Play all",
        shuffle_tooltip="Shuffle",
        refresh_tooltip="Refresh",
        show_download_all=True,
        download_all_enabled=True,
        download_all_tooltip="Download all",
    )

    widget = QQuickWidget()
    widget.resize(*size)
    configure_qml_host(
        widget,
        type_name="LibraryView",
        clear_color=PALETTE.bg0,
        context_properties={
            "catalogModel": catalog_model,
            "downloadsModel": downloads_proxy,
            "librarySectionModel": sections,
            "controller": bridge,
        },
        image_providers={
            "libraryicons": LibraryIconProvider(),
            "librarythumbs": downloads_thumbnail_provider,
        },
        dismiss_text_focus_on_pointer_press=True,
    )
    widget.show()
    _APP.processEvents()

    assert widget.errors() == []
    assert widget.rootObject() is not None
    assert "Library" in _visible_texts(widget.rootObject())
    assert "Sample song" in _visible_texts(widget.rootObject())

    search_field = widget.rootObject().findChild(QObject, "librarySearchField")
    assert search_field is not None
    assert search_field.property("activeFocus") is False
    search_icon_area = search_field.mapToScene(QPointF(10, search_field.height() / 2))
    QTest.mouseClick(
        widget,
        Qt.MouseButton.LeftButton,
        pos=QPoint(round(search_icon_area.x()), round(search_icon_area.y())),
    )
    _APP.processEvents()
    assert search_field.property("activeFocus") is True
    inside = search_field.mapToScene(QPointF(search_field.width() / 2, search_field.height() / 2))
    QTest.mouseClick(
        widget,
        Qt.MouseButton.LeftButton,
        pos=QPoint(round(inside.x()), round(inside.y())),
    )
    _APP.processEvents()
    assert search_field.property("activeFocus") is True
    QTest.mouseClick(
        widget,
        Qt.MouseButton.LeftButton,
        pos=QPoint(widget.width() - 24, widget.height() - 24),
    )
    _APP.processEvents()
    assert search_field.property("activeFocus") is False

    bridge.update_state(
        active_section="downloads",
        section_title="Downloads",
        section_subtitle="Available offline",
        search_placeholder="Search downloads…",
        status_text="No downloaded media found.",
        downloads_loading=False,
        downloads_has_items=False,
        downloads_summary="0 B · 0 files",
        downloads_selection_text="2 selected",
        downloads_selection_size="14.5 MB",
        downloads_delete_label="Delete",
        downloads_has_selection=True,
    )
    _APP.processEvents()

    assert widget.errors() == []
    selection_toolbar = widget.rootObject().findChild(QObject, "downloadsSelectionToolbar")
    assert selection_toolbar is not None
    visible = _visible_texts(widget.rootObject())
    assert "Downloads" in visible
    assert "No downloaded media found." in visible
    assert "2 selected" in visible
    assert "14.5 MB" in visible
    assert "Delete" in visible
    assert selection_toolbar.property("visible") is True
    clear_action = widget.rootObject().findChild(
        QObject, "downloadsClearSelectionAction"
    )
    assert clear_action is not None
    clear_position = clear_action.mapToItem(selection_toolbar, QPointF(0, 0))
    right_inset = selection_toolbar.width() - (
        clear_position.x() + clear_action.width()
    )
    assert right_inset == pytest.approx(7, abs=1)

    bridge.update_state(downloads_deleting=True)
    _APP.processEvents()
    assert selection_toolbar.property("enabled") is False

    bridge.update_state(downloads_deleting=False)
    _APP.processEvents()
    assert selection_toolbar.property("enabled") is True

    error_banner = widget.rootObject().findChild(
        QObject, "downloadsRefreshErrorBanner"
    )
    assert error_banner is not None
    assert error_banner.property("visible") is False
    bridge.update_state(
        downloads_has_items=True,
        status_text="Could not load downloaded media. Try again.",
    )
    _APP.processEvents()
    assert error_banner.property("visible") is True

    bridge.update_state(
        downloads_has_items=False,
        status_text="No downloaded media found.",
    )
    _APP.processEvents()
    assert error_banner.property("visible") is False

    bridge.update_state(downloads_has_selection=False)
    _APP.processEvents()
    assert selection_toolbar.property("visible") is False

    widget.close()
    catalog_model.cleanup()


def test_downloaded_thumbnail_provider_updates_model_with_bounded_preview():
    provider = DownloadedMediaThumbnailProvider(capacity=32)
    model = DownloadedMediaModel()
    item = CachedMediaItem(
        path="C:/media/sample.mp4",
        filename="sample.mp4",
        display_title="Sample",
        size=128,
        media_type="video",
    )
    model.set_items([item], set())
    pixmap = QPixmap(320, 180)
    pixmap.fill(QColor("#5a78ff"))

    source = provider.store(item.path, pixmap)
    assert model.update_thumbnail_source(item.path, source) is True

    assert model.data(model.index(0, 0), model.ThumbnailSourceRole) == source
    provider_id = source.removeprefix("image://librarythumbs/")
    preview = provider.requestPixmap(provider_id, QSize(), QSize(124, 76))
    assert preview.size() == QSize(124, 76)
