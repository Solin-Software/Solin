from __future__ import annotations

from datetime import date

from solin.core.timer.application import TimerSession
from solin.core.timer.models import (
    ClockConfig,
    ClockMode,
    Direction,
    MeetingSchedule,
    MeetingType,
    PartState,
    Section,
)
from solin.core.timer.schedule_factory import build_default_schedule
from tests._paths import REPO_ROOT


_WEEK = date(2026, 6, 1)


class _Repository:
    def __init__(self) -> None:
        self.clock_config = ClockConfig(
            mode=ClockMode.ANALOG,
            text_scale_pct=250,
            freeze_seconds=-10,
        )
        self.timer_visible = True
        self.last_meeting_type = MeetingType.MIDWEEK
        self.schedules: dict[tuple[date, MeetingType], MeetingSchedule] = {}
        self.saved_clock_configs: list[ClockConfig] = []
        self.saved_meeting_types: list[MeetingType] = []
        self.saved_schedules: list[MeetingSchedule] = []

    def load_clock_config(self) -> ClockConfig:
        return self.clock_config

    def save_clock_config(self, config: ClockConfig) -> None:
        self.saved_clock_configs.append(config)
        self.clock_config = config

    def get_timer_visible(self) -> bool:
        return self.timer_visible

    def set_timer_visible(self, visible: bool) -> None:
        self.timer_visible = visible

    def load_last_meeting_type(self) -> MeetingType:
        return self.last_meeting_type

    def save_last_meeting_type(self, meeting_type: MeetingType) -> None:
        self.saved_meeting_types.append(meeting_type)
        self.last_meeting_type = meeting_type

    def load_schedule(
        self,
        week_monday: date,
        meeting_type: MeetingType,
    ) -> MeetingSchedule | None:
        return self.schedules.get((week_monday, meeting_type))

    def save_schedule(self, schedule: MeetingSchedule) -> None:
        self.saved_schedules.append(schedule)


def test_timer_session_loads_context_and_clamps_clock_config() -> None:
    repo = _Repository()
    persisted = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    repo.schedules[(_WEEK, MeetingType.MIDWEEK)] = persisted

    session = TimerSession(repo, current_week_monday=_WEEK)

    assert session.week_monday == _WEEK
    assert session.is_current_week
    assert session.meeting_type is MeetingType.MIDWEEK
    assert session.schedule is persisted
    assert session.clock_config.text_scale_pct == 100
    assert session.clock_config.freeze_seconds == 0


def test_timer_session_switches_week_and_meeting_type_through_repository() -> None:
    repo = _Repository()
    next_week = date(2026, 6, 8)
    weekend = build_default_schedule(next_week, MeetingType.WEEKEND)
    repo.schedules[(next_week, MeetingType.WEEKEND)] = weekend
    session = TimerSession(repo, current_week_monday=_WEEK)

    session.next_week()
    assert session.week_monday == next_week
    assert not session.is_current_week

    assert session.set_meeting_type(MeetingType.WEEKEND)
    assert session.meeting_type is MeetingType.WEEKEND
    assert session.schedule is weekend
    assert repo.saved_meeting_types == [MeetingType.WEEKEND]

    assert session.go_to_current_week(_WEEK) == -1
    assert session.week_monday == _WEEK
    assert session.is_current_week


def test_timer_session_persists_schedule_edits_only_for_idle_parts() -> None:
    repo = _Repository()
    session = TimerSession(repo, current_week_monday=_WEEK)
    part = session.schedule.parts_in(Section.MINISTRY)[0]
    original_seconds = part.planned_seconds

    assert session.adjust_part(part.id, 60)
    assert part.planned_seconds == original_seconds + 60
    assert repo.saved_schedules[-1] is session.schedule

    saved_count = len(repo.saved_schedules)
    part.state = PartState.RUNNING

    assert not session.set_part_seconds(part.id, 60)
    assert len(repo.saved_schedules) == saved_count


def test_timer_session_updates_clock_and_visibility_through_repository() -> None:
    repo = _Repository()
    session = TimerSession(repo, current_week_monday=_WEEK)

    assert session.update_clock("unknown", "ignored") is None
    assert repo.saved_clock_configs == []

    config = session.update_clock("direction", Direction.UP.value)
    assert config is not None
    assert config.direction is Direction.UP
    assert repo.saved_clock_configs == [config]

    assert session.timer_visible() is True
    session.set_timer_visible(False)
    assert session.timer_visible() is False


def test_timer_source_boundaries_keep_application_pure_and_widget_injected() -> None:
    application_source = (
        REPO_ROOT / "src" / "solin" / "core" / "timer" / "application.py"
    ).read_text(encoding="utf-8")
    widget_source = (
        REPO_ROOT / "src" / "solin" / "widgets" / "timer_widget.py"
    ).read_text(encoding="utf-8")
    bridge_source = (
        REPO_ROOT / "src" / "solin" / "ui" / "qml" / "timer_bridge.py"
    ).read_text(encoding="utf-8")

    assert "PySide6" not in application_source
    assert "ProfileSettings" not in application_source
    assert "SettingsStore" not in application_source
    assert "TimerBridge(" not in widget_source
    assert "QFileDialog" not in bridge_source
    assert "QMessageBox" not in bridge_source
    assert "QStandardPaths" not in bridge_source
