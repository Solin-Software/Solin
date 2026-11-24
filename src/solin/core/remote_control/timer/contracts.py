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
    MIN_SECTION_PART_COUNT,
    MIN_CUSTOM_PART_SECONDS,
    normalize_timer_title,
    AnalogClockStyle,
    ClockConfig,
    ClockMode,
    Direction,
    MeetingType,
    PartState,
    PartTimerDisplay,
    Section,
)
from ...timer.pacing import PacingSnapshot
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
    "CreateCustomScheduleCommand",
    "EditCustomScheduleCommand",
    "DeleteCustomScheduleCommand",
    "SelectCustomScheduleCommand",
    "DuplicateCustomScheduleCommand",
    "RenamePartCommand",
    "ResetAllCommand",
    "SelectMeetingScheduleCommand",
    "CustomSchedulePartWireItem",
    "CustomScheduleSectionWireItem",
    "CustomScheduleWireItem",
    "PacingTransitionsWireSnapshot",
    "CustomScheduleCatalogWireSnapshot",
    "CustomScheduleCatalogQuery",
    "NavigateWeekCommand",
    "ResetPartCommand",
    "SetMeetingTypeCommand",
    "SetMeetingPacingCommand",
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
    "TimerPacingWireSnapshot",
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
    can_manage_custom_schedules: bool = False
    can_manage_pacing: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "can_control_parts",
            "can_adjust_planned_minutes",
            "can_set_planned_seconds",
            "can_set_section_count",
            "can_switch_meeting_type",
            "can_navigate_weeks",
            "can_manage_custom_schedules",
            "can_manage_pacing",
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
            can_manage_custom_schedules=flags.allow_custom_schedules,
            can_manage_pacing=flags.allow_pacing,
        )

    def allows_command(self, command: TimerCommand) -> bool:
        if isinstance(command, SetMeetingPacingCommand):
            return self.can_manage_pacing
        if isinstance(command, EditCustomScheduleCommand) and command.action in (
            "set_pacing", "set_adjustment_weight", "set_pacing_transition"
        ):
            return self.can_manage_custom_schedules and self.can_manage_pacing
        if isinstance(
            command,
            (
                CreateCustomScheduleCommand,
                EditCustomScheduleCommand,
                DeleteCustomScheduleCommand,
                SelectCustomScheduleCommand,
                DuplicateCustomScheduleCommand,
                SelectMeetingScheduleCommand,
            ),
        ):
            return self.can_manage_custom_schedules
        if isinstance(command, (RenamePartCommand, ResetAllCommand)):
            return self.can_manage_custom_schedules and self.can_control_parts
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
        result: JsonObject = {
            "controlParts": self.can_control_parts,
            "adjustPlannedMinutes": self.can_adjust_planned_minutes,
            "setPlannedSeconds": self.can_set_planned_seconds,
            "setSectionCount": self.can_set_section_count,
            "switchMeetingType": self.can_switch_meeting_type,
            "navigateWeeks": self.can_navigate_weeks,
        }
        result["manageCustomSchedules"] = self.can_manage_custom_schedules
        result["managePacing"] = self.can_manage_pacing
        return result


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
    active_section: Section | str | None = None
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
        if self.active_section is not None and not isinstance(self.active_section, str):
            raise TypeError("active_section must be a string or None")
        if self.active_section is not None:
            require_string(self.active_section, field_name="active_section", allow_empty=True)
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
            "activeSection": self.active_section.value
            if isinstance(self.active_section, Section)
            else self.active_section,
            "plannedSeconds": self.planned_seconds,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "clockConfig": self.clock_config.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class TimerPartWireItem:
    id: str
    display_number: int
    section: Section | str | None
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
    effective_seconds: int | None = None
    adjustment_weight: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.id, field_name="id")
        require_nonnegative_int(self.display_number, field_name="display_number")
        if self.display_number < 1:
            raise ValueError("display_number must be positive")
        if self.section is not None and not isinstance(self.section, str):
            raise TypeError("section must be a string or None")
        if self.section is not None:
            require_string(self.section, field_name="section", allow_empty=True)
        require_string(self.section_key, field_name="section_key", allow_empty=True)
        require_string(
            self.section_header_title, field_name="section_header_title", allow_empty=True
        )
        require_string(self.title, field_name="title")
        require_nonnegative_int(self.planned_seconds, field_name="planned_seconds")
        if self.effective_seconds is not None:
            require_nonnegative_int(self.effective_seconds, field_name="effective_seconds")
        require_nonnegative_int(self.adjustment_weight, field_name="adjustment_weight")
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
        data: JsonObject = {
            "id": self.id,
            "displayNumber": self.display_number,
            "section": self.section.value if isinstance(self.section, Section) else self.section,
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
        if self.effective_seconds is not None:
            data["effectiveSeconds"] = self.effective_seconds
            data["adjustmentWeight"] = self.adjustment_weight
        return data


@dataclass(frozen=True, slots=True)
class CustomSchedulePartWireItem:
    id: str
    title: str
    planned_seconds: int
    state: PartState = PartState.IDLE
    elapsed_seconds: float = 0.0
    first_started_epoch: float | None = None
    started_at_epoch: float | None = None
    effective_seconds: int | None = None
    adjustment_weight: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.id, field_name="id")
        if normalize_timer_title(self.title) != self.title:
            raise ValueError("title must be normalized")
        require_int(self.planned_seconds, field_name="planned_seconds")
        if self.effective_seconds is not None:
            require_nonnegative_int(self.effective_seconds, field_name="effective_seconds")
        require_nonnegative_int(self.adjustment_weight, field_name="adjustment_weight")
        if not MIN_CUSTOM_PART_SECONDS <= self.planned_seconds <= MAX_PLANNED_PART_SECONDS:
            raise ValueError("planned_seconds is out of range")
        if not isinstance(self.state, PartState):
            raise TypeError("state must be a PartState")
        if require_finite_float(self.elapsed_seconds, field_name="elapsed_seconds") < 0:
            raise ValueError("elapsed_seconds must not be negative")
        if self.first_started_epoch is not None:
            require_finite_float(self.first_started_epoch, field_name="first_started_epoch")
        if self.started_at_epoch is not None:
            require_finite_float(self.started_at_epoch, field_name="started_at_epoch")
        if (self.state is PartState.RUNNING) != (self.started_at_epoch is not None):
            raise ValueError("running state requires a start anchor")

    def to_dict(self) -> JsonObject:
        data: JsonObject = {
            "type": "part",
            "id": self.id,
            "title": self.title,
            "plannedSeconds": self.planned_seconds,
            "state": self.state.value,
            "elapsedSeconds": self.elapsed_seconds,
            "firstStartedEpoch": self.first_started_epoch,
            "startedAtEpoch": self.started_at_epoch,
        }
        if self.effective_seconds is not None:
            data["effectiveSeconds"] = self.effective_seconds
            data["adjustmentWeight"] = self.adjustment_weight
        return data


@dataclass(frozen=True, slots=True)
class CustomScheduleSectionWireItem:
    id: str
    title: str
    parts: tuple[CustomSchedulePartWireItem, ...]

    def __post_init__(self) -> None:
        require_identifier(self.id, field_name="id")
        if normalize_timer_title(self.title) != self.title:
            raise ValueError("title must be normalized")
        if not isinstance(self.parts, tuple) or not all(
            isinstance(p, CustomSchedulePartWireItem) for p in self.parts
        ):
            raise TypeError("parts must be a tuple of custom schedule parts")

    def to_dict(self) -> JsonObject:
        return {
            "type": "section",
            "id": self.id,
            "title": self.title,
            "parts": [part.to_dict() for part in self.parts],
        }


@dataclass(frozen=True, slots=True)
class PacingTransitionsWireSnapshot:
    before_parts: tuple[tuple[str, int], ...]
    closing_seconds: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.before_parts, tuple):
            raise TypeError("before_parts must be a tuple")
        ids: set[str] = set()
        for item in self.before_parts:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("transition estimates must be pairs")
            part_id, seconds = item
            require_identifier(part_id, field_name="part_id")
            require_nonnegative_int(seconds, field_name="estimated_seconds")
            if part_id in ids:
                raise ValueError("transition part ids must be unique")
            ids.add(part_id)
        if self.closing_seconds is not None:
            require_nonnegative_int(self.closing_seconds, field_name="closing_seconds")

    def to_dict(self) -> JsonObject:
        return {"before_parts": dict(self.before_parts), "closing_seconds": self.closing_seconds}


@dataclass(frozen=True, slots=True)
class CustomScheduleWireItem:
    id: str
    title: str
    revision: int
    entries: tuple[CustomSchedulePartWireItem | CustomScheduleSectionWireItem, ...]
    start_epoch: int | None = None
    end_epoch: int | None = None
    automatic: bool = False
    untracked_seconds: int | None = None
    transitions: PacingTransitionsWireSnapshot | None = None

    def __post_init__(self) -> None:
        require_identifier(self.id, field_name="id")
        if normalize_timer_title(self.title) != self.title:
            raise ValueError("title must be normalized")
        require_nonnegative_int(self.revision, field_name="revision")
        if not isinstance(self.automatic, bool):
            raise TypeError("automatic must be a boolean")
        if (self.start_epoch is None) != (self.end_epoch is None):
            raise ValueError("pacing configuration requires both window endpoints")
        if self.start_epoch is not None:
            require_int(self.start_epoch, field_name="start_epoch")
        if self.end_epoch is not None:
            require_int(self.end_epoch, field_name="end_epoch")
        if self.untracked_seconds is not None:
            require_nonnegative_int(self.untracked_seconds, field_name="untracked_seconds")
        if self.transitions is not None and not isinstance(self.transitions, PacingTransitionsWireSnapshot):
            raise TypeError("transitions must be a pacing transitions snapshot")
        _validate_custom_entries(self.entries)

    def to_dict(self) -> JsonObject:
        result: JsonObject = {
            "id": self.id,
            "title": self.title,
            "revision": self.revision,
            "entries": [entry.to_dict() for entry in self.entries],
        }
        if self.start_epoch is not None:
            pacing_config: JsonObject = {
                "start_epoch": self.start_epoch,
                "end_epoch": self.end_epoch,
                "automatic": self.automatic,
                "untracked_seconds": self.untracked_seconds,
            }
            if self.transitions is not None:
                pacing_config["transitions"] = self.transitions.to_dict()
            result["pacingConfig"] = pacing_config
        return result


def _validate_custom_entries(
    entries: tuple[CustomSchedulePartWireItem | CustomScheduleSectionWireItem, ...],
) -> None:
    if not isinstance(entries, tuple) or not all(
        isinstance(e, (CustomSchedulePartWireItem, CustomScheduleSectionWireItem)) for e in entries
    ):
        raise TypeError("entries must be a tuple of custom schedule entries")
    ids = [entry.id for entry in entries]
    ids.extend(
        part.id
        for entry in entries
        if isinstance(entry, CustomScheduleSectionWireItem)
        for part in entry.parts
    )
    if len(ids) != len(set(ids)):
        raise ValueError("custom schedule entry ids must be unique")


@dataclass(frozen=True, slots=True)
class CustomScheduleCatalogWireSnapshot:
    schedules: tuple[CustomScheduleWireItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.schedules, tuple) or not all(
            isinstance(s, CustomScheduleWireItem) for s in self.schedules
        ):
            raise TypeError("schedules must be a tuple of custom schedules")
        if len({s.id for s in self.schedules}) != len(self.schedules):
            raise ValueError("custom schedule ids must be unique")

    def to_dict(self) -> JsonObject:
        return {"schedules": [schedule.to_dict() for schedule in self.schedules]}


@dataclass(frozen=True, slots=True)
class CustomScheduleCatalogQuery:
    pass


@dataclass(frozen=True, slots=True)
class TimerPacingWireSnapshot:
    """Stable projection anchors for authenticated clients to rebase locally."""

    start_epoch: int
    end_epoch: int
    complete: bool
    automatic: bool
    projection_anchor_epoch: float
    projection_tail_seconds: float
    available_shorten_seconds: int
    available_extend_seconds: int
    tolerance_seconds: int

    @classmethod
    def from_snapshot(cls, snapshot: PacingSnapshot) -> TimerPacingWireSnapshot:
        return cls(
            snapshot.start_epoch, snapshot.end_epoch, snapshot.complete, snapshot.automatic,
            snapshot.projection_anchor_epoch, snapshot.projection_tail_seconds,
            snapshot.available_shorten_seconds, snapshot.available_extend_seconds, snapshot.tolerance_seconds,
        )

    def to_dict(self) -> JsonObject:
        return {
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
            "complete": self.complete,
            "automatic": self.automatic,
            "projection_anchor_epoch": self.projection_anchor_epoch,
            "projection_tail_seconds": self.projection_tail_seconds,
            "available_shorten_seconds": self.available_shorten_seconds,
            "available_extend_seconds": self.available_extend_seconds,
            "tolerance_seconds": self.tolerance_seconds,
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
    custom_schedule: CustomScheduleWireItem | None = None
    pacing: TimerPacingWireSnapshot | None = None

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
        if self.custom_schedule is not None and not isinstance(
            self.custom_schedule, CustomScheduleWireItem
        ):
            raise TypeError("custom_schedule must be a CustomScheduleWireItem or None")
        if self.pacing is not None and not isinstance(self.pacing, TimerPacingWireSnapshot):
            raise TypeError("pacing must be a TimerPacingWireSnapshot or None")

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
        result: JsonObject = {
            "scheduleRevision": self.schedule_revision,
            "weekMonday": self.week_monday,
            "weekLabel": self.week_label,
            "isCurrentWeek": self.is_current_week,
            "meetingType": self.meeting_type.value,
            "parts": [part.to_dict() for part in self.parts],
            "capabilities": self.capabilities.to_dict(),
        }
        if self.custom_schedule is not None and self.capabilities.can_manage_custom_schedules:
            result["customSchedule"] = self.custom_schedule.to_dict()
        if self.pacing is not None:
            result["pacing"] = self.pacing.to_dict()
        return result


class TimerCommandType(StrEnum):
    START_PART = "start_part"
    STOP_PART = "stop_part"
    RESET_PART = "reset_part"
    ADJUST_PART = "adjust_part"
    SET_PART_SECONDS = "set_part_seconds"
    SET_SECTION_COUNT = "set_section_count"
    SET_MEETING_TYPE = "set_meeting_type"
    NAVIGATE_WEEK = "navigate_week"
    CREATE_CUSTOM_SCHEDULE = "create_custom_schedule"
    EDIT_CUSTOM_SCHEDULE = "edit_custom_schedule"
    DELETE_CUSTOM_SCHEDULE = "delete_custom_schedule"
    SELECT_CUSTOM_SCHEDULE = "select_custom_schedule"
    DUPLICATE_CUSTOM_SCHEDULE = "duplicate_custom_schedule"
    RENAME_PART = "rename_part"
    RESET_ALL = "reset_all"
    SELECT_MEETING_SCHEDULE = "select_meeting_schedule"
    SET_MEETING_PACING = "set_meeting_pacing"


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
        if not MIN_CUSTOM_PART_SECONDS <= self.planned_seconds <= MAX_PLANNED_PART_SECONDS:
            raise ValueError(
                f"planned_seconds must be between {MIN_CUSTOM_PART_SECONDS} and {MAX_PLANNED_PART_SECONDS}"
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


@dataclass(frozen=True, slots=True)
class CreateCustomScheduleCommand(_ScheduleBoundTimerCommand):
    title: str
    TYPE: ClassVar[TimerCommandType] = TimerCommandType.CREATE_CUSTOM_SCHEDULE

    def __post_init__(self) -> None:
        super(CreateCustomScheduleCommand, self).__post_init__()
        if normalize_timer_title(self.title) != self.title:
            raise ValueError("title must be normalized")

    def to_dict(self) -> JsonObject:
        result = super(CreateCustomScheduleCommand, self).to_dict()
        result["title"] = self.title
        return result


@dataclass(frozen=True, slots=True)
class _ExistingCustomScheduleCommand(_ScheduleBoundTimerCommand):
    schedule_id: str
    custom_revision: int

    def __post_init__(self) -> None:
        super(_ExistingCustomScheduleCommand, self).__post_init__()
        require_identifier(self.schedule_id, field_name="schedule_id")
        require_nonnegative_int(self.custom_revision, field_name="custom_revision")

    def to_dict(self) -> JsonObject:
        result = super(_ExistingCustomScheduleCommand, self).to_dict()
        result.update({"scheduleId": self.schedule_id, "customRevision": self.custom_revision})
        return result


_CUSTOM_EDIT_FIELDS = {
    "add_part": ("title", "planned_seconds", "section_id", "before_id"),
    "add_section": ("title", "before_id"),
    "rename_part": ("part_id", "title"),
    "rename_section": ("section_id", "title"),
    "rename_schedule": ("title",),
    "move_part": ("part_id", "section_id", "before_id"),
    "move_section": ("section_id", "before_id"),
    "delete_part": ("part_id",),
    "delete_section": ("section_id",),
    "set_part_seconds": ("part_id", "planned_seconds"),
    "set_pacing": ("start_epoch", "end_epoch", "automatic", "untracked_seconds"),
    "set_adjustment_weight": ("part_id", "adjustment_weight"),
    "set_pacing_transition": ("before_part_id", "estimated_seconds"),
}
_EDIT_OPTIONAL_FIELDS = {"before_id", "section_id"}


def _validate_custom_edit(command: EditCustomScheduleCommand) -> None:
    fields = _CUSTOM_EDIT_FIELDS.get(command.action)
    if fields is None:
        raise ValueError("unknown custom schedule action")
    for key in ("part_id", "section_id", "before_id", "title", "planned_seconds",
                "start_epoch", "end_epoch", "automatic", "adjustment_weight",
                "untracked_seconds", "before_part_id", "estimated_seconds"):
        value = getattr(command, key)
        if key not in fields:
            if value is not None:
                raise ValueError(f"{key} is not valid for this action")
            continue
        if value is None:
            if command.action == "set_pacing_transition" and key in ("before_part_id", "estimated_seconds"):
                continue
            if key in ("start_epoch", "end_epoch") and command.action == "set_pacing":
                continue
            if key == "untracked_seconds" and command.action == "set_pacing":
                continue
            if key in _EDIT_OPTIONAL_FIELDS and (
                key != "section_id" or command.action in ("add_part", "move_part")
            ):
                continue
            raise ValueError(f"{key} is required")
        if key == "title":
            if normalize_timer_title(value) != value:
                raise ValueError("title must be normalized")
        elif key == "automatic":
            if not isinstance(value, bool):
                raise TypeError("automatic must be a boolean")
        elif key in ("start_epoch", "end_epoch"):
            require_int(value, field_name=key)
        elif key in ("adjustment_weight", "untracked_seconds", "estimated_seconds"):
            require_nonnegative_int(value, field_name=key)
        elif key == "planned_seconds":
            require_int(value, field_name=key)
            if not MIN_CUSTOM_PART_SECONDS <= value <= MAX_PLANNED_PART_SECONDS:
                raise ValueError("planned_seconds is out of range")
        else:
            require_identifier(value, field_name=key)


@dataclass(frozen=True, slots=True)
class EditCustomScheduleCommand(_ExistingCustomScheduleCommand):
    action: str
    part_id: str | None = None
    section_id: str | None = None
    before_id: str | None = None
    title: str | None = None
    planned_seconds: int | None = None
    start_epoch: int | None = None
    end_epoch: int | None = None
    automatic: bool | None = None
    adjustment_weight: int | None = None
    untracked_seconds: int | None = None
    before_part_id: str | None = None
    estimated_seconds: int | None = None
    TYPE: ClassVar[TimerCommandType] = TimerCommandType.EDIT_CUSTOM_SCHEDULE

    def __post_init__(self) -> None:
        super(EditCustomScheduleCommand, self).__post_init__()
        _validate_custom_edit(self)

    @property
    def values(self) -> dict[str, str | int | bool | None]:
        return {
            key: getattr(self, key)
            for key in _CUSTOM_EDIT_FIELDS[self.action]
            if key != "untracked_seconds" or self.untracked_seconds is not None
        }

    def to_dict(self) -> JsonObject:
        result = super(EditCustomScheduleCommand, self).to_dict()
        result["action"] = self.action
        result["values"] = {key: value for key, value in self.values.items()}
        return result


@dataclass(frozen=True, slots=True)
class DeleteCustomScheduleCommand(_ExistingCustomScheduleCommand):
    TYPE: ClassVar[TimerCommandType] = TimerCommandType.DELETE_CUSTOM_SCHEDULE


@dataclass(frozen=True, slots=True)
class SelectCustomScheduleCommand(_ExistingCustomScheduleCommand):
    TYPE: ClassVar[TimerCommandType] = TimerCommandType.SELECT_CUSTOM_SCHEDULE


@dataclass(frozen=True, slots=True)
class DuplicateCustomScheduleCommand(_ExistingCustomScheduleCommand):
    title: str
    TYPE: ClassVar[TimerCommandType] = TimerCommandType.DUPLICATE_CUSTOM_SCHEDULE

    def __post_init__(self) -> None:
        super(DuplicateCustomScheduleCommand, self).__post_init__()
        if normalize_timer_title(self.title) != self.title:
            raise ValueError("title must be normalized")

    def to_dict(self) -> JsonObject:
        result = super(DuplicateCustomScheduleCommand, self).to_dict()
        result["title"] = self.title
        return result


@dataclass(frozen=True, slots=True)
class RenamePartCommand(_ScheduleBoundTimerCommand):
    part_id: str
    title: str | None
    TYPE: ClassVar[TimerCommandType] = TimerCommandType.RENAME_PART

    def __post_init__(self) -> None:
        super(RenamePartCommand, self).__post_init__()
        require_identifier(self.part_id, field_name="part_id")
        if self.title is not None and normalize_timer_title(self.title) != self.title:
            raise ValueError("title must be normalized")

    def to_dict(self) -> JsonObject:
        result = super(RenamePartCommand, self).to_dict()
        result.update({"partId": self.part_id, "title": self.title})
        return result


@dataclass(frozen=True, slots=True)
class ResetAllCommand(_ScheduleBoundTimerCommand):
    TYPE: ClassVar[TimerCommandType] = TimerCommandType.RESET_ALL


@dataclass(frozen=True, slots=True)
class SelectMeetingScheduleCommand(_ScheduleBoundTimerCommand):
    TYPE: ClassVar[TimerCommandType] = TimerCommandType.SELECT_MEETING_SCHEDULE


@dataclass(frozen=True, slots=True)
class SetMeetingPacingCommand(_ScheduleBoundTimerCommand):
    automatic: bool
    TYPE: ClassVar[TimerCommandType] = TimerCommandType.SET_MEETING_PACING

    def __post_init__(self) -> None:
        super(SetMeetingPacingCommand, self).__post_init__()
        if not isinstance(self.automatic, bool):
            raise TypeError("automatic must be a boolean")

    def to_dict(self) -> JsonObject:
        result = super(SetMeetingPacingCommand, self).to_dict()
        result["automatic"] = self.automatic
        return result


TimerRemoteCommand: TypeAlias = (
    StartPartCommand
    | StopPartCommand
    | ResetPartCommand
    | AdjustPartCommand
    | SetPartSecondsCommand
    | SetSectionCountCommand
    | SetMeetingTypeCommand
    | NavigateWeekCommand
    | CreateCustomScheduleCommand
    | EditCustomScheduleCommand
    | DeleteCustomScheduleCommand
    | SelectCustomScheduleCommand
    | DuplicateCustomScheduleCommand
    | RenamePartCommand
    | ResetAllCommand
    | SelectMeetingScheduleCommand
    | SetMeetingPacingCommand
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
    DURATION_BUDGET = "duration_budget"


@dataclass(frozen=True, slots=True)
class TimerCommandError:
    code: TimerCommandErrorCode
    message: str
    retryable: bool = False
    available_seconds: int | None = None
    requested_seconds: int | None = None

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
        if self.available_seconds is not None or self.requested_seconds is not None:
            if (
                self.code is not TimerCommandErrorCode.DURATION_BUDGET
                or self.available_seconds is None or self.requested_seconds is None
            ):
                raise ValueError("duration budget errors require both durations")
            require_nonnegative_int(self.available_seconds, field_name="available_seconds")
            require_nonnegative_int(self.requested_seconds, field_name="requested_seconds")

    def to_dict(self) -> JsonObject:
        data: JsonObject = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.available_seconds is not None:
            data["availableSeconds"] = self.available_seconds
            data["requestedSeconds"] = self.requested_seconds
        return data


@dataclass(frozen=True, slots=True)
class TimerCommandResult:
    command_id: str
    ok: bool
    live_revision: int
    schedule_revision: int
    error: TimerCommandError | None = None
    created_schedule_id: str | None = None

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
        if self.created_schedule_id is not None:
            require_string(self.created_schedule_id, field_name="created_schedule_id")
            if not self.ok:
                raise ValueError("only a successful result can include a created timer schedule")

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
        result: JsonObject = {
            "commandId": self.command_id,
            "ok": self.ok,
            "liveRevision": self.live_revision,
            "scheduleRevision": self.schedule_revision,
            "error": self.error.to_dict() if self.error is not None else None,
        }
        if self.created_schedule_id is not None:
            result["createdScheduleId"] = self.created_schedule_id
        return result


def parse_timer_command(payload: Mapping[str, object]) -> TimerRemoteCommand:
    """Parse and validate a strict JSON timer command envelope."""

    if not isinstance(payload, Mapping):
        raise TypeError("command payload must be an object")
    command_id = payload_string(payload, "commandId")
    try:
        command_type = TimerCommandType(payload.get("type"))
    except (TypeError, ValueError) as exc:
        raise ValueError("command type is invalid") from exc

    base = {"commandId", "type", "scheduleRevision"}
    if command_type is TimerCommandType.SET_MEETING_PACING:
        reject_unknown_fields(payload, base | {"automatic"})
        automatic = payload.get("automatic")
        if not isinstance(automatic, bool):
            raise TypeError("automatic must be a boolean")
        return SetMeetingPacingCommand(command_id, payload_int(payload, "scheduleRevision"), automatic)
    if command_type in (TimerCommandType.RESET_ALL, TimerCommandType.SELECT_MEETING_SCHEDULE):
        reject_unknown_fields(payload, base)
        cls = (
            ResetAllCommand
            if command_type is TimerCommandType.RESET_ALL
            else SelectMeetingScheduleCommand
        )
        return cls(command_id, payload_int(payload, "scheduleRevision"))
    if command_type is TimerCommandType.RENAME_PART:
        reject_unknown_fields(payload, base | {"partId", "title"})
        if "title" not in payload:
            raise ValueError("title is required")
        return RenamePartCommand(
            command_id,
            payload_int(payload, "scheduleRevision"),
            payload_string(payload, "partId"),
            payload_optional_string(payload, "title"),
        )
    if command_type is TimerCommandType.CREATE_CUSTOM_SCHEDULE:
        reject_unknown_fields(payload, base | {"title"})
        return CreateCustomScheduleCommand(
            command_id, payload_int(payload, "scheduleRevision"), payload_string(payload, "title")
        )
    if command_type in (
        TimerCommandType.EDIT_CUSTOM_SCHEDULE,
        TimerCommandType.DELETE_CUSTOM_SCHEDULE,
        TimerCommandType.SELECT_CUSTOM_SCHEDULE,
        TimerCommandType.DUPLICATE_CUSTOM_SCHEDULE,
    ):
        fields = base | {"scheduleId", "customRevision"}
        if command_type is TimerCommandType.EDIT_CUSTOM_SCHEDULE:
            fields |= {"action", "values"}
        elif command_type is TimerCommandType.DUPLICATE_CUSTOM_SCHEDULE:
            fields.add("title")
        reject_unknown_fields(payload, fields)
        args = (
            command_id,
            payload_int(payload, "scheduleRevision"),
            payload_string(payload, "scheduleId"),
            payload_int(payload, "customRevision"),
        )
        if command_type is TimerCommandType.DELETE_CUSTOM_SCHEDULE:
            return DeleteCustomScheduleCommand(*args)
        if command_type is TimerCommandType.SELECT_CUSTOM_SCHEDULE:
            return SelectCustomScheduleCommand(*args)
        if command_type is TimerCommandType.DUPLICATE_CUSTOM_SCHEDULE:
            return DuplicateCustomScheduleCommand(*args, payload_string(payload, "title"))
        action = payload_string(payload, "action")
        values = payload.get("values")
        if not isinstance(values, Mapping):
            raise TypeError("values must be an object")
        allowed = _CUSTOM_EDIT_FIELDS.get(action)
        if allowed is None:
            raise ValueError("unknown custom schedule action")
        reject_unknown_fields(values, set(allowed))
        if action == "set_pacing" and not {"start_epoch", "end_epoch", "automatic"}.issubset(values):
            raise ValueError("all pacing fields are required")
        if action == "set_pacing_transition" and not {"before_part_id", "estimated_seconds"}.issubset(values):
            raise ValueError("all transition fields are required")
        return EditCustomScheduleCommand(*args, action, **dict(values))

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
