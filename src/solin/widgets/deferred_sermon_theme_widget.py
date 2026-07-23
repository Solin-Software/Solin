from __future__ import annotations

from PySide6.QtCore import QEvent, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from solin.ui.async_load import AsyncLoadHandle
from solin.ui.loading_placeholder import DeferredLoadingPlaceholder


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
        self._loading_placeholder = DeferredLoadingPlaceholder(
            self.tr("Loading…"),
            self,
        )
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
        self._loading_placeholder.finish()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.preparation_handle.start()

    def apply_theme(self) -> None:
        if self._content is not None:
            self._content.apply_theme()
        else:
            self._loading_placeholder.refresh_theme()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            if self._content is not None:
                self._content.retranslateUi()
            else:
                self._loading_placeholder.set_text(self.tr("Loading…"))
        super().changeEvent(event)


__all__ = ["DeferredSermonThemeWidget"]
