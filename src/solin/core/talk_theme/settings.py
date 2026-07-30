"""Profile-scoped preferences for the talk-theme editor."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings


DEFAULT_CUSTOM_COLOR = "#FFFFFF"
MAX_CUSTOM_COLORS = 16
_ARGB_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{8}$")
_RGB_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


class CustomColorSettings(Protocol):
    def custom_colors(self) -> tuple[str, ...]: ...

    def set_custom_colors(self, colors: tuple[str, ...]) -> None: ...


class OutputAspectSettings(Protocol):
    def follow_output_aspect(self) -> bool: ...

    def set_follow_output_aspect(self, enabled: bool) -> None: ...


def normalize_custom_colors(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_colors: tuple[object, ...] = (value,)
    elif isinstance(value, (list, tuple)):
        raw_colors = tuple(value)
    else:
        return ()

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_color in raw_colors:
        color = raw_color.strip() if isinstance(raw_color, str) else ""
        if _RGB_COLOR_PATTERN.fullmatch(color):
            canonical = color.upper()
        elif _ARGB_COLOR_PATTERN.fullmatch(color):
            canonical = f"#{color[-6:].upper()}"
        else:
            continue
        if canonical == DEFAULT_CUSTOM_COLOR or canonical in seen:
            continue
        normalized.append(canonical)
        seen.add(canonical)
        if len(normalized) == MAX_CUSTOM_COLORS:
            break
    return tuple(normalized)


def merge_custom_colors(
    stored: object,
    observed: object,
    *,
    capacity: int = MAX_CUSTOM_COLORS,
) -> tuple[str, ...]:
    """Append newly observed swatches without losing restored Qt slots."""

    if capacity <= 0:
        return ()
    current = list(normalize_custom_colors(stored))
    new_colors = [
        color
        for color in normalize_custom_colors(observed)
        if color not in current
    ]
    if not new_colors:
        return tuple(current[:capacity])
    return tuple((current + new_colors)[-capacity:])


@dataclass(frozen=True, slots=True)
class TalkThemeSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> TalkThemeSettingsStore:
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )

    def custom_colors(self) -> tuple[str, ...]:
        raw = self.settings.value(SettingsKey.TALK_THEME_CUSTOM_COLORS, None)
        if raw is None:
            raw = self.settings.value(SettingsKey.LEGACY_TALK_THEME_CUSTOM_TEXT_COLORS, [])
        return normalize_custom_colors(raw)

    def set_custom_colors(self, colors: tuple[str, ...]) -> None:
        normalized = normalize_custom_colors(colors)
        if normalized:
            self.settings.set_value(
                SettingsKey.TALK_THEME_CUSTOM_COLORS,
                list(normalized),
                sync=False,
            )
        else:
            self.settings.remove(SettingsKey.TALK_THEME_CUSTOM_COLORS, sync=False)
        self.settings.remove(SettingsKey.LEGACY_TALK_THEME_CUSTOM_TEXT_COLORS)


@dataclass(frozen=True, slots=True)
class TalkThemeOutputSettingsStore:
    """Global editor preferences that are independent from profiles and presets."""

    settings: SettingsStore

    def follow_output_aspect(self) -> bool:
        return bool(
            self.settings.value(
                SettingsKey.TALK_THEME_FOLLOW_OUTPUT_ASPECT,
                True,
                bool,
            )
        )

    def set_follow_output_aspect(self, enabled: bool) -> None:
        self.settings.set_value(
            SettingsKey.TALK_THEME_FOLLOW_OUTPUT_ASPECT,
            bool(enabled),
        )


__all__ = [
    "CustomColorSettings",
    "DEFAULT_CUSTOM_COLOR",
    "MAX_CUSTOM_COLORS",
    "OutputAspectSettings",
    "TalkThemeOutputSettingsStore",
    "TalkThemeSettingsStore",
    "merge_custom_colors",
    "normalize_custom_colors",
]
