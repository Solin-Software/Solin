from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_media_card_keeps_resolved_title_outside_frozen_cache_item():
    repo_root = Path(__file__).resolve().parents[1]
    script = """
from PySide6.QtWidgets import QApplication, QPushButton

from solin.core.media.cache_listing import CachedMediaItem
from solin.widgets.cache_manager_widget import MediaCard

app = QApplication([])
item = CachedMediaItem(
    path="C:/media/clip.mp4",
    filename="clip.mp4",
    display_title="clip",
    size=5,
    media_type="video",
    original_url="https://example.test/clip.mp4",
)
card = MediaCard(item, object())
card.set_title("Resolved title")

assert item.display_title == "clip"
assert card.display_title == "Resolved title"

emitted = []
card.play_requested.connect(
    lambda path, media_type, original_url, display_title: emitted.append(
        (path, media_type, original_url, display_title)
    )
)
buttons = card.findChildren(QPushButton)
assert len(buttons) == 1
buttons[0].click()
assert emitted == [
    (
        "C:/media/clip.mp4",
        "video",
        "https://example.test/clip.mp4",
        "Resolved title",
    )
]

card.close()
app.processEvents()
"""
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(repo_root / "src")

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
