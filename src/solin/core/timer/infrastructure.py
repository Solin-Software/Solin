"""Profile-local timer persistence for settings, weekly schedules and custom schedules.

What is persisted (all under the injected ``Timer`` settings app):
    • the profile's ``ClockConfig`` and pacing mode;
    • the last-selected meeting type and the timer-window visibility flag;
    • one ``MeetingSchedule`` per (week-monday, meeting-type), serialized as
      JSON *including* each part's live run-state (started_at / accumulated /
      state) so a meeting in progress is restored verbatim after a restart.

Custom schedules and the selected custom ID have separate atomic JSON files.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

from solin.core.foundation.constants import QSETTINGS_TIMER_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings
from solin.core.storage.json_files import JsonDirectorySyncError, read_json_file, write_json_atomic

from .custom_schedule import TimerOperationError
from .models import AssignmentSetup, ClockConfig, CustomTimerSchedule, MeetingSchedule, MeetingType
from .pacing_models import PacingMode
from .schedule_factory import build_default_schedule, normalize_schedule


log = logging.getLogger(__name__)
_CUSTOM_SCHEDULE_ID = re.compile(r"[0-9a-f]{32}")
_CUSTOM_SCHEDULE_VERSION = 1


def _schedule_key(week_monday: str, meeting_type: MeetingType) -> str:
    return f"schedule/{week_monday}/{meeting_type.value}"


def _settings_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


class QSettingsTimerRepository:
    """Thin persistence facade bound to one profile."""

    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "QSettingsTimerRepository":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_TIMER_APP,
            )
        )

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
            checked=True,
        )

    def load_pacing_mode(self) -> PacingMode:
        raw = self._settings.string(SettingsKey.TIMER_PACING_MODE)
        try:
            return PacingMode(raw)
        except ValueError:
            return PacingMode.OFF

    def save_pacing_mode(self, mode: PacingMode) -> None:
        self._settings.set_value(SettingsKey.TIMER_PACING_MODE, mode.value, checked=True)

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
        self._settings.set_value(
            SettingsKey.TIMER_LAST_MEETING_TYPE, meeting_type.value, checked=True,
        )

    # ── Schedules ─────────────────────────────────────────────────────────────

    def load_schedule(
        self,
        week_monday: date,
        meeting_type: MeetingType,
    ) -> MeetingSchedule | None:
        key = _schedule_key(week_monday.isoformat(), meeting_type)
        raw = self._settings.value(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Invalid schedule")
            schedule = MeetingSchedule.from_dict(data)
            if schedule.week_monday != week_monday.isoformat() or schedule.meeting_type is not meeting_type:
                raise ValueError("Schedule does not match its key")
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            # Preserve an unreadable stored plan until the operator edits its fallback.
            fallback = build_default_schedule(week_monday, meeting_type)
            fallback.assignment_setup = AssignmentSetup.MANUAL
            fallback.has_unreadable_record = True
            return fallback
        if normalize_schedule(schedule):
            self.save_schedule(schedule)
        return schedule

    def save_schedule(self, schedule: MeetingSchedule) -> None:
        key = _schedule_key(schedule.week_monday, schedule.meeting_type)
        self._settings.set_value(key, json.dumps(schedule.to_dict()), checked=True)


class JsonCustomTimerRepository:
    """One atomic document per custom schedule in the active profile."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def list_schedules(self) -> tuple[CustomTimerSchedule, ...]:
        schedules = []
        try:
            paths = sorted(self._directory.iterdir())
        except FileNotFoundError:
            return ()
        for path in paths:
            if path.match("*.json") and _CUSTOM_SCHEDULE_ID.fullmatch(path.stem):
                schedule = self.load_schedule(path.stem)
                if schedule is not None:
                    schedules.append(schedule)
        return tuple(sorted(schedules, key=lambda schedule: (schedule.title.casefold(), schedule.id)))

    def load_schedule(self, schedule_id: str) -> CustomTimerSchedule | None:
        path = self._path(schedule_id)
        try:
            data = read_json_file(path)
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError):
            log.warning("Unreadable custom timer schedule %s", schedule_id, exc_info=True)
            return None
        try:
            if data["version"] != _CUSTOM_SCHEDULE_VERSION or data["id"] != schedule_id:
                raise ValueError("Invalid timer schedule version or identity.")
            return CustomTimerSchedule.from_dict(data)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            log.warning("Invalid custom timer schedule %s", schedule_id, exc_info=True)
            return None

    def save_schedule(self, schedule: CustomTimerSchedule) -> bool:
        path = self._path(schedule.id)
        try:
            write_json_atomic(path, dict(schedule.to_dict(), version=_CUSTOM_SCHEDULE_VERSION))
        except JsonDirectorySyncError:
            log.warning("Custom timer schedule was replaced without confirming durability.", exc_info=True)
            return False
        return True

    def delete_schedule(self, schedule_id: str) -> None:
        self._path(schedule_id).unlink()

    def load_selection(self) -> str | None:
        try:
            schedule_id = read_json_file(self._directory / "selection.json")
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError):
            log.warning("Unreadable custom timer selection.", exc_info=True)
            return None
        if isinstance(schedule_id, str) and _CUSTOM_SCHEDULE_ID.fullmatch(schedule_id):
            return schedule_id
        return None

    def save_selection(self, schedule_id: str | None) -> None:
        if schedule_id is not None:
            self._path(schedule_id)
        write_json_atomic(self._directory / "selection.json", schedule_id)

    def _path(self, schedule_id: str) -> Path:
        if not isinstance(schedule_id, str) or not _CUSTOM_SCHEDULE_ID.fullmatch(schedule_id):
            raise TimerOperationError("invalid", "Invalid timer schedule ID.")
        return self._directory / f"{schedule_id}.json"
