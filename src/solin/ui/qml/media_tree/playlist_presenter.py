"""I/O-free conversion from playlist state to a full media-tree snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from solin.core.media.operations import MediaOperationRecord
from solin.core.meetings.colors import (
    accent_from_hue,
    badge_bg_from_hue,
    section_text_from_hue,
)
from solin.core.playlists.tree_editing import PlaylistNode, build_playlist_tree
from solin.core.meetings.tree_editing import children_for_tree_target, parse_tree_list_id
from solin.styles.theme import current_theme_scheme
from solin.ui.qml.media_tree.media_presenter import MediaRoleInput, media_roles
from solin.ui.qml.media_tree.snapshot import (
    MediaTreeNodeSnapshot,
    MediaTreeNodeType,
    MediaTreeSnapshot,
)
from solin.ui.qml.media_tree.state import MediaPresentationState
from solin.ui.qml.playlist.visuals import playlist_media_badge


@dataclass(frozen=True, slots=True)
class PendingPlaylistMedia:
    item: dict[str, Any]
    target_list_id: str = "root"
    insert_index: int = -1


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
        pending_media: tuple[PendingPlaylistMedia, ...] = (),
    ) -> MediaTreeSnapshot:
        playlist_id = str(playlist.get("id") or "")
        if not playlist_id:
            raise ValueError("A playlist snapshot requires a persisted playlist ID")
        states = runtime_states or {}
        operation_records = operations or {}
        source_versions = source_revisions or {}
        tree = build_playlist_tree(playlist)
        for pending in pending_media:
            kind, target_id = parse_tree_list_id(pending.target_list_id)
            target = children_for_tree_target(tree, kind, target_id)
            if target is None:
                target = tree
            row = (
                len(target)
                if pending.insert_index < 0
                else max(0, min(pending.insert_index, len(target)))
            )
            item = pending.item
            target.insert(
                row,
                {"id": item["id"], "type": "media", "ref": item, "children": []},
            )
        roots = tuple(
            self._present_node(node, states, operation_records, source_versions)
            for node in tree
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
        base_ticks = _ticks(item.get("base_duration_ticks"))
        start_ticks = _ticks(item.get("start_trim_ticks"))
        end_ticks = _ticks(item.get("end_trim_ticks"))
        media_type = str(item.get("type") or "video")
        return MediaTreeNodeSnapshot.create(
            node_id,
            MediaTreeNodeType.MEDIA,
            source_revision=source_revision,
            roles=media_roles(
                MediaRoleInput(
                    node_id=node_id,
                    title=str(item.get("title") or ""),
                    media_type=media_type,
                    badge=playlist_media_badge(media_type),
                    url=str(item.get("url") or ""),
                    start_trim_ticks=start_ticks,
                    end_trim_ticks=end_ticks,
                    base_duration_ticks=base_ticks,
                    image_framing=item.get("image_framing"),
                ),
                state,
                operation,
                source_revision=source_revision,
            ),
        )


def _media_count(node: PlaylistNode) -> int:
    return sum(
        1 if child.get("type") == "media" else _media_count(child)
        for child in node.get("children", [])
    )


def _ticks(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


__all__ = ["PendingPlaylistMedia", "PlaylistTreePresenter"]
