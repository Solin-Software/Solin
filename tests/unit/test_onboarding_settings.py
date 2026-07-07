from __future__ import annotations

from solin.core.onboarding.application import (
    OBSOnboardingConfiguration,
    ProfileOnboardingCommand,
    ZoomShareOnboardingConfiguration,
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


class _MediaSettings:
    def __init__(self) -> None:
        self.meetings_auto_download = False

    def set_meetings_auto_download(self, enabled: bool) -> None:
        self.meetings_auto_download = enabled


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


class _AutoShareSettings:
    def __init__(self) -> None:
        self.enabled = False
        self.hotkey = ""
        self.click_position = None

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_hotkey(self, hotkey: str) -> None:
        self.hotkey = hotkey

    def set_click_position(self, x: int, y: int) -> None:
        self.click_position = (x, y)


def test_qsettings_onboarding_settings_uses_injected_profile_stores():
    app_settings = _AppSettings()
    media_language_settings = _MediaLanguageSettings()
    media_settings = _MediaSettings()
    obs_settings = _OBSSettings()
    auto_share_settings = _AutoShareSettings()
    requested_profiles = []

    adapter = QSettingsOnboardingSettings(
        lambda profile_id: (
            requested_profiles.append(profile_id)
            or OnboardingSettingsStores(
                app_settings=app_settings,
                media_language_settings=media_language_settings,
                media_settings=media_settings,
                obs_settings=obs_settings,
                auto_share_settings=auto_share_settings,
            )
        )
    )

    adapter.apply(
        "main_hall",
        ProfileOnboardingCommand(
            name="Main Hall",
            interface_language="pt_BR",
            media_language="T",
            download_meeting_media=True,
            obs=OBSOnboardingConfiguration(
                enabled=True,
                port=4456,
                password="secret",
                automatic_scene_switching=True,
                default_scene="Default",
                media_scene="Media",
            ),
            zoom_share=ZoomShareOnboardingConfiguration(
                enabled=True,
                hotkey="Ctrl+Shift+S",
                click_x=300,
                click_y=250,
            ),
        ),
    )

    assert requested_profiles == ["main_hall"]
    assert app_settings.language == "pt_BR"
    assert media_language_settings.language == "T"
    assert media_settings.meetings_auto_download is True
    assert obs_settings.enabled is True
    assert obs_settings.connection == (4456, "secret")
    assert obs_settings.scenes == ("Default", "Media")
    assert auto_share_settings.enabled is True
    assert auto_share_settings.hotkey == "Ctrl+Shift+S"
    assert auto_share_settings.click_position == (300, 250)


def test_qsettings_onboarding_settings_keeps_optional_integrations_disabled():
    obs_settings = _OBSSettings()
    auto_share_settings = _AutoShareSettings()
    media_settings = _MediaSettings()
    adapter = QSettingsOnboardingSettings(
        lambda _profile_id: OnboardingSettingsStores(
            app_settings=_AppSettings(),
            media_language_settings=_MediaLanguageSettings(),
            media_settings=media_settings,
            obs_settings=obs_settings,
            auto_share_settings=auto_share_settings,
        )
    )

    adapter.apply(
        "main_hall",
        ProfileOnboardingCommand(
            name="Main Hall",
            interface_language="en",
            obs=OBSOnboardingConfiguration(
                enabled=True,
                automatic_scene_switching=False,
                default_scene="Ignored",
                media_scene="Ignored",
            ),
        ),
    )

    assert media_settings.meetings_auto_download is False
    assert obs_settings.enabled is True
    assert obs_settings.scenes == ("", "")
    assert auto_share_settings.enabled is False
    assert auto_share_settings.hotkey == ""
    assert auto_share_settings.click_position is None
