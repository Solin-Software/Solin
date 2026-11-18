"""Qt main-thread adapter bridging TimerBridge/TimerEngine and /remote/timer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from ..core.i18n.meeting_sections import translate_meeting_section_title
from ..core.meetings.section_meta import SECTION_META
from ..core.remote_control.features import (
    DEFAULT_TIMER_REMOTE_FEATURE_FLAGS,
    TimerRemoteFeatureFlags,
)
from ..core.remote_control.sanitization import public_title
from ..core.remote_control.security import (
    InMemorySessionStore,
    LoginRateLimiter,
    RemoteControlCredentialsStore,
)
from ..core.remote_control.server import SetupProvider
from ..core.remote_control.timer.contracts import (
    AdjustPartCommand,
    NavigateWeekCommand,
    ResetPartCommand,
    SetMeetingTypeCommand,
    SetPartSecondsCommand,
    SetSectionCountCommand,
    StartPartCommand,
    StopPartCommand,
    TimerClockWireConfig,
    TimerCommandError,
    TimerCommandErrorCode,
    TimerControlCapabilities,
    TimerLiveWireSnapshot,
    TimerPartWireItem,
    TimerRemoteCommand,
    TimerScheduleWireSnapshot,
    TimerWeekTarget,
)
from ..core.remote_control.timer.http import TimerRemoteDependencies, TimerRemoteHttpModule
from ..core.remote_control.timer.state import RemoteTimerStateStore, TimerCommandSession
from ..core.timer.models import ClockConfig, MeetingType, PartState, Section
from ..core.timer.schedule_factory import is_part_duration_editable
from ..styles.theme import current_theme_scheme
from .qt_command_bridge import QtCommandBridge


_SECTION_HEADER_CODES = {
    Section.TREASURES: "tgw",
    Section.MINISTRY: "ayfm",
    Section.LIVING: "lac",
    Section.PUBLIC_TALK: "public_talk",
    Section.WATCHTOWER: "wt",
}


def _local_utc_offset_minutes(epoch_seconds: float) -> int:
    local_dt = datetime.fromtimestamp(epoch_seconds).astimezone()
    offset = local_dt.utcoffset()
    return int(round(offset.total_seconds() / 60.0)) if offset is not None else 0


def _section_header_title(section: Section) -> str:
    code = _SECTION_HEADER_CODES.get(section)
    if not code:
        return ""
    source = SECTION_META.get(code, ("", 0))[0]
    return translate_meeting_section_title(source) if source else ""


@dataclass(frozen=True, slots=True)
class RemoteTimerAdapterDependencies:
    active_profile_id: str
    active_profile_name: str
    credentials: RemoteControlCredentialsStore
    timer_engine: Any
    timer_bridge: Any
    feature_flags: TimerRemoteFeatureFlags = DEFAULT_TIMER_REMOTE_FEATURE_FLAGS


class RemoteTimerAdapter(QObject):
    """Synchronizes discrete desktop timer transitions with /remote/timer without tick flooding."""

    snapshot_updated = Signal()

    def __init__(self, dependencies: RemoteTimerAdapterDependencies, parent: QObject) -> None:
        super().__init__(parent)
        self._dependencies = dependencies
        self._state = RemoteTimerStateStore()
        self._active = False
        self._bridge: QtCommandBridge[TimerRemoteCommand, TimerCommandError] = QtCommandBridge(
            self._execute_command,
            lambda: TimerCommandError(
                TimerCommandErrorCode.FAILED,
                "The timer command could not be completed.",
            ),
            self,
        )
        self._connect_publishers()

    @property
    def state(self) -> RemoteTimerStateStore:
        return self._state

    @property
    def feature_flags(self) -> TimerRemoteFeatureFlags:
        return self._dependencies.feature_flags

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        if self._active:
            self.refresh_all()

    def build_http_module(
        self,
        *,
        sessions: InMemorySessionStore,
        rate_limiter: LoginRateLimiter,
        assets_directory: Path,
        shared_assets_directory: Path | None,
        setup_provider: SetupProvider,
        enabled_predicate: Callable[[], bool],
    ) -> TimerRemoteHttpModule:
        self._state.rotate_server_instance()
        self.refresh_all(emit=False)
        return TimerRemoteHttpModule(
            TimerRemoteDependencies(
                state=self._state,
                command_session=TimerCommandSession(self._state),
                credentials=self._dependencies.credentials,
                sessions=sessions,
                rate_limiter=rate_limiter,
                assets_directory=assets_directory,
                shared_assets_directory=shared_assets_directory,
                command_handler=self._bridge.execute,
                setup_provider=setup_provider,
                enabled_predicate=enabled_predicate,
                theme_provider=current_theme_scheme,
                profile_id=self._dependencies.active_profile_id,
                profile_name=self._dependencies.active_profile_name,
            )
        )

    def abort_pending_commands(self) -> None:
        self._bridge.abort_pending()

    def stop(self) -> None:
        self._active = False
        self._bridge.abort_pending()
        engine = self._dependencies.timer_engine
        bridge = self._dependencies.timer_bridge
        for signal, slot in (
            (engine.state_changed, self._on_engine_state_changed),
            (bridge.scheduleChanged, self._on_schedule_changed),
            (bridge.clockConfigChanged, self._on_clock_config_changed),
            (bridge.weekChanged, self._on_week_changed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def on_language_changed(self) -> None:
        if self._active:
            self.refresh_all()

    def refresh_all(self, *, emit: bool = True) -> None:
        prev_live = self._state.live_revision
        prev_schedule = self._state.schedule_revision
        self._state.update_schedule(self._build_schedule_snapshot())
        self._state.update_live(self._build_live_snapshot())
        if emit and (
            self._state.live_revision != prev_live or self._state.schedule_revision != prev_schedule
        ):
            self.snapshot_updated.emit()

    def _connect_publishers(self) -> None:
        engine = self._dependencies.timer_engine
        bridge = self._dependencies.timer_bridge
        engine.state_changed.connect(self._on_engine_state_changed)
        bridge.scheduleChanged.connect(self._on_schedule_changed)
        bridge.clockConfigChanged.connect(self._on_clock_config_changed)
        bridge.weekChanged.connect(self._on_week_changed)

    @Slot(dict)
    def _on_engine_state_changed(self, _snapshot: dict[str, Any]) -> None:
        if self._active:
            self.refresh_all()

    @Slot()
    def _on_schedule_changed(self) -> None:
        if self._active:
            self.refresh_all()

    @Slot()
    def _on_clock_config_changed(self) -> None:
        if self._active:
            self.refresh_all()

    @Slot()
    def _on_week_changed(self) -> None:
        if self._active:
            self.refresh_all()

    def _build_live_snapshot(self) -> TimerLiveWireSnapshot:
        engine = self._dependencies.timer_engine
        bridge = self._dependencies.timer_bridge
        raw_config = bridge.clockConfig
        config = (
            ClockConfig.from_dict(raw_config).clamped()
            if isinstance(raw_config, dict)
            else ClockConfig()
        )
        wire_config = TimerClockWireConfig.from_clock_config(config)
        snap = engine.snapshot()
        epoch_seconds = float(snap.wall_clock_epoch)
        server_epoch_ms = max(0, int(round(epoch_seconds * 1000.0)))
        utc_offset = _local_utc_offset_minutes(epoch_seconds)
        if not snap.active or snap.active_part_id is None:
            return TimerLiveWireSnapshot.idle(
                server_epoch_ms=server_epoch_ms,
                utc_offset_minutes=utc_offset,
                clock_config=wire_config,
            )

        return TimerLiveWireSnapshot(
            live_revision=0,
            server_epoch_ms=server_epoch_ms,
            utc_offset_minutes=utc_offset,
            active=True,
            state=snap.state,
            direction=snap.direction,
            active_part_id=snap.active_part_id,
            active_part_title=public_title(snap.active_part_title, "Part"),
            active_section=snap.active_section,
            planned_seconds=max(0, int(snap.planned_seconds)),
            elapsed_seconds=max(0.0, float(snap.elapsed_seconds)),
            clock_config=wire_config,
        )

    def _build_schedule_snapshot(self) -> TimerScheduleWireSnapshot:
        bridge = self._dependencies.timer_bridge
        schedule = self._dependencies.timer_engine.schedule
        raw_parts = bridge.parts if isinstance(bridge.parts, list) else []
        items: list[TimerPartWireItem] = []
        for index, raw in enumerate(raw_parts, start=1):
            if not isinstance(raw, dict):
                continue
            part_id = str(raw["id"])
            section = Section(str(raw["section"]))
            part_state = PartState(str(raw.get("state") or PartState.IDLE.value))
            show_header = bool(raw.get("showSectionHeader", False))
            domain_part = schedule.part_by_id(part_id) if schedule is not None else None
            duration_editable = (
                is_part_duration_editable(schedule, domain_part)
                if schedule is not None and domain_part is not None
                else part_state is PartState.IDLE
            )
            items.append(
                TimerPartWireItem(
                    id=part_id,
                    display_number=int(raw.get("displayNumber", index)),
                    section=section,
                    section_key=str(raw.get("sectionKey") or section.value),
                    section_header_title=_section_header_title(section) if show_header else "",
                    title=public_title(raw.get("title"), f"Part {index}"),
                    planned_seconds=max(0, int(raw.get("plannedSeconds", 0))),
                    planned_label=str(raw.get("plannedLabel") or "00:00"),
                    state=part_state,
                    first_of_section=bool(raw.get("firstOfSection", False)),
                    show_section_header=show_header,
                    configurable_count=bool(raw.get("configurableCount", False)),
                    section_count=max(0, int(raw.get("sectionCount", 0))),
                    duration_editable=duration_editable,
                    section_total_label=str(raw.get("sectionTotalLabel") or ""),
                    started_label=str(raw.get("startedLabel") or ""),
                    result_label=str(raw.get("resultLabel") or ""),
                )
            )
        week_monday = str(schedule.week_monday) if schedule is not None else "2026-01-05"
        meeting_type = MeetingType(str(bridge.meetingType))
        return TimerScheduleWireSnapshot(
            schedule_revision=0,
            week_monday=week_monday,
            week_label=public_title(bridge.weekLabel, week_monday),
            is_current_week=bool(bridge.isCurrentWeek),
            meeting_type=meeting_type,
            parts=tuple(items),
            capabilities=TimerControlCapabilities.from_feature_flags(
                self._dependencies.feature_flags
            ),
        )

    def _execute_command(self, command: TimerRemoteCommand) -> TimerCommandError | None:
        if precondition_error := self._state.validate_command_preconditions(command):
            return precondition_error

        bridge = self._dependencies.timer_bridge
        engine = self._dependencies.timer_engine
        schedule = engine.schedule
        if schedule is None:
            return TimerCommandError(
                TimerCommandErrorCode.UNAVAILABLE,
                "The meeting schedule is not available.",
            )

        if isinstance(command, StartPartCommand):
            part = schedule.part_by_id(command.part_id)
            if part is None:
                return TimerCommandError(
                    TimerCommandErrorCode.NOT_FOUND,
                    "The selected meeting part no longer exists.",
                )
            if part.state is PartState.RUNNING:
                return TimerCommandError(
                    TimerCommandErrorCode.BLOCKED,
                    "This meeting part is already running.",
                )
            bridge.startPart(command.part_id)
        elif isinstance(command, StopPartCommand):
            snap = engine.snapshot()
            target_id = command.part_id or snap.active_part_id
            if target_id is None:
                return TimerCommandError(
                    TimerCommandErrorCode.BLOCKED,
                    "No meeting part is currently running.",
                )
            part = schedule.part_by_id(target_id)
            if part is None:
                return TimerCommandError(
                    TimerCommandErrorCode.NOT_FOUND,
                    "The selected meeting part no longer exists.",
                )
            if part.state is not PartState.RUNNING:
                return TimerCommandError(
                    TimerCommandErrorCode.BLOCKED,
                    "This meeting part is not currently running.",
                )
            bridge.stopPart(target_id)
        elif isinstance(command, ResetPartCommand):
            part = schedule.part_by_id(command.part_id)
            if part is None:
                return TimerCommandError(
                    TimerCommandErrorCode.NOT_FOUND,
                    "The selected meeting part no longer exists.",
                )
            bridge.resetPart(command.part_id)
        elif isinstance(command, AdjustPartCommand):
            part = schedule.part_by_id(command.part_id)
            if part is None:
                return TimerCommandError(
                    TimerCommandErrorCode.NOT_FOUND,
                    "The selected meeting part no longer exists.",
                )
            if abs(command.delta_seconds) == 1:
                if part.state is not PartState.RUNNING:
                    return TimerCommandError(
                        TimerCommandErrorCode.BLOCKED,
                        "Elapsed-second adjustment requires a running part.",
                    )
            elif not is_part_duration_editable(schedule, part):
                return TimerCommandError(
                    TimerCommandErrorCode.BLOCKED,
                    "This meeting part duration cannot be changed.",
                )
            bridge.adjustPart(command.part_id, command.delta_seconds)
        elif isinstance(command, SetPartSecondsCommand):
            part = schedule.part_by_id(command.part_id)
            if part is None:
                return TimerCommandError(
                    TimerCommandErrorCode.NOT_FOUND,
                    "The selected meeting part no longer exists.",
                )
            if not is_part_duration_editable(schedule, part):
                return TimerCommandError(
                    TimerCommandErrorCode.BLOCKED,
                    "This meeting part duration cannot be changed.",
                )
            bridge.setPartSeconds(command.part_id, command.planned_seconds)
        elif isinstance(command, SetSectionCountCommand):
            if schedule.meeting_type is not MeetingType.MIDWEEK:
                return TimerCommandError(
                    TimerCommandErrorCode.BLOCKED,
                    "Part count is only configurable on midweek meetings.",
                )
            bridge.setSectionCount(command.section.value, command.count)
        elif isinstance(command, SetMeetingTypeCommand):
            bridge.setMeetingType(command.meeting_type.value)
        elif isinstance(command, NavigateWeekCommand):
            if command.target is TimerWeekTarget.PREVIOUS:
                bridge.previousWeek()
            elif command.target is TimerWeekTarget.NEXT:
                bridge.nextWeek()
            else:
                bridge.goToCurrentWeek()

        self.refresh_all(emit=False)
        return None
