"""I/O-free conversion from playlist state to a full media-tree snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from solin.core.media.operations import MediaOperationRecord
from solin.core.meetings.colors import (
    ThemeScheme,
    accent_from_hue,
    badge_bg_from_hue,
    section_text_from_hue,
)
from solin.core.playlists.tree_editing import PlaylistNode, build_playlist_tree
from solin.core.meetings.tree_editing import children_for_tree_target, parse_tree_list_id
from solin.styles.theme import current_theme_scheme
from solin.ui.qml.media_tree.media_presenter import MediaRoleInput, media_roles
from solin.ui.qml.media_tree.presenter_cache import PresenterNodeCache
from solin.ui.qml.media_tree.snapshot import (
    MediaTreeNodeSnapshot,
    MediaTreeNodeType,
    MediaTreeSnapshot,
    freeze_role_value,
)
from solin.ui.qml.media_tree.state import (
    EMPTY_PRESENTATION_STATE,
    MediaPresentationState,
)
from solin.ui.qml.playlist.visuals import playlist_media_badge


@dataclass(frozen=True, slots=True)
class PendingPlaylistMedia:
    item: dict[str, Any]
    target_list_id: str = "root"
    insert_index: int = -1


class PlaylistTreePresenter:
    """Build complete immutable roles without consulting filesystem-backed stores."""

    def __init__(self) -> None:
        self._cache = PresenterNodeCache()

    def build(
        self,
        playlist: dict[str, Any],
        *,
        revision: int,
        runtime_states: Mapping[str, MediaPresentationState] | None = None,
        operations: Mapping[str, MediaOperationRecord] | None = None,
        source_revisions: Mapping[str, int] | None = None,
        pending_media: tuple[PendingPlaylistMedia, ...] = (),
        theme_scheme: ThemeScheme | None = None,
        media_badges: Mapping[str, str] | None = None,
    ) -> MediaTreeSnapshot:
        playlist_id = str(playlist.get("id") or "")
        if not playlist_id:
            raise ValueError("A playlist snapshot requires a persisted playlist ID")
        states = runtime_states or {}
        operation_records = operations or {}
        source_versions = source_revisions or {}
        tree_id = f"playlist:{playlist_id}"
        self._cache.begin(tree_id)
        scheme = theme_scheme or current_theme_scheme()
        badges = dict(media_badges or {})
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
            self._present_node(
                node,
                states,
                operation_records,
                source_versions,
                scheme,
                badges,
            )
            for node in tree
        )
        snapshot = MediaTreeSnapshot.create(tree_id, revision, roots)
        self._cache.finish()
        return snapshot

    def _present_node(
        self,
        node: PlaylistNode,
        states: Mapping[str, MediaPresentationState],
        operations: Mapping[str, MediaOperationRecord],
        source_revisions: Mapping[str, int],
        scheme: ThemeScheme,
        badges: dict[str, str],
    ) -> MediaTreeNodeSnapshot:
        node_id = str(node["id"])
        node_type = str(node["type"])
        source_revision = int(source_revisions.get(node_id, 0))
        if node_type in {"section", "subsection"}:
            ref = node["ref"]
            hue = int(ref.get("color_hue", 145 if node_type == "subsection" else 215))
            children = tuple(
                self._present_node(
                    child,
                    states,
                    operations,
                    source_revisions,
                    scheme,
                    badges,
                )
                for child in node.get("children", [])
            )
            presented_type = (
                MediaTreeNodeType.SUBSECTION
                if node_type == "subsection"
                else MediaTreeNodeType.SECTION
            )
            title = str(ref.get("name") or "")
            collapsed = bool(ref.get("collapsed", False))
            item_count = _media_count(node)
            key = (
                presented_type,
                source_revision,
                title,
                hue,
                scheme,
                collapsed,
                item_count,
                tuple(id(child) for child in children),
            )
            return self._cache.resolve(
                node_id,
                key,
                lambda: MediaTreeNodeSnapshot.create(
                    node_id,
                    presented_type,
                    source_revision=source_revision,
                    roles={
                        "title": title,
                        "color": accent_from_hue(hue),
                        "textColor": section_text_from_hue(hue, scheme),
                        "badgeBg": badge_bg_from_hue(hue, scheme),
                        "collapsed": collapsed,
                        "itemCount": item_count,
                        "canDrag": True,
                        "canEdit": True,
                        "canProject": False,
                        "canRemove": True,
                    },
                    children=children,
                ),
            )
        if node_type == "marker":
            ref = node["ref"]
            text = str(ref.get("text") or "")
            subsection_id = str(ref.get("subsection_id") or "")
            return self._cache.resolve(
                node_id,
                (MediaTreeNodeType.MARKER, source_revision, text, subsection_id),
                lambda: MediaTreeNodeSnapshot.create(
                    node_id,
                    MediaTreeNodeType.MARKER,
                    source_revision=source_revision,
                    roles={
                        "text": text,
                        "subsectionId": subsection_id,
                        "canDrag": True,
                        "canEdit": True,
                        "canProject": False,
                        "canRemove": True,
                    },
                ),
            )
        return self._present_media(
            node["ref"],
            states.get(node_id, EMPTY_PRESENTATION_STATE),
            operations.get(node_id),
            source_revision,
            badges,
        )

    def _present_media(
        self,
        item: dict[str, Any],
        state: MediaPresentationState,
        operation: MediaOperationRecord | None,
        source_revision: int,
        badges: dict[str, str],
    ) -> MediaTreeNodeSnapshot:
        node_id = str(item["id"])
        base_ticks = _ticks(item.get("base_duration_ticks"))
        start_ticks = _ticks(item.get("start_trim_ticks"))
        end_ticks = _ticks(item.get("end_trim_ticks"))
        media_type = str(item.get("type") or "video")
        badge = badges.get(media_type)
        if badge is None:
            badge = playlist_media_badge(media_type)
            badges[media_type] = badge
        title = str(item.get("title") or "")
        url = str(item.get("url") or "")
        framing = item.get("image_framing")
        key = (
            MediaTreeNodeType.MEDIA,
            source_revision,
            title,
            media_type,
            badge,
            url,
            start_ticks,
            end_ticks,
            base_ticks,
            freeze_role_value(framing),
            state,
            operation,
        )
        return self._cache.resolve(
            node_id,
            key,
            lambda: MediaTreeNodeSnapshot.create(
                node_id,
                MediaTreeNodeType.MEDIA,
                source_revision=source_revision,
                roles=media_roles(
                    MediaRoleInput(
                        node_id=node_id,
                        title=title,
                        media_type=media_type,
                        badge=badge,
                        url=url,
                        start_trim_ticks=start_ticks,
                        end_trim_ticks=end_ticks,
                        base_duration_ticks=base_ticks,
                        image_framing=framing,
                    ),
                    state,
                    operation,
                    source_revision=source_revision,
                ),
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
