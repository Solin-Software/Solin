from __future__ import annotations

import os
import random
import uuid
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from solin.core.foundation.exception_logging import log_ignored_exception
from solin.core.foundation.qt_threads import stop_owned_qthread
from solin.core.i18n.manager import LanguageManager
from solin.core.i18n.strings import tr_item_count
from solin.core.jw.language_context import jw_media_language_context
from solin.core.jw.songs import JWSongsStore
from solin.core.media.cache import MediaCacheManager
from solin.core.media.cache_listing import CachedMediaItem
from solin.core.media.cache_delete import (
    CacheDeletionSession,
    CacheDeletionSessionFactory,
)
from solin.core.media.cache_scan import CacheScanSession, CacheScanSessionFactory
from solin.styles.theme import PALETTE
from solin.ui.helpers import begin_qml_pointer_cursor, end_qml_pointer_cursor
from solin.ui.qml.host import apply_qml_theme, configure_qml_host
from solin.ui.qml.library import (
    DownloadedMediaModel,
    DownloadedMediaProxyModel,
    DownloadedMediaThumbnailProvider,
    LibraryBridge,
    LibraryIconProvider,
    LibrarySectionModel,
    MediaCatalogModel,
    format_file_size,
)

if TYPE_CHECKING:
    from solin.core.jw.clip_fetch import ClipFetchThread, ClipFetchThreadFactory
    from solin.ui.media_info import MediaInfoService


_CATALOG_SECTIONS = frozenset({"songs", "clips"})
_LIBRARY_SECTIONS = _CATALOG_SECTIONS | {"downloads"}


@dataclass(slots=True)
class _CatalogState:
    items: list[dict] = field(default_factory=list)
    filtered_items: list[dict] = field(default_factory=list)
    query: str = ""
    audio_mode: bool = False
    loaded: bool = False
    loading: bool = False
    error_text: str = ""
    publication_name: str = ""
    fetched_at: float = 0.0
    from_cache: bool = False
    request_key: str = ""


class LibraryWidget(QWidget):
    """One production UI for JW catalogs and locally downloaded media."""

    project_media_signal = Signal(str, str, str, object, str)
    play_cached_media_signal = Signal(str, str, str, str)

    def __init__(
        self,
        lang_manager: LanguageManager,
        cache_manager: MediaCacheManager,
        songs_store: JWSongsStore,
        jw_cache_dir: str | os.PathLike[str],
        clip_fetch_thread_factory: ClipFetchThreadFactory,
        cache_scan_session_factory: CacheScanSessionFactory,
        cache_deletion_session_factory: CacheDeletionSessionFactory,
        media_info_service_factory: Callable[[QObject], MediaInfoService],
        *,
        defer_qml: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.lang = lang_manager
        self._cache_manager = cache_manager
        self._songs_store = songs_store
        self._jw_cache_dir = jw_cache_dir
        self._clip_fetch_thread_factory = clip_fetch_thread_factory
        self._cache_scan_session_factory = cache_scan_session_factory
        self._cache_deletion_session_factory = cache_deletion_session_factory
        self._defer_qml = defer_qml
        self._active_section = "songs"
        self._catalogs = {
            "songs": _CatalogState(),
            "clips": _CatalogState(),
        }
        self._clip_generation = 0
        self._clip_threads: list[ClipFetchThread] = []
        self._download_batch_ids: dict[tuple[str, str], str] = {}
        self._download_error_batches: set[str] = set()

        self._downloads_items: list[CachedMediaItem] = []
        self._downloads_titles: dict[str, str] = {}
        self._downloads_filtered: list[CachedMediaItem] = []
        self._downloads_selected: set[str] = set()
        self._downloads_query = ""
        self._downloads_filter = "all"
        self._downloads_loaded = False
        self._downloads_dirty = True
        self._downloads_scan: CacheScanSession | None = None
        self._downloads_scan_items: list[CachedMediaItem] = []
        self._downloads_scan_is_initial = True
        self._downloads_change_revision = 0
        self._downloads_scan_revision = 0
        self._downloads_error_text = ""
        self._downloads_deletion: CacheDeletionSession | None = None

        self._disposed = False
        self._qml_pointer_depth = 0
        self._download_state_timer = self._single_shot_timer(120, self._refresh_download_all_state)
        self._downloads_search_timer = self._single_shot_timer(100, self._apply_downloads_filter)
        self._downloads_refresh_timer = self._single_shot_timer(500, self._refresh_dirty_downloads)

        self.catalog_model = MediaCatalogModel(cache_manager, self)
        self.section_model = LibrarySectionModel(self)
        self._downloads_thumbnail_provider = DownloadedMediaThumbnailProvider()
        self.downloads_source_model = DownloadedMediaModel(self)
        self.downloads_model = DownloadedMediaProxyModel(self)
        self.downloads_model.setSourceModel(self.downloads_source_model)
        self._downloads_info_service = media_info_service_factory(self)
        self.bridge = LibraryBridge(self)
        self._build_qml()
        self._connect_signals()
        self._sync_static_text()
        self._publish_catalog_state()
        self._load_catalog("songs")

    def _single_shot_timer(self, interval: int, callback) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval)
        timer.timeout.connect(callback)
        return timer

    def _build_qml(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.qml_widget = QQuickWidget(self)
        self.qml_widget.installEventFilter(self)
        self.qml_load_handle = configure_qml_host(
            self.qml_widget,
            type_name="LibraryView",
            clear_color=PALETTE.bg0,
            image_providers={
                "libraryicons": LibraryIconProvider(),
                "librarythumbs": self._downloads_thumbnail_provider,
            },
            context_properties={
                "catalogModel": self.catalog_model,
                "downloadsModel": self.downloads_model,
                "librarySectionModel": self.section_model,
                "controller": self.bridge,
            },
            mouse_tracking=True,
            defer_load=self._defer_qml,
            dismiss_text_focus_on_pointer_press=True,
        )
        layout.addWidget(self.qml_widget, stretch=1)

    def _connect_signals(self) -> None:
        bridge = self.bridge
        bridge.sectionRequested.connect(self._activate_section)
        bridge.refreshRequested.connect(self._refresh_active_catalog)
        bridge.playAllRequested.connect(self._play_all)
        bridge.shuffleRequested.connect(self._play_shuffle)
        bridge.downloadAllRequested.connect(self._download_all_current_catalog)
        bridge.searchChanged.connect(self._filter_active_catalog)
        bridge.modeChanged.connect(self._set_song_mode)
        bridge.itemPlayRequested.connect(self._play_catalog_row)
        bridge.itemDownloadRequested.connect(self._download_catalog_row)
        bridge.downloadsRefreshRequested.connect(lambda: self._scan_downloads(force=True))
        bridge.downloadsSearchChanged.connect(self._set_downloads_query)
        bridge.downloadsFilterChanged.connect(self._set_downloads_filter)
        bridge.downloadsItemPlayRequested.connect(self._play_downloaded_row)
        bridge.downloadsItemSelectionRequested.connect(self._toggle_downloaded_row)
        bridge.downloadsToggleSelectAllRequested.connect(self._toggle_select_all_downloads)
        bridge.downloadsClearSelectionRequested.connect(self._clear_downloads_selection)
        bridge.downloadsDeleteRequested.connect(self._confirm_delete_downloads)
        bridge.downloadsInfoRequested.connect(self._request_downloaded_info)
        bridge.pointerEntered.connect(self.begin_qml_pointer_cursor)
        bridge.pointerExited.connect(self.end_qml_pointer_cursor)

        store = self._songs_store
        store.songs_ready.connect(self._on_songs_ready)
        store.songs_failed.connect(self._on_songs_failed)
        store.loading_changed.connect(self._on_songs_loading_changed)
        media_language = getattr(self.lang, "jw_lang_service", None)
        if media_language is not None:
            media_language.media_language_changed.connect(self._on_media_language_changed)

        manager = self._cache_manager
        manager.cache_changed.connect(self._on_cache_inventory_changed)
        manager.cache_removed.connect(self._on_cache_inventory_changed)
        manager.prefetch_queued.connect(self._schedule_download_state_refresh)
        manager.prefetch_dequeued.connect(self._schedule_download_state_refresh)
        manager.prefetch_batch_changed.connect(self._on_prefetch_batch_changed)
        manager.prefetch_batch_error.connect(self._on_prefetch_batch_error)
        self._downloads_info_service.info_ready.connect(self._on_downloaded_info_ready)

    def _sync_static_text(self) -> None:
        self.section_model.set_sections(
            (
                {
                    "id": "songs",
                    "label": self.tr("Songs"),
                    "view_kind": "catalog",
                },
                {
                    "id": "clips",
                    "label": self.tr("Original Songs"),
                    "view_kind": "catalog",
                },
                {
                    "id": "downloads",
                    "label": self.tr("Downloads"),
                    "view_kind": "downloads",
                },
            )
        )
        self.bridge.update_state(
            page_title=self.tr("Library"),
            page_subtitle="",
            songs_label=self.tr("Songs"),
            clips_label=self.tr("Original Songs"),
            downloads_label=self.tr("Downloads"),
            refresh_tooltip=self.tr("Refresh"),
            play_all_tooltip=self.tr("Play all (in order)"),
            shuffle_tooltip=self.tr("Play in random order"),
            video_tooltip=self.tr("Video songs"),
            audio_tooltip=self.tr("Audio songs"),
            downloads_all_label=self.tr("All"),
            downloads_video_label=self.tr("Videos"),
            downloads_audio_label=self.tr("Audio"),
            downloads_image_label=self.tr("Images"),
            downloads_delete_label=self.tr("Delete"),
            downloads_delete_tooltip=self.tr("Delete selected downloads"),
            downloads_clear_selection_tooltip=self.tr("Clear selection"),
            downloads_empty_text=self.tr("No downloaded media found."),
        )
        if self._active_section in _CATALOG_SECTIONS:
            self._publish_catalog_state()
        else:
            self._publish_downloads_state()

    @Slot(str)
    def _activate_section(self, section: str) -> None:
        if section not in _LIBRARY_SECTIONS or section == self._active_section:
            return
        self._active_section = section
        self._reset_qml_pointer_cursor()
        self.bridge.update_state(active_section=section)
        if section == "downloads":
            self._publish_downloads_state()
            self._ensure_downloads_loaded()
            return
        self._publish_catalog_state()
        state = self._catalogs[section]
        if not state.loaded and not state.loading:
            self._load_catalog(section)

    def _refresh_active_catalog(self) -> None:
        if self._active_section in _CATALOG_SECTIONS:
            self._load_catalog(self._active_section, force=True)

    def _load_catalog(self, section: str, *, force: bool = False) -> None:
        if self._disposed or section not in _CATALOG_SECTIONS:
            return
        state = self._catalogs[section]
        state.loading = True
        state.error_text = ""
        if section == self._active_section:
            self.catalog_model.set_items([], audio_mode=state.audio_mode)
            self._publish_catalog_state()

        context = jw_media_language_context(self.lang)
        if section == "songs":
            request = self._songs_store.request_for(
                api_code=context.api_code,
                fallback_code=context.fallback_code,
                is_sign_language=context.is_sign_language,
                audio_mode=state.audio_mode,
            )
            state.request_key = self._songs_store.ensure_loaded(request, force=force)
            return

        self._clip_generation += 1
        generation = self._clip_generation
        self._stop_clip_threads(wait=False)
        thread = self._clip_fetch_thread_factory.create(
            generation,
            api_code=context.api_code,
            fallback_code=context.fallback_code,
            is_sign_language=context.is_sign_language,
            force=force,
            cache_dir=self._jw_cache_dir,
            parent=self,
        )
        thread.items_ready.connect(self._on_clips_ready)
        thread.failed.connect(self._on_clips_failed)
        thread.finished.connect(
            lambda current=thread: (
                self._clip_threads.remove(current) if current in self._clip_threads else None
            )
        )
        self._clip_threads.append(thread)
        thread.start()

    @Slot(str, list, str, float, bool)
    def _on_songs_ready(
        self,
        key: str,
        items: list,
        publication_name: str,
        fetched_at: float,
        from_cache: bool,
    ) -> None:
        state = self._catalogs["songs"]
        if key == state.request_key:
            self._catalog_loaded("songs", items, publication_name, fetched_at, from_cache)

    @Slot(str, str)
    def _on_songs_failed(self, key: str, message: str) -> None:
        if key == self._catalogs["songs"].request_key:
            self._catalog_failed("songs", message)

    @Slot(str, bool)
    def _on_songs_loading_changed(self, key: str, loading: bool) -> None:
        state = self._catalogs["songs"]
        if key == state.request_key and loading:
            state.loading = True
            if self._active_section == "songs":
                self._publish_catalog_state()

    @Slot(int, list, float, bool)
    def _on_clips_ready(
        self,
        generation: int,
        items: list,
        fetched_at: float,
        from_cache: bool,
    ) -> None:
        if not self._disposed and generation == self._clip_generation:
            self._catalog_loaded("clips", items, "", fetched_at, from_cache)

    @Slot(int, str)
    def _on_clips_failed(self, generation: int, message: str) -> None:
        if not self._disposed and generation == self._clip_generation:
            self._catalog_failed("clips", message)

    def _catalog_loaded(
        self,
        section: str,
        items: list,
        publication_name: str,
        fetched_at: float,
        from_cache: bool,
    ) -> None:
        state = self._catalogs[section]
        state.items = list(items or [])
        state.loaded = True
        state.loading = False
        state.error_text = ""
        state.publication_name = publication_name
        state.fetched_at = fetched_at
        state.from_cache = from_cache
        self._filter_catalog(section, state.query)
        if section == self._active_section:
            self._publish_catalog_state()
        self._refresh_download_all_state()

    def _catalog_failed(self, section: str, message: str) -> None:
        state = self._catalogs[section]
        state.loading = False
        state.loaded = False
        state.error_text = str(message or "")
        if section == self._active_section:
            self._publish_catalog_state()
        self._refresh_download_all_state()

    @Slot(str)
    def _filter_active_catalog(self, query: str) -> None:
        if self._active_section not in _CATALOG_SECTIONS:
            return
        self._filter_catalog(self._active_section, query)
        self._publish_catalog_state()

    def _filter_catalog(self, section: str, query: str) -> None:
        state = self._catalogs[section]
        state.query = str(query or "").strip()
        folded_query = state.query.casefold()
        if not folded_query:
            filtered = state.items
        elif section == "songs":
            filtered = [
                item
                for item in state.items
                if folded_query in str(item.get("title", "")).casefold()
                or folded_query == str(item.get("number", ""))
            ]
        else:
            filtered = [
                item
                for item in state.items
                if folded_query in str(item.get("title", "")).casefold()
            ]
        state.filtered_items = list(filtered)
        if section == self._active_section:
            self.catalog_model.set_items(
                state.filtered_items,
                audio_mode=state.audio_mode,
            )

    @Slot(str)
    def _set_song_mode(self, mode: str) -> None:
        if self._active_section != "songs":
            return
        state = self._catalogs["songs"]
        wants_audio = mode == "audio"
        if wants_audio == state.audio_mode:
            return
        if wants_audio and not self._songs_support_audio():
            return
        state.audio_mode = wants_audio
        state.loaded = False
        state.items.clear()
        state.filtered_items.clear()
        self._publish_catalog_state()
        self._load_catalog("songs")

    @Slot(str)
    def _on_media_language_changed(self, _code: str) -> None:
        for state in self._catalogs.values():
            state.loaded = False
            state.loading = False
            state.items.clear()
            state.filtered_items.clear()
        songs = self._catalogs["songs"]
        if songs.audio_mode and not self._songs_support_audio():
            songs.audio_mode = False
        self._clip_generation += 1
        self._stop_clip_threads(wait=False)
        if self._active_section in _CATALOG_SECTIONS:
            self._load_catalog(self._active_section, force=True)

    def _songs_support_audio(self) -> bool:
        return not jw_media_language_context(self.lang).is_sign_language

    def _publish_catalog_state(self) -> None:
        if self._active_section not in _CATALOG_SECTIONS:
            return
        section = self._active_section
        state = self._catalogs[section]
        is_songs = section == "songs"
        if state.loading:
            status = self.tr("Loading songs…") if is_songs else self.tr("Loading clips…")
        elif state.error_text:
            prefix = (
                self.tr("Error loading songs. Check your connection.")
                if is_songs
                else self.tr("Error loading clips. Check your connection.")
            )
            status = f"{prefix}\n{state.error_text}" if state.error_text else prefix
        elif state.loaded and not state.filtered_items:
            status = (
                self.tr("No songs match your search.")
                if is_songs
                else self.tr("No clips match your search.")
            )
        else:
            status = ""
        search_label = self.tr("Search songs…") if is_songs else self.tr("Search music videos…")
        self.bridge.update_state(
            active_section=section,
            section_title=self.tr("Songs") if is_songs else self.tr("Original Songs"),
            section_subtitle=state.publication_name,
            search_placeholder=self.tr("{search} · {count}").format(
                search=search_label,
                count=tr_item_count(len(state.items)),
            ),
            search_text=state.query,
            status_text=status,
            loading=state.loading,
            has_items=bool(state.items),
            supports_audio=is_songs and self._songs_support_audio(),
            audio_mode=state.audio_mode,
            refresh_enabled=not state.loading,
            show_download_all=True,
        )
        self.catalog_model.set_items(
            state.filtered_items,
            audio_mode=state.audio_mode,
        )
        self._refresh_download_all_state()

    def _play_catalog_row(self, row_index: int) -> None:
        item = self.catalog_model.item_at(row_index)
        if item:
            self._play_catalog_item(item)

    def _play_catalog_item(self, item: dict) -> None:
        section = self._active_section
        if section not in _CATALOG_SECTIONS:
            return
        url = str(item.get("url", ""))
        if not url:
            return
        title = self._display_title(section, item)
        playlist = self._rotated_playlist(section, item)
        self.project_media_signal.emit(section, url, title, playlist, "")

    def _play_all(self) -> None:
        section = self._active_section
        if section not in _CATALOG_SECTIONS:
            return
        ordered = self._ordered_items(section)
        if not ordered:
            return
        playlist = [
            {"url": item["url"], "title": self._display_title(section, item)}
            for item in ordered
            if item.get("url")
        ]
        if not playlist:
            return
        self.project_media_signal.emit(
            section, playlist[0]["url"], playlist[0]["title"], playlist, "next"
        )

    def _play_shuffle(self) -> None:
        section = self._active_section
        if section not in _CATALOG_SECTIONS:
            return
        ordered = self._ordered_items(section)
        if not ordered:
            return
        start_index = random.randrange(len(ordered))
        rotated = ordered[start_index:] + ordered[:start_index]
        playlist = [
            {"url": item["url"], "title": self._display_title(section, item)}
            for item in rotated
            if item.get("url")
        ]
        if playlist:
            self.project_media_signal.emit(
                section,
                playlist[0]["url"],
                playlist[0]["title"],
                playlist,
                "random",
            )

    def _rotated_playlist(self, section: str, start_item: dict) -> list[dict]:
        ordered = self._ordered_items(section)
        try:
            start_index = next(index for index, item in enumerate(ordered) if item is start_item)
        except StopIteration:
            start_index = 0
        rotated = ordered[start_index:] + ordered[:start_index]
        return [
            {"url": item["url"], "title": self._display_title(section, item)}
            for item in rotated
            if item.get("url")
        ]

    def _ordered_items(self, section: str) -> list[dict]:
        items = self._catalogs[section].items
        if section == "songs":
            return sorted(items, key=lambda item: int(item.get("number") or 0))
        return list(items)

    @staticmethod
    def _display_title(section: str, item: dict) -> str:
        if section == "songs":
            return f"{item.get('number')}. {item.get('title', '')}"
        return str(item.get("title", ""))

    def _download_catalog_row(self, row_index: int) -> None:
        item = self.catalog_model.item_at(row_index)
        if not item:
            return
        url = str(item.get("url", ""))
        manager = self._cache_manager
        if url and not manager.is_cached(url):
            manager.prefetch(url, priority=True)
            index = self.catalog_model.index(row_index, 0)
            self.catalog_model.dataChanged.emit(index, index)
        self._refresh_download_all_state()

    def _download_all_current_catalog(self) -> None:
        section = self._active_section
        if section not in _CATALOG_SECTIONS:
            return
        mode = self._download_mode(section)
        key = (section, mode)
        manager = self._cache_manager
        batch_id = self._download_batch_ids.get(key, "")
        if batch_id and manager.batch_is_active(batch_id):
            manager.cancel_batch(batch_id)
            self._download_batch_ids.pop(key, None)
            self._refresh_download_all_state()
            return
        urls = self._pending_download_urls(section)
        if not urls:
            self._refresh_download_all_state()
            return
        reply = QMessageBox.question(
            self,
            self._download_all_title(section, mode),
            self._download_all_confirmation(section, mode, len(urls)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        batch_id = f"library-{section}-{mode}:{uuid.uuid4().hex}"
        if manager.prefetch_many(urls, batch_id) > 0:
            self._download_batch_ids[key] = batch_id
            self._download_error_batches.discard(batch_id)
        self._refresh_download_all_state()

    def _pending_download_urls(self, section: str | None = None) -> list[str]:
        section = section or self._active_section
        if section not in _CATALOG_SECTIONS:
            return []
        state = self._catalogs[section]
        if section == "songs" and state.audio_mode and not self._songs_support_audio():
            return []
        manager = self._cache_manager
        urls: list[str] = []
        seen: set[str] = set()
        for item in self._ordered_items(section):
            url = str(item.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            if not MediaCacheManager.is_remote(url):
                continue
            if manager.is_cached(url) or manager.is_prefetching(url) or manager.is_queued(url):
                continue
            urls.append(url)
        return urls

    def _download_mode(self, section: str) -> str:
        if section == "songs":
            return "audio" if self._catalogs[section].audio_mode else "video"
        return "clips"

    def _download_all_title(self, section: str, mode: str) -> str:
        if section == "clips":
            return self.tr("Download all music videos")
        if mode == "audio":
            return self.tr("Download all audio songs")
        return self.tr("Download all video songs")

    def _download_all_confirmation(self, section: str, mode: str, count: int) -> str:
        if section == "clips":
            return self.tr(
                "Download %n music video(s) for offline playback?", "", count
            )
        if mode == "audio":
            return self.tr(
                "Download %n audio song(s) for offline playback?", "", count
            )
        return self.tr(
            "Download %n video song(s) for offline playback?", "", count
        )

    def _download_all_complete_text(self, section: str, mode: str) -> str:
        if section == "clips":
            return self.tr("All music videos downloaded")
        if mode == "audio":
            return self.tr("All audio songs downloaded")
        return self.tr("All video songs downloaded")

    def _download_all_failed_text(self, section: str, mode: str) -> str:
        if section == "clips":
            return self.tr("Could not finish downloading all music videos.")
        if mode == "audio":
            return self.tr("Could not finish downloading all audio songs.")
        return self.tr("Could not finish downloading all video songs.")

    def _batch_key(self, batch_id: str) -> tuple[str, str] | None:
        return next(
            (key for key, value in self._download_batch_ids.items() if value == batch_id),
            None,
        )

    def _refresh_download_all_state(self) -> None:
        section = self._active_section
        if section not in _CATALOG_SECTIONS:
            return
        state = self._catalogs[section]
        mode = self._download_mode(section)
        key = (section, mode)
        batch_id = self._download_batch_ids.get(key, "")
        active = bool(batch_id and self._cache_manager.batch_is_active(batch_id))
        pending_count = len(self._pending_download_urls(section))
        if active:
            tooltip = self.tr("Cancel downloads")
        elif pending_count:
            tooltip = self._download_all_title(section, mode)
        else:
            tooltip = self._download_all_complete_text(section, mode)
        self.bridge.update_state(
            show_download_all=True,
            download_all_enabled=not state.loading and (active or pending_count > 0),
            download_all_active=active,
            download_all_tooltip=tooltip,
        )

    @Slot(str)
    def _on_cache_inventory_changed(self, _value: str) -> None:
        self._schedule_download_state_refresh()
        self._downloads_change_revision += 1
        self._downloads_dirty = True
        if self._active_section == "downloads":
            self._downloads_refresh_timer.start()

    @Slot()
    def _schedule_download_state_refresh(self, *_args) -> None:
        if not self._disposed:
            self._download_state_timer.start()

    @Slot(str, int, int, int, int)
    def _on_prefetch_batch_changed(
        self,
        batch_id: str,
        queued: int,
        active: int,
        _done: int,
        _failed: int,
    ) -> None:
        key = self._batch_key(batch_id)
        if key is None:
            return
        if queued + active == 0:
            self._download_batch_ids.pop(key, None)
        self._schedule_download_state_refresh()

    @Slot(str, str)
    def _on_prefetch_batch_error(self, batch_id: str, message: str) -> None:
        key = self._batch_key(batch_id)
        if key is None or batch_id in self._download_error_batches:
            return
        self._download_error_batches.add(batch_id)
        section, mode = key
        detail = str(message or "").strip()
        text = self._download_all_failed_text(section, mode)
        QMessageBox.warning(
            self,
            self.tr("Download failed"),
            f"{text}\n\n{detail}" if detail else text,
        )
        self._download_batch_ids.pop(key, None)
        self._refresh_download_all_state()

    def _ensure_downloads_loaded(self) -> None:
        if not self._downloads_loaded or self._downloads_dirty:
            self._scan_downloads()

    def _refresh_dirty_downloads(self) -> None:
        if self._active_section == "downloads" and self._downloads_dirty:
            self._scan_downloads()

    def _scan_downloads(self, *, force: bool = False) -> None:
        if self._disposed or (not force and not self._downloads_dirty and self._downloads_loaded):
            return
        self._cancel_downloads_scan()
        self._downloads_error_text = ""
        self._downloads_scan_items = []
        self._downloads_scan_is_initial = not self._downloads_loaded
        self._downloads_scan_revision = self._downloads_change_revision
        if self._downloads_scan_is_initial:
            self._downloads_items = []
            self.downloads_source_model.set_items([], self._downloads_selected)
        self.bridge.update_state(
            downloads_loading=True,
            status_text=self.tr("Loading downloaded media…"),
        )
        session = self._cache_scan_session_factory.create(
            self._cache_manager.media_cache_dir,
            parent=self,
        )
        session.batch_ready.connect(
            lambda items, current=session: self._on_downloads_batch(current, items)
        )
        session.results_ready.connect(
            lambda items, current=session: self._on_downloads_scanned(current, items)
        )
        session.finished.connect(lambda current=session: self._clear_downloads_scan(current))
        session.failed.connect(
            lambda message, current=session: self._on_downloads_scan_failed(current, message)
        )
        session.finished.connect(session.deleteLater)
        self._downloads_scan = session
        session.start()

    def _cancel_downloads_scan(self) -> None:
        session = self._downloads_scan
        self._downloads_scan = None
        if session is not None:
            session.cancel()

    def _clear_downloads_scan(self, session: CacheScanSession) -> None:
        if self._downloads_scan is session:
            self._downloads_scan = None
            if self._active_section == "downloads":
                self._publish_downloads_state()

    def _on_downloads_scanned(
        self,
        session: CacheScanSession,
        items: list,
    ) -> None:
        if self._disposed or session is not self._downloads_scan:
            return
        previous_paths = {item.path for item in self._downloads_items}
        self._downloads_items = self._hydrate_downloaded_items(items or [])
        existing_paths = {item.path for item in self._downloads_items}
        removed_paths = previous_paths - existing_paths
        self._downloads_thumbnail_provider.discard(removed_paths)
        self.downloads_source_model.discard_metadata(removed_paths)
        self._downloads_selected.intersection_update(existing_paths)
        self._downloads_titles = {
            path: title for path, title in self._downloads_titles.items() if path in existing_paths
        }
        self._downloads_loaded = True
        self._downloads_dirty = self._downloads_change_revision != self._downloads_scan_revision
        self.downloads_source_model.set_items(
            self._downloads_items,
            self._downloads_selected,
        )
        self._apply_downloads_filter()
        if self._downloads_dirty:
            self._downloads_refresh_timer.start()

    def _on_downloads_batch(
        self,
        session: CacheScanSession,
        items: list,
    ) -> None:
        if self._disposed or session is not self._downloads_scan:
            return
        batch = self._hydrate_downloaded_items(items or [])
        self._downloads_scan_items.extend(batch)
        if not self._downloads_scan_is_initial:
            return
        self._downloads_items.extend(batch)
        self.downloads_source_model.append_items(batch)
        self._apply_downloads_filter()

    def _hydrate_downloaded_items(self, items) -> list[CachedMediaItem]:
        hydrated: list[CachedMediaItem] = []
        for item in items:
            title = self._downloads_titles.get(item.path, "")
            if not title or title == item.display_title:
                hydrated.append(item)
                continue
            hydrated.append(
                CachedMediaItem(
                    path=item.path,
                    filename=item.filename,
                    display_title=title,
                    size=item.size,
                    media_type=item.media_type,
                    original_url=item.original_url,
                )
            )
        return hydrated

    def _on_downloads_scan_failed(
        self,
        session: CacheScanSession,
        message: str,
    ) -> None:
        if self._disposed or session is not self._downloads_scan:
            return
        self._downloads_error_text = str(message or self.tr("Could not read downloaded media."))
        self._downloads_dirty = True
        self._publish_downloads_state()

    @Slot(str)
    def _set_downloads_query(self, query: str) -> None:
        if self._downloads_deletion is not None:
            return
        normalized = str(query or "").strip()
        if normalized == self._downloads_query:
            return
        had_selection = bool(self._downloads_selected)
        self._reset_downloads_selection()
        self._downloads_query = normalized
        if had_selection:
            self._publish_downloads_state()
        self._downloads_search_timer.start()

    @Slot(str)
    def _set_downloads_filter(self, media_filter: str) -> None:
        if (
            self._downloads_deletion is not None
            or media_filter not in {"all", "video", "audio", "image"}
            or media_filter == self._downloads_filter
        ):
            return
        self._reset_downloads_selection()
        self._downloads_filter = media_filter
        self._apply_downloads_filter()

    def _apply_downloads_filter(self) -> None:
        self.downloads_model.set_query(self._downloads_query)
        self.downloads_model.set_media_filter(self._downloads_filter)
        self._downloads_filtered = self.downloads_model.visible_items()
        self._publish_downloads_state()

    def _publish_downloads_state(self) -> None:
        counts = {"video": 0, "audio": 0, "image": 0}
        total_size = 0
        selected_size = 0
        for item in self._downloads_items:
            if item.media_type in counts:
                counts[item.media_type] += 1
            total_size += item.size
            if item.path in self._downloads_selected:
                selected_size += item.size
        selected_count = len(self._downloads_selected)
        visible_paths = {item.path for item in self._downloads_filtered}
        all_visible_selected = bool(visible_paths) and visible_paths.issubset(
            self._downloads_selected
        )
        summary = self.tr(
            "{size} · %n file(s)",
            "",
            len(self._downloads_items),
        ).format(size=format_file_size(total_size))
        selection = self.tr(
            "%n file(s) selected",
            "",
            selected_count,
        )
        status = ""
        if self._downloads_error_text:
            status = self.tr("Could not load downloaded media. Try again.")
        elif not self._downloads_loaded:
            status = self.tr("Loading downloaded media…")
        elif not self._downloads_filtered:
            status = (
                self.tr("No downloaded media matches these filters.")
                if self._downloads_items
                else self.tr("No downloaded media found.")
            )
        self.bridge.update_state(
            active_section="downloads",
            section_title=self.tr("Downloads"),
            section_subtitle=self.tr("Manage media available offline"),
            search_placeholder=self.tr("Search downloaded media…"),
            search_text=self._downloads_query,
            status_text=status,
            downloads_summary=summary,
            downloads_filter=self._downloads_filter,
            downloads_all_label=self.tr(
                "All · %n file(s)", "", len(self._downloads_items)
            ),
            downloads_video_label=self.tr("%n video(s)", "", counts["video"]),
            downloads_audio_label=self.tr(
                "%n audio file(s)", "", counts["audio"]
            ),
            downloads_image_label=self.tr("%n image(s)", "", counts["image"]),
            downloads_selection_text=selection,
            downloads_selection_size=format_file_size(selected_size),
            downloads_select_all_text=(
                self.tr("Deselect visible") if all_visible_selected else self.tr("Select visible")
            ),
            downloads_loading=(self._downloads_scan is not None and not self._downloads_loaded),
            downloads_has_items=bool(self._downloads_filtered),
            downloads_has_selection=selected_count > 0,
            downloads_deleting=self._downloads_deletion is not None,
        )

    def _play_downloaded_row(self, source_path: str) -> None:
        item = self.downloads_source_model.item_for_path(source_path)
        if item is not None:
            self.play_cached_media_signal.emit(
                item.path,
                item.media_type,
                item.original_url,
                item.display_title,
            )

    def _toggle_downloaded_row(self, source_path: str) -> None:
        if self._downloads_deletion is not None:
            return
        item = self.downloads_source_model.item_for_path(source_path)
        if item is None:
            return
        if item.path in self._downloads_selected:
            self._downloads_selected.remove(item.path)
        else:
            self._downloads_selected.add(item.path)
        self.downloads_source_model.set_selected_paths(self._downloads_selected)
        self._publish_downloads_state()

    def _toggle_select_all_downloads(self) -> None:
        if self._downloads_deletion is not None:
            return
        visible_paths = {item.path for item in self._downloads_filtered}
        if visible_paths and visible_paths.issubset(self._downloads_selected):
            self._downloads_selected.difference_update(visible_paths)
        else:
            self._downloads_selected.update(visible_paths)
        self.downloads_source_model.set_selected_paths(self._downloads_selected)
        self._publish_downloads_state()

    def _clear_downloads_selection(self) -> None:
        if self._downloads_deletion is not None or not self._downloads_selected:
            return
        self._reset_downloads_selection()
        self._publish_downloads_state()

    def _reset_downloads_selection(self) -> None:
        self._downloads_selected.clear()
        self.downloads_source_model.set_selected_paths(set())

    def _confirm_delete_downloads(self) -> None:
        if self._downloads_deletion is not None:
            return
        visible_selected = set(self._visible_selected_download_paths())
        if visible_selected != self._downloads_selected:
            self._downloads_selected = visible_selected
            self.downloads_source_model.set_selected_paths(visible_selected)
            self._publish_downloads_state()
        count = len(visible_selected)
        if count == 0:
            return
        reply = QMessageBox.question(
            self,
            self.tr("Confirm deletion"),
            self.tr(
                "Delete %n downloaded file(s) from this computer?",
                "",
                count,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._delete_selected_downloads()

    def _delete_selected_downloads(self) -> None:
        if self._downloads_deletion is not None:
            return
        selected_paths = self._visible_selected_download_paths()
        if not selected_paths:
            self._clear_downloads_selection()
            return
        session = self._cache_deletion_session_factory.create(
            self._cache_manager.media_cache_dir,
            selected_paths,
            parent=self,
        )
        session.completed.connect(self._on_downloads_deleted)
        session.finished.connect(lambda current=session: self._clear_downloads_deletion(current))
        session.finished.connect(session.deleteLater)
        self._downloads_deletion = session
        self._publish_downloads_state()
        session.start()

    def _visible_selected_download_paths(self) -> list[str]:
        visible_paths = {item.path for item in self.downloads_model.visible_items()}
        return sorted(self._downloads_selected.intersection(visible_paths))

    def _on_downloads_deleted(self, deleted: list, failures: list) -> None:
        deleted_paths = set(deleted)
        failed_paths = {str(path) for path, _message in failures}
        for path in deleted_paths:
            self._cache_manager.notify_cache_removed_threadsafe(path)
        self._downloads_items = [
            item for item in self._downloads_items if item.path not in deleted_paths
        ]
        for path in deleted_paths:
            self._downloads_titles.pop(path, None)
        self._downloads_thumbnail_provider.discard(deleted_paths)
        self.downloads_source_model.discard_metadata(deleted_paths)
        self._downloads_selected = failed_paths
        self.downloads_source_model.set_items(
            self._downloads_items,
            self._downloads_selected,
        )
        self._apply_downloads_filter()
        if failures:
            names = "\n".join(
                f"  • {os.path.basename(str(path))}: {message}" for path, message in failures
            )
            QMessageBox.warning(
                self,
                self.tr("Delete error"),
                f"{self.tr('Could not delete the following files:')}\n\n{names}",
            )

    def _clear_downloads_deletion(self, session: CacheDeletionSession) -> None:
        if self._downloads_deletion is session:
            self._downloads_deletion = None
            self._publish_downloads_state()

    @Slot(str)
    def _request_downloaded_info(self, source_path: str) -> None:
        item = self.downloads_source_model.item_for_path(source_path)
        if item is not None:
            self._downloads_info_service.request(
                item.path,
                item.media_type,
                require_thumbnail=True,
            )

    def _on_downloaded_info_ready(self, path: str, pixmap, title: str) -> None:
        if pixmap is not None and not pixmap.isNull():
            source = self._downloads_thumbnail_provider.store(path, pixmap)
            self.downloads_source_model.update_thumbnail_source(path, source)
        cleaned_title = str(title or "").strip()
        if cleaned_title:
            self._downloads_titles[path] = cleaned_title
        updated = self.downloads_source_model.update_title(path, title)
        if updated is None:
            return
        for index, item in enumerate(self._downloads_items):
            if item.path == path:
                self._downloads_items[index] = updated
                break

    def begin_qml_pointer_cursor(self) -> None:
        self._qml_pointer_depth += 1
        begin_qml_pointer_cursor(self.qml_widget)

    def end_qml_pointer_cursor(self) -> None:
        self._qml_pointer_depth = max(0, self._qml_pointer_depth - 1)
        if self._qml_pointer_depth == 0:
            end_qml_pointer_cursor(self.qml_widget)

    def _reset_qml_pointer_cursor(self) -> None:
        self._qml_pointer_depth = 0
        end_qml_pointer_cursor(self.qml_widget)

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is getattr(self, "qml_widget", None) and event.type() == QEvent.Type.Leave:
            self._reset_qml_pointer_cursor()
        return super().eventFilter(watched, event)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:  # noqa: N802
        self._sync_static_text()

    def apply_theme(self) -> None:
        apply_qml_theme(self.qml_widget, clear_color=PALETTE.bg0)
        self.catalog_model.layoutChanged.emit()
        self.downloads_source_model.layoutChanged.emit()

    def _stop_clip_threads(self, *, wait: bool) -> None:
        for thread in list(self._clip_threads):
            if wait:
                stop_owned_qthread(thread, wait_ms=3_000, label="Clip catalog")
            else:
                thread.requestInterruption()
        if wait:
            self._clip_threads.clear()

    def cleanup(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._clip_generation += 1
        self._stop_clip_threads(wait=True)
        self._cancel_downloads_scan()
        if self._downloads_deletion is not None:
            self._downloads_deletion.cancel()
            self._downloads_deletion = None
        self._downloads_info_service.clear()
        self.catalog_model.cleanup()
        try:
            self._songs_store.songs_ready.disconnect(self._on_songs_ready)
            self._songs_store.songs_failed.disconnect(self._on_songs_failed)
            self._songs_store.loading_changed.disconnect(self._on_songs_loading_changed)
        except Exception:  # noqa: BLE001 - Qt cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect Library song signals")
        try:
            media_language = getattr(self.lang, "jw_lang_service", None)
            if media_language is not None:
                media_language.media_language_changed.disconnect(self._on_media_language_changed)
        except Exception:  # noqa: BLE001 - Qt cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect Library language signal")
        try:
            manager = self._cache_manager
            manager.cache_changed.disconnect(self._on_cache_inventory_changed)
            manager.cache_removed.disconnect(self._on_cache_inventory_changed)
            manager.prefetch_queued.disconnect(self._schedule_download_state_refresh)
            manager.prefetch_dequeued.disconnect(self._schedule_download_state_refresh)
            manager.prefetch_batch_changed.disconnect(self._on_prefetch_batch_changed)
            manager.prefetch_batch_error.disconnect(self._on_prefetch_batch_error)
        except Exception:  # noqa: BLE001 - Qt cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect Library batch signals")
        if hasattr(self, "qml_widget"):
            self.qml_load_handle.cancel()
            try:
                self.qml_widget.removeEventFilter(self)
            except Exception:  # noqa: BLE001 - Qt cleanup boundary
                log_ignored_exception(__name__, "Could not remove Library event filter")
            self.qml_widget.setSource(QUrl())

    def deleteLater(self) -> None:  # noqa: N802
        self.cleanup()
        super().deleteLater()
