from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtWidgets import QStackedWidget

from .lazy_page_controller import LazyPageController


class NavigationController:
    """Owns page switching and browser-specific navigation side effects."""

    def __init__(
        self,
        stack: QStackedWidget,
        lazy_pages: LazyPageController,
        *,
        nav_buttons: Callable[[], Sequence[Any]],
        projection_bar: Callable[[], Any | None],
        quick_toolbar: Callable[[], Any | None],
    ) -> None:
        self._stack = stack
        self._lazy_pages = lazy_pages
        self._nav_buttons = nav_buttons
        self._projection_bar = projection_bar
        self._quick_toolbar = quick_toolbar

    def switch_page(self, index: int) -> None:
        self._lazy_pages.ensure_page(index)
        self._stack.setCurrentIndex(index)
        for button_index, button in enumerate(self._nav_buttons()):
            button.set_active(button_index == index)
        projection_bar = self._projection_bar()
        if projection_bar is not None and projection_bar.is_expanded():
            projection_bar.collapse_overlay()
        self.update_quick_toolbar_browser_style()

    def update_quick_toolbar_browser_style(self) -> None:
        quick_toolbar = self._quick_toolbar()
        if quick_toolbar is None:
            return
        browser_active = (
            self._stack.currentWidget() is self._lazy_pages.browser_widget
        )
        quick_toolbar.set_browser_rect_mode(browser_active)
        if browser_active:
            quick_toolbar.raise_()
            quick_toolbar.reposition()

    def stop_browser_tab_projection(self) -> None:
        self._lazy_pages.stop_browser_tab_projection()
