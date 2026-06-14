"""Persistence adapters for onboarding."""

from __future__ import annotations

from solin.core.integrations.automation.settings import OBSSettingsStore
from solin.core.jw.language_settings import JWLanguageSettingsStore
from solin.core.onboarding.application import ProfileOnboardingCommand
from solin.core.profiles.settings import ProfileSettings


class QSettingsOnboardingSettings:
    def apply(
        self,
        profile_id: str,
        command: ProfileOnboardingCommand,
    ) -> None:
        profile_settings = ProfileSettings.for_profile_id(profile_id)
        profile_settings.app_settings().set_app_language(
            command.interface_language
        )
        if command.media_language:
            JWLanguageSettingsStore.for_profile_settings(
                profile_settings
            ).set_media_language_code(command.media_language)

        obs_settings = OBSSettingsStore.for_profile_settings(profile_settings)
        obs_settings.set_enabled(command.obs.enabled)
        if command.obs.enabled:
            obs_settings.set_connection(command.obs.port, command.obs.password)
            obs_settings.set_scenes(
                command.obs.default_scene,
                command.obs.media_scene,
            )
