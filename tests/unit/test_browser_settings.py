from __future__ import annotations

import math
import uuid

from solin.core.network.browser_settings import BrowserSettingsStore
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.profiles.settings import ProfileSettings


def _settings() -> BrowserSettingsStore:
    profile = ProfileSettings.for_profile_id(f"browser_zoom_{uuid.uuid4().hex}")
    return BrowserSettingsStore.for_profile_settings(profile)


def test_browser_zoom_factor_is_profile_scoped_and_persisted() -> None:
    settings = _settings()
    other_profile = _settings()

    assert settings.zoom_factor() == 1.0

    settings.set_zoom_factor(1.5)

    assert settings.zoom_factor() == 1.5
    assert other_profile.zoom_factor() == 1.0


def test_browser_zoom_factor_normalizes_invalid_and_out_of_range_values() -> None:
    settings = _settings()

    settings.set_zoom_factor(99.0)
    assert settings.zoom_factor() == 5.0

    settings.set_zoom_factor(0.01)
    assert settings.zoom_factor() == 0.25

    settings.settings.set_value(SettingsKey.BROWSER_ZOOM_FACTOR, math.nan)
    assert settings.zoom_factor() == 1.0
