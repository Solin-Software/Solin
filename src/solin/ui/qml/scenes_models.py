"""Stable QML-facing models for the Scenes workspace."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import IntEnum
from typing import Generic, TypeVar

from PySide6.QtCore import QAbstractListModel, QByteArray, QModelIndex, Qt, Signal, Slot


class _SceneRole(IntEnum):
    ID = int(Qt.ItemDataRole.UserRole) + 1
    NAME = ID + 1
    METADATA = NAME + 1
    DEFAULT = METADATA + 1
    MEDIA = DEFAULT + 1
    LIVE = MEDIA + 1
    TRANSITION_OVERRIDE = LIVE + 1
    TRANSITION_KIND = TRANSITION_OVERRIDE + 1
    TRANSITION_DURATION = TRANSITION_KIND + 1
    TRANSITION_LABEL = TRANSITION_DURATION + 1


class _LayerRole(IntEnum):
    ID = int(Qt.ItemDataRole.UserRole) + 1
    SOURCE_ID = ID + 1
    NAME = SOURCE_ID + 1
    KIND = NAME + 1
    VISIBLE = KIND + 1
    LOCKED = VISIBLE + 1
    X = LOCKED + 1
    Y = X + 1
    WIDTH = Y + 1
    HEIGHT = WIDTH + 1
    CROP_LEFT = HEIGHT + 1
    CROP_TOP = CROP_LEFT + 1
    CROP_RIGHT = CROP_TOP + 1
    CROP_BOTTOM = CROP_RIGHT + 1
    FIT_MODE = CROP_BOTTOM + 1
    ROTATION = FIT_MODE + 1
    OPACITY = ROTATION + 1
    COLOR = OPACITY + 1
    PTZ_AVAILABLE = COLOR + 1
    SOURCE_WARNING = PTZ_AVAILABLE + 1
    SOURCE_WARNING_TEXT = SOURCE_WARNING + 1


_SCENE_ROLES = {
    _SceneRole.ID: QByteArray(b"sceneId"),
    _SceneRole.NAME: QByteArray(b"sceneName"),
    _SceneRole.METADATA: QByteArray(b"sceneMetadata"),
    _SceneRole.DEFAULT: QByteArray(b"isDefaultScene"),
    _SceneRole.MEDIA: QByteArray(b"isMediaScene"),
    _SceneRole.LIVE: QByteArray(b"isLiveScene"),
    _SceneRole.TRANSITION_OVERRIDE: QByteArray(b"hasTransitionOverride"),
    _SceneRole.TRANSITION_KIND: QByteArray(b"transitionOverrideKind"),
    _SceneRole.TRANSITION_DURATION: QByteArray(b"transitionOverrideDurationMs"),
    _SceneRole.TRANSITION_LABEL: QByteArray(b"transitionOverrideLabel"),
}

_LAYER_ROLES = {
    _LayerRole.ID: QByteArray(b"layerId"),
    _LayerRole.SOURCE_ID: QByteArray(b"sourceId"),
    _LayerRole.NAME: QByteArray(b"layerName"),
    _LayerRole.KIND: QByteArray(b"sourceKind"),
    _LayerRole.VISIBLE: QByteArray(b"layerVisible"),
    _LayerRole.LOCKED: QByteArray(b"layerLocked"),
    _LayerRole.X: QByteArray(b"layerX"),
    _LayerRole.Y: QByteArray(b"layerY"),
    _LayerRole.WIDTH: QByteArray(b"layerWidth"),
    _LayerRole.HEIGHT: QByteArray(b"layerHeight"),
    _LayerRole.CROP_LEFT: QByteArray(b"cropLeft"),
    _LayerRole.CROP_TOP: QByteArray(b"cropTop"),
    _LayerRole.CROP_RIGHT: QByteArray(b"cropRight"),
    _LayerRole.CROP_BOTTOM: QByteArray(b"cropBottom"),
    _LayerRole.FIT_MODE: QByteArray(b"fitMode"),
    _LayerRole.ROTATION: QByteArray(b"rotationDegrees"),
    _LayerRole.OPACITY: QByteArray(b"layerOpacity"),
    _LayerRole.COLOR: QByteArray(b"sourceColor"),
    _LayerRole.PTZ_AVAILABLE: QByteArray(b"ptzAvailable"),
    _LayerRole.SOURCE_WARNING: QByteArray(b"sourceWarning"),
    _LayerRole.SOURCE_WARNING_TEXT: QByteArray(b"sourceWarningText"),
}

_RoleT = TypeVar("_RoleT", bound=IntEnum)


class _MappingListModel(QAbstractListModel, Generic[_RoleT]):
    countChanged = Signal()

    def __init__(
        self,
        roles: Mapping[_RoleT, QByteArray],
        keys: Mapping[_RoleT, str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._roles = dict(roles)
        self._keys = {int(role): key for role, key in keys.items()}
        self._items: list[dict[str, object]] = []

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802 - Qt override
        return {int(role): name for role, name in self._roles.items()}

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802 - Qt override
        return 0 if parent is not None and parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == int(Qt.ItemDataRole.DisplayRole):
            return item.get("name", "")
        try:
            key = self._keys[role]
        except KeyError:
            return None
        return item.get(key)

    @Slot(int, result="QVariantMap")
    def get(self, row: int) -> dict[str, object]:
        if not 0 <= row < len(self._items):
            return {}
        return dict(self._items[row])

    def replace_items(self, items: Sequence[Mapping[str, object]]) -> None:
        normalized = [dict(item) for item in items]
        if normalized == self._items:
            return
        old_ids = [item.get("id") for item in self._items]
        new_ids = [item.get("id") for item in normalized]
        same_identities = (
            len(normalized) == len(self._items)
            and len(set(old_ids)) == len(old_ids)
            and set(old_ids) == set(new_ids)
        )
        if same_identities:
            single_move = self._single_move(old_ids, new_ids)
            if single_move is not None:
                self._move_row(*single_move)
            else:
                for target_row, item_id in enumerate(new_ids):
                    current_row = next(
                        row
                        for row in range(target_row, len(self._items))
                        if self._items[row].get("id") == item_id
                    )
                    if current_row != target_row:
                        self._move_row(current_row, target_row)

            previous = self._items
            self._items = normalized
            self._emit_changed_roles(previous, normalized)
            return
        old_count = len(self._items)
        self.beginResetModel()
        self._items = normalized
        self.endResetModel()
        if old_count != len(self._items):
            self.countChanged.emit()

    def _emit_changed_roles(
        self,
        previous: Sequence[Mapping[str, object]],
        current: Sequence[Mapping[str, object]],
    ) -> None:
        for row, (old_item, new_item) in enumerate(zip(previous, current, strict=True)):
            changed_roles = [
                role
                for role, key in self._keys.items()
                if old_item.get(key) != new_item.get(key)
            ]
            if changed_roles:
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, changed_roles)

    def _move_row(self, current_row: int, target_row: int) -> None:
        destination_child = target_row if current_row > target_row else target_row + 1
        self.beginMoveRows(
            QModelIndex(),
            current_row,
            current_row,
            QModelIndex(),
            destination_child,
        )
        moved = self._items.pop(current_row)
        self._items.insert(target_row, moved)
        self.endMoveRows()

    @staticmethod
    def _single_move(
        old_ids: Sequence[object],
        new_ids: Sequence[object],
    ) -> tuple[int, int] | None:
        for current_row in range(len(old_ids)):
            for target_row in range(len(new_ids)):
                if current_row == target_row:
                    continue
                candidate = list(old_ids)
                moved = candidate.pop(current_row)
                candidate.insert(target_row, moved)
                if candidate == list(new_ids):
                    return current_row, target_row
        return None


class SceneListModel(_MappingListModel[_SceneRole]):
    def __init__(self, parent=None) -> None:
        super().__init__(
            _SCENE_ROLES,
            {
                _SceneRole.ID: "id",
                _SceneRole.NAME: "name",
                _SceneRole.METADATA: "metadata",
                _SceneRole.DEFAULT: "default",
                _SceneRole.MEDIA: "media",
                _SceneRole.LIVE: "live",
                _SceneRole.TRANSITION_OVERRIDE: "transition_override",
                _SceneRole.TRANSITION_KIND: "transition_kind",
                _SceneRole.TRANSITION_DURATION: "transition_duration_ms",
                _SceneRole.TRANSITION_LABEL: "transition_label",
            },
            parent,
        )


class SceneLayerListModel(_MappingListModel[_LayerRole]):
    def __init__(self, parent=None) -> None:
        super().__init__(
            _LAYER_ROLES,
            {
                _LayerRole.ID: "id",
                _LayerRole.SOURCE_ID: "source_id",
                _LayerRole.NAME: "name",
                _LayerRole.KIND: "kind",
                _LayerRole.VISIBLE: "visible",
                _LayerRole.LOCKED: "locked",
                _LayerRole.X: "x",
                _LayerRole.Y: "y",
                _LayerRole.WIDTH: "width",
                _LayerRole.HEIGHT: "height",
                _LayerRole.CROP_LEFT: "crop_left",
                _LayerRole.CROP_TOP: "crop_top",
                _LayerRole.CROP_RIGHT: "crop_right",
                _LayerRole.CROP_BOTTOM: "crop_bottom",
                _LayerRole.FIT_MODE: "fit_mode",
                _LayerRole.ROTATION: "rotation",
                _LayerRole.OPACITY: "opacity",
                _LayerRole.COLOR: "color",
                _LayerRole.PTZ_AVAILABLE: "ptz_available",
                _LayerRole.SOURCE_WARNING: "source_warning",
                _LayerRole.SOURCE_WARNING_TEXT: "source_warning_text",
            },
            parent,
        )


__all__ = ["SceneLayerListModel", "SceneListModel"]
