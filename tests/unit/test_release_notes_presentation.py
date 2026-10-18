from html.parser import HTMLParser

from PySide6.QtGui import QTextDocument
import pytest

from solin.ui.release_notes import release_notes_html


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_release_notes_keep_formatting_and_safe_links_without_active_resources():
    rendered = release_notes_html(
        "## Changes\n\n**Important** [PR #1](https://github.com/Solin-Software/Solin/pull/1)\n\n"
        "![image](https://example.com/tracker.png)\n\n"
        "[Local](file:///secret.txt) [Script](javascript:alert)\n\n"
        '<img src="https://example.com/inline.png"><iframe src="https://example.com"></iframe>'
    )
    parsed = _Tags()
    parsed.feed(rendered)
    assert not any(tag in {"img", "iframe", "script", "object"} for tag, _ in parsed.tags)
    assert [attrs["href"] for tag, attrs in parsed.tags if tag == "a" and "href" in attrs] == [
        "https://github.com/Solin-Software/Solin/pull/1"
    ]
    document = QTextDocument()
    document.setHtml(rendered)
    assert "Changes" in document.toPlainText()
    assert "Important" in document.toPlainText()
    assert "Local" in document.toPlainText()
    assert any(tag == "h2" for tag, _ in parsed.tags)


def test_release_notes_do_not_interpret_literal_html_as_formatting():
    rendered = release_notes_html('<a href="https://example.com">HTML link</a>')
    parsed = _Tags()
    parsed.feed(rendered)
    assert not any(tag == "a" and attrs.get("href") for tag, attrs in parsed.tags)


@pytest.mark.parametrize("url", ["http://[", "https://[invalid]", "https://example.com／path"])
def test_malformed_links_do_not_prevent_notes_from_rendering(url):
    rendered = release_notes_html(f"## Changes\n\n[Broken link]({url})\n\nStill readable.")
    parsed = _Tags()
    parsed.feed(rendered)
    assert not any(tag == "a" and attrs.get("href") for tag, attrs in parsed.tags)
    document = QTextDocument()
    document.setHtml(rendered)
    assert "Still readable." in document.toPlainText()
