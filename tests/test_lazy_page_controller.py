from app.controllers.lazy_page_controller import LazyPageController


class _WidgetStub:
    def __init__(self):
        self.deleted = False

    def deleteLater(self):
        self.deleted = True


class _StackStub:
    def __init__(self):
        self._widgets = [_WidgetStub() for _ in range(10)]

    def widget(self, index):
        return self._widgets[index]

    def insertWidget(self, index, widget):
        self._widgets.insert(index, widget)

    def removeWidget(self, widget):
        self._widgets.remove(widget)

    def count(self):
        return len(self._widgets)


class _WindowStub:
    def __init__(self):
        self.stack = _StackStub()
        self.lang = object()
        self.notifications = object()


class _BrowserStub:
    def __init__(self):
        self.stopped = False
        self.cleaned = False

    def _stop_tab_projection_internal(self):
        self.stopped = True

    def cleanup_browser(self):
        self.cleaned = True


def test_lazy_page_controller_initializes_expected_window_attrs():
    window = _WindowStub()

    LazyPageController(window)

    assert window.browser_widget is None
    assert window.cache_manager_widget is None
    assert window.wifi_receive_widget is None


def test_lazy_page_controller_replaces_stack_placeholder_in_place():
    window = _WindowStub()
    controller = LazyPageController(window)

    old_widget = window.stack.widget(LazyPageController.BROWSER_INDEX)
    replacement = _WidgetStub()
    controller._replace_stack_widget(LazyPageController.BROWSER_INDEX, replacement)

    assert window.stack.count() == 10
    assert window.stack.widget(LazyPageController.BROWSER_INDEX) is replacement
    assert old_widget.deleted is True


def test_lazy_page_controller_delegates_browser_lifecycle():
    window = _WindowStub()
    controller = LazyPageController(window)
    browser = _BrowserStub()
    window.browser_widget = browser

    controller.stop_browser_tab_projection()
    controller.cleanup_browser()

    assert browser.stopped is True
    assert browser.cleaned is True


def test_lazy_page_controller_routes_known_stack_indices(monkeypatch):
    window = _WindowStub()
    controller = LazyPageController(window)
    calls = []

    monkeypatch.setattr(
        controller,
        "ensure_browser_widget",
        lambda: calls.append("browser"),
    )
    monkeypatch.setattr(
        controller,
        "ensure_cache_manager_widget",
        lambda: calls.append("cache"),
    )
    monkeypatch.setattr(
        controller,
        "ensure_wifi_receive_widget",
        lambda: calls.append("wifi"),
    )

    controller.ensure_page(LazyPageController.BROWSER_INDEX)
    controller.ensure_page(LazyPageController.CACHE_INDEX)
    controller.ensure_page(LazyPageController.WIFI_INDEX)
    controller.ensure_page(999)

    assert calls == ["browser", "cache", "wifi"]


def test_lazy_page_controller_builds_browser_without_window_parent(monkeypatch):
    import app.widgets.browser.widget as browser_module

    class _BrowserFactory:
        def __init__(self, lang_manager, parent=None):
            self.lang_manager = lang_manager
            self.parent = parent

    window = _WindowStub()
    controller = LazyPageController(window)
    monkeypatch.setattr(browser_module, "BrowserWidget", _BrowserFactory)
    monkeypatch.setattr(controller, "_connect_browser_signals", lambda: None)

    browser = controller.ensure_browser_widget()

    assert browser is window.browser_widget
    assert browser.lang_manager is window.lang
    assert browser.parent is None
