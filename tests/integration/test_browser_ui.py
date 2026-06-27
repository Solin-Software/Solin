import threading
from pathlib import Path

import solin.widgets.browser.widget as browser_widget
from PySide6.QtWidgets import QApplication
from solin.core.network.browser_images import BrowserImageFetchService
from solin.widgets.browser.ui import BrowserUiMixin, BrowserUrlBar


def test_browser_widget_uses_ui_mixin():
    assert browser_widget.BrowserUiMixin is BrowserUiMixin
    assert issubclass(browser_widget.BrowserWidget, BrowserUiMixin)
    assert browser_widget.BrowserWidget._build_ui is BrowserUiMixin._build_ui
    assert browser_widget.BrowserWidget._std_icon is BrowserUiMixin._std_icon


def test_browser_widget_does_not_resolve_the_active_profile_globally():
    source = browser_widget.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")

    assert "core.profiles.manager" not in text
    assert "_get_pm" not in text


def test_browser_url_bar_shows_only_non_default_zoom():
    app = QApplication.instance() or QApplication([])
    bar = BrowserUrlBar()

    bar.set_zoom_factor(1.0)
    assert bar.zoom_indicator.isHidden()

    bar.set_zoom_factor(1.25)
    assert not bar.zoom_indicator.isHidden()
    assert bar.zoom_indicator.text() == "125%"

    bar.set_zoom_factor(1.004)
    assert not bar.zoom_indicator.isHidden()
    assert bar.zoom_indicator.text() == "100.4%"

    bar.set_zoom_factor(1.0)
    assert bar.zoom_indicator.isHidden()

    bar.deleteLater()
    app.processEvents()


def test_image_fetch_coordinator_drops_replaced_worker_result():
    old_started = threading.Event()
    release_old = threading.Event()
    delivered_new = threading.Event()
    delivered: list[tuple[int, bytes]] = []

    def _fetch(url: str) -> bytes:
        if url == "old":
            old_started.set()
            assert release_old.wait(2)
        return url.encode()

    def _deliver(generation: int, payload: bytes) -> None:
        delivered.append((generation, payload))
        if payload == b"new":
            delivered_new.set()

    coordinator = BrowserImageFetchService(_fetch)
    first_generation = coordinator.start("old", _deliver)
    assert first_generation is not None
    assert old_started.wait(1)

    second_generation = coordinator.start("new", _deliver)
    assert second_generation is not None
    assert delivered_new.wait(1)

    release_old.set()
    assert coordinator.shutdown(timeout=1) == []

    assert delivered == [(second_generation, b"new")]
