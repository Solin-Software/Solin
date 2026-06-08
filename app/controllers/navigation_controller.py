from __future__ import annotations


class NavigationController:
    """Owns page switching and browser-specific navigation side effects."""

    def __init__(self, window) -> None:
        self._window = window

    def switch_page(self, index: int) -> None:
        window = self._window
        window._lazy_pages.ensure_page(index)
        window.stack.setCurrentIndex(index)
        for i, btn in enumerate(window._nav_btns):
            btn.set_active(i == index)
        proj_bar = getattr(window, "proj_bar", None)
        if proj_bar and proj_bar.is_expanded():
            proj_bar.collapse_overlay()
        self.update_quick_toolbar_browser_style()

    def update_quick_toolbar_browser_style(self) -> None:
        window = self._window
        quick_toolbar = getattr(window, "_quick_toolbar", None)
        if quick_toolbar is None:
            return
        browser_active = (
            getattr(window, "stack", None) is not None
            and window.stack.currentWidget() is getattr(window, "browser_widget", None)
        )
        quick_toolbar.set_browser_rect_mode(browser_active)
        if browser_active and hasattr(quick_toolbar, "raise_"):
            quick_toolbar.raise_()
            if hasattr(quick_toolbar, "reposition"):
                quick_toolbar.reposition()

    def stop_browser_tab_projection(self) -> None:
        self._window._lazy_pages.stop_browser_tab_projection()
