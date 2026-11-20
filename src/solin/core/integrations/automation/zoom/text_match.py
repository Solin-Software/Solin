from __future__ import annotations

import re
import unicodedata

from .i18n_labels import (
    AUDIO_MUTED_TEXT,
    AUDIO_UNMUTED_TEXT,
    CONNECT_AUDIO_TEXT,
    GRID_POSITION_TEXT,
    VIDEO_STARTED_TEXT,
    VIDEO_STOPPED_TEXT,
)
from .state import AudioState, VideoState


def _normalize_label(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


_GRID_POSITION = re.compile(
    r"\s+(?:"
    + "|".join(
        re.escape(_normalize_label(template)).replace("@1", r"\d+").replace("@2", r"\d+")
        for template in GRID_POSITION_TEXT
    )
    + r")(?=\s|$)"
)


def _grid_item_title(name: str) -> str:
    """Return the title before Zoom's localized grid accessibility metadata."""
    return _GRID_POSITION.split(_normalize_label(name), maxsplit=1)[0]


def _label_match_score(text: str, labels: list[str], *, decorated: bool = False) -> int:
    """Match a Zoom label, optionally followed by delimited accessibility text."""
    target = _normalize_label(text)
    if not target:
        return 0
    best = 0
    for label in labels:
        prefix = _normalize_label(label)
        if target == prefix:
            return len(prefix)
        if decorated and prefix and target.startswith(prefix):
            suffix = target[len(prefix) :].lstrip()
            if suffix.startswith((",", "、", ";", ":", "(", "[", "-", "–", "—")):
                best = max(best, len(prefix))
    return best


def _audio_button_state_from_text(text: str) -> AudioState:
    muted_score = _label_match_score(text, AUDIO_MUTED_TEXT, decorated=True)
    unmuted_score = _label_match_score(text, AUDIO_UNMUTED_TEXT, decorated=True)
    if muted_score > unmuted_score:
        return AudioState.MUTED
    if unmuted_score > 0:
        return AudioState.UNMUTED
    if _label_match_score(text, CONNECT_AUDIO_TEXT, decorated=True):
        return AudioState.DISCONNECTED
    return AudioState.UNKNOWN


def _video_button_state_from_text(text: str) -> VideoState:
    stopped_score = _label_match_score(text, VIDEO_STOPPED_TEXT, decorated=True)
    started_score = _label_match_score(text, VIDEO_STARTED_TEXT, decorated=True)
    if stopped_score > started_score:
        return VideoState.STOPPED
    if started_score > 0:
        return VideoState.STARTED
    return VideoState.UNKNOWN
