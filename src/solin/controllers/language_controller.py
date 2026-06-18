from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .main_window_nav import NAV_LABELS, SIDEBAR_SUBTITLE_SOURCE, SIDEBAR_TITLE_SOURCE


@dataclass(frozen=True, slots=True)
class LanguageContext:
    """Presentation targets that need explicit translation refreshes."""

    set_window_title: Callable[[str], None]
    sidebar_title_label: Any
    sidebar_subtitle_label: Any
    nav_buttons: Mapping[str, Any]
    translate: Callable[[str], str]


class LanguageController:
    """Handles MainWindow texts that are not automatically updated by Qt."""

    def __init__(self, context: LanguageContext) -> None:
        self._context = context

    def change_language(self, _code: str) -> None:
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        context = self._context
        context.set_window_title(context.translate(SIDEBAR_TITLE_SOURCE))
        context.sidebar_title_label.setText(context.translate(SIDEBAR_TITLE_SOURCE))
        context.sidebar_subtitle_label.setText(
            context.translate(SIDEBAR_SUBTITLE_SOURCE)
        )
        self.retranslate_sidebar()

    def retranslate_sidebar(self) -> None:
        context = self._context
        for attr, source_text in NAV_LABELS:
            button = context.nav_buttons.get(attr)
            if button is None:
                continue
            button.set_label(context.translate(source_text))
