from __future__ import annotations

import pytest

from solin.core.meetings.schedule import (
    MIDWEEK,
    WEEKEND,
    MeetingSchedule,
    MeetingSlot,
)
from solin.core.onboarding.application import (
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


class _MeetingScheduleSettings:
    def __init__(self) -> None:
        self.slots: dict[str, tuple[int, str]] = {}

    def set_slot(self, kind: str, weekday: int, time_text: str) -> None:
        self.slots[kind] = (weekday, time_text)


class _BackgroundSongSettings:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled


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
    meeting_schedule_settings = _MeetingScheduleSettings()
    auto_share_settings = _AutoShareSettings()
    requested_profiles = []

    adapter = QSettingsOnboardingSettings(
        lambda profile_id: (
            requested_profiles.append(profile_id)
            or OnboardingSettingsStores(
                app_settings=app_settings,
                media_language_settings=media_language_settings,
                media_settings=media_settings,
                meeting_schedule_settings=meeting_schedule_settings,
                background_song_settings=_BackgroundSongSettings(),
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
            meeting_schedule=MeetingSchedule(
                midweek=MeetingSlot.from_values(MIDWEEK, 2, "19:45"),
                weekend=MeetingSlot.from_values(WEEKEND, 6, "10:00"),
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
    assert meeting_schedule_settings.slots == {
        MIDWEEK: (2, "19:45"),
        WEEKEND: (6, "10:00"),
    }
    assert auto_share_settings.enabled is True
    assert auto_share_settings.hotkey == "Ctrl+Shift+S"
    assert auto_share_settings.click_position == (300, 250)


def test_qsettings_onboarding_settings_defaults_zoom_sharing_to_disabled():
    auto_share_settings = _AutoShareSettings()
    media_settings = _MediaSettings()
    meeting_schedule_settings = _MeetingScheduleSettings()
    adapter = QSettingsOnboardingSettings(
        lambda _profile_id: OnboardingSettingsStores(
            app_settings=_AppSettings(),
            media_language_settings=_MediaLanguageSettings(),
            media_settings=media_settings,
            meeting_schedule_settings=meeting_schedule_settings,
            background_song_settings=_BackgroundSongSettings(),
            auto_share_settings=auto_share_settings,
        )
    )

    adapter.apply(
        "main_hall",
        ProfileOnboardingCommand(
            name="Main Hall",
            interface_language="en",
        ),
    )

    assert media_settings.meetings_auto_download is False
    assert meeting_schedule_settings.slots == {}
    assert auto_share_settings.enabled is False
    assert auto_share_settings.hotkey == ""
    assert auto_share_settings.click_position is None


@pytest.mark.parametrize("initially_enabled", [False, True])
@pytest.mark.parametrize(
    ("meeting_schedule", "expected_enabled"),
    [
        pytest.param(None, False, id="no-congregation-schedule"),
        pytest.param(
            MeetingSchedule(MeetingSlot(MIDWEEK), MeetingSlot(WEEKEND)),
            False,
            id="unconfigured-schedule",
        ),
        pytest.param(
            MeetingSchedule(
                MeetingSlot.from_values(MIDWEEK, 2, "19:45"),
                MeetingSlot(WEEKEND),
            ),
            True,
            id="midweek-only",
        ),
        pytest.param(
            MeetingSchedule(
                MeetingSlot(MIDWEEK),
                MeetingSlot.from_values(WEEKEND, 6, "10:00"),
            ),
            True,
            id="weekend-only",
        ),
        pytest.param(
            MeetingSchedule(
                MeetingSlot.from_values(MIDWEEK, 2, "19:45"),
                MeetingSlot.from_values(WEEKEND, 6, "10:00"),
            ),
            True,
            id="both-meetings",
        ),
    ],
)
def test_onboarding_enables_background_song_only_with_configured_schedule(
    meeting_schedule, expected_enabled, initially_enabled,
):
    background_song_settings = _BackgroundSongSettings(initially_enabled)
    adapter = QSettingsOnboardingSettings(
        lambda _profile_id: OnboardingSettingsStores(
            app_settings=_AppSettings(),
            media_language_settings=_MediaLanguageSettings(),
            media_settings=_MediaSettings(),
            meeting_schedule_settings=_MeetingScheduleSettings(),
            background_song_settings=background_song_settings,
            auto_share_settings=_AutoShareSettings(),
        )
    )

    adapter.apply(
        "main_hall",
        ProfileOnboardingCommand(
            name="Main Hall",
            interface_language="en",
            meeting_schedule=meeting_schedule,
        ),
    )

    assert background_song_settings.enabled is expected_enabled
