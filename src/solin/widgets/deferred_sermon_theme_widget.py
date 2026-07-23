from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QVBoxLayout, QWidget

from solin.styles.theme import PALETTE
from solin.ui.async_load import AsyncLoadHandle


def _load_sermon_theme_widget_type():
    from .sermon_theme_widget import SermonThemeWidget

    return SermonThemeWidget


class DeferredSermonThemeWidget(QWidget):
    """Stable talk-theme page host materialized outside the first-frame path."""

    project_theme_signal = Signal(str, str)

    def __init__(self, lang, *, parent=None) -> None:
        super().__init__(parent)
        self._lang = lang
        self._content = None
        self.preparation_handle = AsyncLoadHandle(
            _load_sermon_theme_widget_type,
            self._materialize,
            self,
            thread_name_prefix="solin-sermon-theme-import",
        )

    def _materialize(self, widget_type) -> None:
        if self._content is not None:
            return

        content = widget_type(self._lang, parent=self)
        content.project_theme_signal.connect(self.project_theme_signal)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(content)
        self._content = content
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.preparation_handle.start()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._content is not None:
            return
        painter = QPainter(self)
        painter.setPen(QColor(str(PALETTE.text_muted)))
        painter.drawText(
            self.rect().adjusted(0, 0, 0, -18),
            Qt.AlignmentFlag.AlignCenter,
            self.tr("Loading…"),
        )
        bar = QRect((self.width() - 220) // 2, self.height() // 2 + 16, 220, 3)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(str(PALETTE.surface)))
        painter.drawRoundedRect(bar, 1, 1)
        painter.setBrush(QColor(str(PALETTE.accent)))
        painter.drawRoundedRect(QRect(bar.x(), bar.y(), 72, bar.height()), 1, 1)

    def apply_theme(self) -> None:
        if self._content is not None:
            self._content.apply_theme()
        self.update()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange and self._content is not None:
            self._content.retranslateUi()
        super().changeEvent(event)


__all__ = ["DeferredSermonThemeWidget"]
