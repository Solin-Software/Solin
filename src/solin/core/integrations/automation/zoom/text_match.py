from __future__ import annotations

import re
import unicodedata

from .i18n_labels import (
    AUDIO_MUTED_TEXT,
    AUDIO_UNMUTED_TEXT,
    VIDEO_STARTED_TEXT,
    VIDEO_STOPPED_TEXT,
)
from .state import AudioState, VideoState


def _matches_any(text: str, patterns: list[str]) -> bool:
    t = text.lower()
    return any(p in t for p in patterns)


def _normalize_toolbar_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def _matches_toolbar_action(text: str, patterns: list[str]) -> bool:
    """
    Match Zoom toolbar action labels without depending on the variable suffix.
    Examples: "Share, Alt+S", "Mute, currently unmuted, Alt+A".
    """
    return _toolbar_action_match_score(text, patterns) > 0


def _toolbar_action_match_score(text: str, patterns: list[str]) -> int:
    """Return the longest action-prefix match for a Zoom toolbar label."""
    target = _normalize_toolbar_text(text)
    if not target:
        return 0
    boundary_chars = set(" ,，、;:([（-–—")
    best = 0
    for pattern in patterns:
        p = _normalize_toolbar_text(pattern)
        if not p:
            continue
        if target == p:
            best = max(best, len(p))
            continue
        if target.startswith(p):
            if len(target) == len(p) or target[len(p)] in boundary_chars:
                best = max(best, len(p))
    return best


def _audio_button_state_from_text(text: str) -> AudioState:
    muted_score = _toolbar_action_match_score(text, AUDIO_MUTED_TEXT)
    unmuted_score = _toolbar_action_match_score(text, AUDIO_UNMUTED_TEXT)
    if muted_score > unmuted_score:
        return AudioState.MUTED
    if unmuted_score > 0:
        return AudioState.UNMUTED
    if _normalize_toolbar_text(text):
        return AudioState.DISCONNECTED
    return AudioState.UNKNOWN


def _video_button_state_from_text(text: str) -> VideoState:
    stopped_score = _toolbar_action_match_score(text, VIDEO_STOPPED_TEXT)
    started_score = _toolbar_action_match_score(text, VIDEO_STARTED_TEXT)
    if stopped_score > started_score:
        return VideoState.STOPPED
    if started_score > 0:
        return VideoState.STARTED
    return VideoState.UNKNOWN
