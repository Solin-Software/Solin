"""Persistence adapters for onboarding."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.integrations.automation.settings import AutoShareSettingsStore
from solin.core.jw.background_song_settings import BackgroundSongSettingsStore
from solin.core.jw.language_settings import JWLanguageSettingsStore
from solin.core.media.settings import MediaSettingsStore
from solin.core.meetings.schedule_settings import MeetingScheduleSettingsStore
from solin.core.onboarding.application import ProfileOnboardingCommand


@dataclass(frozen=True, slots=True)
class OnboardingSettingsStores:
    app_settings: ProfileAppSettingsStore
    media_language_settings: JWLanguageSettingsStore
    media_settings: MediaSettingsStore
    meeting_schedule_settings: MeetingScheduleSettingsStore
    background_song_settings: BackgroundSongSettingsStore
    auto_share_settings: AutoShareSettingsStore


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
        stores.media_settings.set_meetings_auto_download(
            command.download_meeting_media
        )
        if command.meeting_schedule is not None:
            for slot in command.meeting_schedule.slots:
                stores.meeting_schedule_settings.set_slot(
                    slot.kind,
                    slot.weekday,
                    slot.time_text,
                )
        stores.background_song_settings.set_enabled(
            command.meeting_schedule is not None
            and command.meeting_schedule.has_configured_slot
        )

        auto_share_settings = stores.auto_share_settings
        auto_share_settings.set_enabled(command.zoom_share.enabled)
        if command.zoom_share.enabled:
            auto_share_settings.set_hotkey(command.zoom_share.hotkey)
            auto_share_settings.set_click_position(
                command.zoom_share.click_x,
                command.zoom_share.click_y,
            )
