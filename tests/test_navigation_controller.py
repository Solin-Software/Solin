from solin.controllers.navigation_controller import NavigationController


class _LazyPagesStub:
    def __init__(self):
        self.ensured = []
        self.stopped_browser_tab_projection = False

    def ensure_page(self, index):
        self.ensured.append(index)

    def stop_browser_tab_projection(self):
        self.stopped_browser_tab_projection = True


class _StackStub:
    def __init__(self, widgets):
        self.widgets = widgets
        self.current_index = 0

    def setCurrentIndex(self, index):
        self.current_index = index

    def currentWidget(self):
        return self.widgets[self.current_index]


class _NavButtonStub:
    def __init__(self):
        self.active = None

    def set_active(self, active):
        self.active = active


class _ProjectionBarStub:
    def __init__(self, expanded=True):
        self.expanded = expanded
        self.collapsed = False

    def is_expanded(self):
        return self.expanded

    def collapse_overlay(self):
        self.collapsed = True
        self.expanded = False


class _QuickToolbarStub:
    def __init__(self):
        self.browser_rect_modes = []
        self.raised = False
        self.repositioned = False

    def set_browser_rect_mode(self, active):
        self.browser_rect_modes.append(active)

    def raise_(self):
        self.raised = True

    def reposition(self):
        self.repositioned = True


class _WindowStub:
    def __init__(self):
        self.page0 = object()
        self.browser_widget = object()
        self._lazy_pages = _LazyPagesStub()
        self.stack = _StackStub([self.page0, self.browser_widget])
        self._nav_btns = [_NavButtonStub(), _NavButtonStub()]
        self.proj_bar = _ProjectionBarStub(expanded=True)
        self._quick_toolbar = _QuickToolbarStub()


def test_switch_page_updates_stack_buttons_projection_bar_and_toolbar():
    window = _WindowStub()
    controller = NavigationController(window)

    controller.switch_page(1)

    assert window._lazy_pages.ensured == [1]
    assert window.stack.current_index == 1
    assert [btn.active for btn in window._nav_btns] == [False, True]
    assert window.proj_bar.collapsed is True
    assert window._quick_toolbar.browser_rect_modes == [True]
    assert window._quick_toolbar.raised is True
    assert window._quick_toolbar.repositioned is True


def test_update_quick_toolbar_browser_style_handles_missing_toolbar():
    window = _WindowStub()
    delattr(window, "_quick_toolbar")
    controller = NavigationController(window)

    controller.update_quick_toolbar_browser_style()

    assert window.stack.current_index == 0


def test_stop_browser_tab_projection_delegates_to_lazy_pages():
    window = _WindowStub()
    controller = NavigationController(window)

    controller.stop_browser_tab_projection()

    assert window._lazy_pages.stopped_browser_tab_projection is True
