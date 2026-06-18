from __future__ import annotations

from solin.core.onboarding.application import (
    OBSOnboardingConfiguration,
    ProfileOnboardingCommand,
)
from solin.core.onboarding.infrastructure import (
    OnboardingSettingsStores,
    QSettingsOnboardingSettings,
)


class _AppSettings:
    def __init__(self) -> None:
        self.language = ""

    def set_app_language(self, language: str) -> None:
        self.language = language


class _MediaLanguageSettings:
    def __init__(self) -> None:
        self.language = ""

    def set_media_language_code(self, language: str) -> None:
        self.language = language


class _OBSSettings:
    def __init__(self) -> None:
        self.enabled = False
        self.connection = None
        self.scenes = None

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_connection(self, port: int, password: str) -> None:
        self.connection = (port, password)

    def set_scenes(self, default_scene: str, media_scene: str) -> None:
        self.scenes = (default_scene, media_scene)


def test_qsettings_onboarding_settings_uses_injected_profile_stores():
    app_settings = _AppSettings()
    media_settings = _MediaLanguageSettings()
    obs_settings = _OBSSettings()
    requested_profiles = []

    adapter = QSettingsOnboardingSettings(
        lambda profile_id: (
            requested_profiles.append(profile_id)
            or OnboardingSettingsStores(
                app_settings=app_settings,
                media_language_settings=media_settings,
                obs_settings=obs_settings,
            )
        )
    )

    adapter.apply(
        "main_hall",
        ProfileOnboardingCommand(
            name="Main Hall",
            interface_language="pt_BR",
            media_language="T",
            obs=OBSOnboardingConfiguration(
                enabled=True,
                port=4456,
                password="secret",
                default_scene="Default",
                media_scene="Media",
            ),
        ),
    )

    assert requested_profiles == ["main_hall"]
    assert app_settings.language == "pt_BR"
    assert media_settings.language == "T"
    assert obs_settings.enabled is True
    assert obs_settings.connection == (4456, "secret")
    assert obs_settings.scenes == ("Default", "Media")
