from solin.controllers.navigation_controller import NavigationController


class _LazyPagesStub:
    def __init__(self, browser_widget):
        self.browser_widget = browser_widget
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


def _fixture(*, quick_toolbar=True):
    page = object()
    browser = object()
    stack = _StackStub([page, browser])
    lazy_pages = _LazyPagesStub(browser)
    buttons = [_NavButtonStub(), _NavButtonStub()]
    projection_bar = _ProjectionBarStub(expanded=True)
    toolbar = _QuickToolbarStub() if quick_toolbar else None
    controller = NavigationController(
        stack,
        lazy_pages,
        nav_buttons=lambda: buttons,
        projection_bar=lambda: projection_bar,
        quick_toolbar=lambda: toolbar,
    )
    return controller, stack, lazy_pages, buttons, projection_bar, toolbar


def test_switch_page_updates_stack_buttons_projection_bar_and_toolbar():
    controller, stack, lazy_pages, buttons, projection_bar, toolbar = _fixture()

    controller.switch_page(1)

    assert lazy_pages.ensured == [1]
    assert stack.current_index == 1
    assert [button.active for button in buttons] == [False, True]
    assert projection_bar.collapsed is True
    assert toolbar.browser_rect_modes == [True]
    assert toolbar.raised is True
    assert toolbar.repositioned is True


def test_update_quick_toolbar_browser_style_handles_missing_toolbar():
    controller, stack, *_rest = _fixture(quick_toolbar=False)

    controller.update_quick_toolbar_browser_style()

    assert stack.current_index == 0


def test_stop_browser_tab_projection_delegates_to_lazy_pages():
    controller, _stack, lazy_pages, *_rest = _fixture()

    controller.stop_browser_tab_projection()

    assert lazy_pages.stopped_browser_tab_projection is True
    assert not hasattr(controller, "_window")
