from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Protocol

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings

BROWSER_ZOOM_FACTOR_DEFAULT = 1.0
BROWSER_ZOOM_FACTOR_MIN = 0.25
BROWSER_ZOOM_FACTOR_MAX = 5.0


def normalize_browser_zoom_factor(value: object) -> float:
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return BROWSER_ZOOM_FACTOR_DEFAULT
    if not math.isfinite(factor):
        return BROWSER_ZOOM_FACTOR_DEFAULT
    return max(BROWSER_ZOOM_FACTOR_MIN, min(BROWSER_ZOOM_FACTOR_MAX, factor))


class BrowserZoomSettings(Protocol):
    def zoom_factor(self) -> float: ...

    def set_zoom_factor(self, factor: float) -> None: ...


@dataclass(frozen=True, slots=True)
class BrowserSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> BrowserSettingsStore:
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )

    def zoom_factor(self) -> float:
        return normalize_browser_zoom_factor(
            self.settings.value(
                SettingsKey.BROWSER_ZOOM_FACTOR,
                BROWSER_ZOOM_FACTOR_DEFAULT,
            )
        )

    def set_zoom_factor(self, factor: float) -> None:
        self.settings.set_value(
            SettingsKey.BROWSER_ZOOM_FACTOR,
            normalize_browser_zoom_factor(factor),
        )
