from __future__ import annotations

from .main_window_nav import NAV_LABELS, SIDEBAR_SUBTITLE_SOURCE, SIDEBAR_TITLE_SOURCE


class LanguageController:
    """Handles MainWindow texts that are not automatically updated by Qt."""

    def __init__(self, window) -> None:
        self._window = window

    def change_language(self, _code: str) -> None:
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self._window.setWindowTitle(self._window.tr(SIDEBAR_TITLE_SOURCE))
        self._window._sidebar_title_lbl.setText(self._window.tr(SIDEBAR_TITLE_SOURCE))
        self._window._sidebar_subtitle_lbl.setText(
            self._window.tr(SIDEBAR_SUBTITLE_SOURCE)
        )
        self.retranslate_sidebar()

    def retranslate_sidebar(self) -> None:
        for attr, source_text in NAV_LABELS:
            btn = getattr(self._window, attr, None)
            if btn is None:
                continue
            btn._label = self._window.tr(source_text)
            btn._update_icon_and_text()
