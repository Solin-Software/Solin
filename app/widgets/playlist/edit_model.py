"""Playlist edit QML list model.

This module owns the flat/tree model used by PlaylistEditView.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal, Slot

from ...core.media.cache import MediaCacheManager
from ...core.i18n.strings import (
    tr_offline_download,
    tr_offline_downloading,
    tr_offline_downloading_progress,
)
from ...core.meetings.colors import (
    accent_from_hue,
    badge_bg_from_hue,
    card_bg_from_hue,
    card_border_from_hue,
    section_text_from_hue,
)
from .edit_visuals import (
    _format_duration,
    _media_badge,
    _thumb_cache_path,
    _UNSECTIONED_BG,
)

_MARKER_POSITION_FALLBACK = 1_000_000_000

# ── Flat model ─────────────────────────────────────────────────────────────────

class PlaylistEditModel(QAbstractListModel):
    """Flat model for QML ListView.

    Every row is one of:
        - ``"section"``      – top-level section header
        - ``"subsection"``   – nested subsection header
        - ``"item"``         – media item

    Visual card-grouping metadata (``cardTop``, ``cardBottom``, accent bar) is
    computed automatically so that consecutive entries belonging to the same
    section render as a single rounded card with a colored left bar.
    """

    # ── Roles ──────────────────────────────────────────────────────────────
    EntryTypeRole     = Qt.ItemDataRole.UserRole + 1
    EntryIdRole       = Qt.ItemDataRole.UserRole + 2
    NameRole          = Qt.ItemDataRole.UserRole + 3
    TitleRole         = Qt.ItemDataRole.UserRole + 4
    MediaTypeRole     = Qt.ItemDataRole.UserRole + 5
    DepthRole         = Qt.ItemDataRole.UserRole + 6
    CardTopRole       = Qt.ItemDataRole.UserRole + 7
    CardBottomRole    = Qt.ItemDataRole.UserRole + 8
    CardBgRole        = Qt.ItemDataRole.UserRole + 9
    CardBorderRole    = Qt.ItemDataRole.UserRole + 10
    GapAboveRole      = Qt.ItemDataRole.UserRole + 11
    ShowAccentRole    = Qt.ItemDataRole.UserRole + 12
    AccentColorRole   = Qt.ItemDataRole.UserRole + 13
    CollapsedRole     = Qt.ItemDataRole.UserRole + 14
    ItemCountRole     = Qt.ItemDataRole.UserRole + 15
    ThumbSourceRole   = Qt.ItemDataRole.UserRole + 16
    ThumbVersionRole  = Qt.ItemDataRole.UserRole + 17
    CloudVisibleRole  = Qt.ItemDataRole.UserRole + 18
    CloudActiveRole   = Qt.ItemDataRole.UserRole + 19
    CloudProgressRole = Qt.ItemDataRole.UserRole + 20
    CloudTooltipRole  = Qt.ItemDataRole.UserRole + 21
    DurationTextRole  = Qt.ItemDataRole.UserRole + 22
    SectionIdRole     = Qt.ItemDataRole.UserRole + 23
    ParentIdRole      = Qt.ItemDataRole.UserRole + 24
    SectionTextRole   = Qt.ItemDataRole.UserRole + 25
    BadgeBgRole       = Qt.ItemDataRole.UserRole + 26
    UrlRole           = Qt.ItemDataRole.UserRole + 27
    SubCardTopRole    = Qt.ItemDataRole.UserRole + 28
    SubCardBottomRole = Qt.ItemDataRole.UserRole + 29
    MissingRole       = Qt.ItemDataRole.UserRole + 30

    _ROLE_NAMES = {
        EntryTypeRole:     b"entryType",
        EntryIdRole:       b"entryId",
        NameRole:          b"name",
        TitleRole:         b"title",
        MediaTypeRole:     b"mediaType",
        DepthRole:         b"depth",
        CardTopRole:       b"cardTop",
        CardBottomRole:    b"cardBottom",
        CardBgRole:        b"cardBg",
        CardBorderRole:    b"cardBorder",
        GapAboveRole:      b"gapAbove",
        ShowAccentRole:    b"showAccent",
        AccentColorRole:   b"accentColor",
        CollapsedRole:     b"collapsed",
        ItemCountRole:     b"itemCount",
        ThumbSourceRole:   b"thumbSource",
        ThumbVersionRole:  b"thumbVersion",
        CloudVisibleRole:  b"cloudVisible",
        CloudActiveRole:   b"cloudActive",
        CloudProgressRole: b"cloudProgress",
        CloudTooltipRole:  b"cloudTooltip",
        DurationTextRole:  b"durationText",
        SectionIdRole:     b"sectionId",
        ParentIdRole:      b"parentId",
        SectionTextRole:   b"sectionText",
        BadgeBgRole:       b"badgeBg",
        UrlRole:           b"url",
        SubCardTopRole:    b"subCardTop",
        SubCardBottomRole: b"subCardBottom",
        MissingRole:       b"isMissing",
    }

    _ROLE_KEY = {
        EntryTypeRole:     "type",
        EntryIdRole:       "id",
        NameRole:          "name",
        TitleRole:         "title",
        MediaTypeRole:     "media_type",
        DepthRole:         "depth",
        CardTopRole:       "card_top",
        CardBottomRole:    "card_bottom",
        CardBgRole:        "card_bg",
        CardBorderRole:    "card_border",
        GapAboveRole:      "gap_above",
        ShowAccentRole:    "show_accent",
        AccentColorRole:   "accent_color",
        CollapsedRole:     "collapsed",
        ItemCountRole:     "item_count",
        ThumbSourceRole:   "thumb_source",
        ThumbVersionRole:  "thumb_version",
        CloudVisibleRole:  "cloud_visible",
        CloudActiveRole:   "cloud_active",
        CloudProgressRole: "cloud_progress",
        CloudTooltipRole:  "cloud_tooltip",
        DurationTextRole:  "duration_text",
        SectionIdRole:     "section_id",
        ParentIdRole:      "parent_id",
        SectionTextRole:   "section_text",
        BadgeBgRole:       "badge_bg",
        UrlRole:           "url",
        SubCardTopRole:    "sub_card_top",
        SubCardBottomRole: "sub_card_bottom",
        MissingRole:       "is_missing",
    }


    orderSynced = Signal()  # emitted after finalizeDrag to trigger save

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[dict] = []
        self._pl: Optional[dict] = None
        self._thumb_versions: dict[str, int] = {}
        self._cloud_progress_by_url: dict[str, float] = {}

    # ── QAbstractListModel overrides ───────────────────────────────────────

    def rowCount(self, parent=None):
        return len(self._entries)

    def roleNames(self):
        return self._ROLE_NAMES

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._entries):
            return None
        key = self._ROLE_KEY.get(role)
        if key:
            return self._entries[index.row()].get(key)
        return None

    # ── Rebuild ────────────────────────────────────────────────────────────

    def rebuild(self, pl: dict) -> None:
        """Full rebuild from playlist dict."""
        self.beginResetModel()
        self._pl = pl
        self._entries = self._build_entries(pl)
        self._compute_card_properties()
        self.endResetModel()

    def tree_data(self) -> list[dict]:
        """Return a QML-friendly recursive playlist tree."""
        if not self._pl:
            return []

        items = self._pl.get("items", [])
        sections = self._pl.get("sections", [])
        sections_map = {s["id"]: s for s in sections}
        markers_by_subsection = self._markers_by_subsection(sections_map)
        top_sections = [s for s in sections if not s.get("parent_id")]
        subsections_by_parent: dict[str, list[dict]] = {}
        for sec in sections:
            parent_id = sec.get("parent_id")
            if parent_id:
                subsections_by_parent.setdefault(parent_id, []).append(sec)

        direct_counts: dict[str, int] = {}
        first_index: dict[str, int] = {}
        for idx, item in enumerate(items):
            sid = item.get("section_id") or ""
            if not sid:
                continue
            direct_counts[sid] = direct_counts.get(sid, 0) + 1
            first_index.setdefault(sid, idx)
            sec = sections_map.get(sid)
            parent_id = sec.get("parent_id") if sec else ""
            if parent_id:
                first_index.setdefault(parent_id, idx)

        def section_sort_key(sec: dict, fallback: int) -> tuple[float, int]:
            pos = sec.get("position")
            if isinstance(pos, int):
                return (float(pos), fallback)
            if sec["id"] in first_index:
                return (float(first_index[sec["id"]]), fallback)
            return (float(len(items) + fallback + 1), fallback)

        root_nodes: list[tuple[float, int, str, dict]] = []
        for idx, item in enumerate(items):
            if not item.get("section_id"):
                root_nodes.append((float(idx), idx, "media", item))
        for order, sec in enumerate(top_sections):
            pos, fallback = section_sort_key(sec, order)
            root_nodes.append((pos, len(items) + fallback, "section", sec))
        root_nodes.sort(key=lambda node: (node[0], node[1]))

        result: list[dict] = []
        for _, _, node_type, obj in root_nodes:
            if node_type == "media":
                result.append(self._media_node(obj))
            else:
                result.append(self._section_node(
                    obj, subsections_by_parent, direct_counts, items,
                    markers_by_subsection,
                    is_subsection=False))
        return result

    def _section_node(
        self,
        sec: dict,
        subsections_by_parent: dict[str, list[dict]],
        direct_counts: dict[str, int],
        items: list[dict],
        markers_by_subsection: dict[str, list[dict]],
        *,
        is_subsection: bool,
    ) -> dict:
        section_id = sec["id"]
        subsections = [] if is_subsection else subsections_by_parent.get(section_id, [])
        subsection_ids = {sub["id"] for sub in subsections}
        children: list[dict] = (
            self._subsection_children(section_id, items, markers_by_subsection)
            if is_subsection else []
        )
        opened_subsections: set[str] = set()

        if not is_subsection:
            for item in items:
                sid = item.get("section_id")
                if sid == section_id:
                    children.append(self._media_node(item))
                elif sid in subsection_ids:
                    sub = next(s for s in subsections if s["id"] == sid)
                    if sid not in opened_subsections:
                        children.append(self._section_node(
                            sub, subsections_by_parent, direct_counts, items,
                            markers_by_subsection,
                            is_subsection=True))
                        opened_subsections.add(sid)

            for sub in subsections:
                if sub["id"] not in opened_subsections:
                    children.append(self._section_node(
                        sub, subsections_by_parent, direct_counts, items,
                        markers_by_subsection,
                        is_subsection=True))

        total_count = direct_counts.get(section_id, 0)
        if not is_subsection:
            for sub in subsections:
                total_count += direct_counts.get(sub["id"], 0)

        hue = sec.get("color_hue", 145 if is_subsection else 215)
        return {
            "id": sec["id"],
            "type": "subsection" if is_subsection else "section",
            "title": sec.get("name", ""),
            "color": accent_from_hue(hue),
            "textColor": section_text_from_hue(hue),
            "badgeBg": badge_bg_from_hue(hue),
            "collapsed": sec.get("collapsed", False),
            "itemCount": total_count,
            "children": children,
        }

    def _markers_by_subsection(self, sections_map: dict[str, dict]) -> dict[str, list[dict]]:
        markers_by_subsection: dict[str, list[dict]] = {}
        for marker in self._pl.get("markers", []):
            subsection_id = marker.get("subsection_id", "")
            subsection = sections_map.get(subsection_id)
            if not subsection or not subsection.get("parent_id"):
                continue
            markers_by_subsection.setdefault(subsection_id, []).append(marker)
        return markers_by_subsection

    def _subsection_children(
        self,
        section_id: str,
        items: list[dict],
        markers_by_subsection: dict[str, list[dict]],
    ) -> list[dict]:
        ordered_nodes: list[tuple[float, int, int, int, str, dict]] = []
        for idx, item in enumerate(items):
            if item.get("section_id") == section_id:
                ordered_nodes.append((float(idx), 1, 0, idx, "media", item))

        for fallback, marker in enumerate(markers_by_subsection.get(section_id, [])):
            position = marker.get("position")
            if not isinstance(position, int):
                position = _MARKER_POSITION_FALLBACK + fallback
            slot_order = marker.get("slot_order")
            if not isinstance(slot_order, int):
                slot_order = fallback
            ordered_nodes.append(
                (float(position), 0, slot_order, fallback, "marker", marker))

        ordered_nodes.sort(key=lambda node: (node[0], node[1], node[2], node[3]))
        children: list[dict] = []
        for _, _, _, _, node_type, obj in ordered_nodes:
            children.append(
                self._media_node(obj)
                if node_type == "media" else self._marker_node(obj))
        return children

    def _marker_node(self, marker: dict) -> dict:
        return {
            "id": marker["id"],
            "type": "marker",
            "text": marker.get("text", ""),
            "subsectionId": marker.get("subsection_id", ""),
            "children": [],
        }

    def _media_node(self, item: dict) -> dict:
        url = item.get("url", "")
        is_remote = MediaCacheManager.is_remote(url)
        cm = MediaCacheManager.instance()
        cached = cm.is_cached(url) if is_remote else True
        prefetching = cm.is_prefetching(url) if is_remote else False
        media_type = item.get("type", "video")
        cloud_visible = bool(is_remote and not cached)
        cloud_progress = self._cloud_progress_by_url.get(url, -1.0)
        cloud_tooltip = ""
        if cloud_visible:
            if prefetching and cloud_progress >= 0:
                cloud_tooltip = tr_offline_downloading_progress(
                    int(round(cloud_progress * 100)))
            elif prefetching:
                cloud_tooltip = tr_offline_downloading()
            else:
                cloud_tooltip = tr_offline_download()

        # Local-only missing detection — never persisted to JSON.
        # A physical file is "missing" if its path doesn't exist on this
        # machine's disk right now.  Remote URLs are never missing here
        # (they have their own cloud download state above).
        is_missing = bool(url and not is_remote and not os.path.exists(url))

        return {
            "id": item["id"],
            "type": "media",
            "title": item.get("title", ""),
            "mediaType": media_type,
            "badge": _media_badge(media_type),
            "duration": _format_duration(item.get("base_duration_ticks", 0)),
            "thumbSource": self._thumb_source_for(item["id"]),
            "url": url,
            "cloudVisible": cloud_visible,
            "cloudActive": bool(prefetching),
            "cloudProgress": cloud_progress,
            "cloudTooltip": cloud_tooltip,
            "isMissing": is_missing,
            "children": [],
        }

    def _thumb_source_for(self, item_id: str) -> str:
        version = self._thumb_versions.get(item_id, 0)
        if version > 0 or os.path.exists(_thumb_cache_path(item_id)):
            return f"image://playlistthumbs/{item_id}/{version}"
        return ""

    def can_drop_node(self, node_id: str, node_type: str, target_list_id: str) -> bool:
        target_kind, _ = self._parse_list_id(target_list_id)
        if target_kind == "root":
            return node_type in ("media", "section")
        if target_kind == "section":
            return node_type in ("media", "subsection")
        if target_kind == "subsection":
            _, target_id = self._parse_list_id(target_list_id)
            if node_type == "media":
                return True
            if node_type == "marker":
                return self._marker_subsection_id(node_id) == target_id
            return False
        return False

    def _marker_subsection_id(self, marker_id: str) -> str:
        if not self._pl:
            return ""
        marker = next(
            (m for m in self._pl.get("markers", [])
             if m.get("id") == marker_id),
            None,
        )
        return marker.get("subsection_id", "") if marker else ""

    def move_node(self, node_id: str, target_list_id: str, insert_index: int) -> bool:
        """Move a tree node, then flatten back into playlist storage."""
        if not self._pl:
            return False

        target_kind, target_id = self._parse_list_id(target_list_id)
        tree = self._storage_tree()
        source = self._pop_tree_node(tree, node_id)
        if not source:
            return False

        node_type = source["type"]
        if not self.can_drop_node(node_id, node_type, target_list_id):
            self._insert_tree_node(tree, "root", "", len(tree), source)
            return False
        if self._contains_tree_node(source, target_id):
            self._insert_tree_node(tree, "root", "", len(tree), source)
            return False

        ok = self._insert_tree_node(tree, target_kind, target_id, insert_index, source)
        if not ok:
            self._insert_tree_node(tree, "root", "", len(tree), source)
            return False

        self._flatten_storage_tree(tree)
        self.rebuild(self._pl)
        return True

    def flat_insert_index_for_list(self, target_list_id: str, insert_index: int) -> int:
        """Convert a tree-list insertion slot to a flat media item index."""
        tree = self._storage_tree()
        target_kind, target_id = self._parse_list_id(target_list_id)
        counter = 0

        def media_count(node: dict) -> int:
            if node["type"] == "media":
                return 1
            return sum(media_count(child) for child in node.get("children", []))

        def visit(children: list[dict], kind: str, node_id: str) -> int | None:
            nonlocal counter
            if kind == target_kind and node_id == target_id:
                idx = max(0, min(insert_index, len(children)))
                return counter + sum(media_count(child) for child in children[:idx])
            for child in children:
                if child["type"] == "media":
                    counter += 1
                    continue
                found = visit(child.get("children", []), child["type"], child["id"])
                if found is not None:
                    return found
            return None

        result = visit(tree, "root", "")
        if result is None:
            return len(self._pl.get("items", []))
        return result

    def insert_media_refs(
        self,
        target_list_id: str,
        insert_index: int,
        media_items: list[dict],
    ) -> bool:
        """Insert new media refs into the storage tree at a visual list slot."""
        if not self._pl or not media_items:
            return False
        target_kind, target_id = self._parse_list_id(target_list_id)
        if not self.can_drop_node("", "media", target_list_id):
            return False
        tree = self._storage_tree()
        media_nodes = [
            {
                "id": item["id"],
                "type": "media",
                "ref": item,
                "children": [],
            }
            for item in media_items
        ]
        for offset, node in enumerate(media_nodes):
            if not self._insert_tree_node(
                    tree, target_kind, target_id, insert_index + offset, node):
                return False
        self._flatten_storage_tree(tree)
        self.rebuild(self._pl)
        return True

    def _parse_list_id(self, list_id: str) -> tuple[str, str]:
        if list_id == "root":
            return "root", ""
        if ":" not in list_id:
            return "", ""
        kind, node_id = list_id.split(":", 1)
        return kind, node_id

    def _storage_tree(self) -> list[dict]:
        items = self._pl.get("items", [])
        sections = self._pl.get("sections", [])
        sections_map = {s["id"]: s for s in sections}
        markers_by_subsection = self._markers_by_subsection(sections_map)
        top_sections = [s for s in sections if not s.get("parent_id")]
        subsections_by_parent: dict[str, list[dict]] = {}
        for sec in sections:
            parent_id = sec.get("parent_id")
            if parent_id:
                subsections_by_parent.setdefault(parent_id, []).append(sec)

        direct_counts: dict[str, int] = {}
        first_index: dict[str, int] = {}
        for idx, item in enumerate(items):
            sid = item.get("section_id") or ""
            if not sid:
                continue
            direct_counts[sid] = direct_counts.get(sid, 0) + 1
            first_index.setdefault(sid, idx)
            sec = sections_map.get(sid)
            parent_id = sec.get("parent_id") if sec else ""
            if parent_id:
                first_index.setdefault(parent_id, idx)

        def section_sort_key(sec: dict, fallback: int) -> tuple[float, int]:
            pos = sec.get("position")
            if isinstance(pos, int):
                return (float(pos), fallback)
            if sec["id"] in first_index:
                return (float(first_index[sec["id"]]), fallback)
            return (float(len(items) + fallback + 1), fallback)

        def make_media(item: dict) -> dict:
            return {"id": item["id"], "type": "media", "ref": item, "children": []}

        def make_marker(marker: dict) -> dict:
            return {
                "id": marker["id"],
                "type": "marker",
                "ref": marker,
                "children": [],
            }

        def make_subsection_children(section_id: str) -> list[dict]:
            ordered_nodes: list[tuple[float, int, int, int, str, dict]] = []
            for idx, item in enumerate(items):
                if item.get("section_id") == section_id:
                    ordered_nodes.append((float(idx), 1, 0, idx, "media", item))
            for fallback, marker in enumerate(markers_by_subsection.get(section_id, [])):
                position = marker.get("position")
                if not isinstance(position, int):
                    position = _MARKER_POSITION_FALLBACK + fallback
                slot_order = marker.get("slot_order")
                if not isinstance(slot_order, int):
                    slot_order = fallback
                ordered_nodes.append(
                    (float(position), 0, slot_order, fallback, "marker", marker))
            ordered_nodes.sort(key=lambda node: (node[0], node[1], node[2], node[3]))
            return [
                make_media(obj) if node_type == "media" else make_marker(obj)
                for _, _, _, _, node_type, obj in ordered_nodes
            ]

        def make_section(sec: dict, is_subsection: bool) -> dict:
            sid = sec["id"]
            children: list[dict] = (
                make_subsection_children(sid) if is_subsection else []
            )
            opened_subsections: set[str] = set()
            subsections = [] if is_subsection else subsections_by_parent.get(sid, [])
            sub_ids = {s["id"] for s in subsections}
            if not is_subsection:
                for item in items:
                    item_sid = item.get("section_id")
                    if item_sid == sid:
                        children.append(make_media(item))
                    elif item_sid in sub_ids:
                        sub = next(s for s in subsections if s["id"] == item_sid)
                        if item_sid not in opened_subsections:
                            children.append(make_section(sub, True))
                            opened_subsections.add(item_sid)
                for sub in subsections:
                    if sub["id"] not in opened_subsections:
                        children.append(make_section(sub, True))
            return {
                "id": sid,
                "type": "subsection" if is_subsection else "section",
                "ref": sec,
                "children": children,
            }

        root_nodes: list[tuple[float, int, dict]] = []
        for idx, item in enumerate(items):
            if not item.get("section_id"):
                root_nodes.append((float(idx), idx, make_media(item)))
        for order, sec in enumerate(top_sections):
            pos, fallback = section_sort_key(sec, order)
            root_nodes.append((pos, len(items) + fallback, make_section(sec, False)))
        root_nodes.sort(key=lambda node: (node[0], node[1]))
        return [node for _, _, node in root_nodes]

    def _pop_tree_node(self, nodes: list[dict], node_id: str) -> dict | None:
        for idx, node in enumerate(nodes):
            if node["id"] == node_id:
                return nodes.pop(idx)
            found = self._pop_tree_node(node.get("children", []), node_id)
            if found:
                return found
        return None

    def _insert_tree_node(
        self,
        nodes: list[dict],
        target_kind: str,
        target_id: str,
        insert_index: int,
        node: dict,
    ) -> bool:
        if target_kind == "root":
            target_children = nodes
        else:
            target = self._find_tree_node(nodes, target_id)
            if not target or target["type"] != target_kind:
                return False
            target_children = target.setdefault("children", [])
        insert_index = max(0, min(insert_index, len(target_children)))
        target_children.insert(insert_index, node)
        return True

    def _find_tree_node(self, nodes: list[dict], node_id: str) -> dict | None:
        for node in nodes:
            if node["id"] == node_id:
                return node
            found = self._find_tree_node(node.get("children", []), node_id)
            if found:
                return found
        return None

    def _contains_tree_node(self, node: dict, node_id: str) -> bool:
        if not node_id:
            return False
        if node["id"] == node_id:
            return True
        return any(self._contains_tree_node(child, node_id)
                   for child in node.get("children", []))

    def _flatten_storage_tree(self, nodes: list[dict]) -> None:
        items_out: list[dict] = []
        sections_out: list[dict] = []
        markers_out: list[dict] = []
        marker_slot_counts: dict[int, int] = {}
        item_counter = 0

        def walk(children: list[dict], parent_section_id: str | None = None):
            nonlocal item_counter
            for node in children:
                ref = node["ref"]
                if node["type"] == "media":
                    ref["section_id"] = parent_section_id
                    items_out.append(ref)
                    item_counter += 1
                elif node["type"] == "marker":
                    if not parent_section_id:
                        continue
                    ref["subsection_id"] = parent_section_id
                    ref["position"] = item_counter
                    ref["slot_order"] = marker_slot_counts.get(item_counter, 0)
                    marker_slot_counts[item_counter] = ref["slot_order"] + 1
                    markers_out.append(ref)
                elif node["type"] == "section":
                    ref["parent_id"] = None
                    ref["position"] = item_counter
                    sections_out.append(ref)
                    walk(node.get("children", []), ref["id"])
                elif node["type"] == "subsection":
                    ref["parent_id"] = parent_section_id
                    ref["position"] = item_counter
                    sections_out.append(ref)
                    walk(node.get("children", []), ref["id"])

        walk(nodes, None)
        self._pl["items"] = items_out
        self._pl["sections"] = sections_out
        self._pl["markers"] = markers_out

    def _build_entries(self, pl: dict) -> list[dict]:
        """Build the visible flat rows from the playlist tree.

        The saved data is still compact (items + sections), but the visual
        model is intentionally hierarchical: a top-level section owns one
        continuous block, and subsections own continuous nested blocks.
        """
        items = pl.get("items", [])
        sections = pl.get("sections", [])
        sections_map = {s["id"]: s for s in sections}
        top_sections = [s for s in sections if not s.get("parent_id")]
        subsections_by_parent: dict[str, list[dict]] = {}
        for sec in sections:
            parent_id = sec.get("parent_id")
            if parent_id:
                subsections_by_parent.setdefault(parent_id, []).append(sec)

        # Repair orphan item references before rendering.
        for item in items:
            sid = item.get("section_id")
            if sid and sid not in sections_map:
                item["section_id"] = None

        direct_counts: dict[str, int] = {}
        first_index: dict[str, int] = {}
        for idx, item in enumerate(items):
            sid = item.get("section_id") or ""
            if not sid:
                continue
            direct_counts[sid] = direct_counts.get(sid, 0) + 1
            first_index.setdefault(sid, idx)
            sec = sections_map.get(sid)
            parent_id = sec.get("parent_id") if sec else ""
            if parent_id:
                first_index.setdefault(parent_id, idx)

        def section_sort_key(sec: dict, fallback: int) -> tuple[float, int]:
            pos = sec.get("position")
            if isinstance(pos, int):
                return (float(pos), fallback)
            if sec["id"] in first_index:
                return (float(first_index[sec["id"]]), fallback)
            return (float(len(items) + fallback + 1), fallback)

        root_nodes: list[tuple[float, int, str, dict]] = []
        for idx, item in enumerate(items):
            if not item.get("section_id"):
                root_nodes.append((float(idx), idx, "item", item))
        for order, sec in enumerate(top_sections):
            pos, fallback = section_sort_key(sec, order)
            root_nodes.append((pos, len(items) + fallback, "section", sec))
        root_nodes.sort(key=lambda node: (node[0], node[1]))

        entries: list[dict] = []
        for _, _, node_type, obj in root_nodes:
            if node_type == "item":
                entries.append(self._make_item_entry(
                    obj, depth=0, section=None, show_accent=False))
            else:
                self._append_section_block(
                    entries, obj, subsections_by_parent, direct_counts, items)
        return entries

    def _append_section_block(
        self,
        entries: list[dict],
        sec: dict,
        subsections_by_parent: dict[str, list[dict]],
        direct_counts: dict[str, int],
        items: list[dict],
    ) -> None:
        section_id = sec["id"]
        subsections = subsections_by_parent.get(section_id, [])
        subsection_ids = {sub["id"] for sub in subsections}
        total = direct_counts.get(section_id, 0)
        for sub in subsections:
            total += direct_counts.get(sub["id"], 0)

        entries.append(self._make_section_entry(sec, total))
        if sec.get("collapsed", False):
            return

        opened_subsections: set[str] = set()
        for item in items:
            sid = item.get("section_id")
            if sid == section_id:
                entries.append(self._make_item_entry(
                    item, depth=1, section=sec, show_accent=True))
            elif sid in subsection_ids:
                sub = next(s for s in subsections if s["id"] == sid)
                if sid not in opened_subsections:
                    entries.append(self._make_subsection_entry(
                        sub, direct_counts.get(sid, 0), sec))
                    opened_subsections.add(sid)
                if not sub.get("collapsed", False):
                    entries.append(self._make_item_entry(
                        item, depth=2, section=sec, show_accent=False))

        for sub in subsections:
            if sub["id"] not in opened_subsections:
                entries.append(self._make_subsection_entry(
                    sub, direct_counts.get(sub["id"], 0), sec))

    def _make_section_entry(self, sec: dict, count: int) -> dict:
        hue = sec.get("color_hue", 215)
        return {
            "type":          "section",
            "id":            sec["id"],
            "name":          sec.get("name", ""),
            "title":         "",
            "media_type":    "",
            "depth":         0,
            "card_top":      False,   # computed later
            "card_bottom":   False,
            "card_bg":       card_bg_from_hue(hue),
            "card_border":   card_border_from_hue(hue),
            "gap_above":     0,
            "show_accent":   True,
            "accent_color":  accent_from_hue(hue),
            "collapsed":     sec.get("collapsed", False),
            "item_count":    count,
            "thumb_source":  "",
            "thumb_version": 0,
            "cloud_visible": False,
            "cloud_active":  False,
            "cloud_progress": -1.0,
            "cloud_tooltip": "",
            "duration_text": "",
            "section_id":    sec["id"],
            "parent_id":     sec.get("parent_id", ""),
            "section_text":  section_text_from_hue(hue),
            "badge_bg":      badge_bg_from_hue(hue),
            "url":           "",
            "sub_card_top":  False,
            "sub_card_bottom": False,
            "_card_group":   sec["id"],   # internal: card grouping key
            "_section_ref":  sec,         # internal: ref to section dict
        }

    def _make_subsection_entry(self, sub: dict, count: int,
                                parent: dict | None) -> dict:
        parent_id = sub.get("parent_id", "")
        parent_hue = (parent or {}).get("color_hue", 215)
        return {
            "type":          "subsection",
            "id":            sub["id"],
            "name":          sub.get("name", ""),
            "title":         "",
            "media_type":    "",
            "depth":         1,
            "card_top":      False,
            "card_bottom":   False,
            "card_bg":       card_bg_from_hue(parent_hue),
            "card_border":   card_border_from_hue(parent_hue),
            "gap_above":     0,
            "show_accent":   True,
            "accent_color":  accent_from_hue(parent_hue),
            "collapsed":     sub.get("collapsed", False),
            "item_count":    count,
            "thumb_source":  "",
            "thumb_version": 0,
            "cloud_visible": False,
            "cloud_active":  False,
            "cloud_progress": -1.0,
            "cloud_tooltip": "",
            "duration_text": "",
            "section_id":    sub["id"],
            "parent_id":     parent_id,
            "section_text":  "#8b949e",
            "badge_bg":      "#1a1f28",
            "url":           "",
            "sub_card_top":  False,
            "sub_card_bottom": False,
            "_card_group":   parent_id,   # belongs to parent's card
            "_section_ref":  sub,
        }

    def _make_item_entry(self, item: dict, depth: int,
                          section: dict | None,
                          show_accent: bool) -> dict:
        hue = (section or {}).get("color_hue", 215)
        sid = item.get("section_id", "")
        card_group = ""
        if section:
            card_group = section["id"]
        elif sid:
            card_group = sid

        # Cloud state
        url = item.get("url", "")
        is_remote = MediaCacheManager.is_remote(url)
        cm = MediaCacheManager.instance()
        cached = cm.is_cached(url) if is_remote else True
        prefetching = cm.is_prefetching(url) if is_remote else False
        cloud_visible = is_remote and not cached
        cloud_active = prefetching
        cloud_progress = self._cloud_progress_by_url.get(url, -1.0)
        cloud_tooltip = ""
        if cloud_visible:
            if prefetching and cloud_progress >= 0:
                cloud_tooltip = tr_offline_downloading_progress(
                    int(round(cloud_progress * 100)))
            elif prefetching:
                cloud_tooltip = tr_offline_downloading()
            else:
                cloud_tooltip = tr_offline_download()

        return {
            "type":          "item",
            "id":            item["id"],
            "name":          "",
            "title":         item.get("title", ""),
            "media_type":    item.get("type", "video"),
            "depth":         depth,
            "card_top":      False,
            "card_bottom":   False,
            "card_bg":       card_bg_from_hue(hue) if section else _UNSECTIONED_BG,
            "card_border":   card_border_from_hue(hue) if section else "transparent",
            "gap_above":     0,
            "show_accent":   bool(section),
            "accent_color":  accent_from_hue(hue) if section else "",
            "collapsed":     False,
            "item_count":    0,
            "thumb_source":  f"image://playlistthumbs/{item['id']}/0",
            "thumb_version": 0,
            "cloud_visible": cloud_visible,
            "cloud_active":  cloud_active,
            "cloud_progress": cloud_progress,
            "cloud_tooltip": cloud_tooltip,
            "duration_text": _format_duration(item.get("base_duration_ticks", 0)),
            "section_id":    sid,
            "parent_id":     "",
            "section_text":  "",
            "badge_bg":      "",
            "url":           url,
            "sub_card_top":  False,
            "sub_card_bottom": False,
            "_card_group":   card_group,
            "_item_ref":     item,
        }


    def _compute_card_properties(self) -> None:
        """Set card_top, card_bottom, gap_above for visual grouping."""
        entries = self._entries
        if not entries:
            return
        for entry in entries:
            entry["card_top"] = False
            entry["card_bottom"] = False
            entry["gap_above"] = 0
            entry["sub_card_top"] = False
            entry["sub_card_bottom"] = False

        prev_group = ""
        for i, entry in enumerate(entries):
            group = entry.get("_card_group", "")

            if group:
                # Check if this is the first entry in its card group
                if group != prev_group:
                    entry["card_top"] = True
                    entry["gap_above"] = 14 if i > 0 else 0

                # Check if this is the last entry in its card group
                next_group = (entries[i + 1].get("_card_group", "")
                              if i + 1 < len(entries) else "")
                if group != next_group:
                    entry["card_bottom"] = True
            else:
                # Unsectioned item
                entry["gap_above"] = 6 if i > 0 else 0

            prev_group = group

        prev_sub = ""
        for i, entry in enumerate(entries):
            is_sub = (entry["type"] == "subsection"
                      or (entry["type"] == "item" and entry["depth"] == 2))
            if is_sub:
                sub_id = entry.get("section_id", "")
                if sub_id != prev_sub:
                    entry["sub_card_top"] = True
                next_is_sub = False
                next_sub_id = ""
                if i + 1 < len(entries):
                    next_entry = entries[i + 1]
                    next_is_sub = (
                        next_entry["type"] == "subsection"
                        or (next_entry["type"] == "item"
                            and next_entry["depth"] == 2)
                    )
                    next_sub_id = next_entry.get("section_id", "")
                if not next_is_sub or next_sub_id != sub_id:
                    entry["sub_card_bottom"] = True
                prev_sub = sub_id
            else:
                prev_sub = ""

    # ── Move / reorder ─────────────────────────────────────────────────────

    def move_entry(self, from_idx: int, insert_idx: int) -> int:
        """Move a visible row/block to an insertion slot.

        ``insert_idx`` is the slot before a row (0..rowCount), not a target row.
        The method returns the dragged row's new index so QML can keep tracking
        it after the model reset.
        """
        count = len(self._entries)
        if from_idx < 0 or from_idx >= count:
            return from_idx
        insert_idx = max(0, min(insert_idx, count))

        block_size = self._count_children(from_idx) + 1
        block_end = from_idx + block_size
        if from_idx <= insert_idx <= block_end:
            return from_idx

        moving = [dict(e) for e in self._entries[from_idx:block_end]]
        remaining = (
            self._entries[:from_idx] + self._entries[block_end:]
        )
        target = insert_idx - block_size if insert_idx > from_idx else insert_idx
        target = max(0, min(target, len(remaining)))

        entry_type = moving[0]["type"]
        context = self._drop_context_for_slot(remaining, target, entry_type)
        if not self._can_drop(entry_type, context["target_type"]):
            return from_idx

        self._apply_drop_context(moving, context)

        self.beginResetModel()
        self._entries = remaining[:target] + moving + remaining[target:]
        self._compute_card_properties()
        self.endResetModel()
        return target

    @Slot(int, int, result=int)
    def moveEntry(self, from_idx: int, insert_idx: int) -> int:
        """QML-facing wrapper for live drag movement."""
        return self.move_entry(from_idx, insert_idx)

    def _drop_context_for_slot(
        self,
        entries: list[dict],
        slot: int,
        moving_type: str = "item",
    ) -> dict:
        """Return the logical target for a slot in a visible flat list."""
        before = entries[slot] if 0 <= slot < len(entries) else None
        after = entries[slot - 1] if slot > 0 and slot - 1 < len(entries) else None

        if moving_type == "section":
            if slot == len(entries) or self._is_root_boundary(before):
                return {"target_type": "root", "section": None, "subsection": None}
            return {"target_type": "blocked", "section": None, "subsection": None}

        if before and before["type"] == "section":
            if after and after.get("_card_group"):
                return self._context_from_entry(after)
            return {"target_type": "root", "section": None, "subsection": None}

        if before:
            return self._context_from_entry(
                before, prefer_parent=before["type"] == "subsection")
        if after:
            return self._context_from_entry(after)
        return {"target_type": "root", "section": None, "subsection": None}

    def _is_root_boundary(self, entry: dict | None) -> bool:
        if entry is None:
            return True
        return entry["type"] == "section" or (
            entry["type"] == "item" and entry.get("depth", 0) == 0
        )

    def _context_from_entry(self, entry: dict, prefer_parent: bool = False) -> dict:
        if entry["type"] == "section":
            return {"target_type": "section", "section": entry, "subsection": None}
        if entry["type"] == "subsection":
            parent = self._entry_for_section_id(entry.get("parent_id", ""))
            return {"target_type": "section", "section": parent, "subsection": None}
        if entry["type"] == "item":
            depth = entry.get("depth", 0)
            if depth == 2 and not prefer_parent:
                sub = self._entry_for_section_id(entry.get("section_id", ""))
                parent = self._entry_for_section_id(sub.get("parent_id", "") if sub else "")
                return {"target_type": "subsection", "section": parent, "subsection": sub}
            if depth >= 1:
                sid = entry.get("_card_group") or entry.get("section_id", "")
                sec = self._entry_for_section_id(sid)
                return {"target_type": "section", "section": sec, "subsection": None}
        return {"target_type": "root", "section": None, "subsection": None}

    def _entry_for_section_id(self, section_id: str) -> dict | None:
        for entry in self._entries:
            if entry["type"] in ("section", "subsection") and entry["id"] == section_id:
                return entry
        return None

    def _can_drop(self, moving_type: str, target_type: str) -> bool:
        if target_type == "root":
            return moving_type in ("item", "section")
        if target_type == "section":
            return moving_type in ("item", "subsection")
        if target_type == "subsection":
            return moving_type == "item"
        return False

    def _apply_drop_context(self, block: list[dict], context: dict) -> None:
        moving_type = block[0]["type"]
        if moving_type == "section":
            block[0]["parent_id"] = ""
            sec_ref = block[0].get("_section_ref")
            if sec_ref:
                sec_ref["parent_id"] = None
            return

        if moving_type == "subsection":
            parent = context.get("section")
            if not parent:
                return
            parent_ref = parent.get("_section_ref")
            parent_id = parent["id"]
            block[0]["parent_id"] = parent_id
            block[0]["_card_group"] = parent_id
            block[0]["card_bg"] = parent["card_bg"]
            block[0]["card_border"] = parent["card_border"]
            block[0]["accent_color"] = parent["accent_color"]
            sec_ref = block[0].get("_section_ref")
            if sec_ref:
                sec_ref["parent_id"] = parent_id
            for child in block[1:]:
                if child["type"] == "item":
                    self._apply_item_context(child, "subsection", parent_ref, sec_ref)
            return

        if moving_type == "item":
            section = context.get("section")
            subsection = context.get("subsection")
            if context["target_type"] == "subsection" and subsection:
                self._apply_item_context(
                    block[0],
                    "subsection",
                    section.get("_section_ref") if section else None,
                    subsection.get("_section_ref"),
                )
            elif context["target_type"] == "section" and section:
                self._apply_item_context(
                    block[0],
                    "section",
                    section.get("_section_ref"),
                    None,
                )
            else:
                self._apply_item_context(block[0], "root", None, None)

    def _apply_item_context(
        self,
        entry: dict,
        target_type: str,
        section_ref: dict | None,
        subsection_ref: dict | None,
    ) -> None:
        if target_type == "subsection" and section_ref and subsection_ref:
            hue = section_ref.get("color_hue", 215)
            entry["section_id"] = subsection_ref["id"]
            entry["_card_group"] = section_ref["id"]
            entry["show_accent"] = False
            entry["accent_color"] = accent_from_hue(hue)
            entry["card_bg"] = card_bg_from_hue(hue)
            entry["card_border"] = card_border_from_hue(hue)
            entry["depth"] = 2
        elif target_type == "section" and section_ref:
            hue = section_ref.get("color_hue", 215)
            entry["section_id"] = section_ref["id"]
            entry["_card_group"] = section_ref["id"]
            entry["show_accent"] = True
            entry["accent_color"] = accent_from_hue(hue)
            entry["card_bg"] = card_bg_from_hue(hue)
            entry["card_border"] = card_border_from_hue(hue)
            entry["depth"] = 1
        else:
            entry["section_id"] = ""
            entry["_card_group"] = ""
            entry["show_accent"] = False
            entry["accent_color"] = ""
            entry["card_bg"] = _UNSECTIONED_BG
            entry["card_border"] = "transparent"
            entry["depth"] = 0

    def _count_children(self, section_idx: int) -> int:
        """Count entries belonging to the section/subsection at *section_idx*."""
        entry = self._entries[section_idx]
        if entry["type"] == "item":
            return 0
        sec_id = entry["id"]
        count = 0
        for i in range(section_idx + 1, len(self._entries)):
            e = self._entries[i]
            if entry["type"] == "section":
                if e.get("_card_group") == sec_id:
                    count += 1
                else:
                    break
            elif entry["type"] == "subsection":
                if (e["type"] == "item"
                        and e.get("section_id") == sec_id):
                    count += 1
                else:
                    break
        return count

    def _recompute_cards_around(self, lo: int, hi: int) -> None:
        """Recompute card_top / card_bottom / gap_above for range (calculated globally)."""
        # Save old values to detect changes
        old_vals = []
        for entry in self._entries:
            old_vals.append({
                "card_top": entry.get("card_top", False),
                "card_bottom": entry.get("card_bottom", False),
                "gap_above": entry.get("gap_above", 0),
                "sub_card_top": entry.get("sub_card_top", False),
                "sub_card_bottom": entry.get("sub_card_bottom", False),
            })

        # Reset properties first
        for entry in self._entries:
            entry["card_top"] = False
            entry["card_bottom"] = False
            entry["gap_above"] = 0
            entry["sub_card_top"] = False
            entry["sub_card_bottom"] = False

        prev_group = ""
        for i, entry in enumerate(self._entries):
            group = entry.get("_card_group", "")
            if group:
                if group != prev_group:
                    entry["card_top"] = True
                    entry["gap_above"] = 14 if i > 0 else 0
                next_group = (self._entries[i + 1].get("_card_group", "")
                              if i + 1 < len(self._entries) else "")
                if group != next_group:
                    entry["card_bottom"] = True
            else:
                entry["gap_above"] = 6 if i > 0 else 0
            prev_group = group

        prev_sub = ""
        for i, entry in enumerate(self._entries):
            is_sub = (entry["type"] == "subsection") or (entry["type"] == "item" and entry["depth"] == 2)
            if is_sub:
                sub_id = entry.get("section_id", "")
                if sub_id != prev_sub:
                    entry["sub_card_top"] = True
                next_is_sub = False
                next_sub_id = ""
                if i + 1 < len(self._entries):
                    next_entry = self._entries[i + 1]
                    next_is_sub = (next_entry["type"] == "subsection") or (next_entry["type"] == "item" and next_entry["depth"] == 2)
                    next_sub_id = next_entry.get("section_id", "")
                if not next_is_sub or next_sub_id != sub_id:
                    entry["sub_card_bottom"] = True
                prev_sub = sub_id
            else:
                prev_sub = ""

        # Emit dataChanged for any changed entry
        for i, entry in enumerate(self._entries):
            old = old_vals[i] if i < len(old_vals) else {
                "card_top": False,
                "card_bottom": False,
                "gap_above": 0,
                "sub_card_top": False,
                "sub_card_bottom": False,
            }
            if (entry["card_top"] != old["card_top"] or
                entry["card_bottom"] != old["card_bottom"] or
                entry["gap_above"] != old["gap_above"] or
                entry.get("sub_card_top", False) != old["sub_card_top"] or
                entry.get("sub_card_bottom", False) != old["sub_card_bottom"]):
                
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, [
                    self.CardTopRole, self.CardBottomRole, self.GapAboveRole,
                    self.SubCardTopRole, self.SubCardBottomRole
                ])


    # ── Collapse / expand ──────────────────────────────────────────────────

    def toggle_collapse(self, section_id: str) -> None:
        """Toggle collapsed state for a section or subsection."""
        # Find the section entry
        sec_idx = -1
        for i, e in enumerate(self._entries):
            if e["id"] == section_id and e["type"] in ("section", "subsection"):
                sec_idx = i
                break
        if sec_idx < 0:
            return

        entry = self._entries[sec_idx]
        is_collapsed = entry.get("collapsed", False)
        new_collapsed = not is_collapsed

        # Update the section dict
        sec_ref = entry.get("_section_ref")
        if sec_ref:
            sec_ref["collapsed"] = new_collapsed
        entry["collapsed"] = new_collapsed

        # Update the role
        idx = self.index(sec_idx)
        self.dataChanged.emit(idx, idx, [self.CollapsedRole])

        if new_collapsed:
            # Remove children
            children_count = self._count_children(sec_idx)
            if children_count > 0:
                self.beginRemoveRows(QModelIndex(),
                                     sec_idx + 1,
                                     sec_idx + children_count)
                for _ in range(children_count):
                    self._entries.pop(sec_idx + 1)
                self.endRemoveRows()
        else:
            # Re-insert children by partial rebuild
            self._expand_section(sec_idx)

        # Recompute card properties
        self._recompute_cards_around(
            max(0, sec_idx - 1),
            min(len(self._entries), sec_idx + 2))

    def _expand_section(self, sec_idx: int) -> None:
        """Re-insert children for an expanding section."""
        if not self._pl:
            return
        entry = self._entries[sec_idx]
        sec_id = entry["id"]
        sec_ref = entry.get("_section_ref")
        is_subsection = entry["type"] == "subsection"

        items = self._pl.get("items", [])
        sections_map = {s["id"]: s
                        for s in self._pl.get("sections", [])}

        children: list[dict] = []
        if is_subsection:
            # Only items directly in this subsection
            parent_sec = sections_map.get(entry.get("parent_id", ""))
            for item in items:
                if item.get("section_id") == sec_id:
                    children.append(self._make_item_entry(
                        item, depth=2, section=parent_sec,
                        show_accent=False))
        else:
            # Items and subsections in this section
            subsections_here = [s for s in self._pl.get("sections", [])
                                if s.get("parent_id") == sec_id]
            sub_ids = {s["id"] for s in subsections_here}
            sub_items: dict[str, list] = {sid: [] for sid in sub_ids}
            sec_items: list = []

            for item in items:
                isid = item.get("section_id")
                if isid == sec_id:
                    sec_items.append(item)
                elif isid in sub_ids:
                    sub_items[isid].append(item)

            # Rebuild in order (items first, then subsections — or interleaved
            # based on original position in items list)
            # We follow the items order to maintain interleaving
            opened_subs: set[str] = set()
            for item in items:
                isid = item.get("section_id")
                if isid == sec_id:
                    children.append(self._make_item_entry(
                        item, depth=1, section=sec_ref,
                        show_accent=True))
                elif isid in sub_ids:
                    if isid not in opened_subs:
                        sub_sec = sections_map[isid]
                        sub_count = len(sub_items.get(isid, []))
                        children.append(self._make_subsection_entry(
                            sub_sec, sub_count, sec_ref))
                        opened_subs.add(isid)
                        if not sub_sec.get("collapsed", False):
                            pass  # items follow
                    if not sections_map[isid].get("collapsed", False):
                        children.append(self._make_item_entry(
                            item, depth=2, section=sec_ref,
                            show_accent=False))

            # Add empty subsections not yet opened
            for sub in subsections_here:
                if sub["id"] not in opened_subs:
                    children.append(self._make_subsection_entry(
                        sub, 0, sec_ref))

        if children:
            self.beginInsertRows(QModelIndex(),
                                 sec_idx + 1,
                                 sec_idx + len(children))
            for i, child in enumerate(children):
                self._entries.insert(sec_idx + 1 + i, child)
            self.endInsertRows()

    # ── Thumbnail updates ──────────────────────────────────────────────────

    def update_thumb(self, item_id: str) -> None:
        """Bump the thumb_version for an item so QML reloads the image."""
        self._thumb_versions[item_id] = self._thumb_versions.get(item_id, 0) + 1
        for i, entry in enumerate(self._entries):
            if entry["type"] == "item" and entry["id"] == item_id:
                entry["thumb_version"] = self._thumb_versions[item_id]
                entry["thumb_source"] = (
                    f"image://playlistthumbs/{item_id}/"
                    f"{entry['thumb_version']}")
                idx = self.index(i)
                self.dataChanged.emit(
                    idx, idx,
                    [self.ThumbSourceRole, self.ThumbVersionRole])
                break

    def media_patch(self, item_id: str) -> dict:
        """Return the current QML-facing fields for one media item."""
        if not self._pl:
            return {}
        item = next(
            (it for it in self._pl.get("items", [])
             if it.get("id") == item_id),
            None,
        )
        return self._media_node(item) if item else {}

    def node_patch(self, node_id: str) -> dict:
        """Return the current QML-facing tree node for media or sections."""
        if not self._pl:
            return {}

        def find(nodes: list[dict]) -> dict:
            for node in nodes:
                if node.get("id") == node_id:
                    return node
                found = find(node.get("children", []))
                if found:
                    return found
            return {}

        return find(self.tree_data())

    def update_title(self, item_id: str, title: str) -> None:
        """Update item title in model."""
        for i, entry in enumerate(self._entries):
            if entry["type"] == "item" and entry["id"] == item_id:
                entry["title"] = title
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, [self.TitleRole])
                break

    # ── Cloud state ────────────────────────────────────────────────────────

    def update_cloud_state(self, url: str) -> None:
        """Refresh cloud download state for all items with this URL."""
        cm = MediaCacheManager.instance()
        is_remote = MediaCacheManager.is_remote(url)
        cached = cm.is_cached(url) if is_remote else True
        prefetching = cm.is_prefetching(url) if is_remote else False
        if cached or not prefetching:
            self._cloud_progress_by_url.pop(url, None)

        for i, entry in enumerate(self._entries):
            if entry["type"] == "item" and entry.get("url") == url:
                entry["cloud_visible"] = is_remote and not cached
                entry["cloud_active"] = prefetching
                entry["cloud_progress"] = self._cloud_progress_by_url.get(url, -1.0)
                if entry["cloud_visible"]:
                    entry["cloud_tooltip"] = (
                        tr_offline_downloading()
                        if prefetching else tr_offline_download())
                else:
                    entry["cloud_tooltip"] = ""
                idx = self.index(i)
                self.dataChanged.emit(
                    idx, idx,
                    [
                        self.CloudVisibleRole,
                        self.CloudActiveRole,
                        self.CloudProgressRole,
                        self.CloudTooltipRole,
                    ])

    def update_cloud_progress(self, url: str, pct: int) -> None:
        """Update download progress for items with this URL."""
        progress = max(0.0, min(1.0, pct / 100.0))
        self._cloud_progress_by_url[url] = progress
        for i, entry in enumerate(self._entries):
            if entry["type"] == "item" and entry.get("url") == url:
                entry["cloud_progress"] = progress
                entry["cloud_tooltip"] = tr_offline_downloading_progress(pct)
                idx = self.index(i)
                self.dataChanged.emit(
                    idx, idx,
                    [self.CloudProgressRole, self.CloudTooltipRole])

    def cloud_patches_for_url(self, url: str) -> list[dict]:
        """Return current QML cloud fields for every item with *url*."""
        if not self._pl:
            return []
        patches: list[dict] = []
        for item in self._pl.get("items", []):
            if item.get("url") == url:
                patches.append(self._media_node(item))
        return patches

    # ── Sync back to playlist data ─────────────────────────────────────────

    def finalize_drag(self) -> None:
        """Sync the visual model order back to the playlist data structure.

        Called once when drag ends (not on every move).
        """
        if not self._pl:
            return

        id_map = {it["id"]: it for it in self._pl.get("items", [])}
        new_items: list[dict] = []
        seen_item_ids: set[str] = set()

        for entry in self._entries:
            if entry["type"] == "item":
                item_id = entry["id"]
                item = id_map.get(item_id)
                if item:
                    item["section_id"] = entry.get("section_id") or None
                    new_items.append(item)
                    seen_item_ids.add(item_id)

        for item in self._pl.get("items", []):
            item_id = item.get("id")
            if item_id and item_id not in seen_item_ids:
                new_items.append(item)

        self._pl["items"] = new_items

        # Rebuild section order + positions + parent_ids
        sec_map = {s["id"]: s for s in self._pl.get("sections", [])}
        ordered_sections: list[dict] = []
        seen_secs: set[str] = set()
        item_counter = 0

        for entry in self._entries:
            if entry["type"] in ("section", "subsection"):
                sid = entry["id"]
                if sid in sec_map and sid not in seen_secs:
                    sec = sec_map[sid]
                    sec["position"] = item_counter
                    sec["collapsed"] = entry.get("collapsed", False)
                    # Update parent_id for subsections that were moved
                    if entry["type"] == "subsection":
                        new_parent = entry.get("parent_id", "")
                        sec["parent_id"] = new_parent or None
                    ordered_sections.append(sec)
                    seen_secs.add(sid)
            elif entry["type"] == "item":
                item_counter += 1

        for sec in self._pl.get("sections", []):
            if sec.get("id") not in seen_secs:
                ordered_sections.append(sec)

        self._pl["sections"] = ordered_sections
        self.orderSynced.emit()

    # ── Utility ────────────────────────────────────────────────────────────

    def entry_count(self) -> int:
        return len(self._entries)

    def entry_at(self, idx: int) -> dict | None:
        if 0 <= idx < len(self._entries):
            return self._entries[idx]
        return None

    def find_entry_index(self, entry_id: str) -> int:
        for i, e in enumerate(self._entries):
            if e["id"] == entry_id:
                return i
        return -1
