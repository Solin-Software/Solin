from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QMessageBox

from ..core.foundation.constants import (
    ALLOW_ZOOM_PAN_ON_LIVE_TAB,
)
from ..core.media.formats import AUDIO_EXTS
from ..core.media.playback_request import MediaPlaybackRequest, MediaTrim
from ..core.projection.aspect_ratio import (
    DEFAULT_PROJECTION_ASPECT_RATIO,
    ProjectionAspectRatio,
)
from ..core.projection.image_framing import (
    IDENTITY_IMAGE_TRANSFORM,
    ImageTransform,
    constrain_image_transform_for_aspect,
    image_transform_from_record,
    image_transform_from_values,
    image_transforms_equal,
)
from ..core.projection.result import ProjectionResult, ProjectionResultStatus

#: Projection states that carry a zoom/pan transform so newly created surfaces
#: can restore exactly the same framing.
_TRANSFORMABLE_STATES = frozenset({"image"})

#: Identity transform (no zoom, no pan).
_IDENTITY_TRANSFORM = (1.0, 0.0, 0.0)

_TR_CONTEXT = "MediaProjection"
_SHOWING_IMAGE_SOURCE = QT_TRANSLATE_NOOP(
    "MediaProjection",
    "Showing image",
)
_LIVE_TAB_SOURCE = QT_TRANSLATE_NOOP(
    "MediaProjection",
    "Browser — Live Tab",
)


def _tr(source: str) -> str:
    return QCoreApplication.translate(_TR_CONTEXT, source)


def _default_projection_aspect_ratio() -> ProjectionAspectRatio:
    return DEFAULT_PROJECTION_ASPECT_RATIO


def _discard_content_frame(_frame: object) -> None:
    return


@dataclass(frozen=True, slots=True)
class MediaProjectionContext:
    """Dependencies for media, image, playlist, cache, and live-tab projection."""

    projection_session: Any
    projection_bar: Any
    media_controller: Any
    ndi_service: Any
    projection_windows: Callable[[], list[Any]]
    playlist_edit_is_temp: Callable[[], bool]
    meeting_service: Callable[[], Any]
    dialog_parent: Any
    sjjm_announce_mode: Callable[[], bool]
    start_videos_paused: Callable[[], bool]
    playback_protection: Any
    projection_aspect_ratio_provider: Callable[[], Any] = _default_projection_aspect_ratio
    content_frame_sink: Callable[[object], None] = _discard_content_frame
    camera_service: Any | None = None


@dataclass(frozen=True, slots=True)
class MediaProjectionHandlers:
    """Shell and integration actions used by media projection workflows."""

    stop_browser_tab_projection: Callable[[], None]
    update_projection_status: Callable[..., None]
    prepare_video_session: Callable[[], None]
    prepare_auto_share_playback: Callable[[], bool]


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
        if not self._allow_manual_projection_change():
            return

        if isinstance(item, dict):
            url = item.get("url") or item.get("file_path") or ""
            title = item.get("title") or item.get("label") or "Media"
            media_type = item.get("type") or item.get("media_type") or "video"
            if media_type == "image" and url:
                origin_item = {
                    "url": url,
                    "title": title,
                    "type": media_type,
                    **self._meeting_origin_fields(item),
                }
                self._project_image_path(
                    title,
                    url,
                    playlist=[origin_item],
                    image_framing=item.get("image_framing"),
                    user_initiated=False,
                )
            elif url:
                playlist_item = {
                    "url": url,
                    "title": title,
                    "type": media_type,
                    "start_trim_ticks": item.get("start_trim_ticks"),
                    "end_trim_ticks": item.get("end_trim_ticks"),
                    "base_duration_ticks": item.get("base_duration_ticks"),
                    **self._meeting_origin_fields(item),
                }
                self.project_video(url, title, [playlist_item], None)
            return

        mime = item.mime_type or ""
        title = re.sub(r"<[^>]+>", "", item.label or item.caption or "Media").strip()

        if "image" in mime and item.file_path:
            origin_item = {
                "url": item.file_path,
                "title": title,
                "type": "image",
                **self._meeting_origin_fields(item),
            }
            self._project_image_path(
                title,
                item.file_path,
                playlist=[origin_item],
                image_framing=getattr(item, "image_framing", None),
                user_initiated=False,
            )
            return

        if "video" not in mime and "image" in mime:
            return

        if item.file_path:
            if item.key_symbol and item.key_symbol.lower() in ("sjj", "sjjm") and item.track:
                self._next_is_sjjm = True
            media_type = "audio" if "audio" in mime else "video"
            playlist_item = {
                "url": item.file_path,
                "title": title,
                "type": media_type,
                "start_trim_ticks": getattr(item, "start_trim_ticks", None),
                "end_trim_ticks": getattr(item, "end_trim_ticks", None),
                "base_duration_ticks": getattr(item, "base_duration_ticks", None),
                **self._meeting_origin_fields(item),
            }
            self.project_video(
                item.file_path,
                title,
                [playlist_item],
                None,
                user_initiated=False,
            )
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
            selected_item = playlist[0] if playlist else {}
            self._project_image_path(
                title,
                url,
                playlist=playlist,
                playback_order=order if order else None,
                from_saved_playlist=True,
                keep_expanded=self._context.projection_bar.is_expanded(),
                image_framing=selected_item.get("image_framing"),
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
        user_initiated: bool = True,
    ) -> bool:
        if user_initiated and not self._allow_manual_projection_change():
            self._next_is_sjjm = False
            return False
        items = playlist if playlist else [{"url": url, "title": title}]
        self._context.projection_bar.set_playlist(
            items,
            playback_order,
            from_saved_playlist=from_saved_playlist,
        )
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        selected_item = next(
            (item for item in items if item.get("url") == url),
            items[0],
        )
        return self.project_video_core(
            url,
            title,
            is_audio=ext in AUDIO_EXTS,
            media_item=selected_item,
            user_initiated=False,
        )

    def project_next_auto(self, item: dict[str, Any]) -> None:
        context = self._context
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")
        media_type = str(item.get("type") or "video")
        if url == "__replay__":
            context.media_controller.stop()
            context.ndi_service.stop()
            if context.camera_service is not None:
                context.camera_service.stop()
            current = context.projection_bar.current_playlist_item()
            if current is None:
                return
            current_ext = os.path.splitext(current.get("url", ""))[1].lower()
            if current_ext not in AUDIO_EXTS:
                for projection_window in context.projection_windows():
                    projection_window.begin_video()
            context.media_controller.start_playback(self._playback_request(current, autoplay=True))
            return

        if media_type == "image":
            self._project_image_path(
                title,
                url,
                keep_expanded=context.projection_bar.is_expanded(),
                image_framing=item.get("image_framing"),
                user_initiated=False,
            )
            return

        ext = os.path.splitext(url)[1].lower()
        self.project_video_core(
            url,
            title,
            keep_expanded=context.projection_bar.is_expanded(),
            is_audio=ext in AUDIO_EXTS,
            media_item=item,
            user_initiated=False,
        )

    def project_video_core(
        self,
        url: str,
        title: str,
        keep_expanded: bool = False,
        is_audio: bool = False,
        media_item: dict[str, Any] | None = None,
        user_initiated: bool = True,
    ) -> bool:
        if user_initiated and not self._allow_manual_projection_change():
            self._next_is_sjjm = False
            return False
        context = self._context
        is_sjjm = self._next_is_sjjm
        self._next_is_sjjm = False

        self._session.set_tab_projection_active(False)
        self._handlers.stop_browser_tab_projection()
        context.ndi_service.stop()
        if context.camera_service is not None:
            context.camera_service.stop()
        context.media_controller.stop()
        context.ndi_service.stop()

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
            and not self._item_has_custom_trim(media_item)
        )
        if not is_audio:
            self._handlers.prepare_video_session()

        wait_for_auto_share = not is_audio and self._handlers.prepare_auto_share_playback()
        if announce:
            context.projection_bar.begin_announcement_mode()

        self._session.set_state(
            {
                "type": "video",
                "is_audio": is_audio,
                "title": title,
                "origin": self._projection_origin(media_item),
            }
        )
        start_paused = not is_audio and not announce and context.start_videos_paused()
        context.media_controller.start_playback(
            self._playback_request(
                media_item or {"url": url},
                source=url,
                autoplay=not (start_paused or wait_for_auto_share),
            )
        )
        self._handlers.update_projection_status(
            True,
            title,
            visual=not is_audio,
            auto_keys_media=not is_audio,
        )
        return True

    def project_image_bytes(self, data: bytes) -> None:
        title = _tr(_SHOWING_IMAGE_SOURCE)
        self._project_image_data(title, data, playlist=[])

    def project_generated_image(
        self,
        title: str,
        data: bytes,
        metadata: dict[str, str] | None = None,
    ) -> ProjectionResult:
        """Project an app-generated PNG without persisting it as operator media."""

        image = QImage()
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or not image.loadFromData(data, "PNG"):
            return ProjectionResult(ProjectionResultStatus.INVALID)
        if image.width() <= 0 or image.height() <= 0:
            return ProjectionResult(ProjectionResultStatus.INVALID)
        if not self._allow_manual_projection_change():
            return ProjectionResult(ProjectionResultStatus.BLOCKED)

        details = metadata or {}
        accepted = self._project_image_data(
            title,
            data,
            playlist=[],
            user_initiated=False,
            generated_kind=str(details.get("generated_kind") or "generated"),
            fingerprint=str(details.get("fingerprint") or ""),
            persist_operator_copy=False,
            allow_image_actions=False,
            auto_keys_media=False,
        )
        if not accepted:
            return ProjectionResult(ProjectionResultStatus.FAILED)
        return ProjectionResult(ProjectionResultStatus.ACCEPTED)

    def project_tab_frame(self, frame) -> None:
        context = self._context
        if not self._session.tab_projection_active:
            if not self._allow_manual_projection_change(notify=False):
                return
            self._session.set_tab_projection_active(True)
            context.media_controller.stop()
            context.ndi_service.stop()
            if context.camera_service is not None:
                context.camera_service.stop()
            context.projection_bar.set_playlist([])
            for projection_window in context.projection_windows():
                projection_window.clear()
            live_tab_title = _tr(_LIVE_TAB_SOURCE)
            context.projection_bar.activate_image(live_tab_title)
            context.projection_bar.hide_add_to_destination_action()
            context.projection_bar.set_live_tab_mode(True)
            self._session.set_state({"type": "browser", "title": live_tab_title})
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
        context.content_frame_sink(frame)

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
        sync_preview: bool = False,
    ) -> None:
        # Persist the transform as part of the projection state so a surface
        # created later (hot-plugged monitor / respawned preview) is replayed
        # with the same framing instead of showing it untransformed.  Applies to
        # projected images.
        state = self._session.state
        if state.get("type") in _TRANSFORMABLE_STATES:
            self._session.update_image_transform(
                (zoom, norm_x, norm_y),
                animate=animate,
            )
        if sync_preview:
            self._context.projection_bar.set_projected_image_transform(
                ImageTransform(zoom, norm_x, norm_y)
            )
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
            self._session.update_image_transform(
                _IDENTITY_TRANSFORM,
                animate=True,
            )
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
            if self._session.state_type == "video":
                self._session.update_state(title=title)

    def distribute_frame(self, frame) -> None:
        context = self._context
        state = self._session.state
        if state.get("type") != "video" or state.get("is_audio", False):
            return
        context.content_frame_sink(frame)
        for projection_window in context.projection_windows():
            if getattr(projection_window, "native_output_active", False):
                continue
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
        if not self._allow_manual_projection_change():
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
                image_framing=item.get("image_framing"),
                user_initiated=False,
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
            media_item=item,
            user_initiated=False,
        )

    def on_playlist_navigate(self, index: int) -> None:
        projection_bar = self._context.projection_bar
        playlist = projection_bar.playlist_items()
        if not playlist or index >= len(playlist):
            return
        if not self._allow_manual_projection_change():
            return

        item = playlist[index]
        media_type = item.get("type", "video")
        if media_type == "image":
            self._project_image_path(
                item["title"],
                item["url"],
                keep_expanded=projection_bar.is_expanded(),
                image_framing=item.get("image_framing"),
                user_initiated=False,
            )
            return

        ext = os.path.splitext(item["url"])[1].lower()
        self.project_video_core(
            item["url"],
            item["title"],
            keep_expanded=projection_bar.is_expanded(),
            is_audio=ext in AUDIO_EXTS,
            media_item=item,
            user_initiated=False,
        )

    @staticmethod
    def _item_has_custom_trim(item: dict[str, Any] | None) -> bool:
        if not item:
            return False
        for field in ("start_trim_ticks", "end_trim_ticks"):
            try:
                if int(item.get(field) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def _playback_request(
        item: dict[str, Any],
        *,
        source: str | None = None,
        autoplay: bool = True,
    ) -> MediaPlaybackRequest:
        def ticks(name: str) -> int:
            value = item.get(name)
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0

        start_ticks = ticks("start_trim_ticks")
        end_ticks = ticks("end_trim_ticks")
        base_ticks = ticks("base_duration_ticks")
        try:
            trim = MediaTrim(start_ticks, end_ticks, base_ticks)
        except ValueError:
            # Preserve the requested offsets and let runtime duration
            # resolution reject an impossible range without crashing the UI.
            try:
                trim = MediaTrim(start_ticks, end_ticks)
            except ValueError:
                trim = MediaTrim()
        return MediaPlaybackRequest(
            source=source or str(item.get("url") or ""),
            trim=trim if trim.custom else None,
            autoplay=autoplay,
            occurrence_id=str(item.get("origin_item_id") or item.get("id") or ""),
            occurrence_container_id=str(item.get("origin_container_id") or ""),
        )

    def _resolve_and_project_meeting_item(self, item, title: str) -> None:
        service = self._context.meeting_service()
        resolved = service.resolve_media(item)
        url = resolved.get("url", "")
        if url:
            display_title = resolved.get("title") or title
            playlist_item = {
                "url": url,
                "title": display_title,
                "type": "video",
                "start_trim_ticks": getattr(item, "start_trim_ticks", None),
                "end_trim_ticks": getattr(item, "end_trim_ticks", None),
                "base_duration_ticks": getattr(item, "base_duration_ticks", None),
                **self._meeting_origin_fields(item),
            }
            self.project_video(url, display_title, [playlist_item], None)
            return

        QMessageBox.information(
            self._context.dialog_parent,
            "Meetings",
            f"Could not resolve video URL for: {title}\nCheck your internet connection.",
        )

    @staticmethod
    def _meeting_origin_fields(item: object) -> dict[str, str]:
        def value(name: str) -> object:
            if isinstance(item, dict):
                return item.get(name)
            return getattr(item, name, "")

        return {
            "origin_kind": str(value("origin_kind") or "meeting"),
            "origin_container_id": str(value("origin_container_id") or ""),
            "origin_item_id": str(value("origin_item_id") or ""),
        }

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
        image_framing: object = None,
        user_initiated: bool = True,
    ) -> None:
        if user_initiated and not self._allow_manual_projection_change():
            return
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
            image_framing=image_framing,
            user_initiated=False,
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
        image_framing: object = None,
        user_initiated: bool = True,
        generated_kind: str = "",
        fingerprint: str = "",
        persist_operator_copy: bool = True,
        allow_image_actions: bool = True,
        auto_keys_media: bool = True,
    ) -> bool:
        if user_initiated and not self._allow_manual_projection_change():
            return False
        image = QImage()
        if not data or not image.loadFromData(data) or image.width() <= 0 or image.height() <= 0:
            return False
        context = self._context
        initial_transform = self._prepared_image_transform(data, image_framing)
        if self._update_active_image_framing(data, initial_transform):
            return True

        self._session.set_tab_projection_active(False)
        self._handlers.stop_browser_tab_projection()
        context.media_controller.stop()
        context.ndi_service.stop()
        if context.camera_service is not None:
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
            projection_window.show_image_from_url_data(
                data,
                initial_transform=initial_transform,
            )

        state = {
            "type": "image",
            "title": title,
            "origin": self._projection_origin(
                playlist[index or 0] if playlist and 0 <= (index or 0) < len(playlist) else None
            ),
            "data": data,
            "transform": (
                initial_transform.zoom,
                initial_transform.norm_x,
                initial_transform.norm_y,
            ),
        }
        if generated_kind:
            state["generated_kind"] = generated_kind
        if fingerprint:
            state["fingerprint"] = fingerprint
        self._session.set_state(state)
        context.content_frame_sink(image)
        activate_options: dict[str, Any] = {
            "image_data": data,
            "keep_expanded": keep_expanded,
            "initial_transform": initial_transform,
        }
        if not persist_operator_copy or not allow_image_actions:
            activate_options.update(
                persist_operator_copy=persist_operator_copy,
                allow_add_to_destination=allow_image_actions,
                allow_set_as_idle=allow_image_actions,
            )
        context.projection_bar.activate_image(title, **activate_options)
        self._handlers.update_projection_status(
            True,
            title,
            auto_keys_media=auto_keys_media,
        )
        return True

    def _allow_manual_projection_change(self, *, notify: bool = True) -> bool:
        return self._context.playback_protection.allow_manual_projection_change(notify=notify)

    @staticmethod
    def _projection_origin(item: dict[str, Any] | None) -> dict[str, str] | None:
        if not item:
            return None
        kind = str(item.get("origin_kind") or "")
        container_id = str(item.get("origin_container_id") or "")
        item_id = str(item.get("origin_item_id") or item.get("id") or "")
        if not kind or not item_id:
            return None
        return {
            "kind": kind,
            "container_id": container_id,
            "item_id": item_id,
        }

    def _update_active_image_framing(
        self,
        data: bytes,
        target_transform: ImageTransform,
    ) -> bool:
        """Animate framing only when the exact projected image is already active."""

        state = self._session.state
        if state.get("type") != "image":
            return False

        current_transform = (
            image_transform_from_values(state.get("transform")) or IDENTITY_IMAGE_TRANSFORM
        )
        if image_transforms_equal(current_transform, target_transform):
            return False
        if state.get("data") != data:
            return False

        self._apply_image_transform(
            target_transform.zoom,
            target_transform.norm_x,
            target_transform.norm_y,
            animate=True,
            sync_preview=True,
        )
        return True

    def _prepared_image_transform(
        self,
        data: bytes,
        record: object,
    ) -> ImageTransform:
        transform = image_transform_from_record(record)
        if transform is None:
            return IDENTITY_IMAGE_TRANSFORM

        image = QImage()
        image.loadFromData(data)
        if image.isNull():
            return IDENTITY_IMAGE_TRANSFORM

        try:
            ratio = self._context.projection_aspect_ratio_provider()
        except Exception:  # noqa: BLE001 - defensive projection provider boundary
            ratio = DEFAULT_PROJECTION_ASPECT_RATIO
        if not isinstance(ratio, ProjectionAspectRatio):
            ratio = DEFAULT_PROJECTION_ASPECT_RATIO
        return constrain_image_transform_for_aspect(
            image.width(),
            image.height(),
            ratio.value,
            transform,
        )
