"""
store.py — Solin timer domain
=============================
Per-profile persistence for the advanced timer, bound to an explicitly injected
settings namespace.

What is persisted (all under the injected ``Timer`` settings app):
    • the global ``ClockConfig`` for the profile;
    • the last-selected meeting type and the timer-window visibility flag;
    • one ``MeetingSchedule`` per (week-monday, meeting-type), serialized as
      JSON *including* each part's live run-state (started_at / accumulated /
      state) so a meeting in progress is restored verbatim after a restart.

Values are stored as JSON strings — robust across QSettings backends and
trivially reusable as a future network payload.
"""

from __future__ import annotations

import json
from datetime import date

from solin.core.foundation.constants import QSETTINGS_TIMER_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings

from .models import ClockConfig, MeetingSchedule, MeetingType
from .schedule_factory import normalize_schedule

def _schedule_key(week_monday: str, meeting_type: MeetingType) -> str:
    return f"schedule/{week_monday}/{meeting_type.value}"


def _settings_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


class TimerStore:
    """Thin persistence facade bound to one profile."""

    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    @classmethod
    def for_profile_settings(cls, profile_settings: ProfileSettings) -> "TimerStore":
        return cls(SettingsStore.for_namespace(profile_settings.organization, QSETTINGS_TIMER_APP))

    # ── ClockConfig ───────────────────────────────────────────────────────────

    def load_clock_config(self) -> ClockConfig:
        raw = self._settings.string(SettingsKey.TIMER_CLOCK_CONFIG)
        if not raw:
            return ClockConfig()
        try:
            return ClockConfig.from_dict(json.loads(raw)).clamped()
        except (ValueError, TypeError):
            return ClockConfig()

    def save_clock_config(self, config: ClockConfig) -> None:
        self._settings.set_value(
            SettingsKey.TIMER_CLOCK_CONFIG,
            json.dumps(config.clamped().to_dict()),
        )

    # ── Visibility flag (reserve vs. show) ────────────────────────────────────

    def get_timer_visible(self) -> bool:
        return bool(self._settings.value(SettingsKey.TIMER_VISIBLE, True, bool))

    def set_timer_visible(self, visible: bool) -> None:
        self._settings.set_value(SettingsKey.TIMER_VISIBLE, bool(visible))

    # ── Last-selected meeting type ────────────────────────────────────────────

    def load_last_meeting_type(self) -> MeetingType:
        raw = _settings_str(
            self._settings.value(
                SettingsKey.TIMER_LAST_MEETING_TYPE,
                MeetingType.MIDWEEK.value,
                str,
            ),
            MeetingType.MIDWEEK.value,
        )
        try:
            return MeetingType(raw)
        except ValueError:
            return MeetingType.MIDWEEK

    def save_last_meeting_type(self, meeting_type: MeetingType) -> None:
        self._settings.set_value(SettingsKey.TIMER_LAST_MEETING_TYPE, meeting_type.value)

    # ── Schedules ─────────────────────────────────────────────────────────────

    def load_schedule(self, week_monday: date,
                      meeting_type: MeetingType) -> MeetingSchedule | None:
        key = _schedule_key(week_monday.isoformat(), meeting_type)
        raw = self._settings.string(key)
        if not raw:
            return None
        try:
            schedule = MeetingSchedule.from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError):
            return None
        if normalize_schedule(schedule):
            self.save_schedule(schedule)
        return schedule

    def save_schedule(self, schedule: MeetingSchedule) -> None:
        key = _schedule_key(schedule.week_monday, schedule.meeting_type)
        self._settings.set_value(key, json.dumps(schedule.to_dict()))
