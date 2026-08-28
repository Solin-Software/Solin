"""Playback and destination routing for media received over Wi-Fi."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from solin.core.foundation.constants import JWPUB_EXTS, PDF_EXTS, PLAYLIST_EXTS
from solin.core.media.destinations import (
    MediaDestinationAsset,
    MediaDestinationOutcome,
    MediaDestinationRequest,
)
from solin.core.media.formats import AUDIO_EXTS, IMAGE_EXTS, media_type_from_path
from solin.core.playlists.items import create_playlist_item
from solin.core.playlists.items import PlaylistMediaItem


_TR_CONTEXT = "WifiMedia"
_RECEIVED_ITEMS_SOURCE = QT_TRANSLATE_NOOP(
    "WifiMedia",
    "%n received item(s)",
)
_MEDIA_SOURCE = QT_TRANSLATE_NOOP("WifiMedia", "Media")


def _tr_received_items(count: int) -> str:
    return QCoreApplication.translate(
        _TR_CONTEXT,
        _RECEIVED_ITEMS_SOURCE,
        "",
        max(0, int(count)),
    )


def _tr_media() -> str:
    return QCoreApplication.translate(_TR_CONTEXT, _MEDIA_SOURCE)


@dataclass(frozen=True, slots=True)
class WifiMediaContext:
    destination_controller: Any
    wifi_receive_widget: Callable[[], Any | None]


@dataclass(frozen=True, slots=True)
class WifiMediaHandlers:
    play_cached_media: Callable[..., None]
    project_video: Callable[..., None]


class WifiMediaController:
    """Keep Wi-Fi source ownership separate from destination persistence."""

    _JW_METADATA_KEYS = (
        "key_symbol",
        "track",
        "issue_tag",
        "doc_id",
        "meps_language",
    )

    def __init__(
        self,
        context: WifiMediaContext,
        handlers: WifiMediaHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers

    def on_wifi_media_received(self, _path: str, _orig_name: str) -> None:
        return

    def on_wifi_request_play(self, path: str, title: str) -> None:
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXTS:
            self._handlers.play_cached_media(path, "image", "", title)
            return

        media_type = "audio" if ext in AUDIO_EXTS else "video"
        playlist = [{"url": path, "title": title, "type": media_type}]
        self._handlers.project_video(
            path,
            title,
            playlist,
            None,
            from_saved_playlist=False,
        )

    def on_wifi_request_add_single(
        self,
        path: str,
        title: str,
        orig_name: str,
    ) -> None:
        entry = self._received_entry(path)
        asset = self._asset_from_entry(
            entry,
            path=path,
            title=title,
            orig_name=orig_name,
        )
        request = MediaDestinationRequest(title=title, assets=(asset,))
        self._context.destination_controller.route(
            request,
            completed=lambda outcome: self._complete_single(path, outcome),
        )

    def on_wifi_add_all(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        assets = tuple(self._asset_from_entry(entry) for entry in entries)
        request = MediaDestinationRequest(
            title=self._batch_title(entries),
            assets=assets,
        )
        self._context.destination_controller.route(
            request,
            completed=lambda outcome: self._complete_all(entries, outcome),
        )

    def item_from_wifi_entry(
        self,
        entry: dict[str, Any],
        *,
        path: str | None = None,
        title: str | None = None,
        orig_name: str | None = None,
    ) -> PlaylistMediaItem:
        item_path = path or str(entry.get("path") or "")
        item_title = title or str(entry.get("title") or "")
        media_type = str(entry.get("type") or "") or media_type_from_path(
            item_path,
            default="video",
        )
        kwargs = self._metadata_kwargs(entry, orig_name)
        return create_playlist_item(
            title=item_title,
            url=item_path,
            type=media_type,
            **kwargs,
        )

    def _asset_from_entry(
        self,
        entry: dict[str, Any],
        *,
        path: str | None = None,
        title: str | None = None,
        orig_name: str | None = None,
    ) -> MediaDestinationAsset:
        item_path = path or str(entry.get("path") or "")
        item_title = title or str(entry.get("title") or "")
        ext = os.path.splitext(item_path)[1].lower()
        if ext in PDF_EXTS:
            import_kind = "pdf"
        elif ext in JWPUB_EXTS:
            import_kind = "jwpub"
        elif ext in PLAYLIST_EXTS:
            import_kind = "jwlplaylist"
        else:
            return MediaDestinationAsset(
                title=item_title,
                source_id=item_path,
                item=self.item_from_wifi_entry(
                    entry,
                    path=item_path,
                    title=item_title,
                    orig_name=orig_name,
                ),
            )
        return MediaDestinationAsset(
            title=item_title,
            source_id=item_path,
            import_path=item_path,
            import_kind=import_kind,
        )

    def _metadata_kwargs(
        self,
        entry: dict[str, Any],
        orig_name: str | None = None,
    ) -> dict[str, Any]:
        kwargs = {
            key: entry[key]
            for key in self._JW_METADATA_KEYS
            if key in entry and entry[key] is not None
        }
        original_filename = (
            orig_name if orig_name is not None else str(entry.get("orig_name") or "")
        )
        if original_filename:
            kwargs["original_filename"] = original_filename
        return kwargs

    def _received_entry(self, path: str) -> dict[str, Any]:
        wifi_receive = self._context.wifi_receive_widget()
        if wifi_receive is None:
            return {}
        return wifi_receive.received_entry(path)

    def _complete_single(
        self,
        path: str,
        outcome: MediaDestinationOutcome,
    ) -> None:
        wifi_receive = self._context.wifi_receive_widget()
        if wifi_receive is None:
            return
        if path not in outcome.handled_sources:
            return
        if path in outcome.referenced_urls:
            wifi_receive.preserve_temp_file(path)
        wifi_receive.remove_received_file(path)

    def _complete_all(
        self,
        entries: list[dict[str, Any]],
        outcome: MediaDestinationOutcome,
    ) -> None:
        wifi_receive = self._context.wifi_receive_widget()
        if wifi_receive is None:
            return
        referenced = set(outcome.referenced_urls)
        handled = set(outcome.handled_sources)
        for entry in entries:
            path = str(entry.get("path") or "")
            if not path or path not in handled:
                continue
            if path and path in referenced:
                wifi_receive.preserve_temp_file(path)
            wifi_receive.remove_received_file(path)

    def _batch_title(self, entries: list[dict[str, Any]]) -> str:
        if len(entries) == 1:
            return str(entries[0].get("title") or _tr_media())
        return _tr_received_items(len(entries))


__all__ = ["WifiMediaContext", "WifiMediaController", "WifiMediaHandlers"]
