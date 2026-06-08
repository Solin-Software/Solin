import app.widgets.browser.widget as browser_widget
from app.widgets.browser.ui import _BrowserUiMixin


def test_browser_widget_uses_ui_mixin():
    assert browser_widget._BrowserUiMixin is _BrowserUiMixin
    assert issubclass(browser_widget.BrowserWidget, _BrowserUiMixin)
    assert browser_widget.BrowserWidget._build_ui is _BrowserUiMixin._build_ui
    assert browser_widget.BrowserWidget._std_icon is _BrowserUiMixin._std_icon
