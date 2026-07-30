from __future__ import annotations

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from solin.ui.async_load import AsyncLoadHandle
from solin.ui.loading_placeholder import DeferredLoadingPlaceholder


def _load_talk_theme_widget_type():
    from solin.ui.qml.talk_theme import TalkThemeEditorWidget

    return TalkThemeEditorWidget


class DeferredTalkThemeWidget(QWidget):
    """Stable talk-theme page host materialized outside the first-frame path."""

    def __init__(
        self,
        lang,
        *,
        profile_paths,
        notifications,
        projection_session,
        settings,
        output_settings,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang
        self._profile_paths = profile_paths
        self._notifications = notifications
        self._projection_session = projection_session
        self._settings = settings
        self._output_settings = output_settings
        self._projection_handler = None
        self._content = None
        self._loading_placeholder = DeferredLoadingPlaceholder(
            self.tr("Loading…"),
            self,
        )
        self.preparation_handle = AsyncLoadHandle(
            _load_talk_theme_widget_type,
            self._materialize,
            self,
            thread_name_prefix="solin-talk-theme-import",
        )

    def _materialize(self, widget_type) -> None:
        if self._content is not None:
            return

        content = widget_type(
            self._lang,
            profile_paths=self._profile_paths,
            notifications=self._notifications,
            projection_session=self._projection_session,
            settings=self._settings,
            output_settings=self._output_settings,
            parent=self,
        )
        if self._projection_handler is not None:
            content.set_projection_handler(self._projection_handler)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(content)
        self._content = content
        self._loading_placeholder.finish()

    def set_projection_handler(self, handler) -> None:
        self._projection_handler = handler
        if self._content is not None:
            self._content.set_projection_handler(handler)

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

    def cleanup(self) -> None:
        self.preparation_handle.cancel()
        if self._content is not None:
            self._content.cleanup()

    def confirm_close(self) -> bool:
        if self._content is None:
            return True
        return self._content.confirm_close()


__all__ = ["DeferredTalkThemeWidget"]
