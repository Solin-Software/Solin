from __future__ import annotations

from PySide6.QtWidgets import QComboBox


class NoScrollComboBox(QComboBox):
    """Ignore wheel events to avoid accidental selection changes."""

    def wheelEvent(self, event) -> None:
        event.ignore()
