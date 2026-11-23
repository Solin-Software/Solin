"""Framework-independent contracts for Solin's local meeting-timer remote control."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import ClassVar, Final, TypeAlias

from ...timer.hand_alert import TimerHandAlert
from ...timer.models import (
    CONFIGURABLE_PART_SECTIONS,
    MAX_PLANNED_PART_SECONDS,
    MAX_SECTION_PART_COUNT,
    MIN_PLANNED_PART_SECONDS,
    MIN_SECTION_PART_COUNT,
    AnalogClockStyle,
    ClockConfig,
    ClockMode,
    Direction,
    MeetingType,
    PartState,
    PartTimerDisplay,
    Section,
)
from ..features import (
    DEFAULT_TIMER_REMOTE_FEATURE_FLAGS,
    TimerRemoteFeatureFlags,
    TimerRemoteHandAlertPolicy,
)
from ..validation import (
    MAX_ERROR_TEXT_LENGTH,
    JsonObject,
    payload_int,
    payload_optional_string,
    payload_string,
    reject_unknown_fields,
    require_finite_float,
    require_identifier,
    require_int,
    require_nonnegative_int,
    require_string,
    require_uuid,
)


__all__ = [
    "AdjustPartCommand",
    "NavigateWeekCommand",
    "ResetPartCommand",
    "SetMeetingTypeCommand",
    "SetPartSecondsCommand",
    "SetSectionCountCommand",
    "StartPartCommand",
    "StopPartCommand",
    "TimerClockWireConfig",
    "TimerCommand",
    "TimerCommandError",
    "TimerCommandErrorCode",
    "TimerCommandResult",
    "TimerCommandType",
    "TimerControlCapabilities",
    "TimerLiveWireSnapshot",
    "TimerPartWireItem",
    "TimerRemoteCommand",
    "TimerScheduleWireSnapshot",
    "TimerWeekTarget",
    "parse_timer_command",
]

_MIN_UTC_OFFSET_MINUTES: Final = -14 * 60
_MAX_UTC_OFFSET_MINUTES: Final = 14 * 60
_ALLOWED_ADJUST_DELTAS: Final = frozenset({-60, -1, 1, 60})


def hand_alert_payload(
    alert: TimerHandAlert,
    policy: TimerRemoteHandAlertPolicy,
    *,
    authenticated: bool,
) -> JsonObject | None:
    if policy is TimerRemoteHandAlertPolicy.DISABLED or (
        policy is TimerRemoteHandAlertPolicy.AUTHENTICATED_ONLY and not authenticated
    ):
        return None
    if alert.names:
        payload: JsonObject = {"kind": "automatic", "count": len(alert.names)}
        if authenticated or policy is TimerRemoteHandAlertPolicy.PUBLIC_NAMES:
            payload["names"] = list(alert.names)
        return payload
    return {"kind": "manual"} if alert.manual else None


@dataclass(frozen=True, slots=True)
class TimerClockWireConfig:
    """Validated wire representation of ClockConfig."""

    mode: ClockMode = ClockMode.DIGITAL
    analog_style: AnalogClockStyle = AnalogClockStyle.SIGNATURE
    hour_format_24h: bool = True
    show_ampm: bool = False
    show_seconds: bool = True
    direction: Direction = Direction.DOWN
    part_timer_display: PartTimerDisplay = PartTimerDisplay.TIMER
    text_scale_pct: int = 100
    freeze_seconds: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ClockMode):
            raise TypeError("mode must be a ClockMode")
        if not isinstance(self.analog_style, AnalogClockStyle):
            raise TypeError("analog_style must be an AnalogClockStyle")
        for bool_field in ("hour_format_24h", "show_ampm", "show_seconds"):
            if not isinstance(getattr(self, bool_field), bool):
                raise TypeError(f"{bool_field} must be a boolean")
        if not isinstance(self.direction, Direction):
            raise TypeError("direction must be a Direction")
        if not isinstance(self.part_timer_display, PartTimerDisplay):
            raise TypeError("part_timer_display must be a PartTimerDisplay")
        require_int(self.text_scale_pct, field_name="text_scale_pct")
        if not 10 <= self.text_scale_pct <= 100:
            raise ValueError("text_scale_pct must be between 10 and 100")
        require_nonnegative_int(self.freeze_seconds, field_name="freeze_seconds")
        if self.freeze_seconds > 30:
            raise ValueError("freeze_seconds must be at most 30")

    @classmethod
    def from_clock_config(cls, config: ClockConfig) -> TimerClockWireConfig:
        clamped = config.clamped()
        return cls(
            mode=clamped.mode,
            analog_style=clamped.analog_style,
            hour_format_24h=clamped.hour_format_24h,
            show_ampm=clamped.show_ampm,
            show_seconds=clamped.show_seconds,
            direction=clamped.direction,
            part_timer_display=clamped.part_timer_display,
            text_scale_pct=clamped.text_scale_pct,
            freeze_seconds=clamped.freeze_seconds,
        )

    def to_dict(self) -> JsonObject:
        return {
            "mode": self.mode.value,
            "analogStyle": self.analog_style.value,
            "hourFormat24h": self.hour_format_24h,
            "showAmpm": self.show_ampm,
            "showSeconds": self.show_seconds,
            "direction": self.direction.value,
            "partTimerDisplay": self.part_timer_display.value,
            "textScalePct": self.text_scale_pct,
            "freezeSeconds": self.freeze_seconds,
        }


@dataclass(frozen=True, slots=True)
class TimerControlCapabilities:
    """Server-authoritative capabilities governing timer remote control."""

    can_control_parts: bool = True
    can_adjust_planned_minutes: bool = True
    can_set_planned_seconds: bool = True
    can_set_section_count: bool = True
    can_switch_meeting_type: bool = True
    can_navigate_weeks: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "can_control_parts",
            "can_adjust_planned_minutes",
            "can_set_planned_seconds",
            "can_set_section_count",
            "can_switch_meeting_type",
            "can_navigate_weeks",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a boolean")

    @classmethod
    def from_feature_flags(
        cls,
        flags: TimerRemoteFeatureFlags = DEFAULT_TIMER_REMOTE_FEATURE_FLAGS,
    ) -> TimerControlCapabilities:
        return cls(
            can_control_parts=True,
            can_adjust_planned_minutes=True,
            can_set_planned_seconds=flags.allow_schedule_structure_edits,
            can_set_section_count=flags.allow_schedule_structure_edits,
            can_switch_meeting_type=True,
            can_navigate_weeks=flags.allow_schedule_structure_edits,
        )

    def allows_command(self, command: TimerCommand) -> bool:
        if isinstance(command, (StartPartCommand, StopPartCommand, ResetPartCommand)):
            return self.can_control_parts
        if isinstance(command, AdjustPartCommand):
            if abs(command.delta_seconds) == 1:
                return self.can_control_parts
            return self.can_adjust_planned_minutes
        if isinstance(command, SetPartSecondsCommand):
            return self.can_set_planned_seconds
        if isinstance(command, SetSectionCountCommand):
            return self.can_set_section_count
        if isinstance(command, SetMeetingTypeCommand):
            return self.can_switch_meeting_type
        if isinstance(command, NavigateWeekCommand):
            return self.can_navigate_weeks
        return False

    def to_dict(self) -> JsonObject:
        return {
            "controlParts": self.can_control_parts,
            "adjustPlannedMinutes": self.can_adjust_planned_minutes,
            "setPlannedSeconds": self.can_set_planned_seconds,
            "setSectionCount": self.can_set_section_count,
            "switchMeetingType": self.can_switch_meeting_type,
            "navigateWeeks": self.can_navigate_weeks,
        }


@dataclass(frozen=True, slots=True)
class TimerLiveWireSnapshot:
    """Public-safe live clock and countdown state anchored to desktop epoch time."""

    live_revision: int
    server_epoch_ms: int
    utc_offset_minutes: int
    active: bool = False
    state: PartState = PartState.IDLE
    direction: Direction = Direction.DOWN
    active_part_id: str | None = None
    active_part_title: str = ""
    active_section: Section | None = None
    planned_seconds: int = 0
    elapsed_seconds: float = 0.0
    clock_config: TimerClockWireConfig = TimerClockWireConfig()

    def __post_init__(self) -> None:
        require_nonnegative_int(self.live_revision, field_name="live_revision")
        require_nonnegative_int(self.server_epoch_ms, field_name="server_epoch_ms")
        require_int(self.utc_offset_minutes, field_name="utc_offset_minutes")
        if not _MIN_UTC_OFFSET_MINUTES <= self.utc_offset_minutes <= _MAX_UTC_OFFSET_MINUTES:
            raise ValueError("utc_offset_minutes is out of range")
        if not isinstance(self.active, bool):
            raise TypeError("active must be a boolean")
        if not isinstance(self.state, PartState):
            raise TypeError("state must be a PartState")
        if not isinstance(self.direction, Direction):
            raise TypeError("direction must be a Direction")
        if self.active_part_id is not None:
            require_identifier(self.active_part_id, field_name="active_part_id")
        require_string(self.active_part_title, field_name="active_part_title", allow_empty=True)
        if self.active_section is not None and not isinstance(self.active_section, Section):
            raise TypeError("active_section must be a Section or None")
        require_nonnegative_int(self.planned_seconds, field_name="planned_seconds")
        elapsed = require_finite_float(self.elapsed_seconds, field_name="elapsed_seconds")
        if elapsed < 0.0:
            raise ValueError("elapsed_seconds must not be negative")
        if not isinstance(self.clock_config, TimerClockWireConfig):
            raise TypeError("clock_config must be a TimerClockWireConfig")
        if not self.active:
            if self.active_part_id is not None:
                raise ValueError("inactive timer snapshot cannot carry active_part_id")
            if self.state is not PartState.IDLE:
                raise ValueError("inactive timer snapshot must have idle state")
        elif self.active_part_id is None:
            raise ValueError("active timer snapshot requires active_part_id")

    @classmethod
    def idle(
        cls,
        *,
        server_epoch_ms: int,
        utc_offset_minutes: int = 0,
        clock_config: TimerClockWireConfig | None = None,
        live_revision: int = 0,
    ) -> TimerLiveWireSnapshot:
        cfg = clock_config or TimerClockWireConfig()
        return cls(
            live_revision=live_revision,
            server_epoch_ms=server_epoch_ms,
            utc_offset_minutes=utc_offset_minutes,
            active=False,
            state=PartState.IDLE,
            direction=cfg.direction,
            clock_config=cfg,
        )

    def rebased_at(self, now_epoch_ms: int) -> TimerLiveWireSnapshot:
        """Return a fresh-epoch view without altering the revision."""

        require_nonnegative_int(now_epoch_ms, field_name="now_epoch_ms")
        elapsed = self.elapsed_seconds
        if self.active and self.state is PartState.RUNNING:
            delta_seconds = max(0.0, (now_epoch_ms - self.server_epoch_ms) / 1000.0)
            elapsed = self.elapsed_seconds + delta_seconds
        return TimerLiveWireSnapshot(
            live_revision=self.live_revision,
            server_epoch_ms=now_epoch_ms,
            utc_offset_minutes=self.utc_offset_minutes,
            active=self.active,
            state=self.state,
            direction=self.direction,
            active_part_id=self.active_part_id,
            active_part_title=self.active_part_title,
            active_section=self.active_section,
            planned_seconds=self.planned_seconds,
            elapsed_seconds=elapsed,
            clock_config=self.clock_config,
        )

    def comparable_key(self) -> tuple[object, ...]:
        """Identity key that ignores wall-clock drift while detecting anchor shifts."""

        if self.active and self.state is PartState.RUNNING:
            timing_identity: object = round(
                (self.server_epoch_ms - (self.elapsed_seconds * 1000.0)) / 100.0
            )
        else:
            timing_identity = round(self.elapsed_seconds, 3)
        return (
            self.utc_offset_minutes,
            self.active,
            self.state,
            self.direction,
            self.active_part_id,
            self.active_part_title,
            self.active_section,
            self.planned_seconds,
            timing_identity,
            self.clock_config,
        )

    def to_dict(self) -> JsonObject:
        return {
            "liveRevision": self.live_revision,
            "serverEpochMs": self.server_epoch_ms,
            "utcOffsetMinutes": self.utc_offset_minutes,
            "active": self.active,
            "state": self.state.value,
            "direction": self.direction.value,
            "activePartId": self.active_part_id,
            "activePartTitle": self.active_part_title,
            "activeSection": self.active_section.value if self.active_section else None,
            "plannedSeconds": self.planned_seconds,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "clockConfig": self.clock_config.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class TimerPartWireItem:
    id: str
    display_number: int
    section: Section
    section_key: str
    section_header_title: str
    title: str
    planned_seconds: int
    planned_label: str
    state: PartState
    first_of_section: bool
    show_section_header: bool
    configurable_count: bool
    section_count: int
    duration_editable: bool = True
    section_total_label: str = ""
    started_label: str = ""
    result_label: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.id, field_name="id")
        require_nonnegative_int(self.display_number, field_name="display_number")
        if self.display_number < 1:
            raise ValueError("display_number must be positive")
        if not isinstance(self.section, Section):
            raise TypeError("section must be a Section")
        require_identifier(self.section_key, field_name="section_key")
        require_string(
            self.section_header_title, field_name="section_header_title", allow_empty=True
        )
        require_string(self.title, field_name="title")
        require_nonnegative_int(self.planned_seconds, field_name="planned_seconds")
        require_string(self.planned_label, field_name="planned_label")
        if not isinstance(self.state, PartState):
            raise TypeError("state must be a PartState")
        for bool_field in (
            "first_of_section",
            "show_section_header",
            "configurable_count",
            "duration_editable",
        ):
            if not isinstance(getattr(self, bool_field), bool):
                raise TypeError(f"{bool_field} must be a boolean")
        require_nonnegative_int(self.section_count, field_name="section_count")
        require_string(self.section_total_label, field_name="section_total_label", allow_empty=True)
        require_string(self.started_label, field_name="started_label", allow_empty=True)
        require_string(self.result_label, field_name="result_label", allow_empty=True)

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "displayNumber": self.display_number,
            "section": self.section.value,
            "sectionKey": self.section_key,
            "sectionHeaderTitle": self.section_header_title,
            "title": self.title,
            "plannedSeconds": self.planned_seconds,
            "plannedLabel": self.planned_label,
            "state": self.state.value,
            "firstOfSection": self.first_of_section,
            "showSectionHeader": self.show_section_header,
            "configurableCount": self.configurable_count,
            "sectionCount": self.section_count,
            "durationEditable": self.duration_editable,
            "sectionTotalLabel": self.section_total_label,
            "startedLabel": self.started_label,
            "resultLabel": self.result_label,
        }


@dataclass(frozen=True, slots=True)
class TimerScheduleWireSnapshot:
    schedule_revision: int
    week_monday: str
    week_label: str
    is_current_week: bool
    meeting_type: MeetingType
    parts: tuple[TimerPartWireItem, ...] = ()
    capabilities: TimerControlCapabilities = TimerControlCapabilities()

    def __post_init__(self) -> None:
        require_nonnegative_int(self.schedule_revision, field_name="schedule_revision")
        require_string(self.week_monday, field_name="week_monday")
        try:
            parsed_date = date.fromisoformat(self.week_monday)
        except ValueError as exc:
            raise ValueError("week_monday must be an ISO YYYY-MM-DD date") from exc
        if parsed_date.isoformat() != self.week_monday:
            raise ValueError("week_monday must use canonical ISO format")
        require_string(self.week_label, field_name="week_label", allow_empty=True)
        if not isinstance(self.is_current_week, bool):
            raise TypeError("is_current_week must be a boolean")
        if not isinstance(self.meeting_type, MeetingType):
            raise TypeError("meeting_type must be a MeetingType")
        if not isinstance(self.parts, tuple) or not all(
            isinstance(part, TimerPartWireItem) for part in self.parts
        ):
            raise TypeError("parts must be a tuple of TimerPartWireItem objects")
        part_ids = [part.id for part in self.parts]
        if len(part_ids) != len(set(part_ids)):
            raise ValueError("schedule part ids must be unique")
        if not isinstance(self.capabilities, TimerControlCapabilities):
            raise TypeError("capabilities must be TimerControlCapabilities")

    @classmethod
    def empty(
        cls,
        *,
        week_monday: str = "2026-01-05",
        meeting_type: MeetingType = MeetingType.MIDWEEK,
        capabilities: TimerControlCapabilities | None = None,
    ) -> TimerScheduleWireSnapshot:
        return cls(
            schedule_revision=0,
            week_monday=week_monday,
            week_label="",
            is_current_week=True,
            meeting_type=meeting_type,
            parts=(),
            capabilities=capabilities or TimerControlCapabilities(),
        )

    def to_dict(self) -> JsonObject:
        return {
            "scheduleRevision": self.schedule_revision,
            "weekMonday": self.week_monday,
            "weekLabel": self.week_label,
            "isCurrentWeek": self.is_current_week,
            "meetingType": self.meeting_type.value,
            "parts": [part.to_dict() for part in self.parts],
            "capabilities": self.capabilities.to_dict(),
        }


class TimerCommandType(StrEnum):
    START_PART = "start_part"
    STOP_PART = "stop_part"
    RESET_PART = "reset_part"
    ADJUST_PART = "adjust_part"
    SET_PART_SECONDS = "set_part_seconds"
    SET_SECTION_COUNT = "set_section_count"
    SET_MEETING_TYPE = "set_meeting_type"
    NAVIGATE_WEEK = "navigate_week"


class TimerWeekTarget(StrEnum):
    PREVIOUS = "previous"
    NEXT = "next"
    CURRENT = "current"


@dataclass(frozen=True, slots=True)
class TimerCommand:
    command_id: str

    TYPE: ClassVar[TimerCommandType]

    def __post_init__(self) -> None:
        require_uuid(self.command_id, field_name="command_id")
        if not hasattr(type(self), "TYPE"):
            raise TypeError("TimerCommand is an abstract command contract")

    @property
    def type(self) -> TimerCommandType:
        return self.TYPE

    def to_dict(self) -> JsonObject:
        return {"commandId": self.command_id, "type": self.type.value}


@dataclass(frozen=True, slots=True)
class _ScheduleBoundTimerCommand(TimerCommand):
    schedule_revision: int

    def __post_init__(self) -> None:
        super(_ScheduleBoundTimerCommand, self).__post_init__()
        require_nonnegative_int(self.schedule_revision, field_name="schedule_revision")

    def to_dict(self) -> JsonObject:
        payload = super(_ScheduleBoundTimerCommand, self).to_dict()
        payload["scheduleRevision"] = self.schedule_revision
        return payload


@dataclass(frozen=True, slots=True)
class StartPartCommand(_ScheduleBoundTimerCommand):
    part_id: str

    TYPE: ClassVar[TimerCommandType] = TimerCommandType.START_PART

    def __post_init__(self) -> None:
        super(StartPartCommand, self).__post_init__()
        require_identifier(self.part_id, field_name="part_id")

    def to_dict(self) -> JsonObject:
        payload = super(StartPartCommand, self).to_dict()
        payload["partId"] = self.part_id
        return payload


@dataclass(frozen=True, slots=True)
class StopPartCommand(TimerCommand):
    part_id: str | None = None

    TYPE: ClassVar[TimerCommandType] = TimerCommandType.STOP_PART

    def __post_init__(self) -> None:
        super(StopPartCommand, self).__post_init__()
        if self.part_id is not None:
            require_identifier(self.part_id, field_name="part_id")

    def to_dict(self) -> JsonObject:
        payload = super(StopPartCommand, self).to_dict()
        payload["partId"] = self.part_id
        return payload


@dataclass(frozen=True, slots=True)
class ResetPartCommand(_ScheduleBoundTimerCommand):
    part_id: str

    TYPE: ClassVar[TimerCommandType] = TimerCommandType.RESET_PART

    def __post_init__(self) -> None:
        super(ResetPartCommand, self).__post_init__()
        require_identifier(self.part_id, field_name="part_id")

    def to_dict(self) -> JsonObject:
        payload = super(ResetPartCommand, self).to_dict()
        payload["partId"] = self.part_id
        return payload


@dataclass(frozen=True, slots=True)
class AdjustPartCommand(_ScheduleBoundTimerCommand):
    part_id: str
    delta_seconds: int

    TYPE: ClassVar[TimerCommandType] = TimerCommandType.ADJUST_PART

    def __post_init__(self) -> None:
        super(AdjustPartCommand, self).__post_init__()
        require_identifier(self.part_id, field_name="part_id")
        require_int(self.delta_seconds, field_name="delta_seconds")
        if self.delta_seconds not in _ALLOWED_ADJUST_DELTAS:
            raise ValueError("delta_seconds must be one of -60, -1, 1, or 60")

    def to_dict(self) -> JsonObject:
        payload = super(AdjustPartCommand, self).to_dict()
        payload.update({"partId": self.part_id, "deltaSeconds": self.delta_seconds})
        return payload


@dataclass(frozen=True, slots=True)
class SetPartSecondsCommand(_ScheduleBoundTimerCommand):
    part_id: str
    planned_seconds: int

    TYPE: ClassVar[TimerCommandType] = TimerCommandType.SET_PART_SECONDS

    def __post_init__(self) -> None:
        super(SetPartSecondsCommand, self).__post_init__()
        require_identifier(self.part_id, field_name="part_id")
        require_int(self.planned_seconds, field_name="planned_seconds")
        if not MIN_PLANNED_PART_SECONDS <= self.planned_seconds <= MAX_PLANNED_PART_SECONDS:
            raise ValueError(
                f"planned_seconds must be between {MIN_PLANNED_PART_SECONDS} and {MAX_PLANNED_PART_SECONDS}"
            )

    def to_dict(self) -> JsonObject:
        payload = super(SetPartSecondsCommand, self).to_dict()
        payload.update({"partId": self.part_id, "plannedSeconds": self.planned_seconds})
        return payload


@dataclass(frozen=True, slots=True)
class SetSectionCountCommand(_ScheduleBoundTimerCommand):
    section: Section
    count: int

    TYPE: ClassVar[TimerCommandType] = TimerCommandType.SET_SECTION_COUNT

    def __post_init__(self) -> None:
        super(SetSectionCountCommand, self).__post_init__()
        if not isinstance(self.section, Section):
            raise TypeError("section must be a Section")
        if self.section not in CONFIGURABLE_PART_SECTIONS:
            raise ValueError("section count is only configurable for ministry and living")
        require_int(self.count, field_name="count")
        if not MIN_SECTION_PART_COUNT <= self.count <= MAX_SECTION_PART_COUNT:
            raise ValueError(
                f"count must be between {MIN_SECTION_PART_COUNT} and {MAX_SECTION_PART_COUNT}"
            )

    def to_dict(self) -> JsonObject:
        payload = super(SetSectionCountCommand, self).to_dict()
        payload.update({"section": self.section.value, "count": self.count})
        return payload


@dataclass(frozen=True, slots=True)
class SetMeetingTypeCommand(TimerCommand):
    meeting_type: MeetingType

    TYPE: ClassVar[TimerCommandType] = TimerCommandType.SET_MEETING_TYPE

    def __post_init__(self) -> None:
        super(SetMeetingTypeCommand, self).__post_init__()
        if not isinstance(self.meeting_type, MeetingType):
            raise TypeError("meeting_type must be a MeetingType")

    def to_dict(self) -> JsonObject:
        payload = super(SetMeetingTypeCommand, self).to_dict()
        payload["meetingType"] = self.meeting_type.value
        return payload


@dataclass(frozen=True, slots=True)
class NavigateWeekCommand(TimerCommand):
    target: TimerWeekTarget

    TYPE: ClassVar[TimerCommandType] = TimerCommandType.NAVIGATE_WEEK

    def __post_init__(self) -> None:
        super(NavigateWeekCommand, self).__post_init__()
        if not isinstance(self.target, TimerWeekTarget):
            raise TypeError("target must be a TimerWeekTarget")

    def to_dict(self) -> JsonObject:
        payload = super(NavigateWeekCommand, self).to_dict()
        payload["target"] = self.target.value
        return payload


TimerRemoteCommand: TypeAlias = (
    StartPartCommand
    | StopPartCommand
    | ResetPartCommand
    | AdjustPartCommand
    | SetPartSecondsCommand
    | SetSectionCountCommand
    | SetMeetingTypeCommand
    | NavigateWeekCommand
)


class TimerCommandErrorCode(StrEnum):
    SCHEDULE_STALE = "schedule_stale"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    INVALID = "invalid"
    COMMAND_ID_CONFLICT = "command_id_conflict"
    COMMAND_IN_PROGRESS = "command_in_progress"


@dataclass(frozen=True, slots=True)
class TimerCommandError:
    code: TimerCommandErrorCode
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, TimerCommandErrorCode):
            raise TypeError("code must be a TimerCommandErrorCode")
        require_string(
            self.message,
            field_name="message",
            maximum=MAX_ERROR_TEXT_LENGTH,
        )
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a boolean")

    def to_dict(self) -> JsonObject:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class TimerCommandResult:
    command_id: str
    ok: bool
    live_revision: int
    schedule_revision: int
    error: TimerCommandError | None = None

    def __post_init__(self) -> None:
        require_uuid(self.command_id, field_name="command_id")
        if not isinstance(self.ok, bool):
            raise TypeError("ok must be a boolean")
        require_nonnegative_int(self.live_revision, field_name="live_revision")
        require_nonnegative_int(self.schedule_revision, field_name="schedule_revision")
        if self.error is not None and not isinstance(self.error, TimerCommandError):
            raise TypeError("error must be a TimerCommandError or None")
        if self.ok == (self.error is not None):
            raise ValueError("successful results cannot have errors; failed results require one")

    @classmethod
    def success(
        cls,
        command_id: str,
        *,
        live_revision: int,
        schedule_revision: int,
    ) -> TimerCommandResult:
        return cls(
            command_id=command_id,
            ok=True,
            live_revision=live_revision,
            schedule_revision=schedule_revision,
        )

    @classmethod
    def failure(
        cls,
        command_id: str,
        *,
        live_revision: int,
        schedule_revision: int,
        error: TimerCommandError,
    ) -> TimerCommandResult:
        return cls(
            command_id=command_id,
            ok=False,
            live_revision=live_revision,
            schedule_revision=schedule_revision,
            error=error,
        )

    def to_dict(self) -> JsonObject:
        return {
            "commandId": self.command_id,
            "ok": self.ok,
            "liveRevision": self.live_revision,
            "scheduleRevision": self.schedule_revision,
            "error": self.error.to_dict() if self.error is not None else None,
        }


def parse_timer_command(payload: Mapping[str, object]) -> TimerRemoteCommand:
    """Parse and validate a strict JSON timer command envelope."""

    if not isinstance(payload, Mapping):
        raise TypeError("command payload must be an object")
    command_id = payload_string(payload, "commandId")
    try:
        command_type = TimerCommandType(payload.get("type"))
    except (TypeError, ValueError) as exc:
        raise ValueError("command type is invalid") from exc

    if command_type is TimerCommandType.START_PART:
        reject_unknown_fields(payload, {"commandId", "type", "scheduleRevision", "partId"})
        return StartPartCommand(
            command_id=command_id,
            schedule_revision=payload_int(payload, "scheduleRevision"),
            part_id=payload_string(payload, "partId"),
        )

    if command_type is TimerCommandType.STOP_PART:
        reject_unknown_fields(payload, {"commandId", "type", "partId"})
        return StopPartCommand(
            command_id=command_id,
            part_id=payload_optional_string(payload, "partId"),
        )

    if command_type is TimerCommandType.RESET_PART:
        reject_unknown_fields(payload, {"commandId", "type", "scheduleRevision", "partId"})
        return ResetPartCommand(
            command_id=command_id,
            schedule_revision=payload_int(payload, "scheduleRevision"),
            part_id=payload_string(payload, "partId"),
        )

    if command_type is TimerCommandType.ADJUST_PART:
        reject_unknown_fields(
            payload,
            {"commandId", "type", "scheduleRevision", "partId", "deltaSeconds"},
        )
        return AdjustPartCommand(
            command_id=command_id,
            schedule_revision=payload_int(payload, "scheduleRevision"),
            part_id=payload_string(payload, "partId"),
            delta_seconds=payload_int(payload, "deltaSeconds"),
        )

    if command_type is TimerCommandType.SET_PART_SECONDS:
        reject_unknown_fields(
            payload,
            {"commandId", "type", "scheduleRevision", "partId", "plannedSeconds"},
        )
        return SetPartSecondsCommand(
            command_id=command_id,
            schedule_revision=payload_int(payload, "scheduleRevision"),
            part_id=payload_string(payload, "partId"),
            planned_seconds=payload_int(payload, "plannedSeconds"),
        )

    if command_type is TimerCommandType.SET_SECTION_COUNT:
        reject_unknown_fields(
            payload,
            {"commandId", "type", "scheduleRevision", "section", "count"},
        )
        try:
            section = Section(payload_string(payload, "section"))
        except ValueError as exc:
            raise ValueError("section is invalid") from exc
        return SetSectionCountCommand(
            command_id=command_id,
            schedule_revision=payload_int(payload, "scheduleRevision"),
            section=section,
            count=payload_int(payload, "count"),
        )

    if command_type is TimerCommandType.SET_MEETING_TYPE:
        reject_unknown_fields(payload, {"commandId", "type", "meetingType"})
        try:
            meeting_type = MeetingType(payload_string(payload, "meetingType"))
        except ValueError as exc:
            raise ValueError("meetingType is invalid") from exc
        return SetMeetingTypeCommand(
            command_id=command_id,
            meeting_type=meeting_type,
        )

    reject_unknown_fields(payload, {"commandId", "type", "target"})
    try:
        target = TimerWeekTarget(payload_string(payload, "target"))
    except ValueError as exc:
        raise ValueError("target is invalid") from exc
    return NavigateWeekCommand(
        command_id=command_id,
        target=target,
    )
