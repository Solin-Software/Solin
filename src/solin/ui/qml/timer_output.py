"""QML presentation adapter for projected timer output."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Property, Signal, Slot

from solin.core.timer.models import ClockConfig, TimerSnapshot
from solin.core.timer.render import build_render_model


class ClockRenderBridge(QObject):
    """Expose one shared timer render model to projected QML surfaces."""

    modelChanged = Signal()

    def __init__(
        self,
        engine,
        config_provider: Callable[[], ClockConfig],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config_provider = config_provider
        self._model: dict = {}
        self._engine.tick.connect(self._refresh)
        self.refresh_now()

    def _refresh(self, snapshot_data=None) -> None:
        snapshot = (
            TimerSnapshot.from_dict(snapshot_data)
            if isinstance(snapshot_data, dict)
            else self._engine.snapshot()
        )
        model = build_render_model(snapshot, self._config_provider())
        if model == self._model:
            return
        self._model = model
        self.modelChanged.emit()

    @Slot()
    def refresh_now(self) -> None:
        self._refresh()

    def _get_model(self) -> dict:
        return self._model

    model = Property("QVariant", _get_model, notify=modelChanged)
