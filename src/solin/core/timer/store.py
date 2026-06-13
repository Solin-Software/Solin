"""
store.py — Solin timer domain
=============================
Per-profile persistence for the advanced timer, layered on the profile-scoped
``QSettings`` wrapper (:mod:`solin.core.profiles.settings`).

What is persisted (all under the ``Timer`` settings app of the active profile):
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
from solin.core.profiles import settings as _ps

from .models import ClockConfig, MeetingSchedule, MeetingType
from .schedule_factory import normalize_schedule

def _schedule_key(week_monday: str, meeting_type: MeetingType) -> str:
    return f"schedule/{week_monday}/{meeting_type.value}"


def _settings_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


class TimerStore:
    """Thin persistence facade. Stateless — reads the active profile each call."""

    def _prefs(self):
        return _ps.prefs(QSETTINGS_TIMER_APP)

    # ── ClockConfig ───────────────────────────────────────────────────────────

    def load_clock_config(self) -> ClockConfig:
        raw = _settings_str(self._prefs().value(SettingsKey.TIMER_CLOCK_CONFIG, "", str))
        if not raw:
            return ClockConfig()
        try:
            return ClockConfig.from_dict(json.loads(raw)).clamped()
        except (ValueError, TypeError):
            return ClockConfig()

    def save_clock_config(self, config: ClockConfig) -> None:
        s = self._prefs()
        s.setValue(SettingsKey.TIMER_CLOCK_CONFIG, json.dumps(config.clamped().to_dict()))
        s.sync()

    # ── Visibility flag (reserve vs. show) ────────────────────────────────────

    def get_timer_visible(self) -> bool:
        return bool(self._prefs().value(SettingsKey.TIMER_VISIBLE, True, bool))

    def set_timer_visible(self, visible: bool) -> None:
        s = self._prefs()
        s.setValue(SettingsKey.TIMER_VISIBLE, bool(visible))
        s.sync()

    # ── Last-selected meeting type ────────────────────────────────────────────

    def load_last_meeting_type(self) -> MeetingType:
        raw = _settings_str(
            self._prefs().value(
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
        s = self._prefs()
        s.setValue(SettingsKey.TIMER_LAST_MEETING_TYPE, meeting_type.value)
        s.sync()

    # ── Schedules ─────────────────────────────────────────────────────────────

    def load_schedule(self, week_monday: date,
                      meeting_type: MeetingType) -> MeetingSchedule | None:
        key = _schedule_key(week_monday.isoformat(), meeting_type)
        raw = _settings_str(self._prefs().value(key, "", str))
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
        s = self._prefs()
        key = _schedule_key(schedule.week_monday, schedule.meeting_type)
        s.setValue(key, json.dumps(schedule.to_dict()))
        s.sync()
