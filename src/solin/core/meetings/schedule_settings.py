from __future__ import annotations

from dataclasses import dataclass

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.meetings.schedule import (
    MIDWEEK,
    UNCONFIGURED_WEEKDAY,
    WEEKEND,
    MeetingSchedule,
    MeetingSlot,
    normalize_weekday,
)
from solin.core.profiles.settings import ProfileSettings


@dataclass(frozen=True, slots=True)
class MeetingScheduleSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "MeetingScheduleSettingsStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )

    def load(self) -> MeetingSchedule:
        return MeetingSchedule(
            midweek=MeetingSlot.from_values(
                MIDWEEK,
                self.settings.value(
                    SettingsKey.MEETING_MIDWEEK_DAY,
                    UNCONFIGURED_WEEKDAY,
                ),
                self.settings.string(SettingsKey.MEETING_MIDWEEK_TIME),
            ),
            weekend=MeetingSlot.from_values(
                WEEKEND,
                self.settings.value(
                    SettingsKey.MEETING_WEEKEND_DAY,
                    UNCONFIGURED_WEEKDAY,
                ),
                self.settings.string(SettingsKey.MEETING_WEEKEND_TIME),
            ),
        )

    def slot_values(self, kind: str) -> tuple[int, str]:
        day_key, time_key = self._keys_for(kind)
        return (
            normalize_weekday(self.settings.value(day_key, UNCONFIGURED_WEEKDAY)),
            self.settings.string(time_key),
        )

    def set_slot(self, kind: str, weekday: int, time_text: str) -> None:
        day_key, time_key = self._keys_for(kind)
        normalized_weekday = normalize_weekday(weekday)
        if normalized_weekday == UNCONFIGURED_WEEKDAY:
            self.settings.set_value(day_key, UNCONFIGURED_WEEKDAY, sync=False)
            self.settings.set_value(time_key, "", sync=False)
        else:
            self.settings.set_value(day_key, normalized_weekday, sync=False)
            self.settings.set_value(time_key, time_text, sync=False)
        self.settings.sync()

    @staticmethod
    def _keys_for(kind: str) -> tuple[str, str]:
        if kind == MIDWEEK:
            return (
                SettingsKey.MEETING_MIDWEEK_DAY,
                SettingsKey.MEETING_MIDWEEK_TIME,
            )
        if kind == WEEKEND:
            return (
                SettingsKey.MEETING_WEEKEND_DAY,
                SettingsKey.MEETING_WEEKEND_TIME,
            )
        raise ValueError(f"Unknown meeting schedule kind: {kind}")
