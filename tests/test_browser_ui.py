import threading
from pathlib import Path

import solin.widgets.browser.widget as browser_widget
from solin.core.network.browser_images import BrowserImageFetchService
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
