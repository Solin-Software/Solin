from __future__ import annotations

from types import MappingProxyType

from .base import AppTheme
from .dark import DARK_THEME
from .light import LIGHT_THEME


DEFAULT_THEME_ID = DARK_THEME.id

_THEMES_BY_ID = {
    DARK_THEME.id: DARK_THEME,
    LIGHT_THEME.id: LIGHT_THEME,
}

THEMES = MappingProxyType(_THEMES_BY_ID)


def available_themes() -> tuple[AppTheme, ...]:
    return tuple(THEMES.values())


def get_theme(theme_id: str) -> AppTheme:
    return THEMES[normalize_theme_id(theme_id)]


def normalize_theme_id(theme_id: str) -> str:
    candidate = str(theme_id or "").strip().lower()
    if candidate in THEMES:
        return candidate
    return DEFAULT_THEME_ID
