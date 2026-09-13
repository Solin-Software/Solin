"""
tree_builder.py - Solin
===============================
Transforms already parsed meeting data into canonical playlist-shaped trees.

This module intentionally does not parse .jwpub files.  It consumes WeekData,
MemorialData, and MeetingMedia produced by the existing services.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from .models import MeetingMedia, MemorialData, WeekData
from .section_meta import SECTION_META
from .tree_types import Node, clean_dict, stable_node_id, source_hash, publication_subsection_key

_STRIP = re.compile(r"<[^>]+>")


def _strip(value: str) -> str:
    return _STRIP.sub("", value or "").strip()


def _media_title(item: MeetingMedia, fallback_title: str) -> str:
    return _strip(item.label or item.caption) or fallback_title


def _media_type(item: MeetingMedia) -> str:
    mime = (item.mime_type or "").lower()
    if "image" in mime:
        return "image"
    if "audio" in mime:
        return "audio"
    return "video"


def _media_identity(item: MeetingMedia) -> str:
    if item.multimedia_id:
        return f"mm{item.multimedia_id}"
    if item.key_symbol or item.track or item.issue_tag:
        return f"{item.key_symbol}:{item.track}:{item.issue_tag}"
    if item.meps_doc_id:
        return f"doc{item.meps_doc_id}"
    if item.file_path:
        return f"path:{source_hash(item.file_path)[:16]}"
    return source_hash(clean_dict(item))[:20]


def _media_to_ref(item: MeetingMedia) -> dict[str, Any]:
    ref = clean_dict(item)
    for field in (
        "start_trim_ticks",
        "end_trim_ticks",
        "base_duration_ticks",
    ):
        ref.pop(field, None)
    if item.key_symbol or item.meps_doc_id:
        ref["jw_identity_authoritative"] = True
    return ref


def _section_node(
    meeting_type: str,
    section_code: str,
    *,
    title: str | None = None,
    hue: int | None = None,
    children: list[Node] | None = None,
    collapsed: bool | None = None,
) -> Node:
    source_key = f"section:{meeting_type}:{section_code}"
    label, default_hue = SECTION_META.get(section_code, (section_code, 215))
    payload = {
        "type": "section",
        "title": title or label,
        "section_code": section_code,
        "color_hue": default_hue if hue is None else hue,
    }
    return {
        "id": stable_node_id(source_key),
        "type": "section",
        "title": payload["title"],
        "color_hue": payload["color_hue"],
        "collapsed": not bool(children) if collapsed is None else collapsed,
        "children": children or [],
        "meeting_generated": True,
        "meeting_source_key": source_key,
        "meeting_source_hash": source_hash(payload),
    }


def _subsection_node(
    source_key: str,
    title: str,
    *,
    hue: int,
    children: list[Node] | None = None,
    collapsed: bool | None = None,
) -> Node:
    payload = {"type": "subsection", "title": title, "color_hue": hue}
    node_children = children or []
    return {
        "id": stable_node_id(source_key),
        "type": "subsection",
        "title": title,
        "color_hue": hue,
        "collapsed": not bool(node_children) if collapsed is None else collapsed,
        "children": node_children,
        "meeting_generated": True,
        "meeting_source_key": source_key,
        "meeting_source_hash": source_hash(payload),
    }


def _marker_node(source_key: str, text: str) -> Node:
    payload = {"type": "marker", "text": text}
    return {
        "id": stable_node_id(source_key),
        "type": "marker",
        "text": text,
        "children": [],
        "meeting_generated": True,
        "meeting_source_key": source_key,
        "meeting_source_hash": source_hash(payload),
    }


def _media_node(
    source_key: str,
    item: MeetingMedia,
    *,
    scope: str,
    fallback_title: str,
) -> Node:
    ref = _media_to_ref(item)
    title = _media_title(item, fallback_title)
    has_source_title = bool(_strip(item.label or item.caption))
    payload = {
        "type": "media",
        "title": title,
        "media_type": _media_type(item),
        "media_ref": ref,
        "scope": scope,
    }
    node: Node = {
        "id": stable_node_id(source_key),
        "type": "media",
        "title": title,
        "auto_title": not has_source_title,
        "media_type": payload["media_type"],
        "media_ref": ref,
        "children": [],
        "meeting_generated": True,
        "meeting_source_key": source_key,
        "meeting_source_hash": source_hash(payload),
    }
    for field in (
        "start_trim_ticks",
        "end_trim_ticks",
        "base_duration_ticks",
    ):
        value = int(getattr(item, field, 0) or 0)
        if value:
            node[field] = value
    return node


def _ref_attr(ref: Any, name: str, default: Any = None) -> Any:
    if isinstance(ref, dict):
        return ref.get(name, default)
    return getattr(ref, name, default)


class MeetingTreeBuilder:
    """Build canonical meeting trees from service data objects."""

    def __init__(
        self,
        *,
        media_fallback_title: Callable[[], str] | None = None,
    ) -> None:
        self._media_fallback_title = media_fallback_title or (lambda: "Media")

    def build_midweek(self, wd: WeekData) -> list[Node]:
        cbs_start = 999999
        if wd.cbs_ref:
            cbs_start = int(wd.cbs_ref.get("cbs_start", cbs_start))

        grouped: dict[str, list[MeetingMedia]] = {"tgw": [], "ayfm": [], "lac": []}
        for item in wd.mwb_all_media:
            section = item.section or "tgw"
            grouped.setdefault(section, []).append(item)

        publication_subsections = self._build_publication_subsections(wd)
        has_cbs_publication_refs = any(
            bool(_ref_attr(ref, "is_cbs", False))
            and bool(list(_ref_attr(ref, "items", []) or []))
            for ref in getattr(wd, "mwb_publication_refs", []) or []
        )

        nodes: list[Node] = []
        for code in ("tgw", "ayfm", "lac"):
            entries: list[tuple[int, int, Node]] = []
            seq = 0
            for item in grouped.get(code, []):
                entries.append((
                    int(item.begin_ordinal or 0),
                    seq,
                    _media_node(
                        f"media:mwb:{code}:{_media_identity(item)}",
                        item,
                        scope=f"mwb:{code}",
                        fallback_title=self._media_fallback_title(),
                    ),
                ))
                seq += 1
            for ordinal, subsection in publication_subsections.get(code, []):
                entries.append((ordinal, seq, subsection))
                seq += 1
            if code == "lac" and wd.cbs_ref and not has_cbs_publication_refs:
                cbs_subsection = self._build_cbs_subsection(wd)
                if cbs_subsection:
                    entries.append((cbs_start, seq, cbs_subsection))
            children = [
                node for _, _, node in sorted(
                    entries,
                    key=lambda entry: (entry[0], entry[1]),
                )
            ]
            nodes.append(
                _section_node(
                    "mwb",
                    code,
                    children=children,
                )
            )
        return nodes

    def build_weekend(self, wd: WeekData) -> list[Node]:
        children = [
            _media_node(
                f"media:wt:{_media_identity(item)}",
                item,
                scope="wt",
                fallback_title=self._media_fallback_title(),
            )
            for item in wd.wt_all_media
        ]
        return [
            _section_node(
                "wt",
                "public_talk",
                children=[],
                collapsed=False,
            ),
            _section_node(
                "wt",
                "wt",
                children=children,
            ),
        ]

    def build_memorial(self, md: MemorialData) -> list[Node]:
        children = [
            _media_node(
                f"media:memorial:{_media_identity(item)}",
                item,
                scope="memorial",
                fallback_title=self._media_fallback_title(),
            )
            for item in getattr(md, "videos", [])
        ]
        return [
            _section_node(
                "memorial",
                "memorial",
                children=children,
            )
        ]

    def _build_cbs_subsection(self, wd: WeekData) -> Node | None:
        ref = wd.cbs_ref or {}
        pub = str(ref.get("pub") or "cbs").strip() or "cbs"
        title = " ".join(str(ref.get("publication_title") or pub).split()) or pub
        doc_titles = ref.get("doc_titles") or {}
        title_to_doc_id = {
            _strip(str(value)): int(key)
            for key, value in doc_titles.items()
            if str(value).strip()
        }

        grouped: "OrderedDict[str, list[MeetingMedia]]" = OrderedDict()
        for item in wd.cbs_items:
            story = _strip(item.cbs_article_title or ref.get("title", ""))
            grouped.setdefault(story, []).append(item)

        if not grouped:
            return None

        children: list[Node] = []
        for idx, (story_title, items) in enumerate(grouped.items()):
            story_id = title_to_doc_id.get(story_title, 0)
            marker_key = (
                f"marker:cbs:{story_id}" if story_id else
                f"marker:cbs:{source_hash(story_title)[:16]}:{idx}"
            )
            if story_title:
                children.append(_marker_node(marker_key, story_title))
            for item in items:
                media_key = (
                    f"media:cbs:{pub}:{story_id or idx}:{_media_identity(item)}"
                )
                children.append(
                    _media_node(
                        media_key,
                        item,
                        scope=f"cbs:{pub}",
                        fallback_title=self._media_fallback_title(),
                    )
                )

        return _subsection_node(
            publication_subsection_key(f"subsection:cbs:{pub}", title),
            title,
            hue=275,
            children=children,
            collapsed=False,
        )

    def _build_publication_subsections(
        self,
        wd: WeekData,
    ) -> dict[str, list[tuple[int, Node]]]:
        grouped: "OrderedDict[tuple[bool, str, str, str], list[Any]]" = OrderedDict()
        for ref in getattr(wd, "mwb_publication_refs", []) or []:
            items = list(_ref_attr(ref, "items", []) or [])
            if not items:
                continue
            is_cbs = bool(_ref_attr(ref, "is_cbs", False))
            section = str(_ref_attr(ref, "section", "") or "lac")
            pub = str(_ref_attr(ref, "pub", "") or "ref").strip() or "ref"
            title = " ".join(
                str(_ref_attr(ref, "publication_title", "") or pub).split()
            ) or pub
            key = (is_cbs, section, pub, title)
            grouped.setdefault(key, []).append(ref)

        by_section: dict[str, list[tuple[int, Node]]] = {}
        for (is_cbs, section, pub, title), refs in grouped.items():
            children: list[Node] = []
            first_ordinal = 999999
            for idx, ref in enumerate(sorted(
                refs,
                key=lambda item: int(_ref_attr(item, "begin_ordinal", 0) or 0),
            )):
                ordinal = int(_ref_attr(ref, "begin_ordinal", 0) or 0)
                first_ordinal = min(first_ordinal, ordinal)
                meps_doc_id = int(_ref_attr(ref, "meps_doc_id", 0) or 0)
                caption = _strip(str(_ref_attr(ref, "caption", "") or title))
                if is_cbs:
                    marker_key = (
                        f"marker:cbs:{meps_doc_id}"
                        if meps_doc_id else
                        f"marker:cbs:{source_hash(caption)[:16]}:{idx}"
                    )
                else:
                    marker_key = (
                        f"marker:ref:{section}:{pub}:{meps_doc_id}"
                        if meps_doc_id else
                        f"marker:ref:{section}:{pub}:{source_hash(caption)[:16]}:{idx}"
                    )
                if caption:
                    children.append(_marker_node(marker_key, caption))
                for item in list(_ref_attr(ref, "items", []) or []):
                    if is_cbs:
                        media_key = (
                            f"media:cbs:{pub}:{meps_doc_id or idx}:"
                            f"{_media_identity(item)}"
                        )
                        scope = f"cbs:{pub}"
                    else:
                        media_key = (
                            f"media:ref:{section}:{pub}:{meps_doc_id or idx}:"
                            f"{_media_identity(item)}"
                        )
                        scope = f"ref:{section}:{pub}"
                    children.append(
                        _media_node(
                            media_key,
                            item,
                            scope=scope,
                            fallback_title=self._media_fallback_title(),
                        )
                    )
            if not children:
                continue
            hue = 275 if is_cbs else SECTION_META.get(section, ("", 215))[1]
            subsection_key = (
                f"subsection:cbs:{pub}"
                if is_cbs else
                f"subsection:ref:{section}:{pub}"
            )
            subsection = _subsection_node(
                publication_subsection_key(subsection_key, title),
                title,
                hue=hue,
                children=children,
                collapsed=False if is_cbs else section != "lac",
            )
            by_section.setdefault(section, []).append((first_ordinal, subsection))
        return by_section

    def canonical_hash(self, nodes: list[Node]) -> str:
        return source_hash(nodes)
