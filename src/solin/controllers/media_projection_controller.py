from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QMessageBox

from ..core.foundation.constants import (
    ALLOW_ZOOM_PAN_ON_LIVE_TAB,
)
from ..core.media.duration import normalize_duration_ticks
from ..core.media.formats import MediaKind, media_kind_from_path
from ..core.media.playback_request import (
    TICKS_PER_MILLISECOND,
    MediaPlaybackRequest,
    MediaTrim,
)
from ..core.meetings.models import MeetingMedia
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
from .projection_playback_operations import (
    PlaybackEndAction,
    ProjectionPlaybackOperations,
    ProjectionQueue,
)

#: Projection states that carry a zoom/pan transform so newly created surfaces
#: can restore exactly the same framing.
_TRANSFORMABLE_STATES = frozenset({"image"})

#: Identity transform (no zoom, no pan).
_IDENTITY_TRANSFORM = (1.0, 0.0, 0.0)

_TR_CONTEXT = "MediaProjection"
_SHOWING_IMAGE_SOURCE = cast(str, QT_TRANSLATE_NOOP("MediaProjection", "Showing image"))
_LIVE_TAB_SOURCE = cast(str, QT_TRANSLATE_NOOP("MediaProjection", "Browser — Live Tab"))


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
    projection_playback: ProjectionPlaybackOperations
    media_controller: Any
    ndi_service: Any
    projection_windows: Callable[[], list[Any]]
    playlist_edit_is_temp: Callable[[], bool]
    meeting_service: Callable[[], Any]
    dialog_parent: Any
    sjjm_announce_mode: Callable[[], bool]
    start_videos_paused: Callable[[], bool]
    projection_aspect_ratio_provider: Callable[[], Any] = _default_projection_aspect_ratio
    content_frame_sink: Callable[[object], None] = _discard_content_frame
    image_source_lease: Callable[[str], AbstractContextManager] = nullcontext


@dataclass(frozen=True, slots=True)
class MediaProjectionHandlers:
    """Shell and integration actions used by media projection workflows."""

    stop_browser_tab_projection: Callable[[], None]
    update_projection_status: Callable[..., None]
    prepare_video_session: Callable[[], None]
    prepare_auto_share_playback: Callable[[bool], bool]
    stop_projection: Callable[[], None]


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
        self._playback = context.projection_playback

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
        self.project_video(
            url,
            title,
            playlist or None,
            order if order else None,
            from_saved_playlist=True,
            announce_song=True,
        )

    def on_meeting_media_project(
        self,
        item: MeetingMedia | Mapping[str, Any],
        *,
        notify: bool = True,
        start_paused: bool = False,
    ) -> ProjectionResult:
        if not item:
            return ProjectionResult(ProjectionResultStatus.INVALID)
        if not self._playback.allow_manual_projection_change(notify=notify):
            return ProjectionResult(ProjectionResultStatus.BLOCKED)

        announce_song = False
        if isinstance(item, Mapping):
            record = {
                "url": item.get("url") or item.get("file_path"),
                "title": item.get("title") or item.get("label") or "Media",
                "type": item.get("type") or item.get("media_type") or "video",
                "start_trim_ticks": item.get("start_trim_ticks"),
                "end_trim_ticks": item.get("end_trim_ticks"),
                "base_duration_ticks": item.get("base_duration_ticks"),
                "image_framing": item.get("image_framing"),
                **self._meeting_origin_fields(item),
            }
        else:
            announce_song = bool(
                item.key_symbol and item.key_symbol.lower() in ("sjj", "sjjm") and item.track
            )
            record = self._meeting_media_record(item, notify=notify)
            if record is None:
                return ProjectionResult(ProjectionResultStatus.FAILED)

        queue = self._playback.new_queue((record,))
        return self._project_queue_item(
            queue, keep_expanded=False, start_paused=start_paused, announce_song=announce_song
        )

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
                queue=self._playback.new_queue(playlist, order, from_saved_playlist=True),
                keep_expanded=True,
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
        *,
        keep_expanded: bool = False,
        announce_song: bool = False,
    ) -> bool:
        if not url.strip():
            return False
        if user_initiated and not self._playback.allow_manual_projection_change():
            return False
        media_type = (
            MediaKind.AUDIO if media_kind_from_path(url) is MediaKind.AUDIO else MediaKind.VIDEO
        )
        items = playlist or [{"url": url, "title": title, "type": media_type.value}]
        index = next((i for i, item in enumerate(items) if item.get("url") == url), 0)
        queue = self._playback.new_queue(
            items, playback_order, index=index, from_saved_playlist=from_saved_playlist
        )
        return self._project_video(
            url, title, queue, keep_expanded=keep_expanded, announce_song=announce_song
        )

    def on_media_ended(self) -> None:
        if self._session.state_type != "video":
            return
        action = self._playback.after_media_ended()
        if action is PlaybackEndAction.HOLD:
            return
        if action is PlaybackEndAction.REPLAY:
            self._context.media_controller.replay()
        elif action is PlaybackEndAction.STOP:
            self._handlers.stop_projection()
        elif not self._project_queue_item(action, keep_expanded=True).accepted:
            if self._session.state_type == "video":
                self._handlers.stop_projection()

    def _project_video(
        self,
        url: str,
        title: str,
        queue: ProjectionQueue,
        *,
        keep_expanded: bool,
        announce_song: bool = False,
        start_paused: bool = False,
    ) -> bool:
        context = self._context
        media_item = queue.current_item
        is_audio = (
            (media_item is not None and media_item.get("type") == "audio")
            or media_kind_from_path(url) is MediaKind.AUDIO
        )
        profile_paused = not is_audio and context.start_videos_paused()
        pause_without_announcement = start_paused or profile_paused
        duration_ticks = normalize_duration_ticks(ticks=(media_item or {}).get("base_duration_ticks"))
        announce = (
            announce_song
            and not is_audio
            and context.sjjm_announce_mode()
            and not self._item_has_custom_trim(media_item)
            and self._playback.allows_announcement(duration_ticks // TICKS_PER_MILLISECOND)
        )

        self._session.set_tab_projection_active(False)
        self._handlers.stop_browser_tab_projection()
        context.ndi_service.stop()
        with context.media_controller.replace_playback():
            self._playback.reset_transport()
            self._playback.commit_queue(queue)
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

            if not is_audio:
                self._handlers.prepare_video_session()

            wait_for_auto_share = not is_audio and self._handlers.prepare_auto_share_playback(
                not pause_without_announcement
            )
            if announce:
                self._playback.begin_announcement(
                    start_paused=start_paused, pause_if_short=profile_paused
                )

            self._session.set_state(
                {
                    "type": "video",
                    "is_audio": is_audio,
                    "title": title,
                    "origin": self._projection_origin(media_item),
                }
            )
            open_paused = start_paused or (profile_paused and not announce) or wait_for_auto_share
            session_id = self._session.session_id
            context.media_controller.start_playback(
                self._playback_request(
                    media_item or {"url": url},
                    source=url,
                    autoplay=not open_paused,
                )
            )
        if self._session.session_id != session_id:
            return False
        self._handlers.update_projection_status(
            True,
            title,
            visual=not is_audio,
            auto_keys_media=not is_audio,
        )
        return True

    def project_image_bytes(self, data: bytes) -> None:
        title = _tr(_SHOWING_IMAGE_SOURCE)
        self._project_image_data(title, data)

    def project_generated_image(
        self,
        title: str,
        data: bytes,
        metadata: dict[str, str] | None = None,
    ) -> ProjectionResult:
        """Project an app-generated PNG without persisting it as operator media."""

        image = QImage()
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or not image.loadFromData(data):
            return ProjectionResult(ProjectionResultStatus.INVALID)
        if image.width() <= 0 or image.height() <= 0:
            return ProjectionResult(ProjectionResultStatus.INVALID)
        if not self._playback.allow_manual_projection_change():
            return ProjectionResult(ProjectionResultStatus.BLOCKED)

        details = metadata or {}
        accepted = self._project_image_data(
            title,
            data,
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
            if not self._playback.allow_manual_projection_change(notify=False):
                return
            self._session.set_tab_projection_active(True)
            context.media_controller.stop()
            context.ndi_service.stop()
            self._playback.reset_projection()
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
            self._project_image_path(title, path, queue=self._playback.new_queue(playlist))
        else:
            self.project_video(
                path,
                title,
                playlist,
                None,
                from_saved_playlist=False,
            )

    def on_title_from_metadata(self, title: str) -> None:
        if self._session.state_type == "video" and title:
            self._session.update_state(title=title)
            self._context.projection_bar.set_projected_title(title)

    def project_media_at_index(
        self,
        playlist: list,
        index: int = 0,
        keep_expanded: bool = False,
        playback_order: str | None = None,
        *,
        notify: bool = True,
        start_paused: bool = False,
    ) -> ProjectionResult:
        if not 0 <= index < len(playlist):
            return ProjectionResult(ProjectionResultStatus.INVALID)
        if not self._playback.allow_manual_projection_change(notify=notify):
            return ProjectionResult(ProjectionResultStatus.BLOCKED)
        queue = self._playback.new_queue(playlist, playback_order, index=index)
        return self._project_queue_item(queue, keep_expanded=keep_expanded, start_paused=start_paused)

    def on_playlist_navigate(self, index: int, *, notify: bool = True) -> ProjectionResult:
        queue = self._playback.queue
        if not 0 <= index < len(queue.items):
            return ProjectionResult(ProjectionResultStatus.INVALID)
        if index == queue.index:
            return ProjectionResult(ProjectionResultStatus.ACCEPTED)
        if not self._playback.allow_manual_projection_change(notify=notify):
            return ProjectionResult(ProjectionResultStatus.BLOCKED)
        return self._project_queue_item(queue.select(index), keep_expanded=True)

    def _project_queue_item(
        self,
        queue: ProjectionQueue,
        *,
        keep_expanded: bool,
        start_paused: bool = False,
        announce_song: bool = False,
    ) -> ProjectionResult:
        item = queue.current_item
        if not item or not (url := str(item.get("url") or "")).strip():
            return ProjectionResult(ProjectionResultStatus.INVALID)

        title = str(item.get("title") or "")
        media_type = item.get("type")
        if media_type == "image":
            accepted = self._project_image_path(
                title,
                url,
                queue=queue,
                keep_expanded=keep_expanded,
                image_framing=item.get("image_framing"),
                reframe_active_image=False,
                user_initiated=False,
            )
        else:
            accepted = self._project_video(
                url,
                title,
                queue,
                keep_expanded=keep_expanded,
                start_paused=start_paused,
                announce_song=announce_song,
            )
        return ProjectionResult(
            ProjectionResultStatus.ACCEPTED if accepted else ProjectionResultStatus.FAILED
        )

    @staticmethod
    def _item_has_custom_trim(item: Mapping[str, Any] | None) -> bool:
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
        item: Mapping[str, Any],
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

    def _meeting_media_record(
        self, item: MeetingMedia, *, notify: bool
    ) -> dict[str, Any] | None:
        mime = item.mime_type or ""
        title = re.sub(r"<[^>]+>", "", item.label or item.caption or "Media").strip()
        url = item.file_path
        if not url and "image" not in mime and (item.key_symbol or item.meps_doc_id):
            resolved = self._context.meeting_service().resolve_media(item)
            url = resolved.get("url", "")
            if not url and notify:
                QMessageBox.information(
                    self._context.dialog_parent,
                    "Meetings",
                    f"Could not resolve video URL for: {title}\nCheck your internet connection.",
                )
            title = resolved.get("title") or title
        if not url:
            return None
        media_type = "image" if "image" in mime else "audio" if "audio" in mime else "video"
        return {
            "url": url,
            "title": title,
            "type": media_type,
            "start_trim_ticks": getattr(item, "start_trim_ticks", None),
            "end_trim_ticks": getattr(item, "end_trim_ticks", None),
            "base_duration_ticks": getattr(item, "base_duration_ticks", None),
            "image_framing": getattr(item, "image_framing", None),
            **self._meeting_origin_fields(item),
        }

    @staticmethod
    def _meeting_origin_fields(item: object) -> dict[str, str]:
        def value(name: str) -> object:
            if isinstance(item, Mapping):
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
        queue: ProjectionQueue | None = None,
        keep_expanded: bool = False,
        image_framing: object = None,
        reframe_active_image: bool = True,
        user_initiated: bool = True,
    ) -> bool:
        if user_initiated and not self._playback.allow_manual_projection_change():
            return False
        with self._context.image_source_lease(path):
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError:
                return False
            return self._project_image_data(
                title,
                data,
                queue=queue,
                keep_expanded=keep_expanded,
                image_framing=image_framing,
                reframe_active_image=reframe_active_image,
                user_initiated=False,
                source_path=path,
            )

    def _project_image_data(
        self,
        title: str,
        data: bytes,
        *,
        queue: ProjectionQueue | None = None,
        keep_expanded: bool = False,
        image_framing: object = None,
        reframe_active_image: bool = True,
        user_initiated: bool = True,
        generated_kind: str = "",
        fingerprint: str = "",
        persist_operator_copy: bool = True,
        allow_image_actions: bool = True,
        auto_keys_media: bool = True,
        source_path: str = "",
    ) -> bool:
        if user_initiated and not self._playback.allow_manual_projection_change():
            return False
        image = QImage()
        if not data or not image.loadFromData(data) or image.width() <= 0 or image.height() <= 0:
            return False
        context = self._context
        initial_transform = self._prepared_image_transform(data, image_framing)
        if queue is None:
            queue = self._playback.new_queue(())
        origin = self._projection_origin(queue.current_item)
        if (
            reframe_active_image
            and not generated_kind
            and not self._session.state.get("generated_kind")
            and self._session.state.get("origin") == origin
            and self._update_active_image_framing(data, initial_transform)
        ):
            self._playback.commit_queue(queue)
            self._session.update_state(source_path=source_path, title=title, origin=origin)
            context.projection_bar.set_projected_title(title)
            return True

        self._session.set_tab_projection_active(False)
        self._handlers.stop_browser_tab_projection()
        context.media_controller.stop()
        context.ndi_service.stop()
        self._playback.reset_transport()
        self._playback.commit_queue(queue)

        for projection_window in context.projection_windows():
            projection_window.clear()
            projection_window.show_image_from_url_data(
                data,
                initial_transform=initial_transform,
            )

        state = {
            "type": "image",
            "title": title,
            "origin": origin,
            "data": data,
            "transform": (
                initial_transform.zoom,
                initial_transform.norm_x,
                initial_transform.norm_y,
            ),
        }
        if generated_kind:
            state["generated_kind"] = generated_kind
        if source_path:
            state["source_path"] = source_path
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

    @staticmethod
    def _projection_origin(item: Mapping[str, Any] | None) -> dict[str, str] | None:
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
