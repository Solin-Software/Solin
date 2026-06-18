"""Persistence adapters for onboarding."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.integrations.automation.settings import OBSSettingsStore
from solin.core.jw.language_settings import JWLanguageSettingsStore
from solin.core.onboarding.application import ProfileOnboardingCommand


@dataclass(frozen=True, slots=True)
class OnboardingSettingsStores:
    app_settings: ProfileAppSettingsStore
    media_language_settings: JWLanguageSettingsStore
    obs_settings: OBSSettingsStore


class QSettingsOnboardingSettings:
    def __init__(
        self,
        stores_for_profile: Callable[[str], OnboardingSettingsStores],
    ) -> None:
        self._stores_for_profile = stores_for_profile

    def apply(
        self,
        profile_id: str,
        command: ProfileOnboardingCommand,
    ) -> None:
        stores = self._stores_for_profile(profile_id)
        stores.app_settings.set_app_language(command.interface_language)
        if command.media_language:
            stores.media_language_settings.set_media_language_code(
                command.media_language
            )

        obs_settings = stores.obs_settings
        obs_settings.set_enabled(command.obs.enabled)
        if command.obs.enabled:
            obs_settings.set_connection(command.obs.port, command.obs.password)
            obs_settings.set_scenes(
                command.obs.default_scene,
                command.obs.media_scene,
            )
