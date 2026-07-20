from __future__ import annotations

import uuid

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings
from solin.core.timer.media_countdown_automation import (
    DEFAULT_MEDIA_COUNTDOWN_LEAD_SECONDS,
    MAX_MEDIA_COUNTDOWN_LEAD_SECONDS,
    MIN_MEDIA_COUNTDOWN_LEAD_SECONDS,
    MediaCountdownAutomationConfig,
)
from solin.core.timer.media_countdown_settings import (
    MediaCountdownSettings,
    MediaCountdownSettingsStore,
)
from solin.core.timer.models import MediaCountdownPresentation


def _store() -> tuple[MediaCountdownSettingsStore, SettingsStore]:
    profile = ProfileSettings.for_profile_id(
        f"media_countdown_settings_{uuid.uuid4().hex}"
    )
    settings = SettingsStore.for_namespace(
        profile.organization,
        QSETTINGS_PREFS_APP,
    )
    return MediaCountdownSettingsStore(settings), settings


def test_media_countdown_settings_default_to_safe_disabled_configuration() -> None:
    store, settings = _store()
    settings.clear()
    try:
        assert store.load() == MediaCountdownSettings(
            presentation=MediaCountdownPresentation.CIRCULAR,
            automation=MediaCountdownAutomationConfig(
                enabled=False,
                lead_seconds=DEFAULT_MEDIA_COUNTDOWN_LEAD_SECONDS,
            ),
        )
    finally:
        settings.clear()


def test_media_countdown_settings_roundtrip_and_clamp_operator_preferences() -> None:
    store, settings = _store()
    settings.clear()
    try:
        store.set_enabled(True)
        store.set_lead_seconds(1)
        store.set_presentation(MediaCountdownPresentation.YEARLY_TEXT)

        config = store.load()

        assert config.automation.enabled is True
        assert config.automation.lead_seconds == MIN_MEDIA_COUNTDOWN_LEAD_SECONDS
        assert config.presentation is MediaCountdownPresentation.YEARLY_TEXT

        store.set_lead_seconds(MAX_MEDIA_COUNTDOWN_LEAD_SECONDS + 1)

        assert (
            store.load().automation.lead_seconds == MAX_MEDIA_COUNTDOWN_LEAD_SECONDS
        )
    finally:
        settings.clear()


def test_media_countdown_settings_recover_from_invalid_persisted_values() -> None:
    store, settings = _store()
    settings.clear()
    try:
        settings.set_value(SettingsKey.MEDIA_COUNTDOWN_LEAD_SECONDS, "invalid")
        settings.set_value(SettingsKey.MEDIA_COUNTDOWN_PRESENTATION, "unknown")

        config = store.load()

        assert (
            config.automation.lead_seconds == DEFAULT_MEDIA_COUNTDOWN_LEAD_SECONDS
        )
        assert config.presentation is MediaCountdownPresentation.CIRCULAR
    finally:
        settings.clear()


def test_media_countdown_settings_normalization_preserves_enum_presentation() -> None:
    config = MediaCountdownSettings(
        presentation=MediaCountdownPresentation.YEARLY_TEXT,
        automation=MediaCountdownAutomationConfig(
            enabled=True,
            lead_seconds=30,
        ),
    )

    assert config.normalized().presentation is MediaCountdownPresentation.YEARLY_TEXT
