"""Immutable, versioned input contract for declarative media trees."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import math
from typing import Any, TypeAlias

from solin.ui.qml.media_tree.roles import DATA_ROLE_NAMES


class MediaTreeSnapshotError(ValueError):
    """Raised when a producer publishes an invalid media-tree snapshot."""


class MediaTreeNodeType(StrEnum):
    MEDIA = "media"
    SECTION = "section"
    SUBSECTION = "subsection"
    MARKER = "marker"


type ScalarRoleValue = str | bool | int | float | None


@dataclass(frozen=True, slots=True)
class FrozenRoleSequence:
    values: tuple["FrozenRoleValue", ...]


@dataclass(frozen=True, slots=True)
class FrozenRoleMap:
    items: tuple[tuple[str, "FrozenRoleValue"], ...]


type FrozenRoleValue = ScalarRoleValue | FrozenRoleSequence | FrozenRoleMap
type MutableRoleValue = ScalarRoleValue | Sequence[Any] | Mapping[str, Any]
RoleInput: TypeAlias = Mapping[str, MutableRoleValue]

_STRING_ROLES = frozenset({
    "title",
    "text",
    "mediaType",
    "badge",
    "duration",
    "thumbSource",
    "url",
    "trimSource",
    "cloudTooltip",
    "color",
    "textColor",
    "badgeBg",
    "subsectionId",
    "operationId",
    "operationState",
    "operationMessage",
})
_BOOL_ROLES = frozenset({
    "trimAvailable",
    "cloudVisible",
    "cloudActive",
    "isMissing",
    "hasCustomTrim",
    "collapsed",
    "operationCancellable",
    "canDrag",
    "canEdit",
    "canProject",
    "operationRetryable",
    "canRemove",
    "canDownload",
})
_NON_NEGATIVE_INT_ROLES = frozenset({
    "startTrimTicks",
    "endTrimTicks",
    "baseDurationTicks",
    "itemCount",
})
_PROGRESS_ROLES = frozenset({"cloudProgress", "operationProgress"})
_OPERATION_STATES = frozenset({
    "queued",
    "preparing",
    "copying",
    "processing",
    "finalizing",
    "failed",
    "ready",
})
_MEDIA_TYPES = frozenset({"audio", "image", "video"})
_REQUIRED_ROLES = {
    MediaTreeNodeType.MEDIA: frozenset({"title", "mediaType"}),
    MediaTreeNodeType.SECTION: frozenset({"title", "collapsed", "itemCount"}),
    MediaTreeNodeType.SUBSECTION: frozenset({"title", "collapsed", "itemCount"}),
    MediaTreeNodeType.MARKER: frozenset({"text"}),
}


def freeze_role_value(value: MutableRoleValue) -> FrozenRoleValue:
    """Deep-freeze a JSON-like role value and reject ambiguous runtime objects."""

    if isinstance(value, float) and not math.isfinite(value):
        raise MediaTreeSnapshotError("Role numbers must be finite")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        frozen_items: list[tuple[str, FrozenRoleValue]] = []
        for key, child in value.items():
            if not isinstance(key, str):
                raise MediaTreeSnapshotError("Role-object keys must be strings")
            frozen_items.append((key, freeze_role_value(child)))
        return FrozenRoleMap(tuple(sorted(frozen_items)))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return FrozenRoleSequence(tuple(freeze_role_value(child) for child in value))
    raise MediaTreeSnapshotError(
        f"Unsupported role value {type(value).__name__}; use JSON-like immutable data"
    )


def thaw_role_value(value: FrozenRoleValue) -> object:
    """Return a Qt/QML-compatible value from a frozen snapshot value."""

    if isinstance(value, FrozenRoleMap):
        return {key: thaw_role_value(child) for key, child in value.items}
    if isinstance(value, FrozenRoleSequence):
        return [thaw_role_value(child) for child in value.values]
    return value


def freeze_roles(roles: RoleInput | None = None) -> FrozenRoleMap:
    source = roles or {}
    unknown = set(source) - DATA_ROLE_NAMES
    if unknown:
        names = ", ".join(sorted(unknown))
        raise MediaTreeSnapshotError(f"Unknown media-tree roles: {names}")
    for name, value in source.items():
        _validate_role_value(name, value)
    frozen = freeze_role_value(source)
    if not isinstance(frozen, FrozenRoleMap):
        raise AssertionError("A role mapping must freeze to FrozenRoleMap")
    return frozen


def _validate_role_value(name: str, value: object) -> None:
    if name in _STRING_ROLES and not isinstance(value, str):
        raise MediaTreeSnapshotError(f"Role {name!r} must be a string")
    if name in _BOOL_ROLES and not isinstance(value, bool):
        raise MediaTreeSnapshotError(f"Role {name!r} must be a boolean")
    if name in _NON_NEGATIVE_INT_ROLES and (
        not isinstance(value, int) or isinstance(value, bool) or value < 0
    ):
        raise MediaTreeSnapshotError(f"Role {name!r} must be a non-negative integer")
    if name in _PROGRESS_ROLES:
        progress = float(value) if isinstance(value, (int, float)) else float("nan")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(progress)
            or (progress != -1.0 and not 0.0 <= progress <= 1.0)
        ):
            raise MediaTreeSnapshotError(
                f"Role {name!r} must be -1 or between 0 and 1"
            )
    if name == "imageFraming" and value is not None and not isinstance(value, Mapping):
        raise MediaTreeSnapshotError("Role 'imageFraming' must be an object or null")
    if name == "operationState" and value not in _OPERATION_STATES:
        raise MediaTreeSnapshotError(f"Unknown operation state {value!r}")
    if name == "mediaType" and value not in _MEDIA_TYPES:
        raise MediaTreeSnapshotError(f"Unknown media type {value!r}")


@dataclass(frozen=True, slots=True)
class MediaTreeNodeSnapshot:
    node_id: str
    node_type: MediaTreeNodeType
    source_revision: int = 0
    roles: FrozenRoleMap = field(default_factory=lambda: FrozenRoleMap(()))
    children: tuple["MediaTreeNodeSnapshot", ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise MediaTreeSnapshotError("Every media-tree node needs a non-empty ID")
        if not isinstance(self.node_type, MediaTreeNodeType):
            raise MediaTreeSnapshotError("node_type must be a MediaTreeNodeType")
        if (
            not isinstance(self.source_revision, int)
            or isinstance(self.source_revision, bool)
            or self.source_revision < 0
        ):
            raise MediaTreeSnapshotError("source_revision must be a non-negative integer")
        if not isinstance(self.roles, FrozenRoleMap):
            raise MediaTreeSnapshotError("roles must be created with freeze_roles()")
        role_names = [name for name, _value in self.roles.items]
        if len(role_names) != len(set(role_names)):
            raise MediaTreeSnapshotError(f"Node {self.node_id!r} contains duplicate roles")
        unknown = set(role_names) - DATA_ROLE_NAMES
        if unknown:
            names = ", ".join(sorted(unknown))
            raise MediaTreeSnapshotError(f"Unknown media-tree roles: {names}")
        missing = _REQUIRED_ROLES[self.node_type] - set(role_names)
        if missing:
            names = ", ".join(sorted(missing))
            raise MediaTreeSnapshotError(
                f"{self.node_type.value!r} node {self.node_id!r} is missing roles: {names}"
            )
        for name, value in self.roles.items:
            _validate_role_value(name, thaw_role_value(value))
        if not isinstance(self.children, tuple) or not all(
            isinstance(child, MediaTreeNodeSnapshot) for child in self.children
        ):
            raise MediaTreeSnapshotError("children must be a tuple of node snapshots")

    @classmethod
    def create(
        cls,
        node_id: str,
        node_type: MediaTreeNodeType,
        *,
        source_revision: int = 0,
        roles: RoleInput | None = None,
        children: Sequence["MediaTreeNodeSnapshot"] = (),
    ) -> "MediaTreeNodeSnapshot":
        return cls(
            node_id=node_id,
            node_type=node_type,
            source_revision=source_revision,
            roles=freeze_roles(roles),
            children=tuple(children),
        )

    def thawed_roles(self) -> dict[str, object]:
        return {
            name: thaw_role_value(value)
            for name, value in self.roles.items
        }


@dataclass(frozen=True, slots=True)
class MediaTreeSnapshot:
    tree_id: str
    revision: int
    roots: tuple[MediaTreeNodeSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tree_id, str) or not self.tree_id.strip():
            raise MediaTreeSnapshotError("A media-tree snapshot needs a non-empty tree ID")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 0
        ):
            raise MediaTreeSnapshotError("revision must be a non-negative integer")
        if not isinstance(self.roots, tuple) or not all(
            isinstance(root, MediaTreeNodeSnapshot) for root in self.roots
        ):
            raise MediaTreeSnapshotError("roots must be a tuple of node snapshots")
        self._validate_tree()

    @classmethod
    def create(
        cls,
        tree_id: str,
        revision: int,
        roots: Sequence[MediaTreeNodeSnapshot] = (),
    ) -> "MediaTreeSnapshot":
        return cls(tree_id=tree_id, revision=revision, roots=tuple(roots))

    def _validate_tree(self) -> None:
        seen_ids: set[str] = set()
        seen_objects: set[int] = set()
        stack: list[
            tuple[MediaTreeNodeSnapshot, MediaTreeNodeType | None]
        ] = [(node, None) for node in reversed(self.roots)]
        while stack:
            node, parent_type = stack.pop()
            object_id = id(node)
            if object_id in seen_objects:
                raise MediaTreeSnapshotError(
                    f"Node object {node.node_id!r} is reused or cyclic"
                )
            seen_objects.add(object_id)
            if node.node_id in seen_ids:
                raise MediaTreeSnapshotError(f"Duplicate node ID {node.node_id!r}")
            seen_ids.add(node.node_id)
            self._validate_placement(node, parent_type)
            self._validate_item_count(node)
            stack.extend((child, node.node_type) for child in reversed(node.children))

    @staticmethod
    def _validate_item_count(node: MediaTreeNodeSnapshot) -> None:
        role_value = next(
            (value for name, value in node.roles.items if name == "itemCount"),
            None,
        )
        if role_value is None:
            return
        actual = sum(
            1
            for child in node.children
            if child.node_type == MediaTreeNodeType.MEDIA
        ) + sum(
            sum(
                1
                for grandchild in child.children
                if grandchild.node_type == MediaTreeNodeType.MEDIA
            )
            for child in node.children
            if child.node_type == MediaTreeNodeType.SUBSECTION
        )
        if role_value != actual:
            raise MediaTreeSnapshotError(
                f"Node {node.node_id!r} itemCount is {role_value}, expected {actual}"
            )

    @staticmethod
    def _validate_placement(
        node: MediaTreeNodeSnapshot,
        parent_type: MediaTreeNodeType | None,
    ) -> None:
        allowed_parents: dict[MediaTreeNodeType, frozenset[MediaTreeNodeType | None]] = {
            MediaTreeNodeType.MEDIA: frozenset({
                None,
                MediaTreeNodeType.SECTION,
                MediaTreeNodeType.SUBSECTION,
            }),
            MediaTreeNodeType.SECTION: frozenset({None}),
            MediaTreeNodeType.SUBSECTION: frozenset({MediaTreeNodeType.SECTION}),
            MediaTreeNodeType.MARKER: frozenset({MediaTreeNodeType.SUBSECTION}),
        }
        if parent_type not in allowed_parents[node.node_type]:
            parent_name = "root" if parent_type is None else parent_type.value
            raise MediaTreeSnapshotError(
                f"{node.node_type.value!r} node {node.node_id!r} cannot be inside {parent_name!r}"
            )
        if node.node_type in {
            MediaTreeNodeType.MEDIA,
            MediaTreeNodeType.MARKER,
        } and node.children:
            raise MediaTreeSnapshotError(
                f"{node.node_type.value!r} node {node.node_id!r} cannot have children"
            )
