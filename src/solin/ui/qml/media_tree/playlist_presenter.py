"""I/O-free conversion from playlist state to a full media-tree snapshot."""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any

from PySide6.QtCore import QUrl

from solin.core.i18n.strings import (
    tr_offline_download,
    tr_offline_downloading,
    tr_offline_downloading_progress,
)
from solin.core.media.duration import format_effective_duration_ticks
from solin.core.media.operations import MediaOperationRecord, MediaOperationState
from solin.core.meetings.colors import (
    accent_from_hue,
    badge_bg_from_hue,
    section_text_from_hue,
)
from solin.core.playlists.tree_editing import PlaylistNode, build_playlist_tree
from solin.core.projection.image_framing import (
    image_transform_from_record,
    image_transform_to_record,
)
from solin.styles.theme import current_theme_scheme
from solin.ui.qml.media_tree.snapshot import (
    MediaTreeNodeSnapshot,
    MediaTreeNodeType,
    MediaTreeSnapshot,
)
from solin.ui.qml.media_tree.state import MediaAvailability, MediaPresentationState
from solin.ui.qml.playlist.visuals import playlist_media_badge


class PlaylistTreePresenter:
    """Build complete immutable roles without consulting filesystem-backed stores."""

    def build(
        self,
        playlist: dict[str, Any],
        *,
        revision: int,
        runtime_states: Mapping[str, MediaPresentationState] | None = None,
        operations: Mapping[str, MediaOperationRecord] | None = None,
        source_revisions: Mapping[str, int] | None = None,
    ) -> MediaTreeSnapshot:
        playlist_id = str(playlist.get("id") or "")
        if not playlist_id:
            raise ValueError("A playlist snapshot requires a persisted playlist ID")
        states = runtime_states or {}
        operation_records = operations or {}
        source_versions = source_revisions or {}
        roots = tuple(
            self._present_node(node, states, operation_records, source_versions)
            for node in build_playlist_tree(playlist)
        )
        return MediaTreeSnapshot.create(f"playlist:{playlist_id}", revision, roots)

    def _present_node(
        self,
        node: PlaylistNode,
        states: Mapping[str, MediaPresentationState],
        operations: Mapping[str, MediaOperationRecord],
        source_revisions: Mapping[str, int],
    ) -> MediaTreeNodeSnapshot:
        node_id = str(node["id"])
        node_type = str(node["type"])
        source_revision = int(source_revisions.get(node_id, 0))
        if node_type in {"section", "subsection"}:
            ref = node["ref"]
            hue = int(ref.get("color_hue", 145 if node_type == "subsection" else 215))
            scheme = current_theme_scheme()
            children = tuple(
                self._present_node(child, states, operations, source_revisions)
                for child in node.get("children", [])
            )
            return MediaTreeNodeSnapshot.create(
                node_id,
                (
                    MediaTreeNodeType.SUBSECTION
                    if node_type == "subsection"
                    else MediaTreeNodeType.SECTION
                ),
                source_revision=source_revision,
                roles={
                    "title": str(ref.get("name") or ""),
                    "color": accent_from_hue(hue),
                    "textColor": section_text_from_hue(hue, scheme),
                    "badgeBg": badge_bg_from_hue(hue, scheme),
                    "collapsed": bool(ref.get("collapsed", False)),
                    "itemCount": _media_count(node),
                    "canDrag": True,
                    "canEdit": True,
                    "canProject": False,
                    "canRemove": True,
                },
                children=children,
            )
        if node_type == "marker":
            ref = node["ref"]
            return MediaTreeNodeSnapshot.create(
                node_id,
                MediaTreeNodeType.MARKER,
                source_revision=source_revision,
                roles={
                    "text": str(ref.get("text") or ""),
                    "subsectionId": str(ref.get("subsection_id") or ""),
                    "canDrag": True,
                    "canEdit": True,
                    "canProject": False,
                    "canRemove": True,
                },
            )
        return self._present_media(
            node["ref"],
            states.get(node_id, MediaPresentationState()),
            operations.get(node_id),
            source_revision,
        )

    def _present_media(
        self,
        item: dict[str, Any],
        state: MediaPresentationState,
        operation: MediaOperationRecord | None,
        source_revision: int,
    ) -> MediaTreeNodeSnapshot:
        node_id = str(item["id"])
        url = str(item.get("url") or "")
        remote = url.startswith(("http://", "https://"))
        local_available = state.availability == MediaAvailability.AVAILABLE
        trim_source = ""
        if state.local_path:
            trim_source = QUrl.fromLocalFile(os.path.abspath(state.local_path)).toString()
        elif remote:
            trim_source = url
        elif local_available and url:
            trim_source = QUrl.fromLocalFile(os.path.abspath(url)).toString()
        cloud_visible = remote and not state.cached
        cloud_active = cloud_visible and (
            state.availability == MediaAvailability.CHECKING
            or state.cloud_progress >= 0
        )
        cloud_tooltip = ""
        if cloud_visible:
            if cloud_active and state.cloud_progress >= 0:
                cloud_tooltip = tr_offline_downloading_progress(
                    int(round(state.cloud_progress * 100))
                )
            elif cloud_active:
                cloud_tooltip = tr_offline_downloading()
            else:
                cloud_tooltip = tr_offline_download()

        base_ticks = _ticks(item.get("base_duration_ticks")) or state.duration_ticks
        start_ticks = _ticks(item.get("start_trim_ticks"))
        end_ticks = _ticks(item.get("end_trim_ticks"))
        media_type = str(item.get("type") or "video")
        operation_state = (
            operation.state.value
            if operation is not None
            and operation.state not in {MediaOperationState.CANCELLED}
            else MediaOperationState.READY.value
        )
        active = operation_state not in {
            MediaOperationState.READY.value,
            MediaOperationState.FAILED.value,
        }
        operation_message = ""
        if operation is not None:
            operation_message = operation.error or operation.stage or operation.detail
        return MediaTreeNodeSnapshot.create(
            node_id,
            MediaTreeNodeType.MEDIA,
            source_revision=source_revision,
            roles={
                "title": str(item.get("title") or ""),
                "mediaType": media_type,
                "badge": playlist_media_badge(media_type),
                "duration": format_effective_duration_ticks(
                    base_ticks,
                    start_ticks,
                    end_ticks,
                ),
                "thumbSource": state.thumbnail_source,
                "url": url,
                "trimSource": trim_source,
                "trimAvailable": bool(trim_source and not active),
                "cloudVisible": cloud_visible,
                "cloudActive": cloud_active,
                "cloudProgress": float(state.cloud_progress),
                "cloudTooltip": cloud_tooltip,
                "isMissing": state.availability == MediaAvailability.MISSING,
                "startTrimTicks": start_ticks,
                "endTrimTicks": end_ticks,
                "baseDurationTicks": base_ticks,
                "hasCustomTrim": bool(start_ticks or end_ticks),
                "imageFraming": image_transform_to_record(
                    image_transform_from_record(item.get("image_framing"))
                ),
                "operationId": operation.operation_id if operation else "",
                "operationState": operation_state,
                "operationProgress": operation.progress if operation else -1.0,
                "operationMessage": operation_message,
                "operationCancellable": bool(operation and operation.cancellable),
                "operationRetryable": bool(operation and operation.retryable),
                "canDrag": not active,
                "canEdit": not active,
                "canProject": not active and state.availability != MediaAvailability.MISSING,
                "canRemove": not active or operation_state == MediaOperationState.FAILED.value,
                "canDownload": cloud_visible and not active,
            },
        )


def _media_count(node: PlaylistNode) -> int:
    return sum(
        1 if child.get("type") == "media" else _media_count(child)
        for child in node.get("children", [])
    )


def _ticks(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
