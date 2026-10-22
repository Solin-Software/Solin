"""The attendee and stage pages must work under their strict CSP and stay translatable."""

from __future__ import annotations

import re

from tests._paths import REPO_ROOT

WEB = REPO_ROOT / "src" / "solin" / "resources" / "conference_web"
PAGES = ("join.html", "stage.html")


def test_pages_have_no_inline_code_the_csp_would_block():
    for page in PAGES:
        html = (WEB / page).read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), page
        assert "<style" not in html and " style=" not in html, page
        assert not re.search(r"\son[a-z]+=", html), page


def test_referenced_assets_exist():
    for page in PAGES:
        html = (WEB / page).read_text(encoding="utf-8")
        for asset in re.findall(r'(?:src|href)="/assets/([^"]+)"', html):
            assert (WEB / asset).is_file(), f"{page} references missing {asset}"
        for vendor in re.findall(r'src="/vendor/([^"]+)"', html):
            assert vendor.startswith("livekit-client"), vendor


def test_every_language_has_every_message():
    source = (WEB / "i18n.js").read_text(encoding="utf-8")
    blocks = dict(re.findall(r"\n    (\w+): \{(.*?)\n    \},", source, flags=re.S))
    keys = {lang: set(re.findall(r"\n      (\w+):", body)) for lang, body in blocks.items()}
    assert {"en", "pt"} <= keys.keys()
    for lang, names in keys.items():
        assert names == keys["en"], f"{lang} differs: {names ^ keys['en']}"
    used = set()
    for name in ("join.js", "stage.js"):
        used |= set(re.findall(r'T\.t\("(\w+)"', (WEB / name).read_text(encoding="utf-8")))
    for page in PAGES:
        used |= set(re.findall(r'data-t(?:-placeholder|-label)?="(\w+)"', (WEB / page).read_text(encoding="utf-8")))
    assert used <= keys["en"], used - keys["en"]


def test_the_encryption_key_is_only_read_from_the_fragment():
    script = (WEB / "join.js").read_text(encoding="utf-8")
    assert "location.hash" in script
    assert "location.search" not in script and "localStorage.setItem(\"solin.k" not in script
