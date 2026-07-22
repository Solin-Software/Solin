"""I/O-free conversion from meeting domain trees to media-tree snapshots."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QCoreApplication

from solin.core.i18n.meeting_sections import display_meeting_section_title
from solin.core.media.operations import MediaOperationRecord
from solin.core.meetings.colors import section_colors
from solin.core.meetings.media_nodes import media_ref_title
from solin.core.meetings.tree_editing import children_for_tree_target, parse_tree_list_id
from solin.core.meetings.tree_types import clone_nodes
from solin.styles.theme import current_theme_scheme
from solin.ui.qml.media_tree.media_presenter import MediaRoleInput, media_roles
from solin.ui.qml.media_tree.snapshot import (
    MediaTreeNodeSnapshot,
    MediaTreeNodeType,
    MediaTreeSnapshot,
)
from solin.ui.qml.media_tree.state import MediaPresentationState


MeetingNode = dict[str, Any]


@dataclass(frozen=True, slots=True)
class PendingMeetingNodes:
    operation_id: str
    nodes: tuple[MeetingNode, ...]
    target_list_id: str = "root"
    insert_index: int = -1


class MeetingTreePresenter:
    def __init__(
        self,
        badge_provider: Callable[[str], str] | None = None,
    ) -> None:
        self._badge_provider = badge_provider or _translated_badge

    def build(
        self,
        tree_id: str,
        nodes: Sequence[MeetingNode],
        *,
        revision: int,
        runtime_states: Mapping[str, MediaPresentationState] | None = None,
        operations: Mapping[str, MediaOperationRecord] | None = None,
        source_revisions: Mapping[str, int] | None = None,
        resolved_urls: Mapping[str, str] | None = None,
        pending_groups: tuple[PendingMeetingNodes, ...] = (),
    ) -> MediaTreeSnapshot:
        if not tree_id:
            raise ValueError("A meeting snapshot requires a stable tree ID")
        states = runtime_states or {}
        operation_records = operations or {}
        source_versions = source_revisions or {}
        resolved = resolved_urls or {}
        presented_nodes = clone_nodes(list(nodes))
        for pending in pending_groups:
            kind, target_id = parse_tree_list_id(pending.target_list_id)
            target = children_for_tree_target(presented_nodes, kind, target_id)
            if target is None:
                target = presented_nodes
            row = (
                len(target)
                if pending.insert_index < 0
                else max(0, min(pending.insert_index, len(target)))
            )
            for offset, node in enumerate(clone_nodes(list(pending.nodes))):
                target.insert(row + offset, node)
        roots = tuple(
            self._present_node(node, states, operation_records, source_versions, resolved)
            for node in presented_nodes
        )
        return MediaTreeSnapshot.create(f"meeting:{tree_id}", revision, roots)

    def _present_node(
        self,
        node: MeetingNode,
        states: Mapping[str, MediaPresentationState],
        operations: Mapping[str, MediaOperationRecord],
        source_revisions: Mapping[str, int],
        resolved_urls: Mapping[str, str],
    ) -> MediaTreeNodeSnapshot:
        node_id = str(node.get("id") or "")
        node_type = str(node.get("type") or "")
        source_revision = int(source_revisions.get(node_id, 0))
        if node_type in {"section", "subsection"}:
            hue = int(node.get("color_hue", 145 if node_type == "subsection" else 215))
            colors = section_colors(hue, current_theme_scheme())
            children = tuple(
                self._present_node(
                    child,
                    states,
                    operations,
                    source_revisions,
                    resolved_urls,
                )
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
                    "title": display_meeting_section_title(node),
                    "color": colors["accent"],
                    "textColor": colors["text"],
                    "badgeBg": colors["badge"],
                    "collapsed": bool(node.get("collapsed", False)),
                    "itemCount": _media_count(node),
                    "canDrag": True,
                    "canEdit": True,
                    "canProject": False,
                    "canRemove": True,
                },
                children=children,
            )
        if node_type == "marker":
            return MediaTreeNodeSnapshot.create(
                node_id,
                MediaTreeNodeType.MARKER,
                source_revision=source_revision,
                roles={
                    "text": str(node.get("text") or ""),
                    "canDrag": True,
                    "canEdit": True,
                    "canProject": False,
                    "canRemove": True,
                },
            )
        ref = node.get("media_ref") or {}
        media_type = str(node.get("media_type") or _media_type_from_ref(ref))
        state = states.get(node_id, MediaPresentationState())
        return MediaTreeNodeSnapshot.create(
            node_id,
            MediaTreeNodeType.MEDIA,
            source_revision=source_revision,
            roles=media_roles(
                MediaRoleInput(
                    node_id=node_id,
                    title=str(node.get("title") or media_ref_title(ref) or _media_title()),
                    media_type=media_type,
                    badge=self._badge_provider(media_type),
                    url=resolved_urls.get(node_id) or _node_url(node, ref),
                    start_trim_ticks=_trim_ticks(node, ref, "start_trim_ticks"),
                    end_trim_ticks=_trim_ticks(node, ref, "end_trim_ticks"),
                    base_duration_ticks=_trim_ticks(node, ref, "base_duration_ticks"),
                    image_framing=node.get("image_framing"),
                ),
                state,
                operations.get(node_id),
                source_revision=source_revision,
            ),
        )


def _node_url(node: MeetingNode, ref: dict[str, Any]) -> str:
    resolved = str(node.get("resolved_url") or "")
    return resolved or str(ref.get("file_path") or "")


def _trim_ticks(node: MeetingNode, ref: dict[str, Any], field: str) -> int:
    value = node.get(field) if node.get(field) is not None else ref.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _media_type_from_ref(ref: dict[str, Any]) -> str:
    mime = str(ref.get("mime_type") or "").lower()
    if "image" in mime:
        return "image"
    if "audio" in mime:
        return "audio"
    return "video"


def _media_count(node: MeetingNode) -> int:
    return sum(
        1 if child.get("type") == "media" else _media_count(child)
        for child in node.get("children", [])
    )


def _translated_badge(media_type: str) -> str:
    source = {"image": "Image", "audio": "Audio"}.get(media_type, "Video")
    return QCoreApplication.translate("PlaylistPanel", source)


def _media_title() -> str:
    return QCoreApplication.translate("_MediaRow", "Media")


__all__ = ["MeetingTreePresenter", "PendingMeetingNodes"]
