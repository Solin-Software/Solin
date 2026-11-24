"""Qt main-thread adapter between timer operations and /remote/timer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal, Slot

from ..core.i18n.date import week_label
from ..core.i18n.timer_schedule_view import parts_model_for_schedule
from ..core.remote_control.features import (
    DEFAULT_TIMER_REMOTE_FEATURE_FLAGS,
    TimerRemoteFeatureFlags,
)
from ..core.remote_control.sanitization import public_subtitle, public_title
from ..core.remote_control.security import (
    InMemorySessionStore,
    LoginRateLimiter,
    RemoteControlCredentialsStore,
)
from ..core.remote_control.server import SetupProvider
from ..core.remote_control.timer.contracts import (
    AdjustPartCommand,
    CreateCustomScheduleCommand,
    DeleteCustomScheduleCommand,
    DuplicateCustomScheduleCommand,
    EditCustomScheduleCommand,
    RenamePartCommand,
    ResetAllCommand,
    SelectCustomScheduleCommand,
    SelectMeetingScheduleCommand,
    CustomScheduleCatalogQuery,
    CustomScheduleCatalogWireSnapshot,
    CustomSchedulePartWireItem,
    CustomScheduleSectionWireItem,
    CustomScheduleWireItem,
    PacingTransitionsWireSnapshot,
    NavigateWeekCommand,
    ResetPartCommand,
    SetMeetingTypeCommand,
    SetPacingModeCommand,
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
    TimerPacingWireSnapshot,
    TimerRemoteCommand,
    TimerScheduleWireSnapshot,
)
from ..core.remote_control.timer.http import TimerRemoteDependencies, TimerRemoteHttpModule
from ..core.remote_control.timer.state import RemoteTimerStateStore, TimerCommandSession
from ..core.timer.custom_schedule import TimerDurationBudgetError, TimerOperationError
from ..core.timer.hand_alert import TimerHandAlert
from ..core.timer.models import (
    CustomTimerSchedule,
    MeetingType,
    MIN_PLANNED_PART_SECONDS,
    PartState,
    Section,
    TimerPart,
    TimerSection,
)
from ..core.timer.pacing import adjustment_weights
from ..styles.theme import current_theme_scheme
from .qt_command_bridge import QtCommandBridge


if TYPE_CHECKING:
    from .timer_operations import TimerOperations


def _local_utc_offset_minutes(epoch_seconds: float) -> int:
    local_dt = datetime.fromtimestamp(epoch_seconds).astimezone()
    offset = local_dt.utcoffset()
    return int(round(offset.total_seconds() / 60.0)) if offset is not None else 0


@dataclass(frozen=True, slots=True)
class RemoteTimerAdapterDependencies:
    active_profile_id: str
    active_profile_name: str
    credentials: RemoteControlCredentialsStore
    timer_operations: TimerOperations
    feature_flags: TimerRemoteFeatureFlags = DEFAULT_TIMER_REMOTE_FEATURE_FLAGS


class RemoteTimerAdapter(QObject):
    """Synchronizes discrete desktop timer transitions with /remote/timer without tick flooding."""

    snapshot_updated = Signal()

    def __init__(self, dependencies: RemoteTimerAdapterDependencies, parent: QObject) -> None:
        super().__init__(parent)
        self._dependencies = dependencies
        self._state = RemoteTimerStateStore(
            hand_alert_policy=dependencies.feature_flags.hand_alert_policy
        )
        self._active = False
        self._bridge: QtCommandBridge[TimerRemoteCommand, TimerCommandError | str] = QtCommandBridge(
            self._execute_command,
            lambda: TimerCommandError(
                TimerCommandErrorCode.FAILED,
                "The timer command could not be completed.",
            ),
            self,
        )
        self._catalog_bridge: QtCommandBridge[
            CustomScheduleCatalogQuery, CustomScheduleCatalogWireSnapshot | TimerCommandError
        ] = QtCommandBridge(
            self._build_custom_catalog,
            lambda: TimerCommandError(
                TimerCommandErrorCode.FAILED,
                "The timer catalog could not be loaded.",
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

    def set_hand_alert(self, alert: TimerHandAlert) -> None:
        if self._state.update_hand_alert(alert) and self._active:
            self.snapshot_updated.emit()

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
                catalog_handler=self._get_custom_catalog,
                setup_provider=setup_provider,
                enabled_predicate=enabled_predicate,
                theme_provider=current_theme_scheme,
                profile_id=self._dependencies.active_profile_id,
                profile_name=self._dependencies.active_profile_name,
            )
        )

    def abort_pending_commands(self) -> None:
        self._bridge.abort_pending()
        self._catalog_bridge.abort_pending()

    def stop(self) -> None:
        self._active = False
        self._bridge.abort_pending()
        self._catalog_bridge.abort_pending()
        try:
            self._dependencies.timer_operations.changed.disconnect(self._on_operations_changed)
        except (RuntimeError, TypeError):
            pass

    def on_language_changed(self) -> None:
        if self._active:
            self.refresh_all()

    def refresh_all(self, *, emit: bool = True) -> None:
        previous = self._state.snapshot()
        schedule = self._build_schedule_snapshot()
        live = self._build_live_snapshot()
        updated = self._state.update_snapshots(schedule, live)
        if emit and updated is not previous:
            self.snapshot_updated.emit()

    def _connect_publishers(self) -> None:
        self._dependencies.timer_operations.changed.connect(self._on_operations_changed)

    @Slot()
    def _on_operations_changed(self) -> None:
        if self._active:
            self.refresh_all()

    def _build_live_snapshot(self) -> TimerLiveWireSnapshot:
        operations = self._dependencies.timer_operations
        engine = operations.engine
        wire_config = TimerClockWireConfig.from_clock_config(operations.session.clock_config)
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

        active_section = snap.active_section
        if isinstance(active_section, str) and not isinstance(active_section, Section):
            active_section = public_subtitle(active_section) or None

        return TimerLiveWireSnapshot(
            live_revision=0,
            server_epoch_ms=server_epoch_ms,
            utc_offset_minutes=utc_offset,
            active=True,
            state=snap.state,
            direction=snap.direction,
            active_part_id=snap.active_part_id,
            active_part_title=public_title(snap.active_part_title, "Part"),
            active_section=active_section,
            planned_seconds=max(0, int(snap.planned_seconds)),
            elapsed_seconds=max(0.0, float(snap.elapsed_seconds)),
            clock_config=wire_config,
        )

    def _build_schedule_snapshot(self) -> TimerScheduleWireSnapshot:
        operations = self._dependencies.timer_operations
        session = operations.session
        schedule = session.schedule
        pacing = operations.pacing_state()
        raw_parts = parts_model_for_schedule(schedule, operations.time_format)
        pacing_parts = list(schedule.iter_parts()) if pacing.projection is not None else []
        parts_by_id = {part.id: part for part in pacing_parts}
        weights = adjustment_weights(schedule, pacing_parts)
        items: list[TimerPartWireItem] = []
        for index, raw in enumerate(raw_parts, start=1):
            part = parts_by_id.get(str(raw["id"]))
            section_value = raw.get("section") or ""
            try:
                section = Section(section_value)
            except ValueError:
                section = section_value
            show_header = bool(raw.get("showSectionHeader", False))
            header_title = str(raw.get("sectionTitle") or "") if show_header else ""
            items.append(
                TimerPartWireItem(
                    id=str(raw["id"]),
                    display_number=int(raw.get("displayNumber", index)),
                    section=section,
                    section_key=str(raw.get("sectionKey") or section_value),
                    section_icon_key=str(raw.get("sectionIconKey") or ""),
                    section_header_title=header_title,
                    title=public_title(raw.get("title"), f"Part {index}"),
                    planned_seconds=max(0, int(raw.get("plannedSeconds", 0))),
                    planned_label=str(raw.get("plannedLabel") or "00:00"),
                    state=PartState(str(raw.get("state") or PartState.IDLE.value)),
                    first_of_section=bool(raw.get("firstOfSection", False)),
                    show_section_header=show_header,
                    configurable_count=bool(raw.get("configurableCount", False)),
                    section_count=max(0, int(raw.get("sectionCount", 0))),
                    duration_editable=bool(raw.get("durationEditable", False)),
                    section_total_label=str(raw.get("sectionTotalLabel") or ""),
                    started_label=str(raw.get("startedLabel") or ""),
                    result_label=str(raw.get("resultLabel") or ""),
                    effective_seconds=part.effective_seconds if part is not None else None,
                    adjustment_weight=weights.get(part.id, 0) if part is not None else 0,
                )
            )
        monday = session.week_monday
        capabilities = TimerControlCapabilities.from_feature_flags(self._dependencies.feature_flags)
        if isinstance(schedule, CustomTimerSchedule):
            capabilities = replace(
                capabilities,
                can_control_parts=capabilities.can_manage_custom_schedules,
                can_adjust_planned_minutes=capabilities.can_manage_custom_schedules,
                can_set_planned_seconds=(
                    capabilities.can_manage_custom_schedules
                    and capabilities.can_set_planned_seconds
                ),
                can_set_section_count=False,
                can_switch_meeting_type=False,
                can_navigate_weeks=False,
            )
        return TimerScheduleWireSnapshot(
            schedule_revision=0,
            week_monday=monday.isoformat(),
            week_label=public_title(week_label(monday), monday.isoformat()),
            is_current_week=session.is_current_week,
            meeting_type=session.meeting_type,
            parts=tuple(items),
            capabilities=capabilities,
            pacing=TimerPacingWireSnapshot.from_snapshot(pacing.projection) if pacing.projection else None,
            pacing_mode=pacing.mode,
            pacing_availability=pacing.availability,
            custom_schedule=(
                self._custom_schedule_wire(schedule)
                if isinstance(schedule, CustomTimerSchedule)
                else None
            ),
        )

    async def _get_custom_catalog(
        self,
        query: CustomScheduleCatalogQuery,
    ) -> CustomScheduleCatalogWireSnapshot | TimerCommandError:
        result = await self._catalog_bridge.execute(query)
        if result is None:
            return TimerCommandError(
                TimerCommandErrorCode.FAILED, "The timer catalog could not be loaded."
            )
        return result

    def _build_custom_catalog(
        self,
        _query: CustomScheduleCatalogQuery,
    ) -> CustomScheduleCatalogWireSnapshot | TimerCommandError:
        if not self._dependencies.feature_flags.allow_custom_schedules:
            return TimerCommandError(
                TimerCommandErrorCode.BLOCKED, "Custom timer schedules are disabled."
            )

        schedules = tuple(
            self._custom_schedule_wire(schedule)
            for schedule in self._dependencies.timer_operations.list_custom_schedules()
        )
        return CustomScheduleCatalogWireSnapshot(schedules=schedules)

    @staticmethod
    def _custom_schedule_wire(schedule: CustomTimerSchedule) -> CustomScheduleWireItem:
        window = schedule.pacing_window
        transitions = window.transitions if window else None

        def part_item(part: TimerPart) -> CustomSchedulePartWireItem:
            return CustomSchedulePartWireItem(
                id=part.id,
                title=part.title,
                planned_seconds=part.planned_seconds,
                state=part.state,
                elapsed_seconds=part.accumulated_seconds,
                first_started_epoch=part.first_started_epoch,
                started_at_epoch=part.started_at_epoch,
                effective_seconds=part.effective_seconds,
                adjustment_weight=part.adjustment_weight,
            )

        entries = tuple(
            CustomScheduleSectionWireItem(
                id=entry.id,
                title=entry.title,
                parts=tuple(part_item(part) for part in entry.parts),
                icon=entry.icon,
            )
            if isinstance(entry, TimerSection)
            else part_item(entry)
            for entry in schedule.entries
        )
        return CustomScheduleWireItem(
            id=schedule.id,
            title=schedule.title,
            revision=schedule.revision,
            entries=entries,
            start_epoch=window.start_epoch if window else None,
            end_epoch=window.end_epoch if window else None,
            untracked_seconds=window.untracked_seconds if window else None,
            transitions=(
                PacingTransitionsWireSnapshot(
                    tuple(transitions.before_part_seconds), transitions.closing_seconds
                )
                if transitions is not None
                and (transitions.before_part_seconds or transitions.closing_seconds is not None)
                else None
            ),
        )

    def _execute_command(self, command: TimerRemoteCommand) -> TimerCommandError | str | None:
        if precondition_error := self._state.validate_command_preconditions(command):
            return precondition_error

        operations = self._dependencies.timer_operations
        schedule = operations.session.schedule
        if isinstance(
            command,
            (
                StartPartCommand,
                StopPartCommand,
                ResetPartCommand,
                AdjustPartCommand,
                SetPartSecondsCommand,
            ),
        ):
            part_id = command.part_id
            if part_id is None:
                part_id = operations.engine.snapshot().active_part_id
                if part_id is None:
                    return TimerCommandError(
                        TimerCommandErrorCode.BLOCKED, "No timer part is running."
                    )
            part = schedule.part_by_id(part_id)
            if part is None:
                return TimerCommandError(
                    TimerCommandErrorCode.NOT_FOUND, "The timer part no longer exists."
                )
            if isinstance(command, StartPartCommand) and part.state is PartState.RUNNING:
                return TimerCommandError(
                    TimerCommandErrorCode.BLOCKED, "This timer part is already running."
                )
            if isinstance(command, StopPartCommand) and part.state is not PartState.RUNNING:
                return TimerCommandError(
                    TimerCommandErrorCode.BLOCKED, "This timer part is not running."
                )
            if isinstance(command, AdjustPartCommand):
                if abs(command.delta_seconds) == 1 and part.state is not PartState.RUNNING:
                    return TimerCommandError(
                        TimerCommandErrorCode.BLOCKED, "Elapsed adjustment requires a running part."
                    )
                if abs(command.delta_seconds) != 1 and part.state is not PartState.IDLE:
                    return TimerCommandError(
                        TimerCommandErrorCode.BLOCKED, "This duration cannot be changed."
                    )
            if isinstance(command, SetPartSecondsCommand):
                if part.state is not PartState.IDLE:
                    return TimerCommandError(
                        TimerCommandErrorCode.BLOCKED, "This duration cannot be changed."
                    )
                if (
                    not isinstance(schedule, CustomTimerSchedule)
                    and command.planned_seconds < MIN_PLANNED_PART_SECONDS
                ):
                    return TimerCommandError(
                        TimerCommandErrorCode.INVALID,
                        "The duration is too short for a meeting part.",
                    )
        if isinstance(command, SetSectionCountCommand) and (
            isinstance(schedule, CustomTimerSchedule)
            or schedule.meeting_type is not MeetingType.MIDWEEK
        ):
            return TimerCommandError(
                TimerCommandErrorCode.BLOCKED,
                "Part count is only configurable on midweek meetings.",
            )
        created_schedule_id = None
        try:
            if isinstance(command, StartPartCommand):
                operations.start_part(command.part_id)
            elif isinstance(command, StopPartCommand):
                operations.stop_part(command.part_id)
            elif isinstance(command, ResetPartCommand):
                operations.reset_part(command.part_id)
            elif isinstance(command, AdjustPartCommand):
                operations.adjust_part(command.part_id, command.delta_seconds)
            elif isinstance(command, SetPartSecondsCommand):
                operations.set_part_seconds(command.part_id, command.planned_seconds)
            elif isinstance(command, SetSectionCountCommand):
                operations.set_section_count(command.section, command.count)
            elif isinstance(command, SetMeetingTypeCommand):
                operations.set_meeting_type(command.meeting_type)
            elif isinstance(command, SetPacingModeCommand):
                operations.set_pacing_mode(command.mode)
            elif isinstance(command, NavigateWeekCommand):
                operations.navigate_week(command.target.value)
            elif isinstance(command, CreateCustomScheduleCommand):
                created_schedule_id = operations.create_custom_schedule(command.title).id
            elif isinstance(command, DuplicateCustomScheduleCommand):
                created_schedule_id = operations.duplicate_custom_schedule(
                    command.schedule_id,
                    command.title,
                    command.custom_revision,
                ).id
            elif isinstance(command, DeleteCustomScheduleCommand):
                operations.delete_custom_schedule(command.schedule_id, command.custom_revision)
            elif isinstance(command, EditCustomScheduleCommand):
                operations.edit_custom_schedule(
                    command.schedule_id,
                    command.custom_revision,
                    command.action,
                    **command.values,
                )
            elif isinstance(command, SelectCustomScheduleCommand):
                schedule = operations.get_custom_schedule(command.schedule_id)
                if schedule is None:
                    raise TimerOperationError("not_found", "The timer schedule no longer exists.")
                if schedule.revision != command.custom_revision:
                    raise TimerOperationError("schedule_stale", "The timer schedule changed.")
                operations.select_custom_schedule(command.schedule_id)
            elif isinstance(command, SelectMeetingScheduleCommand):
                operations.select_meeting_schedule()
            elif isinstance(command, RenamePartCommand):
                operations.rename_part(command.part_id, command.title)
            elif isinstance(command, ResetAllCommand):
                operations.reset_all()
            else:
                return TimerCommandError(TimerCommandErrorCode.INVALID, "Unknown timer command.")
        except TimerDurationBudgetError as error:
            return TimerCommandError(
                TimerCommandErrorCode.DURATION_BUDGET, str(error),
                available_seconds=error.available_seconds, requested_seconds=error.requested_seconds,
            )
        except TimerOperationError as error:
            code = TimerCommandErrorCode(error.code)
            return TimerCommandError(code, str(error), retryable=code is TimerCommandErrorCode.SCHEDULE_STALE)

        if not self._active:
            self.refresh_all(emit=False)
        return created_schedule_id
