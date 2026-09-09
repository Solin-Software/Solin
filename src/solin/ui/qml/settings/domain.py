"""Shared reactive state boundary for settings presentation domains."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Property, Signal, Slot


class SettingsDomain(QObject):
    stateChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state: dict[str, Any] = {"feedback": "", "feedbackKind": ""}

    @Property("QVariantMap", notify=stateChanged)
    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def publish(self, **values: Any) -> None:
        if any(self._state.get(key) != value for key, value in values.items()):
            self._state.update(values)
            self.stateChanged.emit()

    def fail(self, message: str) -> None:
        self.publish(feedback=message, feedbackKind="error")

    def succeed(self, message: str) -> None:
        self.publish(feedback=message, feedbackKind="success")

    @Slot(str, "QVariant")
    def setValue(self, key: str, value: Any) -> None:  # noqa: N802 - QML API
        raise ValueError(f"Unknown settings field: {key}")

    @Slot(str)
    def invoke(self, action: str) -> None:
        raise ValueError(f"Unknown settings action: {action}")
