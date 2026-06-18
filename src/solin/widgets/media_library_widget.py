from __future__ import annotations

import os
import random
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QUrl, QTimer, Signal, Slot
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from ..core.jw.language_context import jw_media_language_context
from ..core.jw.songs import JWSongsStore
from ..core.foundation.exception_logging import log_ignored_exception
from ..core.foundation.qt_threads import stop_owned_qthread
from ..core.media.cache import MediaCacheManager
from ..core.i18n.manager import LanguageManager
from ..ui.helpers import begin_qml_pointer_cursor, end_qml_pointer_cursor
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.media_library import (
    MediaLibraryBridge,
    MediaLibraryIconProvider,
    MediaLibraryModel,
    java_to_py_fmt,
)

if TYPE_CHECKING:
    from ..core.jw.clip_fetch import ClipFetchThread, ClipFetchThreadFactory

class MediaLibraryWidget(QWidget):
    project_video_signal = Signal(str, str, object, str)

    def __init__(
        self,
        kind: str,
        lang_manager: LanguageManager,
        cache_manager: MediaCacheManager,
        media_ctrl=None,
        *,
        songs_store: JWSongsStore | None = None,
        clip_fetch_thread_factory: ClipFetchThreadFactory | None = None,
        jw_cache_dir: str | os.PathLike[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.kind = kind
        self.lang = lang_manager
        self._cache_manager = cache_manager
        self._jw_cache_dir = jw_cache_dir
        self.items: list[dict] = []
        self.filtered_items: list[dict] = []
        if self.kind == "songs":
            self.songs = self.items
            self.filtered_songs = self.filtered_items
        else:
            self.clips = self.items
            self.filtered_clips = self.filtered_items
        self._query = ""
        self._audio_mode = False
        self._song_request_key = ""
        self._songs_store = songs_store if self.kind == "songs" else None
        if self.kind == "songs" and self._songs_store is None:
            raise ValueError("Songs media library requires a JWSongsStore")
        self._clip_fetch_thread_factory = (
            clip_fetch_thread_factory if self.kind == "clips" else None
        )
        if self.kind == "clips" and self._clip_fetch_thread_factory is None:
            raise ValueError("Clips media library requires a ClipFetchThreadFactory")
        self._qml_pointer_depth = 0
        self._disposed = False
        self._clip_generation = 0
        self._clip_threads: list[ClipFetchThread] = []
        self._download_all_batch_ids: dict[str, str] = {"video": "", "audio": ""}
        self._download_all_error_batches: set[str] = set()
        self._download_all_refresh_timer = QTimer(self)
        self._download_all_refresh_timer.setSingleShot(True)
        self._download_all_refresh_timer.setInterval(120)
        self._download_all_refresh_timer.timeout.connect(self._refresh_download_all_state)

        self.model = MediaLibraryModel(cache_manager, self)
        self.bridge = MediaLibraryBridge(self)
        self._connect_signals()
        self._build_qml()
        self._sync_static_text()
        self._update_mode_availability()
        self._load_items()

    def _build_qml(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.qml_widget = QQuickWidget()
        self.qml_widget.setParent(self)
        self.qml_widget.installEventFilter(self)
        configure_qml_host(
            self.qml_widget,
            type_name="MediaLibraryView",
            clear_color="#0d1117",
            image_providers={"mediaicons": MediaLibraryIconProvider()},
            context_properties={
                "libraryModel": self.model,
                "controller": self.bridge,
            },
            mouse_tracking=True,
        )
        root.addWidget(self.qml_widget, stretch=1)

    def _connect_signals(self) -> None:
        if self._songs_store is not None:
            self._songs_store.songs_ready.connect(self._on_song_store_ready)
            self._songs_store.songs_failed.connect(self._on_song_store_failed)
            self._songs_store.loading_changed.connect(self._on_song_store_loading_changed)
        self.bridge.refreshRequested.connect(lambda: self._load_items(force=True))
        self.bridge.playAllRequested.connect(self._play_all)
        self.bridge.shuffleRequested.connect(self._play_shuffle)
        self.bridge.downloadAllRequested.connect(self._download_all_current_mode)
        self.bridge.searchChanged.connect(self._filter_items)
        self.bridge.modeChanged.connect(self._set_mode)
        self.bridge.itemPlayRequested.connect(self._on_item_play_by_row)
        self.bridge.itemDownloadRequested.connect(self._download_by_row)
        self.bridge.pointerEntered.connect(self.begin_qml_pointer_cursor)
        self.bridge.pointerExited.connect(self.end_qml_pointer_cursor)

        svc = getattr(self.lang, "jw_lang_service", None)
        if svc is not None:
            svc.media_language_changed.connect(self._on_media_language_changed)

        mgr = self._cache_manager
        mgr.cache_changed.connect(self._on_cache_state_changed)
        mgr.cache_removed.connect(self._on_cache_state_changed)
        mgr.prefetch_queued.connect(self._on_cache_state_changed)
        mgr.prefetch_dequeued.connect(self._on_cache_state_changed)
        mgr.prefetch_batch_changed.connect(self._on_prefetch_batch_changed)
        mgr.prefetch_batch_error.connect(self._on_prefetch_batch_error)

    def _sync_static_text(self) -> None:
        self.bridge.update_state(
            title=self.tr("Songs") if self.kind == "songs" else self.tr("Original Songs"),
            search_placeholder=self.tr("Search song...") if self.kind == "songs" else self.tr("Search clip..."),
            refresh_tooltip=self.tr("Refresh"),
            play_all_tooltip=self.tr("Play all (in order)"),
            shuffle_tooltip=self.tr("Play in random order"),
            download_all_tooltip=self._download_all_title(),
            video_tooltip=self.tr("Video songs"),
            audio_tooltip=self.tr("Audio songs"),
            audio_mode=self._audio_mode,
        )
        self._refresh_download_all_state()

    def _update_mode_availability(self) -> None:
        context = jw_media_language_context(self.lang)
        supports_audio = self.kind == "songs" and not context.is_sign_language
        if not supports_audio and self._audio_mode:
            self._audio_mode = False
        self.bridge.update_state(supports_audio=supports_audio, audio_mode=self._audio_mode)
        self._refresh_download_all_state()

    def _set_loading(self, text: str) -> None:
        self.bridge.update_state(
            loading=True,
            status_text=text,
            has_items=False,
            refresh_enabled=False,
            cache_text="",
        )
        self._refresh_download_all_state()

    def _load_items(self, force: bool = False) -> None:
        loading_text = self.tr("Loading songs...") if self.kind == "songs" else self.tr("Loading clips...")
        self._set_loading(loading_text)
        self.model.set_items([], self._audio_mode)

        context = jw_media_language_context(self.lang)
        audio_mode = self._audio_mode
        kind = self.kind

        if kind == "songs" and self._songs_store is not None:
            request = self._songs_store.request_for(
                api_code=context.api_code,
                fallback_code=context.fallback_code,
                is_sign_language=context.is_sign_language,
                audio_mode=audio_mode,
            )
            self._song_request_key = self._songs_store.ensure_loaded(request, force=force)
            return

        self._clip_generation += 1
        generation = self._clip_generation
        for thread in self._clip_threads:
            thread.requestInterruption()
        factory = self._clip_fetch_thread_factory
        if factory is None:
            raise RuntimeError("Clip fetch worker factory is not configured")
        thread = factory.create(
            generation,
            api_code=context.api_code,
            fallback_code=context.fallback_code,
            is_sign_language=context.is_sign_language,
            force=force,
            cache_dir=self._jw_cache_dir,
            parent=self,
        )
        thread.items_ready.connect(self._on_clip_items_loaded)
        thread.failed.connect(self._on_clip_items_error)
        thread.finished.connect(
            lambda current=thread: self._clip_threads.remove(current)
            if current in self._clip_threads
            else None
        )
        self._clip_threads.append(thread)
        thread.start()

    @Slot(int, list, float, bool)
    def _on_clip_items_loaded(
        self,
        generation: int,
        items: list,
        fetched_at: float,
        from_cache: bool,
    ) -> None:
        if not self._disposed and generation == self._clip_generation:
            self._on_items_loaded(items, "", fetched_at, from_cache)

    @Slot(int, str)
    def _on_clip_items_error(self, generation: int, error_msg: str) -> None:
        if not self._disposed and generation == self._clip_generation:
            self._on_items_error(error_msg)

    @Slot(str, list, str, float, bool)
    def _on_song_store_ready(
        self,
        key: str,
        items: list,
        pub_name: str,
        fetched_at: float,
        from_cache: bool,
    ) -> None:
        if key == self._song_request_key:
            self._on_items_loaded(items, pub_name, fetched_at, from_cache)

    @Slot(str, str)
    def _on_song_store_failed(self, key: str, error_msg: str) -> None:
        if key == self._song_request_key:
            self._on_items_error(error_msg)

    @Slot(str, bool)
    def _on_song_store_loading_changed(self, key: str, loading: bool) -> None:
        if key == self._song_request_key and loading:
            self._set_loading(self.tr("Loading songs..."))

    @Slot(str)
    def _on_media_language_changed(self, _code: str) -> None:
        self._update_mode_availability()
        self._load_items(force=True)

    @Slot(list, str, float, bool)
    def _on_items_loaded(self, items: list, pub_name: str, fetched_at: float, from_cache: bool) -> None:
        self.items = list(items or [])
        if self.kind == "songs":
            self.songs = self.items
        else:
            self.clips = self.items
        cache_text = ""
        if from_cache:
            cache_text = self.tr("Updated on {date}").replace("{date}", self._fmt_date(fetched_at))
        self.bridge.update_state(
            subtitle=pub_name,
            loading=False,
            status_text="",
            has_items=bool(self.items),
            count_text=self._count_text(len(self.items)),
            cache_text=cache_text,
            refresh_enabled=True,
        )
        self._filter_items(self._query)
        self._refresh_download_all_state()

    @Slot(str)
    def _on_items_error(self, error_msg: str) -> None:
        text = self.tr("Error loading songs. Check your connection.") if self.kind == "songs" else self.tr("Error loading clips. Check your connection.")
        self.bridge.update_state(
            loading=True,
            status_text=f"{text}\n{error_msg}",
            has_items=False,
            refresh_enabled=True,
        )
        self._refresh_download_all_state()

    def _filter_items(self, query: str) -> None:
        self._query = (query or "").lower().strip()
        if not self._query:
            filtered = self.items
        elif self.kind == "songs":
            filtered = [
                item for item in self.items
                if self._query in item.get("title", "").lower()
                or self._query == str(item.get("number", ""))
            ]
        else:
            filtered = [
                item for item in self.items
                if self._query in item.get("title", "").lower()
            ]
        self.filtered_items = list(filtered)
        if self.kind == "songs":
            self.filtered_songs = self.filtered_items
        else:
            self.filtered_clips = self.filtered_items
        self.model.set_items(self.filtered_items, self._audio_mode)
        self.bridge.update_state(
            has_items=bool(self.items),
            count_text=self._count_text(len(self.filtered_items)),
        )
        self._refresh_download_all_state()

    def _set_mode(self, mode: str) -> None:
        wants_audio = mode == "audio"
        if wants_audio == self._audio_mode:
            return
        if wants_audio and not self.bridge.supports_audio:
            return
        self._audio_mode = wants_audio
        self.bridge.update_state(audio_mode=self._audio_mode)
        self._load_items()
        self._refresh_download_all_state()

    def _on_item_play_by_row(self, row_index: int) -> None:
        item = self.model.item_at(row_index)
        if item:
            self._on_item_play(item)

    def _download_by_row(self, row_index: int) -> None:
        item = self.model.item_at(row_index)
        if not item:
            return
        url = item.get("url", "")
        mgr = self._cache_manager
        if url and not mgr.is_cached(url):
            mgr.prefetch(url, priority=True)
            idx = self.model.index(row_index, 0)
            self.model.dataChanged.emit(idx, idx)
        self._refresh_download_all_state()

    def _download_all_current_mode(self) -> None:
        mgr = self._cache_manager
        mode = self._download_all_mode()
        batch_id = self._download_all_batch_ids.get(mode, "")
        if batch_id and mgr.batch_is_active(batch_id):
            mgr.cancel_batch(batch_id)
            self._download_all_batch_ids[mode] = ""
            self._refresh_download_all_state()
            return

        urls = self._pending_download_all_urls()
        if not urls:
            self._refresh_download_all_state()
            return

        title = self._download_all_title(mode)
        body = self._download_all_confirm_text(mode, len(urls))
        reply = QMessageBox.question(
            self,
            title,
            body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        batch_id = f"songs-{mode}:{uuid.uuid4().hex}"
        added = mgr.prefetch_many(urls, batch_id)
        if added > 0:
            self._download_all_batch_ids[mode] = batch_id
            self._download_all_error_batches.discard(batch_id)
        self._refresh_download_all_state()

    def _pending_download_all_urls(self) -> list[str]:
        if self.kind != "songs":
            return []
        if self._audio_mode and not self.bridge.supports_audio:
            return []
        mgr = self._cache_manager
        urls: list[str] = []
        seen: set[str] = set()
        for item in self._ordered_items(self.items):
            url = str(item.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            if not MediaCacheManager.is_remote(url):
                continue
            if mgr.is_cached(url) or mgr.is_prefetching(url) or mgr.is_queued(url):
                continue
            urls.append(url)
        return urls

    def _download_all_mode(self) -> str:
        return "audio" if self._audio_mode else "video"

    def _download_all_title(self, mode: str | None = None) -> str:
        mode = mode or self._download_all_mode()
        if mode == "audio":
            return self.tr("Download all audio songs")
        return self.tr("Download all video songs")

    def _download_all_confirm_text(self, mode: str, count: int) -> str:
        if mode == "audio":
            return self.tr(
                "Download {count} audio songs for offline playback?"
            ).replace("{count}", str(count))
        return self.tr(
            "Download {count} video songs for offline playback?"
        ).replace("{count}", str(count))

    def _download_all_complete_text(self, mode: str) -> str:
        if mode == "audio":
            return self.tr("All audio songs downloaded")
        return self.tr("All video songs downloaded")

    def _download_all_failed_text(self, mode: str) -> str:
        if mode == "audio":
            return self.tr("Could not finish downloading all audio songs.")
        return self.tr("Could not finish downloading all video songs.")

    def _download_all_mode_for_batch(self, batch_id: str) -> str:
        for mode, active_batch_id in self._download_all_batch_ids.items():
            if active_batch_id == batch_id:
                return mode
        return ""

    def _refresh_download_all_state(self) -> None:
        if not hasattr(self, "bridge"):
            return
        mgr = self._cache_manager
        mode = self._download_all_mode()
        batch_id = self._download_all_batch_ids.get(mode, "")
        active = bool(
            batch_id
            and mgr.batch_is_active(batch_id)
        )
        pending_count = len(self._pending_download_all_urls())
        show = self.kind == "songs"
        enabled = show and not self.bridge.loading and (active or pending_count > 0)
        if active:
            tooltip = self.tr("Cancel downloads")
        elif pending_count > 0:
            tooltip = self._download_all_title(mode)
        else:
            tooltip = self._download_all_complete_text(mode)
        self.bridge.update_state(
            show_download_all=show,
            download_all_enabled=enabled,
            download_all_active=active,
            download_all_tooltip=tooltip,
        )

    @Slot(str)
    def _on_cache_state_changed(self, _value: str) -> None:
        self._schedule_download_all_state_refresh()

    @Slot(str, int, int, int, int)
    def _on_prefetch_batch_changed(
        self,
        batch_id: str,
        queued: int,
        active: int,
        _done: int,
        _failed: int,
    ) -> None:
        mode = self._download_all_mode_for_batch(batch_id)
        if not mode:
            return
        if queued + active == 0:
            self._download_all_batch_ids[mode] = ""
        self._schedule_download_all_state_refresh()

    @Slot(str, str)
    def _on_prefetch_batch_error(self, batch_id: str, message: str) -> None:
        mode = self._download_all_mode_for_batch(batch_id)
        if not mode:
            return
        if batch_id in self._download_all_error_batches:
            return
        self._download_all_error_batches.add(batch_id)
        detail = str(message or "").strip()
        text = self._download_all_failed_text(mode)
        if detail:
            text = f"{text}\n\n{detail}"
        QMessageBox.warning(
            self,
            self.tr("Download failed"),
            text,
        )
        self._download_all_batch_ids[mode] = ""
        self._refresh_download_all_state()

    def _schedule_download_all_state_refresh(self) -> None:
        if self._disposed:
            return
        if not self._download_all_refresh_timer.isActive():
            self._download_all_refresh_timer.start()

    def _on_item_play(self, item: dict) -> None:
        url = item.get("url", "")
        if not url:
            return
        title = self._display_title(item)
        playlist = self._build_rotated_playlist(item)
        self.project_video_signal.emit(url, title, playlist, "")

    def _play_all(self) -> None:
        ordered = self._ordered_items(self.items)
        if not ordered:
            return
        playlist = [{"url": item["url"], "title": self._display_title(item)} for item in ordered]
        first = ordered[0]
        self.project_video_signal.emit(first["url"], self._display_title(first), playlist, "next")

    def _play_shuffle(self) -> None:
        ordered = self._ordered_items(self.items)
        if not ordered:
            return
        first = random.choice(ordered)
        start_idx = ordered.index(first)
        rotated = ordered[start_idx:] + ordered[:start_idx]
        playlist = [{"url": item["url"], "title": self._display_title(item)} for item in rotated]
        self.project_video_signal.emit(first["url"], self._display_title(first), playlist, "random")

    def _build_rotated_playlist(self, start_item: dict) -> list[dict]:
        ordered = self._ordered_items(self.items)
        if not ordered:
            return []
        try:
            if self.kind == "songs":
                start_idx = next(
                    i for i, item in enumerate(ordered)
                    if item.get("number") == start_item.get("number")
                )
            else:
                start_idx = next(
                    i for i, item in enumerate(ordered)
                    if item.get("title") == start_item.get("title")
                )
        except StopIteration:
            start_idx = 0
        rotated = ordered[start_idx:] + ordered[:start_idx]
        return [{"url": item["url"], "title": self._display_title(item)} for item in rotated]

    def _ordered_items(self, items: list[dict]) -> list[dict]:
        if self.kind == "songs":
            return sorted(items, key=lambda item: item.get("number", 0))
        return list(items)

    def _display_title(self, item: dict) -> str:
        if self.kind == "songs":
            return f"{item.get('number')}. {item.get('title', '')}"
        return item.get("title", "")

    def _count_text(self, count: int) -> str:
        if self.kind == "songs":
            return self.tr("{count} songs available").replace("{count}", str(count))
        return self.tr("{count} clips available").replace("{count}", str(count))

    def _fmt_date(self, fetched_at: float) -> str:
        py_fmt = java_to_py_fmt(self.lang.date_format)
        dt = datetime.fromtimestamp(fetched_at)
        escaped_fmt = py_fmt.encode("unicode-escape").decode("utf-8")
        return dt.strftime(escaped_fmt).encode("utf-8").decode("unicode-escape")

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

    def eventFilter(self, obj, event):
        if obj is getattr(self, "qml_widget", None) and event.type() == QEvent.Type.Leave:
            self._reset_qml_pointer_cursor()
        return super().eventFilter(obj, event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self._sync_static_text()
        self._load_items()

    def cleanup(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._clip_generation += 1
        for thread in list(self._clip_threads):
            stop_owned_qthread(
                thread,
                wait_ms=3_000,
                label="Clip catalog",
            )
        self._clip_threads.clear()
        try:
            self.model.cleanup()
        except Exception:  # noqa: BLE001 - widget cleanup boundary
            log_ignored_exception(__name__, "Could not cleanup media library model")
        if self._songs_store is not None:
            try:
                self._songs_store.songs_ready.disconnect(self._on_song_store_ready)
                self._songs_store.songs_failed.disconnect(self._on_song_store_failed)
                self._songs_store.loading_changed.disconnect(
                    self._on_song_store_loading_changed
                )
            except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
                log_ignored_exception(__name__, "Could not disconnect JW songs store signals")
        try:
            svc = getattr(self.lang, "jw_lang_service", None)
            if svc is not None:
                svc.media_language_changed.disconnect(self._on_media_language_changed)
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect media language signal")
        try:
            mgr = self._cache_manager
            mgr.cache_changed.disconnect(self._on_cache_state_changed)
            mgr.cache_removed.disconnect(self._on_cache_state_changed)
            mgr.prefetch_queued.disconnect(self._on_cache_state_changed)
            mgr.prefetch_dequeued.disconnect(self._on_cache_state_changed)
            mgr.prefetch_batch_changed.disconnect(self._on_prefetch_batch_changed)
            mgr.prefetch_batch_error.disconnect(self._on_prefetch_batch_error)
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect media cache batch signals")
        if hasattr(self, "qml_widget"):
            try:
                self.qml_widget.removeEventFilter(self)
            except Exception:  # noqa: BLE001 - Qt event-filter cleanup boundary
                log_ignored_exception(__name__, "Could not remove media library event filter")
            self.qml_widget.setSource(QUrl())

    def deleteLater(self):
        self.cleanup()
        super().deleteLater()
