"""Owner-thread timer operations shared by desktop and remote adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date
import logging

from PySide6.QtCore import QObject, Signal, Slot

from ..core.meetings.meeting_weeks import current_monday
from ..core.timer.application import AssignmentAutofillScope, DetectedPartTiming, TimerSession
from ..core.timer.custom_schedule import TimerOperationError
from ..core.timer.models import CustomTimerSchedule, MeetingType, PartState, Section
from ..core.timer.pacing import PacingState
from ..core.timer.pacing_models import PacingMode
from .timer_engine import TimerEngine


log = logging.getLogger(__name__)


class TimerOperations(QObject):
    changed = Signal()
    context_changed = Signal(int)
    schedule_reselected = Signal()
    clock_changed = Signal()
    custom_catalog_changed = Signal()
    active_custom_changed = Signal(object)
    meeting_schedule_changed = Signal()
    pacing_mode_changed = Signal()

    def __init__(
        self,
        engine: TimerEngine,
        session: TimerSession,
        *,
        time_format: Callable[[], str] = lambda: "HH:mm:ss",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.engine = engine
        self.session = session
        self._time_format = time_format
        self.engine.set_persistence(session.record_schedule_change)
        self.engine.set_direction(session.clock_config.direction)
        self.engine.set_freeze_seconds(session.clock_config.freeze_seconds)
        self.engine.set_schedule(session.schedule)
        self._active_custom_version = self._custom_version()
        self.engine.state_changed.connect(self._on_engine_change)

    @property
    def time_format(self) -> str:
        return self._time_format()

    def _custom_version(self) -> tuple[str, int] | None:
        schedule = self.session.schedule
        if isinstance(schedule, CustomTimerSchedule):
            return schedule.id, schedule.revision
        return None

    @Slot(dict)
    def _on_engine_change(self, _snapshot: dict) -> None:
        previous = self._active_custom_version
        current = self._custom_version()
        self._active_custom_version = current
        if current is not None and previous != current:
            self.active_custom_changed.emit(self.session.schedule)
        self.changed.emit()

    def _publish_catalog_edit(self, *, context_changed: bool = False) -> None:
        if context_changed:
            self._show_context()
        self.custom_catalog_changed.emit()
        if not context_changed:
            self.changed.emit()

    def _show_context(self, direction: int = 0) -> None:
        if self.engine.schedule is not self.session.schedule:
            self.engine.set_schedule(self.session.schedule, notify=False)
        self.context_changed.emit(direction)
        self.engine.refresh_schedule()

    def _publish_schedule_edit(self, changed: bool) -> bool:
        if changed:
            schedule = self.session.schedule
            if self.engine.schedule is schedule:
                self.engine.refresh_schedule()
            else:
                self.engine.set_schedule(schedule, preserve_stopped=True)
        return changed

    def navigate_week(self, target: str) -> None:
        if target == "previous":
            self.session.previous_week()
            direction = -1
        elif target == "next":
            self.session.next_week()
            direction = 1
        elif target == "current":
            direction = self.session.go_to_current_week(current_monday())
        else:
            raise TimerOperationError("invalid", "Unknown meeting week.")
        self._show_context(direction)

    def set_meeting_type(self, meeting_type: MeetingType) -> bool:
        if not self.session.set_meeting_type(meeting_type):
            return False
        self._show_context()
        return True

    def apply_detected_assignments(
        self,
        current_week: date,
        timings: Iterable[DetectedPartTiming],
        scope: AssignmentAutofillScope,
    ) -> bool:
        return self._publish_schedule_edit(
            self.session.apply_detected_assignments(current_week, timings, scope),
        )

    def start_part(self, part_id: str) -> bool:
        part = self.session.schedule.part_by_id(part_id)
        if part is not None and part.state is not PartState.RUNNING:
            self.refresh_meeting_window()
        return self.engine.start_part(part_id, automatic=self.session.pacing_mode is PacingMode.AUTOMATIC)

    def pacing_state(self, now: float | None = None) -> PacingState:
        return self.engine.pacing_state(self.session.pacing_mode, now)

    def refresh_meeting_window(self) -> bool:
        return self._publish_schedule_edit(self.session.refresh_meeting_window())

    @Slot()
    def on_meeting_schedule_changed(self) -> None:
        try:
            self.refresh_meeting_window()
        except (OSError, RuntimeError):
            log.warning("Could not update the meeting timer window.", exc_info=True)
        self.meeting_schedule_changed.emit()

    def set_pacing_mode(self, mode: PacingMode) -> bool:
        if not self.session.set_pacing_mode(mode):
            return False
        self.pacing_mode_changed.emit()
        self.changed.emit()
        return True

    def stop_part(self, part_id: str | None = None) -> bool:
        return self.engine.stop_part(part_id)

    def reset_part(self, part_id: str) -> bool:
        changed = self.engine.reset_part(part_id)
        if changed and self.session.schedule.pacing_run is None:
            self.refresh_meeting_window()
        return changed

    def reset_all(self) -> bool:
        if not isinstance(self.session.schedule, CustomTimerSchedule):
            raise TimerOperationError("blocked", "Only custom timer schedules can be reset as a group.")
        return self.engine.reset_all()

    def adjust_part(self, part_id: str, delta_seconds: int) -> bool:
        part = self.session.schedule.part_by_id(part_id)
        if part is not None and part.state is PartState.RUNNING:
            return self.engine.adjust_elapsed(part_id, delta_seconds)
        return self._publish_schedule_edit(self.session.adjust_part(part_id, delta_seconds))

    def set_part_seconds(self, part_id: str, seconds: int) -> bool:
        return self._publish_schedule_edit(self.session.set_part_seconds(part_id, seconds))

    def set_section_count(self, section: Section, count: int) -> bool:
        return self._publish_schedule_edit(self.session.set_section_count(section, count))

    def rename_part(self, part_id: str, title: str | None) -> bool:
        return self._publish_schedule_edit(self.session.rename_part(part_id, title))

    def edit_part(self, part_id: str, title: str | None, seconds: int | None) -> bool:
        return self._publish_schedule_edit(self.session.edit_part(part_id, title, seconds))

    def update_clock(self, key: str, value: object) -> None:
        config = self.session.update_clock(key, value)
        if config is None:
            return
        self.engine.set_freeze_seconds(config.freeze_seconds)
        if self.engine.direction is config.direction:
            self.engine.refresh_schedule()
        else:
            self.engine.set_direction(config.direction)
        self.clock_changed.emit()

    def list_custom_schedules(self) -> tuple[CustomTimerSchedule, ...]:
        return self.session.list_custom_schedules()

    def get_custom_schedule(self, schedule_id: str) -> CustomTimerSchedule | None:
        return self.session.get_custom_schedule(schedule_id)

    def create_custom_schedule(
        self, title: str, *, first_part_title: str | None = None, first_part_seconds: int = 0,
        select: bool = False,
    ) -> CustomTimerSchedule:
        return self._publish_new_custom(
            self.session.create_custom_schedule,
            title,
            first_part_title=first_part_title,
            first_part_seconds=first_part_seconds,
            select=select,
        )

    def duplicate_custom_schedule(
        self, schedule_id: str, title: str, expected_revision: int, *, select: bool = False,
    ) -> CustomTimerSchedule:
        return self._publish_new_custom(
            self.session.duplicate_custom_schedule, schedule_id, title, expected_revision, select=select,
        )

    def _publish_new_custom(
        self, create: Callable[..., CustomTimerSchedule], *args, select: bool, **kwargs,
    ) -> CustomTimerSchedule:
        try:
            schedule = create(*args, select=select, **kwargs)
        except TimerOperationError as error:
            if error.code == "created_without_selection":
                self._publish_catalog_edit()
            raise
        self._publish_catalog_edit(context_changed=select)
        return schedule

    def delete_custom_schedule(
        self, schedule_id: str, expected_revision: int, *, replace_active: bool = False,
    ) -> bool:
        context_changed = self.session.delete_custom_schedule(
            schedule_id, expected_revision, replace_active=replace_active,
        )
        self._publish_catalog_edit(context_changed=context_changed)
        return context_changed

    def select_custom_schedule(self, schedule_id: str, *, expected_revision: int | None = None) -> bool:
        if not self.session.select_custom_schedule(schedule_id, expected_revision=expected_revision):
            self.schedule_reselected.emit()
            return False
        self._show_context()
        return True

    def select_meeting_schedule(self) -> bool:
        changed = self.session.select_meeting_schedule()
        if changed:
            self._show_context()
        else:
            self.schedule_reselected.emit()
        return changed

    def edit_custom_schedule(
        self, schedule_id: str, expected_revision: int, action: str, **values: object,
    ) -> bool:
        if not self.session.edit_custom_schedule(schedule_id, expected_revision, action, **values):
            return False
        self._publish_custom_edit(schedule_id)
        return True

    def replace_custom_schedule_entries(
        self,
        schedule_id: str,
        expected_revision: int,
        draft_entries: list[dict[str, object]],
    ) -> bool:
        if not self.session.replace_custom_schedule_entries(
            schedule_id, expected_revision, draft_entries
        ):
            return False
        self._publish_custom_edit(schedule_id)
        return True

    def replace_custom_timing_settings(
        self,
        schedule_id: str,
        expected_revision: int,
        *,
        start_time: str | None,
        end_time: str | None,
        untracked_seconds: int | None = None,
        update_untracked: bool = False,
        weights: Mapping[str, int] | None = None,
        before_parts: Mapping[str, int | None] | None = None,
        closing_seconds: int | None = None,
        update_closing: bool = False,
    ) -> bool:
        if not self.session.replace_custom_timing_settings(
            schedule_id,
            expected_revision,
            start_time=start_time,
            end_time=end_time,
            untracked_seconds=untracked_seconds,
            update_untracked=update_untracked,
            weights=weights,
            before_parts=before_parts,
            closing_seconds=closing_seconds,
            update_closing=update_closing,
        ):
            return False
        self._publish_custom_edit(schedule_id)
        return True

    def _publish_custom_edit(self, schedule_id: str) -> None:
        active = self.session.schedule
        if isinstance(active, CustomTimerSchedule) and active.id == schedule_id:
            self._publish_schedule_edit(True)
        else:
            self._publish_catalog_edit()
