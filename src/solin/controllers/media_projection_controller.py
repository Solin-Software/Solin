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

#: Projection states that carry a zoom/pan transform (so it is persisted in the
#: projection session and replayed onto surfaces created later).  Both render
#: through a zoom/pan-capable widget: image -> VideoDisplayWidget, sermon theme
#: -> the talk theme slide.
_TRANSFORMABLE_STATES = frozenset({"image", "sermon_theme"})

#: Identity transform (no zoom, no pan).
_IDENTITY_TRANSFORM = (1.0, 0.0, 0.0)


def _default_projection_aspect_ratio() -> ProjectionAspectRatio:
    return DEFAULT_PROJECTION_ASPECT_RATIO


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
    playback_protection: Any
    projection_aspect_ratio_provider: Callable[[], Any] = (
        _default_projection_aspect_ratio
    )


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
        if not self._allow_manual_projection_change():
            return

        if isinstance(item, dict):
            url = item.get("url") or item.get("file_path") or ""
            title = item.get("title") or item.get("label") or "Media"
            media_type = item.get("type") or item.get("media_type") or "video"
            if media_type == "image" and url and os.path.exists(url):
                self._project_image_path(
                    title,
                    url,
                    playlist=[],
                    image_framing=item.get("image_framing"),
                    user_initiated=False,
                )
            elif url:
                playlist_item = {"url": url, "title": title, "type": media_type}
                self.project_video(url, title, [playlist_item], None)
            return

        mime = item.mime_type or ""
        title = re.sub(r"<[^>]+>", "", item.label or item.caption or "Media").strip()

        if "image" in mime and item.file_path and os.path.exists(item.file_path):
            self._project_image_path(
                title,
                item.file_path,
                playlist=[],
                image_framing=getattr(item, "image_framing", None),
                user_initiated=False,
            )
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
        return self.project_video_core(
            url,
            title,
            is_audio=ext in AUDIO_EXTS,
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
            user_initiated=False,
        )

    def project_video_core(
        self,
        url: str,
        title: str,
        keep_expanded: bool = False,
        is_audio: bool = False,
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
        return True

    def project_image_bytes(self, data: bytes) -> None:
        title = self._context.translate("Showing image")
        self._project_image_data(title, data, playlist=[])

    def project_tab_frame(self, frame) -> None:
        context = self._context
        if not self._session.tab_projection_active:
            if not self._allow_manual_projection_change(notify=False):
                return
            self._session.set_tab_projection_active(True)
            context.media_controller.stop()
            context.ndi_service.stop()
            context.camera_service.stop()
            context.projection_bar.set_playlist([])
            for projection_window in context.projection_windows():
                projection_window.clear()
            live_tab_title = context.translate("Browser — Live Tab")
            context.projection_bar.activate_image(live_tab_title)
            context.projection_bar.hide_add_to_destination_action()
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
        sync_preview: bool = False,
    ) -> None:
        # Persist the transform as part of the projection state so a surface
        # created later (hot-plugged monitor / respawned preview) is replayed
        # with the same framing instead of showing it untransformed.  Applies to
        # both projected images and the sermon-theme slide.
        state = self._session.state
        if state.get("type") in _TRANSFORMABLE_STATES:
            state["transform"] = (zoom, norm_x, norm_y)
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
            user_initiated=False,
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
    ) -> None:
        if user_initiated and not self._allow_manual_projection_change():
            return
        context = self._context
        initial_transform = self._prepared_image_transform(data, image_framing)
        if self._update_active_image_framing(data, initial_transform):
            return

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
            projection_window.show_image_from_url_data(
                data,
                initial_transform=initial_transform,
            )

        self._session.set_state(
            {
                "type": "image",
                "data": data,
                "transform": (
                    initial_transform.zoom,
                    initial_transform.norm_x,
                    initial_transform.norm_y,
                ),
            }
        )
        context.projection_bar.activate_image(
            title,
            image_data=data,
            keep_expanded=keep_expanded,
            initial_transform=initial_transform,
        )
        self._handlers.update_projection_status(
            True,
            title,
            auto_keys_media=True,
        )

    def _allow_manual_projection_change(self, *, notify: bool = True) -> bool:
        return self._context.playback_protection.allow_manual_projection_change(
            notify=notify
        )

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
            image_transform_from_values(state.get("transform"))
            or IDENTITY_IMAGE_TRANSFORM
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
