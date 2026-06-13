from pathlib import Path
from types import SimpleNamespace

import solin.widgets.browser.widget as browser_widget
from solin.widgets.browser.downloads import _BrowserDownloadsMixin


def test_browser_widget_uses_downloads_mixin():
    assert browser_widget._BrowserDownloadsMixin is _BrowserDownloadsMixin
    assert issubclass(browser_widget.BrowserWidget, _BrowserDownloadsMixin)
    assert (
        browser_widget.BrowserWidget._on_download_requested
        is _BrowserDownloadsMixin._on_download_requested
    )
    assert (
        browser_widget.BrowserWidget._browser_download_cache_path
        is _BrowserDownloadsMixin._browser_download_cache_path
    )


def test_browser_download_cache_path_is_stable_and_sanitized(tmp_path):
    owner = _BrowserDownloadsMixin()
    owner._media_cache_manager = SimpleNamespace(media_cache_dir=tmp_path)

    path = _BrowserDownloadsMixin._browser_download_cache_path(
        owner,
        "https://example.test/files/bad%3Aname.pdf?download=1",
        "ignored title",
        "pdf",
    )

    result = Path(path)
    assert result.parent == tmp_path
    assert result.suffix == ".pdf"
    assert "bad_name-" in result.name
    assert path == _BrowserDownloadsMixin._browser_download_cache_path(
        owner,
        "https://example.test/files/bad%3Aname.pdf?download=1",
        "ignored title",
        "pdf",
    )
