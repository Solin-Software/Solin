from pathlib import Path

import solin.widgets.browser.widget as browser_widget
from solin.widgets.browser.ui import _BrowserUiMixin


def test_browser_widget_uses_ui_mixin():
    assert browser_widget._BrowserUiMixin is _BrowserUiMixin
    assert issubclass(browser_widget.BrowserWidget, _BrowserUiMixin)
    assert browser_widget.BrowserWidget._build_ui is _BrowserUiMixin._build_ui
    assert browser_widget.BrowserWidget._std_icon is _BrowserUiMixin._std_icon


def test_browser_widget_does_not_resolve_the_active_profile_globally():
    source = browser_widget.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")

    assert "core.profiles.manager" not in text
    assert "_get_pm" not in text
