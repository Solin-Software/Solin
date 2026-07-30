"""Qt list model for the talk-theme layer stack."""

from __future__ import annotations

from enum import IntEnum

from PySide6.QtCore import QAbstractListModel, QByteArray, QModelIndex, Qt, Signal, Slot

from solin.core.talk_theme.models import TextLayer


class _LayerRole(IntEnum):
    ID = int(Qt.ItemDataRole.UserRole) + 1
    NAME = ID + 1
    TEXT = NAME + 1
    TEMPLATE_KEY = TEXT + 1
    X = TEMPLATE_KEY + 1
    Y = X + 1
    WIDTH = Y + 1
    FONT_SIZE = WIDTH + 1
    FONT_FAMILY = FONT_SIZE + 1
    FONT_WEIGHT = FONT_FAMILY + 1
    COLOR = FONT_WEIGHT + 1
    ALIGNMENT = COLOR + 1
    VISIBLE = ALIGNMENT + 1
    LETTER_SPACING = VISIBLE + 1
    LINE_HEIGHT = LETTER_SPACING + 1
    SNAP_X = LINE_HEIGHT + 1
    SNAP_Y = SNAP_X + 1


_ROLE_NAMES = {
    _LayerRole.ID: QByteArray(b"layerId"),
    _LayerRole.NAME: QByteArray(b"layerName"),
    _LayerRole.TEXT: QByteArray(b"layerText"),
    _LayerRole.TEMPLATE_KEY: QByteArray(b"templateKey"),
    _LayerRole.X: QByteArray(b"layerX"),
    _LayerRole.Y: QByteArray(b"layerY"),
    _LayerRole.WIDTH: QByteArray(b"layerWidth"),
    _LayerRole.FONT_SIZE: QByteArray(b"fontSize"),
    _LayerRole.FONT_FAMILY: QByteArray(b"fontFamily"),
    _LayerRole.FONT_WEIGHT: QByteArray(b"fontWeight"),
    _LayerRole.COLOR: QByteArray(b"textColor"),
    _LayerRole.ALIGNMENT: QByteArray(b"textAlignment"),
    _LayerRole.VISIBLE: QByteArray(b"layerVisible"),
    _LayerRole.LETTER_SPACING: QByteArray(b"letterSpacing"),
    _LayerRole.LINE_HEIGHT: QByteArray(b"lineHeight"),
    _LayerRole.SNAP_X: QByteArray(b"snapX"),
    _LayerRole.SNAP_Y: QByteArray(b"snapY"),
}


def _role_value(layer: TextLayer, role: int) -> object:
    values: dict[_LayerRole, object] = {
        _LayerRole.ID: layer.id,
        _LayerRole.NAME: layer.name,
        _LayerRole.TEXT: layer.text,
        _LayerRole.TEMPLATE_KEY: layer.template_key,
        _LayerRole.X: layer.x,
        _LayerRole.Y: layer.y,
        _LayerRole.WIDTH: layer.width,
        _LayerRole.FONT_SIZE: layer.font_size,
        _LayerRole.FONT_FAMILY: layer.font_family,
        _LayerRole.FONT_WEIGHT: layer.font_weight,
        _LayerRole.COLOR: layer.color,
        _LayerRole.ALIGNMENT: layer.alignment,
        _LayerRole.VISIBLE: layer.visible,
        _LayerRole.LETTER_SPACING: layer.letter_spacing,
        _LayerRole.LINE_HEIGHT: layer.line_height,
        _LayerRole.SNAP_X: layer.snap_x,
        _LayerRole.SNAP_Y: layer.snap_y,
    }
    try:
        return values[_LayerRole(role)]
    except (ValueError, KeyError):
        return None


class TextLayerListModel(QAbstractListModel):
    """Stable QML-facing model; row zero is the front-most layer."""

    countChanged = Signal()

    def __init__(
        self,
        layers: tuple[TextLayer, ...] = (),
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._layers = list(layers)

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802 - Qt override
        return {int(role): name for role, name in _ROLE_NAMES.items()}

    def rowCount(self, parent=None) -> int:  # noqa: N802 - Qt override
        return 0 if parent is not None and parent.isValid() else len(self._layers)

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid() or not 0 <= index.row() < len(self._layers):
            return None
        if role == int(Qt.ItemDataRole.DisplayRole):
            return self._layers[index.row()].name
        return _role_value(self._layers[index.row()], role)

    @Slot(int, result="QVariantMap")
    def get(self, row: int) -> dict[str, object]:
        if not 0 <= row < len(self._layers):
            return {}
        return self._layers[row].to_record()

    def layers(self) -> tuple[TextLayer, ...]:
        return tuple(self._layers)

    def layer(self, layer_id: str) -> TextLayer | None:
        return next((layer for layer in self._layers if layer.id == layer_id), None)

    def row_for_id(self, layer_id: str) -> int:
        return next(
            (index for index, layer in enumerate(self._layers) if layer.id == layer_id),
            -1,
        )

    def replace_layers(
        self,
        layers: tuple[TextLayer, ...],
        *,
        force_reset: bool = False,
    ) -> None:
        old_ids = tuple(layer.id for layer in self._layers)
        new_ids = tuple(layer.id for layer in layers)
        if force_reset or old_ids != new_ids:
            old_count = len(self._layers)
            self.beginResetModel()
            self._layers = list(layers)
            self.endResetModel()
            if old_count != len(self._layers):
                self.countChanged.emit()
            return

        old_layers = tuple(self._layers)
        self._layers = list(layers)
        for row, (before, after) in enumerate(zip(old_layers, layers, strict=True)):
            if before == after:
                continue
            changed_roles = [
                int(role)
                for role in _ROLE_NAMES
                if _role_value(before, int(role)) != _role_value(after, int(role))
            ]
            model_index = self.index(row, 0)
            self.dataChanged.emit(model_index, model_index, changed_roles)

    def move_layer(self, source_row: int, target_row: int) -> bool:
        if not (
            0 <= source_row < len(self._layers)
            and 0 <= target_row < len(self._layers)
            and source_row != target_row
        ):
            return False
        destination_child = target_row + 1 if source_row < target_row else target_row
        self.beginMoveRows(
            QModelIndex(),
            source_row,
            source_row,
            QModelIndex(),
            destination_child,
        )
        layer = self._layers.pop(source_row)
        self._layers.insert(target_row, layer)
        self.endMoveRows()
        return True


__all__ = ["TextLayerListModel"]
