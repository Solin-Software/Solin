from __future__ import annotations

from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pytest

from solin.core.i18n.timer_schedule_view import section_icon_key
from solin.core.remote_control.web_assets import RemoteControlWebAssets
from solin.core.timer.models import CustomSectionIcon
from solin.styles.timer_section_icons import TIMER_SECTION_ICONS
from solin.ui.qml.timer_icons import _TIMER_ICON_MAP

ROOT = Path(__file__).resolve().parents[2] / "src/solin/resources/remote_control"


def assets():
    return RemoteControlWebAssets(
        ROOT / "timer", shared_root=ROOT / "shared",
        installation_id="section-icons-test", url_prefix="/remote/timer",
    )


def shape(svg):
    def normalize(element):
        return (element.tag.rsplit("}", 1)[-1], tuple(sorted(element.attrib.items())),
                tuple(normalize(child) for child in element))
    return normalize(ET.fromstring(svg))


def test_rendered_timer_sections_share_desktop_catalog():
    html = assets().index_html.decode()
    for choice in CustomSectionIcon:
        key = section_icon_key("custom", choice)
        if not key:
            assert choice is CustomSectionIcon.NONE
            continue
        symbol_key = "default" if key == "section_group" else key
        matches = re.findall(
            rf'<symbol id="icon-sec-{re.escape(symbol_key)}"([^>]*)>(.*?)</symbol>',
            html, re.DOTALL,
        )
        assert len(matches) == 1, choice
        attrs, body = matches[0]
        assert shape(f'<svg xmlns="http://www.w3.org/2000/svg"{attrs}>{body}</svg>') == shape(_TIMER_ICON_MAP[key])


def test_icon_changes_invalidate_shell_revision(monkeypatch):
    original = assets()
    monkeypatch.setitem(TIMER_SECTION_ICONS, "book", TIMER_SECTION_ICONS["book"].replace('viewBox="0 0 24 24"', 'viewBox="0 0 25 24"'))
    changed = assets()
    assert changed.index_html != original.index_html
    assert changed.revision != original.revision
    assert changed.revision.encode() in changed.service_worker


def test_invalid_or_duplicate_sprite_placeholder_fails_closed(monkeypatch):
    marker = b"<!-- __SOLIN_TIMER_SPRITE__ -->"
    with pytest.raises(ValueError, match="exactly once"):
        RemoteControlWebAssets._render_timer_sprite(marker + marker)
    monkeypatch.setitem(TIMER_SECTION_ICONS, "book", "<svg>invalid</svg>")
    with pytest.raises(ValueError, match="Invalid canonical"):
        RemoteControlWebAssets._render_timer_sprite(marker)


def test_section_mark_lookup_handles_desktop_keys_and_absent_icons():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to exercise the web icon lookup")
    script = ROOT / "timer/scripts/section-mark.js"
    driver = r'''
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8").replace(
  /^import .*?;\s*/,
  `const svgElement = (tag, options = {}) => ({tag, attrs: {...options.attrs}, children: [], append(child) { this.children.push(child); }, setAttribute(k, v) { this.attrs[k] = v; }});
   const uiIcon = (name) => ({name, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }});\n`
);
import("data:text/javascript;base64," + Buffer.from(source).toString("base64")).then(({sectionMark}) => {
  const assert = require("assert");
  const keys = JSON.parse(process.argv[2]);
  const visualKeys = keys.map(key => key === "section_group" ? "default" : key);
  globalThis.document = {
    getElementById: (id) => visualKeys.some(key => id === `icon-sec-${key}`) ? {} : null,
  };
  for (const key of keys) {
    const expected = key === "section_group" ? "default" : key;
    assert.equal(sectionMark(key, "#123456").name, `sec-${expected}`);
  }
  assert.equal(sectionMark("unknown", null).name, "sec-default");
  assert.equal(sectionMark("none", null), null);
  assert.equal(sectionMark("none", "#123456").children[0].attrs.fill, "#123456");
}).catch(error => { console.error(error); process.exitCode = 1; });
'''
    import json

    subprocess.run(
        [node, "-e", driver, str(script), json.dumps(list(TIMER_SECTION_ICONS))],
        check=True, capture_output=True, text=True,
    )
