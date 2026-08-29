"""Deferred page boundary for the Scenes Qt Quick editor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from solin.ui.async_load import AsyncLoadHandle
from solin.ui.loading_placeholder import DeferredLoadingPlaceholder

if TYPE_CHECKING:
    from solin.controllers.program_recording_controller import ProgramRecordingController


def _load_scenes_widget_type():
    from solin.ui.qml.scenes import ScenesEditorWidget

    return ScenesEditorWidget


class DeferredScenesWidget(QWidget):
    """Keep startup light and materialize Scenes only when first opened."""

    def __init__(
        self,
        controller,
        *,
        recording: ProgramRecordingController | None = None,
        credentials=None,
        notifications=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._recording = recording
        self._credentials = credentials
        self._notifications = notifications
        self._content = None
        self._loading_placeholder = DeferredLoadingPlaceholder(self.tr("Loading…"), self)
        self.preparation_handle = AsyncLoadHandle(
            _load_scenes_widget_type,
            self._materialize,
            self,
            thread_name_prefix="solin-scenes-import",
        )

    def _materialize(self, widget_type) -> None:
        if self._content is not None:
            return
        content = widget_type(
            self._controller,
            recording=self._recording,
            credentials=self._credentials,
            notifications=self._notifications,
            parent=self,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(content)
        self._content = content
        self._loading_placeholder.finish()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.preparation_handle.start()

    def apply_theme(self) -> None:
        if self._content is not None:
            self._content.apply_theme()
        else:
            self._loading_placeholder.refresh_theme()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
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


__all__ = ["DeferredScenesWidget"]
