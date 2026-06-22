from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QMessageBox

from ..core.foundation.constants import (
    ALLOW_ZOOM_PAN_ON_LIVE_TAB,
)
from ..core.media.formats import AUDIO_EXTS

#: Projection states that carry a zoom/pan transform (so it is persisted in the
#: projection session and replayed onto surfaces created later).  Both render
#: through a zoom/pan-capable widget: image -> VideoDisplayWidget, sermon theme
#: -> the talk theme slide.
_TRANSFORMABLE_STATES = frozenset({"image", "sermon_theme"})

#: Identity transform (no zoom, no pan).
_IDENTITY_TRANSFORM = (1.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class MediaProjectionContext:
    """Dependencies for media, image, playlist, cache, and live-tab projection."""

    projection_session: Any
    projection_bar: Any
    media_controller: Any
    ndi_service: Any
    camera_service: Any
    projection_windows: Callable[[], list[Any]]
    playlist_edit_is_temp: Callable[[], bool]
    meeting_service: Callable[[], Any]
    dialog_parent: Any
    translate: Callable[[str], str]
    sjjm_announce_mode: Callable[[], bool]
    start_videos_paused: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class MediaProjectionHandlers:
    """Shell and integration actions used by media projection workflows."""

    stop_browser_tab_projection: Callable[[], None]
    update_projection_status: Callable[..., None]
    prepare_video_session: Callable[[], None]


class MediaProjectionController:
    """Handles media, image, playlist, cache, and live-tab projection flows."""

    def __init__(
        self,
        context: MediaProjectionContext,
        handlers: MediaProjectionHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers
        self._session = context.projection_session
        self._next_is_sjjm = False

    def on_song_project(
        self,
        url: str,
        title: str,
        playlist: list,
        order: str,
    ) -> None:
        self.project_video(
            url,
            title,
            playlist or None,
            order if order else None,
            from_saved_playlist=True,
        )

    def on_sjjm_project(
        self,
        url: str,
        title: str,
        playlist: list,
        order: str,
    ) -> None:
        self._next_is_sjjm = True
        self.project_video(
            url,
            title,
            playlist or None,
            order if order else None,
            from_saved_playlist=True,
        )

    def on_meeting_media_project(self, item) -> None:
        if not item:
            return

        if isinstance(item, dict):
            url = item.get("url") or item.get("file_path") or ""
            title = item.get("title") or item.get("label") or "Media"
            media_type = item.get("type") or item.get("media_type") or "video"
            if media_type == "image" and url and os.path.exists(url):
                self._project_image_path(title, url, playlist=[])
            elif url:
                playlist_item = {"url": url, "title": title, "type": media_type}
                self.project_video(url, title, [playlist_item], None)
            return

        mime = item.mime_type or ""
        title = re.sub(r"<[^>]+>", "", item.label or item.caption or "Media").strip()

        if "image" in mime and item.file_path and os.path.exists(item.file_path):
            self._project_image_path(title, item.file_path, playlist=[])
            return

        if "video" not in mime and "image" in mime:
            return

        if item.file_path and (
            item.file_path.startswith(("http://", "https://"))
            or os.path.exists(item.file_path)
        ):
            if item.key_symbol and item.key_symbol.lower() in ("sjj", "sjjm") and item.track:
                self._next_is_sjjm = True
            media_type = "audio" if "audio" in mime else "video"
            playlist_item = {
                "url": item.file_path,
                "title": title,
                "type": media_type,
            }
            self.project_video(item.file_path, title, [playlist_item], None)
            return

        if item.key_symbol and item.key_symbol.lower() in ("sjj", "sjjm") and item.track:
            self._next_is_sjjm = True
            self._resolve_and_project_meeting_item(item, title)
        elif item.key_symbol or item.meps_doc_id:
            self._resolve_and_project_meeting_item(item, title)

    def on_playlist_project(
        self,
        url: str,
        title: str,
        playlist: list,
        order: str,
    ) -> None:
        mtype = playlist[0].get("type", "video") if playlist else "video"
        if mtype == "image":
            self._project_image_path(
                title,
                url,
                playlist=playlist,
                playback_order=order if order else None,
                from_saved_playlist=True,
                keep_expanded=self._context.projection_bar.is_expanded(),
            )
            return

        self.project_video(
            url,
            title,
            playlist or None,
            order if order else None,
            from_saved_playlist=True,
        )

    def edit_view_is_temp(self) -> bool:
        return self._context.playlist_edit_is_temp()

    def project_video(
        self,
        url: str,
        title: str,
        playlist: list | None = None,
        playback_order: str | None = None,
        from_saved_playlist: bool = False,
    ) -> None:
        items = playlist if playlist else [{"url": url, "title": title}]
        self._context.projection_bar.set_playlist(
            items,
            playback_order,
            from_saved_playlist=from_saved_playlist,
        )
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        self.project_video_core(url, title, is_audio=ext in AUDIO_EXTS)

    def project_next_auto(self, url: str, title: str, media_type: str) -> None:
        context = self._context
        if url == "__replay__":
            context.media_controller.stop()
            context.ndi_service.stop()
            context.camera_service.stop()
            current = context.projection_bar.current_playlist_item()
            if current is None:
                return
            current_ext = os.path.splitext(current.get("url", ""))[1].lower()
            if current_ext not in AUDIO_EXTS:
                for projection_window in context.projection_windows():
                    projection_window.begin_video()
            context.media_controller.play_url(current["url"])
            return

        if media_type == "image":
            self._project_image_path(
                title,
                url,
                keep_expanded=context.projection_bar.is_expanded(),
            )
            return

        ext = os.path.splitext(url)[1].lower()
        self.project_video_core(
            url,
            title,
            keep_expanded=context.projection_bar.is_expanded(),
            is_audio=ext in AUDIO_EXTS,
        )

    def project_video_core(
        self,
        url: str,
        title: str,
        keep_expanded: bool = False,
        is_audio: bool = False,
    ) -> None:
        context = self._context
        is_sjjm = self._next_is_sjjm
        self._next_is_sjjm = False

        self._session.set_tab_projection_active(False)
        self._handlers.stop_browser_tab_projection()
        context.ndi_service.stop()
        context.camera_service.stop()
        context.media_controller.stop()
        context.ndi_service.stop()
        context.camera_service.stop()

        for projection_window in context.projection_windows():
            projection_window.clear()

        context.projection_bar.activate_video(
            title,
            keep_expanded=keep_expanded,
            is_audio=is_audio,
        )

        if not is_audio:
            for projection_window in context.projection_windows():
                projection_window.begin_video()

        announce = (
            is_sjjm
            and not is_audio
            and context.sjjm_announce_mode()
        )
        if announce:
            context.projection_bar.begin_announcement_mode()

        if not is_audio:
            self._handlers.prepare_video_session()

        context.media_controller.play_url(url)

        if (
            not is_audio
            and not announce
            and context.start_videos_paused()
        ):
            context.media_controller.pause()

        self._session.set_state({"type": "video", "is_audio": is_audio})
        self._handlers.update_projection_status(
            True,
            title,
            visual=not is_audio,
            auto_keys_media=not is_audio,
        )

    def project_image_bytes(self, data: bytes) -> None:
        title = self._context.translate("Showing image")
        self._project_image_data(title, data, playlist=[])

    def project_tab_frame(self, frame) -> None:
        context = self._context
        if not self._session.tab_projection_active:
            self._session.set_tab_projection_active(True)
            context.media_controller.stop()
            context.ndi_service.stop()
            context.camera_service.stop()
            context.projection_bar.set_playlist([])
            for projection_window in context.projection_windows():
                projection_window.clear()
            live_tab_title = context.translate("Browser — Live Tab")
            context.projection_bar.activate_image(live_tab_title)
            context.projection_bar.hide_add_to_playlist_action()
            context.projection_bar.set_live_tab_mode(True)
            if not ALLOW_ZOOM_PAN_ON_LIVE_TAB:
                context.projection_bar.preview_content.set_image_mode(False)
            self._handlers.update_projection_status(
                True,
                live_tab_title,
                auto_keys_media=False,
            )

        for projection_window in context.projection_windows():
            if isinstance(frame, QImage) and hasattr(projection_window, "show_image_from_qimage"):
                projection_window.show_image_from_qimage(frame, cache_pixmap=False)
            else:
                projection_window.show_image_from_pixmap(frame)
        context.projection_bar.update_tab_live_preview(frame)

    def on_image_apply_transform(self, zoom: float, norm_x: float, norm_y: float) -> None:
        self._apply_image_transform(zoom, norm_x, norm_y, animate=True)

    def on_image_apply_transform_instant(
        self,
        zoom: float,
        norm_x: float,
        norm_y: float,
    ) -> None:
        self._apply_image_transform(zoom, norm_x, norm_y, animate=False)

    def _apply_image_transform(
        self,
        zoom: float,
        norm_x: float,
        norm_y: float,
        *,
        animate: bool,
    ) -> None:
        # Persist the transform as part of the projection state so a surface
        # created later (hot-plugged monitor / respawned preview) is replayed
        # with the same framing instead of showing it untransformed.  Applies to
        # both projected images and the sermon-theme slide.
        state = self._session.state
        if state.get("type") in _TRANSFORMABLE_STATES:
            state["transform"] = (zoom, norm_x, norm_y)
        for projection_window in self._context.projection_windows():
            projection_window.set_image_transform(
                zoom,
                norm_x,
                norm_y,
                animate=animate,
            )

    def on_image_reset_transform(self) -> None:
        state = self._session.state
        if state.get("type") in _TRANSFORMABLE_STATES:
            state["transform"] = _IDENTITY_TRANSFORM
        for projection_window in self._context.projection_windows():
            projection_window.set_image_transform(*_IDENTITY_TRANSFORM)

    def on_cache_play(
        self,
        path: str,
        media_type: str,
        original_url: str = "",
        display_title: str = "",
    ) -> None:
        title = display_title or Path(path).stem or os.path.basename(path)
        url_for_playlist = original_url if original_url else path
        playlist = [{"url": url_for_playlist, "title": title, "type": media_type}]
        if media_type == "image":
            self._project_image_path(title, path, playlist=playlist)
        else:
            self.project_video(
                path,
                title,
                playlist,
                None,
                from_saved_playlist=False,
            )

    def on_title_from_metadata(self, title: str) -> None:
        projection_bar = self._context.projection_bar
        if projection_bar.is_video_mode() and title:
            projection_bar.set_projected_title(title)

    def distribute_frame(self, frame) -> None:
        context = self._context
        if not context.projection_bar.is_video_mode():
            return
        if context.projection_bar.is_audio_mode():
            return
        for projection_window in context.projection_windows():
            projection_window.update_frame(frame)

    def project_media_at_index(
        self,
        playlist: list,
        index: int = 0,
        keep_expanded: bool = False,
        playback_order: str | None = None,
    ) -> None:
        context = self._context
        if not playlist or index >= len(playlist):
            return

        item = playlist[index]
        media_type = item.get("type", "video")

        if media_type == "image":
            self._project_image_path(
                item["title"],
                item["url"],
                playlist=playlist,
                playback_order=playback_order,
                index=index,
                keep_expanded=keep_expanded,
            )
            return

        ext = os.path.splitext(item["url"])[1].lower()
        context.projection_bar.set_playlist(playlist, playback_order)
        context.projection_bar.set_playlist_index(index)
        self.project_video_core(
            item["url"],
            item["title"],
            keep_expanded=keep_expanded,
            is_audio=ext in AUDIO_EXTS,
        )

    def on_playlist_navigate(self, index: int) -> None:
        projection_bar = self._context.projection_bar
        playlist = projection_bar.playlist_items()
        if not playlist or index >= len(playlist):
            return

        item = playlist[index]
        media_type = item.get("type", "video")
        if media_type == "image":
            self._project_image_path(
                item["title"],
                item["url"],
                keep_expanded=projection_bar.is_expanded(),
            )
            return

        ext = os.path.splitext(item["url"])[1].lower()
        self.project_video_core(
            item["url"],
            item["title"],
            keep_expanded=projection_bar.is_expanded(),
            is_audio=ext in AUDIO_EXTS,
        )

    def _resolve_and_project_meeting_item(self, item, title: str) -> None:
        service = self._context.meeting_service()
        resolved = service.resolve_video(item)
        url = resolved.get("url", "")
        if url:
            display_title = resolved.get("title") or title
            playlist_item = {"url": url, "title": display_title, "type": "video"}
            self.project_video(url, display_title, [playlist_item], None)
            return

        QMessageBox.information(
            self._context.dialog_parent,
            "Meetings",
            f"Could not resolve video URL for: {title}\n"
            "Check your internet connection.",
        )

    def _project_image_path(
        self,
        title: str,
        path: str,
        *,
        playlist: list | None = None,
        playback_order: str | None = None,
        from_saved_playlist: bool = False,
        index: int | None = None,
        keep_expanded: bool = False,
    ) -> None:
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return
        self._project_image_data(
            title,
            data,
            playlist=playlist,
            playback_order=playback_order,
            from_saved_playlist=from_saved_playlist,
            index=index,
            keep_expanded=keep_expanded,
        )

    def _project_image_data(
        self,
        title: str,
        data: bytes,
        *,
        playlist: list | None = None,
        playback_order: str | None = None,
        from_saved_playlist: bool = False,
        index: int | None = None,
        keep_expanded: bool = False,
    ) -> None:
        context = self._context
        self._session.set_tab_projection_active(False)
        self._handlers.stop_browser_tab_projection()
        context.media_controller.stop()
        context.ndi_service.stop()
        context.camera_service.stop()

        if playlist is not None:
            if playback_order is None:
                context.projection_bar.set_playlist(
                    playlist,
                    from_saved_playlist=from_saved_playlist,
                )
            else:
                context.projection_bar.set_playlist(
                    playlist,
                    playback_order,
                    from_saved_playlist=from_saved_playlist,
                )
            if index is not None:
                context.projection_bar.set_playlist_index(index)

        for projection_window in context.projection_windows():
            projection_window.clear()
            projection_window.show_image_from_url_data(data)

        self._session.set_state(
            {"type": "image", "data": data, "transform": _IDENTITY_TRANSFORM}
        )
        context.projection_bar.activate_image(
            title,
            image_data=data,
            keep_expanded=keep_expanded,
        )
        self._handlers.update_projection_status(
            True,
            title,
            auto_keys_media=True,
        )
