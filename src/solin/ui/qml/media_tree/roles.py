"""Canonical roles exposed by the declarative media tree model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType
from typing import Final

from PySide6.QtCore import Qt


class MediaTreeRole(IntEnum):
    """Stable Qt role identifiers shared by every media-tree producer."""

    NODE_ID = Qt.ItemDataRole.UserRole + 1
    NODE_TYPE = Qt.ItemDataRole.UserRole + 2
    SOURCE_REVISION = Qt.ItemDataRole.UserRole + 3
    TITLE = Qt.ItemDataRole.UserRole + 4
    TEXT = Qt.ItemDataRole.UserRole + 5
    MEDIA_TYPE = Qt.ItemDataRole.UserRole + 6
    BADGE = Qt.ItemDataRole.UserRole + 7
    DURATION = Qt.ItemDataRole.UserRole + 8
    THUMB_SOURCE = Qt.ItemDataRole.UserRole + 9
    URL = Qt.ItemDataRole.UserRole + 10
    TRIM_SOURCE = Qt.ItemDataRole.UserRole + 11
    TRIM_AVAILABLE = Qt.ItemDataRole.UserRole + 12
    CLOUD_VISIBLE = Qt.ItemDataRole.UserRole + 13
    CLOUD_ACTIVE = Qt.ItemDataRole.UserRole + 14
    CLOUD_PROGRESS = Qt.ItemDataRole.UserRole + 15
    CLOUD_TOOLTIP = Qt.ItemDataRole.UserRole + 16
    IS_MISSING = Qt.ItemDataRole.UserRole + 17
    START_TRIM_TICKS = Qt.ItemDataRole.UserRole + 18
    END_TRIM_TICKS = Qt.ItemDataRole.UserRole + 19
    BASE_DURATION_TICKS = Qt.ItemDataRole.UserRole + 20
    HAS_CUSTOM_TRIM = Qt.ItemDataRole.UserRole + 21
    IMAGE_FRAMING = Qt.ItemDataRole.UserRole + 22
    COLOR = Qt.ItemDataRole.UserRole + 23
    TEXT_COLOR = Qt.ItemDataRole.UserRole + 24
    BADGE_BG = Qt.ItemDataRole.UserRole + 25
    COLLAPSED = Qt.ItemDataRole.UserRole + 26
    ITEM_COUNT = Qt.ItemDataRole.UserRole + 27
    SUBSECTION_ID = Qt.ItemDataRole.UserRole + 28
    OPERATION_ID = Qt.ItemDataRole.UserRole + 29
    OPERATION_STATE = Qt.ItemDataRole.UserRole + 30
    OPERATION_PROGRESS = Qt.ItemDataRole.UserRole + 31
    OPERATION_MESSAGE = Qt.ItemDataRole.UserRole + 32
    OPERATION_CANCELLABLE = Qt.ItemDataRole.UserRole + 33
    CAN_DRAG = Qt.ItemDataRole.UserRole + 34
    CAN_EDIT = Qt.ItemDataRole.UserRole + 35
    CAN_PROJECT = Qt.ItemDataRole.UserRole + 36
    OPERATION_RETRYABLE = Qt.ItemDataRole.UserRole + 37
    CAN_REMOVE = Qt.ItemDataRole.UserRole + 38
    CAN_DOWNLOAD = Qt.ItemDataRole.UserRole + 39
    CAN_SET_AS_IDLE = Qt.ItemDataRole.UserRole + 40


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    role: MediaTreeRole
    name: str
    default: object


ROLE_DEFINITIONS: Final[tuple[RoleDefinition, ...]] = (
    RoleDefinition(MediaTreeRole.NODE_ID, "nodeId", ""),
    RoleDefinition(MediaTreeRole.NODE_TYPE, "nodeType", ""),
    RoleDefinition(MediaTreeRole.SOURCE_REVISION, "sourceRevision", 0),
    RoleDefinition(MediaTreeRole.TITLE, "title", ""),
    RoleDefinition(MediaTreeRole.TEXT, "text", ""),
    RoleDefinition(MediaTreeRole.MEDIA_TYPE, "mediaType", ""),
    RoleDefinition(MediaTreeRole.BADGE, "badge", ""),
    RoleDefinition(MediaTreeRole.DURATION, "duration", ""),
    RoleDefinition(MediaTreeRole.THUMB_SOURCE, "thumbSource", ""),
    RoleDefinition(MediaTreeRole.URL, "url", ""),
    RoleDefinition(MediaTreeRole.TRIM_SOURCE, "trimSource", ""),
    RoleDefinition(MediaTreeRole.TRIM_AVAILABLE, "trimAvailable", False),
    RoleDefinition(MediaTreeRole.CLOUD_VISIBLE, "cloudVisible", False),
    RoleDefinition(MediaTreeRole.CLOUD_ACTIVE, "cloudActive", False),
    RoleDefinition(MediaTreeRole.CLOUD_PROGRESS, "cloudProgress", -1.0),
    RoleDefinition(MediaTreeRole.CLOUD_TOOLTIP, "cloudTooltip", ""),
    RoleDefinition(MediaTreeRole.IS_MISSING, "isMissing", False),
    RoleDefinition(MediaTreeRole.START_TRIM_TICKS, "startTrimTicks", 0),
    RoleDefinition(MediaTreeRole.END_TRIM_TICKS, "endTrimTicks", 0),
    RoleDefinition(MediaTreeRole.BASE_DURATION_TICKS, "baseDurationTicks", 0),
    RoleDefinition(MediaTreeRole.HAS_CUSTOM_TRIM, "hasCustomTrim", False),
    RoleDefinition(MediaTreeRole.IMAGE_FRAMING, "imageFraming", None),
    RoleDefinition(MediaTreeRole.COLOR, "color", ""),
    RoleDefinition(MediaTreeRole.TEXT_COLOR, "textColor", ""),
    RoleDefinition(MediaTreeRole.BADGE_BG, "badgeBg", ""),
    RoleDefinition(MediaTreeRole.COLLAPSED, "collapsed", False),
    RoleDefinition(MediaTreeRole.ITEM_COUNT, "itemCount", 0),
    RoleDefinition(MediaTreeRole.SUBSECTION_ID, "subsectionId", ""),
    RoleDefinition(MediaTreeRole.OPERATION_ID, "operationId", ""),
    RoleDefinition(MediaTreeRole.OPERATION_STATE, "operationState", "ready"),
    RoleDefinition(MediaTreeRole.OPERATION_PROGRESS, "operationProgress", -1.0),
    RoleDefinition(MediaTreeRole.OPERATION_MESSAGE, "operationMessage", ""),
    RoleDefinition(MediaTreeRole.OPERATION_CANCELLABLE, "operationCancellable", False),
    RoleDefinition(MediaTreeRole.CAN_DRAG, "canDrag", True),
    RoleDefinition(MediaTreeRole.CAN_EDIT, "canEdit", True),
    RoleDefinition(MediaTreeRole.CAN_PROJECT, "canProject", True),
    RoleDefinition(MediaTreeRole.OPERATION_RETRYABLE, "operationRetryable", False),
    RoleDefinition(MediaTreeRole.CAN_REMOVE, "canRemove", True),
    RoleDefinition(MediaTreeRole.CAN_DOWNLOAD, "canDownload", False),
    RoleDefinition(MediaTreeRole.CAN_SET_AS_IDLE, "canSetAsIdle", False),
)

ROLE_NAMES: Final = MappingProxyType(
    {int(definition.role): definition.name.encode("utf-8") for definition in ROLE_DEFINITIONS}
)
ROLE_BY_NAME: Final = MappingProxyType(
    {definition.name: definition.role for definition in ROLE_DEFINITIONS}
)
ROLE_DEFAULTS: Final = MappingProxyType(
    {definition.name: definition.default for definition in ROLE_DEFINITIONS}
)

IDENTITY_ROLE_NAMES: Final = frozenset({"nodeId", "nodeType", "sourceRevision"})
DATA_ROLE_NAMES: Final = frozenset(ROLE_BY_NAME) - IDENTITY_ROLE_NAMES
