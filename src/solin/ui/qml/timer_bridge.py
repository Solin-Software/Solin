"""QML adapter for the profile timer and its projection controls."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QDateTime, QObject, Property, Signal, Slot

from solin.controllers.timer_operations import TimerOperations
from solin.core.i18n.date import format_time_with_seconds, week_label
from solin.core.i18n.meeting_schedule import (
    meeting_kind_label,
    meeting_weekday_names,
)
from solin.core.i18n.timer_schedule_view import (
    CUSTOM_SECTION_DEFAULT_ICON_KEY,
    format_mmss,
    format_total_duration,
    parts_model_for_schedule,
    section_icon_key,
    section_palette,
)
from solin.core.jw.language_context import jw_media_language_context
from solin.core.meetings.meeting_weeks import current_monday
from solin.core.timer.application import (
    DEFAULT_ASSIGNMENT_AUTOFILL_SCOPE,
    AssignmentAutofillScope,
    DetectedPartTiming,
)
from solin.core.timer.custom_schedule import (
    TimerDurationBudgetError,
    TimerOperationError,
)
from solin.core.timer.models import (
    ANALOG_CLOCK_STYLE_OPTIONS,
    CLOCK_MODE_OPTIONS,
    CUSTOM_SECTION_COLOR_PRESETS,
    CUSTOM_SECTION_ICON_CATALOG,
    MAX_PLANNED_PART_SECONDS,
    MEDIA_COUNTDOWN_PRESENTATION_OPTIONS,
    MIN_CUSTOM_PART_SECONDS,
    MIN_PLANNED_PART_SECONDS,
    PART_TIMER_DISPLAY_OPTIONS,
    CustomSectionIcon,
    CustomTimerSchedule,
    MeetingSchedule,
    MeetingType,
    PartKind,
    PartState,
    Section,
    normalize_section_color,
)
from solin.core.timer.pacing_models import PacingMode
from solin.core.timer.schedule_factory import schedule_has_timing_data
from solin.controllers.timer_monitor_controller import TimerMonitorController
from solin.controllers.timer_pdf_export_controller import TimerPdfExportController
from .timer_schedule_models import (
    custom_catalog_item,
    custom_schedule_model,
    custom_schedules_model,
    display_entries_model,
)

if TYPE_CHECKING:
    from solin.controllers.timer_hand_alert_controller import TimerHandAlertController
    from solin.controllers.timer_workbook_lookup import TimerWorkbookLookup
    from solin.core.meetings.workbook import Workbook

    _QVARIANT = object
else:
    _QVARIANT = "QVariant"


log = logging.getLogger(__name__)

_MIN_MANUAL_COUNTDOWN_SECONDS = 10
_MAX_MANUAL_COUNTDOWN_SECONDS = 24 * 60 * 60 - 1
_PACING_MODES: tuple[PacingMode, ...] = (
    PacingMode.OFF,
    PacingMode.MONITOR,
    PacingMode.AUTOMATIC,
)


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    parts = text.strip().split(":")
    if len(parts) != 2 or not all(item.isdecimal() for item in parts):
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _parse_optional_nonnegative_int(value: object) -> tuple[bool, int | None]:
    """Parse a QML optional seconds field (-1/None -> None, >=0 int -> int)."""
    if value is None or value == "":
        return True, None
    if isinstance(value, bool):
        return False, None
    if isinstance(value, int):
        return (True, None) if value == -1 else ((True, value) if value >= 0 else (False, None))
    if isinstance(value, float):
        if not value.is_integer():
            return False, None
        ival = int(value)
        return (True, None) if ival == -1 else ((True, ival) if ival >= 0 else (False, None))
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return True, None
        if cleaned.isascii() and cleaned.isdecimal():
            try:
                return True, int(cleaned)
            except ValueError:
                return False, None
    return False, None


class TimerBridge(QObject):
    """Context object bound to the QML timer tab as ``timer``."""

    scheduleChanged = Signal()
    liveStateChanged = Signal()
    clockConfigChanged = Signal()
    monitorsChanged = Signal()
    manualHandChanged = Signal()
    weekChanged = Signal()
    customSchedulesChanged = Signal()
    sectionAppearanceChanged = Signal()
    pacingChanged = Signal()
    feedbackChanged = Signal()
    # Direction hint for the QML parts-list transition: -1 = earlier (slide in
    # from the left), +1 = later (from the right), 0 = no direction (fade only).
    weekShift = Signal(int)

    # Relayed to TimerWidget.project_timer_signal for the media-countdown mode.
    mediaCountdownRequested = Signal(QDateTime, str)
    mediaCountdownChanged = Signal()
    meetingScheduleConfigurationRequested = Signal()
    pointerEntered = Signal()
    pointerExited = Signal()

    def __init__(
        self,
        *,
        operations: TimerOperations,
        output,
        monitors: TimerMonitorController,
        hand_alert: TimerHandAlertController,
        pdf_export: TimerPdfExportController,
        media_countdown_automation,
        language_manager=None,
        workbook_lookup: TimerWorkbookLookup | None = None,
        assignment_scope: AssignmentAutofillScope = DEFAULT_ASSIGNMENT_AUTOFILL_SCOPE,
        notify_error: Callable[[str], object] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.operations = operations
        self._session = operations.session
        self._output = output
        self._monitors = monitors
        self._hand_alert = hand_alert
        self._pdf_export = pdf_export
        self._media_countdown_automation = media_countdown_automation
        self._language_manager = language_manager
        self._live = operations.engine.snapshot().to_dict()
        self._workbook_lookup = workbook_lookup
        self._assignment_scope = assignment_scope
        self._notify_error = notify_error
        self._timer_page_visible = False
        self._assignments_mode_active = False
        self._assignment_lookup_deferred = False
        self._assignment_context = (self._session.week_monday, self._session.meeting_type)
        self._assignment_target: tuple[date, str] | None = None
        self._assignment_generation = 0
        self._active_tab = (
            "custom" if isinstance(self._session.schedule, CustomTimerSchedule) else "meetings"
        )
        self._last_custom_schedule_id: str | None = (
            self._session.schedule.id
            if isinstance(self._session.schedule, CustomTimerSchedule)
            else None
        )
        self._deleting_current_custom = False
        self._feedback: dict[str, object] = {}
        self._feedback_key: str | None = None
        self._custom_catalog: list[dict[str, object]] = []
        self._custom_catalog_available = False
        self._last_pacing_payload: dict[str, object] = self._compute_pacing_model()
        self.refreshCustomCatalog()

        if workbook_lookup is not None:
            workbook_lookup.setParent(self)
            workbook_lookup.completed.connect(self._apply_workbook)
            if language_manager is not None:
                language_manager.language_changed.connect(self._refresh_assignment_lookup)
                language_manager.jw_lang_service.media_language_changed.connect(
                    self._refresh_assignment_lookup
                )

        operations.engine.state_changed.connect(self._on_engine_state)
        operations.engine.tick.connect(self._on_engine_tick)
        operations.context_changed.connect(self._on_context_changed)
        operations.schedule_reselected.connect(self._on_schedule_reselected)
        operations.clock_changed.connect(self._on_clock_changed)
        operations.custom_catalog_changed.connect(self.refreshCustomCatalog)
        operations.active_custom_changed.connect(self._on_active_custom_changed)
        operations.meeting_schedule_changed.connect(self._on_meeting_schedule_changed)
        operations.pacing_mode_changed.connect(self._on_pacing_mode_changed)
        self._hand_alert.controls_changed.connect(self.manualHandChanged)
        self._media_countdown_automation.state_changed.connect(
            self.mediaCountdownChanged
        )

    # ── Exposed: manual hand alert ──────────────────────────────────────────

    @Property(bool, notify=manualHandChanged)
    def manualHandAvailable(self) -> bool:
        return self._hand_alert.available

    @Property(bool, notify=manualHandChanged)
    def manualHandRaised(self) -> bool:
        return self._hand_alert.manual

    @Slot(bool)
    def setManualHand(self, raised: bool) -> None:
        self._hand_alert.set_manual(raised)

    # ── Schedule lifecycle ─────────────────────────────────────────────────

    @Slot(int)
    def _on_context_changed(self, direction: int) -> None:
        schedule = self._session.schedule
        if isinstance(schedule, CustomTimerSchedule):
            self._active_tab = "custom"
            self._last_custom_schedule_id = schedule.id
        elif self._deleting_current_custom:
            self._active_tab = "custom"
            self._last_custom_schedule_id = None
        else:
            self._active_tab = "meetings"
        context = (self._session.week_monday, self._session.meeting_type)
        if context != self._assignment_context:
            self._assignment_lookup_deferred = False
            self._assignment_context = context
        self.weekChanged.emit()
        self.weekShift.emit(direction)

    @Slot()
    def _on_schedule_reselected(self) -> None:
        if self._is_empty_custom_view():
            self._active_tab = "meetings"
            self.scheduleChanged.emit()
            self.liveStateChanged.emit()
            self._emit_pacing_if_changed(force=False)

    @Slot()
    def _on_clock_changed(self) -> None:
        self.clockConfigChanged.emit()
        self._emit_pacing_if_changed(force=True)

    @Slot()
    def _on_pacing_mode_changed(self) -> None:
        self._emit_pacing_if_changed(force=False)

    def set_timer_page_visible(self, visible: bool) -> None:
        self._timer_page_visible = visible
        if not visible:
            self._assignment_lookup_deferred = False
        self._refresh_assignment_lookup()

    @Slot(bool)
    def setAssignmentsModeActive(self, active: bool) -> None:
        self._assignments_mode_active = active
        if not active:
            self._assignment_lookup_deferred = False
        self._refresh_assignment_lookup()

    @Slot()
    def deferAssignmentLookupForEdit(self) -> None:
        # Let the operator finish this visit without replacing an inline draft.
        self._assignment_lookup_deferred = True
        self._refresh_assignment_lookup()

    def shutdown_assignment_lookup(self) -> None:
        lookup, self._workbook_lookup = self._workbook_lookup, None
        self._assignment_target = None
        if lookup is not None:
            lookup.shutdown()

    def _eligible_assignment_target(self, current_week: date) -> tuple[date, str] | None:
        if (
            self._assignment_scope is AssignmentAutofillScope.OFF
            or self._assignment_lookup_deferred
            or not self._timer_page_visible
            or not self._assignments_mode_active
            or not self._session.can_autofill_assignments(current_week)
        ):
            return None
        return self._session.week_monday, jw_media_language_context(self._language_manager).api_code

    def _refresh_assignment_lookup(self, _language: str = "") -> None:
        lookup = self._workbook_lookup
        if lookup is None:
            return
        target = self._eligible_assignment_target(current_monday())
        if target == self._assignment_target:
            return
        self._assignment_target = target
        self._assignment_generation += 1
        if target is None:
            lookup.invalidate()
        else:
            lookup.request(self._assignment_generation, *target)

    @Slot(object, object)
    def _apply_workbook(self, generation: int, workbook: Workbook) -> None:
        current_week = current_monday()
        if (
            self._assignment_target is None
            or generation != self._assignment_generation
            or self._assignment_target != self._eligible_assignment_target(current_week)
        ):
            return
        timings = (
            DetectedPartTiming(Section(part.section), PartKind(part.kind), part.duration_seconds)
            for part in workbook.parts
        )
        try:
            self.operations.apply_detected_assignments(
                current_week, timings, self._assignment_scope,
            )
        except (OSError, RuntimeError, ValueError):
            log.warning("Could not adopt detected timer assignments for %s", self._assignment_target,
                        exc_info=True)
            return

    def refresh_language(self) -> None:
        self.scheduleChanged.emit()
        self.sectionAppearanceChanged.emit()
        self.clockConfigChanged.emit()
        self.mediaCountdownChanged.emit()
        self._emit_pacing_if_changed(force=True)

    def refresh_theme(self) -> None:
        self.scheduleChanged.emit()
        self.sectionAppearanceChanged.emit()
        self.clockConfigChanged.emit()
        self.monitorsChanged.emit()

    def _on_engine_state(self, snapshot: dict) -> None:
        self._live = snapshot
        if self._is_empty_custom_view() and self._session.schedule.has_running_part():
            self._active_tab = "meetings"
        self._refresh_assignment_lookup()
        self.scheduleChanged.emit()
        self.liveStateChanged.emit()
        self._emit_pacing_if_changed(force=False)

    def _on_engine_tick(self, snapshot: dict) -> None:
        if snapshot == self._live:
            return
        self._live = snapshot
        self.liveStateChanged.emit()
        if self._session.pacing_mode is not PacingMode.OFF:
            self._emit_pacing_if_changed(force=False)

    # ── Exposed: feedback / errors ─────────────────────────────────────────

    def _feedback_model(self) -> dict[str, object]:
        return dict(self._feedback)

    feedback = Property(_QVARIANT, _feedback_model, notify=feedbackChanged)

    def _set_feedback(
        self,
        *,
        code: str,
        message: str,
        scope: str = "schedule",
        retryable: bool = False,
        available_seconds: int = 0,
        requested_seconds: int = 0,
        feedback_key: str | None = None,
        notify: bool = False,
    ) -> None:
        self._feedback_key = feedback_key
        self._feedback = {
            "code": code,
            "message": message,
            "scope": scope,
            "retryable": retryable,
            "availableSeconds": available_seconds,
            "requestedSeconds": requested_seconds,
        }
        self.feedbackChanged.emit()
        if notify and self._notify_error is not None:
            self._notify_error(message)

    @Slot()
    def clearFeedback(self) -> None:
        if self._feedback:
            self._feedback = {}
            self._feedback_key = None
            self.feedbackChanged.emit()

    def _localize_operation_error(self, error: TimerOperationError) -> str:
        if isinstance(error, TimerDurationBudgetError):
            return self.tr(
                "Total duration ({requested}) exceeds the schedule window ({available})."
            ).format(
                requested=format_mmss(error.requested_seconds),
                available=format_mmss(error.available_seconds),
            )
        if error.code == "schedule_stale":
            return self.tr(
                "This schedule was updated elsewhere. Reload it before making changes."
            )
        if error.code == "blocked":
            if self._session.schedule.has_running_part():
                return self.tr("Stop the running timer before making this change.")
            if "section" in str(error).lower():
                return self.tr("Move or delete the section's parts before deleting the section.")
            if "duration" in str(error).lower() or "reset" in str(error).lower():
                return self.tr("Reset the part before changing its duration.")
            return self.tr("This action is currently unavailable.")
        if error.code == "not_found":
            return self.tr("The selected item no longer exists.")
        if error.code == "created_without_selection":
            return self.tr("Could not finish creating the schedule. Check the schedule list before trying again.")
        if error.code == "selection_restore_failed":
            return self.tr("Could not change the schedule or restore the previous selection.")
        if error.code == "invalid" and "start and end" in str(error).lower():
            return self.tr("Enter valid start and end times (HH:MM).")
        return str(error)

    # ── Exposed: schedule tab, summary & parts model ───────────────────────

    def _is_empty_custom_view(self) -> bool:
        return self._active_tab == "custom" and not isinstance(
            self._session.schedule, CustomTimerSchedule
        )

    @Property(str, notify=scheduleChanged)
    def activeScheduleTab(self) -> str:
        return self._active_tab

    @Property(str, notify=scheduleChanged)
    def scheduleKind(self) -> str:
        if self._is_empty_custom_view():
            return "custom"
        return "custom" if isinstance(self._session.schedule, CustomTimerSchedule) else "meeting"

    @Property(bool, notify=liveStateChanged)
    def isRunning(self) -> bool:
        return self._session.schedule.has_running_part()

    @Property(int, constant=True)
    def maxPlannedPartSeconds(self) -> int:
        return MAX_PLANNED_PART_SECONDS

    @Property(int, notify=scheduleChanged)
    def minPlannedPartSeconds(self) -> int:
        if isinstance(self._session.schedule, CustomTimerSchedule):
            return MIN_CUSTOM_PART_SECONDS
        return MIN_PLANNED_PART_SECONDS

    def _can_export_pdf(self) -> bool:
        if self._is_empty_custom_view():
            return False
        return len(self._session.schedule.parts) > 0

    canExportPdf = Property(bool, _can_export_pdf, notify=scheduleChanged)

    def _parts_model(self) -> list:
        if self._is_empty_custom_view():
            return []
        return parts_model_for_schedule(self._session.schedule, self.operations.time_format)

    parts = Property(_QVARIANT, _parts_model, notify=scheduleChanged)

    def _display_entries_model(self) -> list[dict[str, object]]:
        if self._is_empty_custom_view():
            return []
        return display_entries_model(self._session.schedule, self.operations.time_format)

    displayEntries = Property(_QVARIANT, _display_entries_model, notify=scheduleChanged)

    @Slot()
    def _on_meeting_schedule_changed(self) -> None:
        self._emit_pacing_if_changed(force=False)

    def _live_model(self) -> dict:
        if self._is_empty_custom_view():
            return {
                "active": False,
                "active_part_id": None,
                "active_part_title": "",
                "state": PartState.IDLE.value,
                "direction": self._session.clock_config.direction.value,
                "planned_seconds": 0,
                "elapsed_seconds": 0.0,
                "remaining_seconds": 0.0,
                "display_seconds": 0.0,
                "overrun": False,
                "wall_clock_epoch": self._live.get("wall_clock_epoch", 0.0),
            }
        return self._live

    liveState = Property(_QVARIANT, _live_model, notify=liveStateChanged)

    def _current_schedule_binding(self) -> tuple[str, int]:
        schedule = self._session.schedule
        if isinstance(schedule, CustomTimerSchedule):
            return (schedule.id, schedule.revision)
        return (
            f"meeting:{schedule.week_monday}:{schedule.meeting_type.value}",
            self._session.meeting_revision,
        )

    def _schedule_summary_model(self) -> dict[str, object]:
        if self._is_empty_custom_view():
            return {
                "scheduleId": "",
                "scheduleRevision": 0,
                "nextPartId": "",
                "nextPartTitle": "",
                "nextPartPlannedLabel": "",
                "allCompleted": False,
                "completedCount": 0,
                "totalCount": 0,
                "plannedTotalLabel": "00:00",
                "hasAnyTimingData": False,
                "activePlannedSeconds": 0,
                "activePlannedLabel": "",
                "activeEffectiveSeconds": 0,
                "activeEffectiveLabel": "",
                "activeHasAdjustedTarget": False,
            }
        schedule = self._session.schedule
        binding_id, binding_revision = self._current_schedule_binding()
        parts = schedule.parts
        active_id = self._live.get("active_part_id") if self._live.get("active") else None
        active_part = schedule.part_by_id(active_id) if isinstance(active_id, str) else None
        active_index = schedule.index_of(active_id) if isinstance(active_id, str) else -1

        next_part = None
        if active_index >= 0:
            for candidate in parts[active_index + 1:]:
                if candidate.state is PartState.IDLE:
                    next_part = candidate
                    break
        if next_part is None:
            next_part = next(
                (candidate for candidate in parts if candidate.state is PartState.IDLE),
                None,
            )

        rows = parts_model_for_schedule(schedule, self.operations.time_format)
        row_by_id = {row["id"]: row for row in rows}
        next_row = row_by_id.get(next_part.id) if next_part is not None else None
        completed_count = sum(1 for part in parts if part.state is PartState.STOPPED)
        has_timing = schedule_has_timing_data(schedule)
        return {
            "scheduleId": binding_id,
            "scheduleRevision": binding_revision,
            "nextPartId": next_part.id if next_part is not None else "",
            "nextPartTitle": next_row["title"] if next_row is not None else "",
            "nextPartPlannedLabel": next_row["plannedLabel"] if next_row is not None else "",
            "allCompleted": len(parts) > 0 and completed_count == len(parts),
            "completedCount": completed_count,
            "totalCount": len(parts),
            "plannedTotalLabel": format_total_duration(schedule.planned_total_seconds()),
            "hasAnyTimingData": has_timing,
            "activePlannedSeconds": active_part.planned_seconds if active_part is not None else 0,
            "activePlannedLabel": format_mmss(active_part.planned_seconds) if active_part is not None else "",
            "activeEffectiveSeconds": active_part.effective_seconds if active_part is not None else 0,
            "activeEffectiveLabel": format_mmss(active_part.effective_seconds) if active_part is not None else "",
            "activeHasAdjustedTarget": (
                active_part is not None
                and active_part.effective_seconds != active_part.planned_seconds
            ),
        }

    scheduleSummary = Property(_QVARIANT, _schedule_summary_model, notify=scheduleChanged)

    @Slot(str, result=bool)
    def selectScheduleTab(self, tab: str) -> bool:
        if tab not in ("meetings", "custom"):
            return False
        if tab == self._active_tab:
            return True
        if self._session.schedule.has_running_part():
            self._set_feedback(
                code="blocked",
                message=self.tr("Stop the running timer before switching schedules."),
                scope="schedule",
                notify=True,
            )
            return False
        self.clearFeedback()
        if tab == "meetings":
            return self._apply_command(self.operations.select_meeting_schedule, notify=True)

        if not self.refreshCustomCatalog():
            return False
        schedules = self._custom_catalog
        if schedules:
            last_id = self._last_custom_schedule_id
            target_id = (
                last_id
                if last_id is not None and any(item["id"] == last_id for item in schedules)
                else str(schedules[0]["id"])
            )
            return self.selectCustomSchedule(target_id)

        self._show_empty_custom_view()
        return True

    def _show_empty_custom_view(self) -> None:
        self._active_tab = "custom"
        self.scheduleChanged.emit()
        self.liveStateChanged.emit()
        self._emit_pacing_if_changed(force=False)

    # ── Exposed: week / meeting type ───────────────────────────────────────

    def _week_label(self) -> str:
        return week_label(self._session.week_monday)

    weekLabel = Property(str, _week_label, notify=weekChanged)

    def _meeting_type_value(self) -> str:
        return self._session.meeting_type.value

    meetingType = Property(str, _meeting_type_value, notify=weekChanged)

    def _is_current_week(self) -> bool:
        return self._session.is_current_week

    isCurrentWeek = Property(bool, _is_current_week, notify=weekChanged)

    @Slot()
    def previousWeek(self) -> None:
        self._apply_command(self.operations.navigate_week, "previous", notify=True)

    @Slot()
    def nextWeek(self) -> None:
        self._apply_command(self.operations.navigate_week, "next", notify=True)

    @Slot()
    def goToCurrentWeek(self) -> None:
        self._apply_command(self.operations.navigate_week, "current", notify=True)

    @Slot(str)
    def setMeetingType(self, value: str) -> None:
        try:
            mt = MeetingType(value)
        except ValueError:
            return
        self._apply_command(self.operations.set_meeting_type, mt, notify=True)

    @Slot()
    def exportSchedulePdf(self) -> None:
        if not self._can_export_pdf():
            return
        date_format = getattr(
            self._language_manager,
            "date_format",
            "dd/MM/yyyy HH:mm",
        )
        meeting_type_label = (
            self.tr("Weekend") if self._session.meeting_type is MeetingType.WEEKEND
            else self.tr("Midweek")
        )
        self._pdf_export.export(
            self._session.schedule,
            week_label=self._week_label(),
            meeting_type_label=meeting_type_label,
            date_format=date_format,
            time_format=self.operations.time_format,
            pacing_mode=self._session.pacing_mode,
        )

    # ── Exposed: part execution & meeting/inline editing ───────────────────

    @Slot(str)
    def startPart(self, part_id: str) -> None:
        self._apply_command(self.operations.start_part, part_id, notify=True)

    @Slot(result=bool)
    def startNextPart(self) -> bool:
        next_id = str(self._schedule_summary_model().get("nextPartId") or "")
        if not next_id:
            return False
        return self._apply_command(self.operations.start_part, next_id, notify=True)

    @Slot(str)
    def stopPart(self, part_id: str) -> None:
        self._apply_command(self.operations.stop_part, part_id or None, notify=True)

    @Slot(str)
    def resetPart(self, part_id: str) -> None:
        self._apply_command(self.operations.reset_part, part_id, notify=True)

    def _require_schedule_binding(self, expected_schedule_id: str, expected_revision: int) -> None:
        current_id, current_revision = self._current_schedule_binding()
        if (
            self._is_empty_custom_view()
            or not expected_schedule_id
            or current_id != expected_schedule_id
            or current_revision != int(expected_revision)
        ):
            raise TimerOperationError("schedule_stale", "The timer schedule changed. Reload it before editing.")

    def _apply_bound_command(self, schedule_id: str, revision: int, command, *args, scope: str = "schedule") -> bool:
        def apply() -> None:
            self._require_schedule_binding(schedule_id, revision)
            command(*args)

        return self._apply_command(apply, scope=scope)

    @Slot(str, int, result=bool)
    def resetAllResults(self, expected_schedule_id: str, expected_revision: int) -> bool:
        return self._apply_bound_command(expected_schedule_id, expected_revision, self.operations.reset_all)

    @Slot(str, int, str, str, int, result=bool)
    def savePartEdit(self, schedule_id: str, revision: int, part_id: str, title: str, seconds: int) -> bool:
        return self._apply_bound_command(
            schedule_id, revision, self.operations.edit_part, part_id,
            title or None, None if seconds == -1 else seconds, scope=f"part:{part_id}",
        )

    @Slot(str, int, str, result=bool)
    def resetEditedPart(self, schedule_id: str, revision: int, part_id: str) -> bool:
        return self._apply_bound_command(schedule_id, revision, self.operations.reset_part, part_id, scope=f"part:{part_id}")

    @Slot(str, int, str, result=bool)
    def restoreEditedPartTitle(self, schedule_id: str, revision: int, part_id: str) -> bool:
        return self._apply_bound_command(
            schedule_id, revision, self.operations.rename_part, part_id, None, scope=f"part:{part_id}",
        )

    @Slot(str, int)
    def adjustPart(self, part_id: str, delta_seconds: int) -> None:
        self._apply_command(
            self.operations.adjust_part, part_id, delta_seconds, scope=f"part:{part_id}", notify=True,
        )

    @Slot(str, int)
    def setSectionCount(self, section: str, count: int) -> None:
        try:
            sec = Section(section)
        except ValueError:
            return
        self._apply_command(
            self.operations.set_section_count, sec, count, scope=f"section:{section}", notify=True,
        )

    # ── Exposed: custom schedules catalog & editor ─────────────────────────

    def _custom_schedules_model(self) -> list[dict[str, object]]:
        return self._custom_catalog

    customSchedules = Property(_QVARIANT, _custom_schedules_model, notify=customSchedulesChanged)

    @Property(bool, notify=customSchedulesChanged)
    def customCatalogAvailable(self) -> bool:
        return self._custom_catalog_available

    @Slot(result=bool)
    def refreshCustomCatalog(self) -> bool:
        """Publish a complete catalog snapshot, preserving the last successful read on failure."""
        try:
            catalog = custom_schedules_model(self.operations.list_custom_schedules())
        except (OSError, RuntimeError):
            log.warning("Could not list custom timer schedules", exc_info=True)
            if self._custom_catalog_available:
                self._custom_catalog_available = False
                self.customSchedulesChanged.emit()
            return False
        changed = not self._custom_catalog_available or catalog != self._custom_catalog
        self._custom_catalog_available = True
        self._custom_catalog = catalog
        if changed:
            self.customSchedulesChanged.emit()
        return True

    @Slot(object)
    def _on_active_custom_changed(self, schedule: CustomTimerSchedule) -> None:
        for index, item in enumerate(self._custom_catalog):
            if item["id"] != schedule.id:
                continue
            updated = custom_catalog_item(schedule)
            if updated != item:
                self._custom_catalog[index] = updated
                if item["title"] != schedule.title:
                    self._custom_catalog.sort(key=lambda entry: (str(entry["title"]).casefold(), entry["id"]))
                self.customSchedulesChanged.emit()
            return

    def _active_custom_schedule_model(self) -> dict[str, object]:
        schedule = self._session.schedule
        return custom_schedule_model(schedule) if isinstance(schedule, CustomTimerSchedule) else {}

    activeCustomSchedule = Property(
        _QVARIANT, _active_custom_schedule_model, notify=scheduleChanged
    )

    def _section_icon_label(self, icon: CustomSectionIcon) -> str:
        labels = {
            CustomSectionIcon.TREASURES: self.tr("Diamond"),
            CustomSectionIcon.MINISTRY: self.tr("Wheat"),
            CustomSectionIcon.LIVING: self.tr("Sheep"),
            CustomSectionIcon.PUBLIC_TALK: self.tr("Speaker"),
            CustomSectionIcon.WATCHTOWER: self.tr("Tower"),
            CustomSectionIcon.BOOK: self.tr("Book"),
            CustomSectionIcon.PEOPLE: self.tr("People"),
            CustomSectionIcon.MICROPHONE: self.tr("Microphone"),
            CustomSectionIcon.MUSIC: self.tr("Music"),
            CustomSectionIcon.VIDEO: self.tr("Video"),
            CustomSectionIcon.CALENDAR: self.tr("Calendar"),
            CustomSectionIcon.CLOCK: self.tr("Clock"),
            CustomSectionIcon.MARKER: self.tr("Marker"),
            CustomSectionIcon.CHECK: self.tr("Check"),
        }
        return labels.get(icon, icon.value)

    def _section_icon_catalog_model(self) -> list[dict[str, str]]:
        return [
            {
                "id": icon.value,
                "iconKey": section_icon_key("custom", icon),
                "label": self._section_icon_label(icon),
            }
            for icon in CUSTOM_SECTION_ICON_CATALOG
        ]

    sectionIconCatalog = Property(
        _QVARIANT, _section_icon_catalog_model, notify=sectionAppearanceChanged
    )

    def _section_color_label(self, key: str) -> str:
        labels = {
            "teal": self.tr("Teal"),
            "gold": self.tr("Gold"),
            "crimson": self.tr("Crimson"),
            "amber": self.tr("Amber"),
            "indigo": self.tr("Indigo"),
            "blue": self.tr("Blue"),
            "emerald": self.tr("Emerald"),
            "violet": self.tr("Violet"),
            "neutral": self.tr("Neutral"),
        }
        return labels.get(key, key)

    def _section_color_presets_model(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for key, hex_color in CUSTOM_SECTION_COLOR_PRESETS:
            palette = section_palette("custom", hex_color)
            items.append({
                "id": key,
                "color": hex_color,
                "label": self._section_color_label(key),
                "textColor": palette["text"],
                "badgeBg": palette["badge"],
                "borderColor": palette["border"],
            })
        return items

    sectionColorPresets = Property(
        _QVARIANT, _section_color_presets_model, notify=sectionAppearanceChanged
    )

    @Slot(str, result=_QVARIANT)
    def customSectionPalette(self, color: str) -> dict[str, str]:
        try:
            normalized = normalize_section_color(color)
        except ValueError:
            normalized = None
        return section_palette("custom", normalized)

    @Slot(float, bool, result=str)
    def formatPlannedDuration(self, seconds: float, compact: bool) -> str:
        return format_total_duration(seconds) if compact else format_mmss(seconds)

    @Property(str, constant=True)
    def defaultSectionIconKey(self) -> str:
        return CUSTOM_SECTION_DEFAULT_ICON_KEY

    @Slot(str, result=bool)
    def selectCustomSchedule(self, schedule_id: str) -> bool:
        if not schedule_id:
            return False
        return self._apply_command(
            self.operations.select_custom_schedule, schedule_id, notify=True,
        )

    @Slot(str, str, int, result=str)
    def createCustomSchedule(
        self, title: str, first_part_title: str = "", first_part_seconds: int = 0,
    ) -> str:
        _, schedule = self._run_command(
            self.operations.create_custom_schedule,
            title,
            first_part_title=first_part_title if first_part_title.strip() else None,
            first_part_seconds=first_part_seconds,
            select=True,
            scope="create",
        )
        return schedule.id if schedule is not None else ""

    @Slot(str, int, str, result=str)
    def duplicateCustomSchedule(self, schedule_id: str, expected_revision: int, title: str) -> str:
        _, schedule = self._run_command(
            self.operations.duplicate_custom_schedule,
            schedule_id,
            title,
            expected_revision,
            select=True,
        )
        return schedule.id if schedule is not None else ""

    @Slot(str, int, result=bool)
    def deleteCustomSchedule(self, schedule_id: str, expected_revision: int) -> bool:
        current = self._session.schedule
        self._deleting_current_custom = isinstance(current, CustomTimerSchedule) and current.id == schedule_id
        try:
            return self._apply_command(
                self.operations.delete_custom_schedule, schedule_id, expected_revision, replace_active=True,
            )
        finally:
            self._deleting_current_custom = False

    @Slot()
    def reloadActiveCustomSchedule(self) -> None:
        self.clearFeedback()
        self.scheduleChanged.emit()
        self.refreshCustomCatalog()
        self._emit_pacing_if_changed(force=True)

    def _edit_custom(
        self,
        schedule_id: str,
        revision: int,
        action: str,
        *,
        scope: str = "editor",
        notify: bool = False,
        **values: object,
    ) -> bool:
        return self._apply_command(
            self.operations.edit_custom_schedule,
            schedule_id,
            int(revision),
            action,
            scope=scope,
            notify=notify,
            **values,
        )

    @Slot(str, int, str, result=bool)
    def renameCustomSchedule(self, schedule_id: str, revision: int, title: str) -> bool:
        return self._edit_custom(
            schedule_id, revision, "rename_schedule", scope="editor", title=title
        )

    @Slot(str, int, str, int, str, str, result=bool)
    def addCustomPart(
        self,
        schedule_id: str,
        revision: int,
        title: str,
        planned_seconds: int,
        section_id: str = "",
        before_id: str = "",
    ) -> bool:
        return self._edit_custom(
            schedule_id,
            revision,
            "add_part",
            scope=f"section:{section_id}" if section_id else "editor",
            title=title,
            planned_seconds=int(planned_seconds),
            section_id=section_id or None,
            before_id=before_id or None,
        )

    @Slot(str, int, str, str, result=bool)
    def addCustomSection(
        self,
        schedule_id: str,
        revision: int,
        title: str,
        before_id: str = "",
    ) -> bool:
        return self._edit_custom(
            schedule_id,
            revision,
            "add_section",
            scope="editor",
            title=title,
            before_id=before_id or None,
        )

    @Slot(str, int, str, str, str, str, result=bool)
    def addCustomSectionStyled(
        self,
        schedule_id: str,
        revision: int,
        title: str,
        icon: str = "default",
        color: str = "",
        before_id: str = "",
    ) -> bool:
        return self._edit_custom(
            schedule_id,
            revision,
            "add_section",
            scope="editor",
            title=title,
            icon=icon or "default",
            color=color or None,
            before_id=before_id or None,
        )

    @Slot(str, int, str, str, result=bool)
    def renameCustomSection(
        self, schedule_id: str, revision: int, section_id: str, title: str
    ) -> bool:
        return self._edit_custom(
            schedule_id,
            revision,
            "rename_section",
            scope=f"section:{section_id}",
            section_id=section_id,
            title=title,
        )

    @Slot(str, int, str, str, str, result=bool)
    def saveCustomSectionAppearance(
        self,
        schedule_id: str,
        revision: int,
        section_id: str,
        icon: str,
        color: str,
    ) -> bool:
        """Persist icon and color choices atomically on explicit Save."""
        return self._edit_custom(
            schedule_id,
            revision,
            "set_section_appearance",
            scope=f"section:{section_id}",
            section_id=section_id,
            icon=icon or "default",
            color=(color or "").strip().lower() or None,
        )

    @Slot(str, int, "QVariant", result=bool)
    def saveCustomScheduleDraft(
        self,
        schedule_id: str,
        revision: int,
        draft_entries,
    ) -> bool:
        """Commit an entire edited custom-schedule draft atomically."""
        entries_list = draft_entries.toVariant() if hasattr(draft_entries, "toVariant") else draft_entries
        if not isinstance(entries_list, list):
            return False
        return self._apply_command(
            self.operations.replace_custom_schedule_entries,
            schedule_id,
            int(revision),
            entries_list,
            scope="editor",
        )

    @Slot(str, int, str, str, str, result=bool)
    def moveCustomPart(
        self,
        schedule_id: str,
        revision: int,
        part_id: str,
        section_id: str = "",
        before_id: str = "",
    ) -> bool:
        return self._edit_custom(
            schedule_id,
            revision,
            "move_part",
            scope=f"part:{part_id}",
            part_id=part_id,
            section_id=section_id or None,
            before_id=before_id or None,
        )

    @Slot(str, int, str, str, result=bool)
    def moveCustomSection(
        self,
        schedule_id: str,
        revision: int,
        section_id: str,
        before_id: str = "",
    ) -> bool:
        return self._edit_custom(
            schedule_id,
            revision,
            "move_section",
            scope=f"section:{section_id}",
            section_id=section_id,
            before_id=before_id or None,
        )

    @Slot(str, int, str, result=bool)
    def deleteCustomPart(self, schedule_id: str, revision: int, part_id: str) -> bool:
        return self._edit_custom(
            schedule_id,
            revision,
            "delete_part",
            scope=f"part:{part_id}",
            notify=True,
            part_id=part_id,
        )

    @Slot(str, int, str, result=bool)
    def deleteCustomSection(self, schedule_id: str, revision: int, section_id: str) -> bool:
        return self._edit_custom(
            schedule_id,
            revision,
            "delete_section",
            scope=f"section:{section_id}",
            notify=True,
            section_id=section_id,
        )

    @Slot(str, int, "QVariant", result=bool)
    def saveCustomTimingSettings(
        self,
        schedule_id: str,
        revision: int,
        payload,
    ) -> bool:
        """Persist window, adjustable weights, and breaks atomically on one Save."""
        data = payload.toVariant() if hasattr(payload, "toVariant") else payload
        if not isinstance(data, dict):
            return False

        start_raw = str(data.get("startTime") or "").strip()
        end_raw = str(data.get("endTime") or "").strip()
        start_text: str | None = None
        end_text: str | None = None
        if start_raw or end_raw:
            start_hm = _parse_hhmm(start_raw)
            end_hm = _parse_hhmm(end_raw)
            if start_hm is None or end_hm is None:
                self._set_feedback(
                    code="invalid",
                    message=self.tr("Enter valid start and end times (HH:MM)."),
                    scope="window",
                )
                return False
            start_text = f"{start_hm[0]:02d}:{start_hm[1]:02d}"
            end_text = f"{end_hm[0]:02d}:{end_hm[1]:02d}"
            if start_text == end_text:
                self._set_feedback(
                    code="invalid",
                    message=self.tr("End time must be different from start time."),
                    scope="window",
                )
                return False

        has_untracked = "untrackedSeconds" in data
        ok_untracked, untracked = (
            _parse_optional_nonnegative_int(data.get("untrackedSeconds"))
            if has_untracked
            else (True, None)
        )
        has_closing = "closingSeconds" in data
        ok_closing, closing = (
            _parse_optional_nonnegative_int(data.get("closingSeconds"))
            if has_closing
            else (True, None)
        )
        untracked_arg: int | None = untracked if ok_untracked else -1
        closing_arg: int | None = closing if ok_closing else -1

        weights_map: dict[str, int] = {}
        raw_weights = data.get("weights")
        if raw_weights is not None:
            if not isinstance(raw_weights, dict):
                return False
            for part_id, raw_w in raw_weights.items():
                if isinstance(raw_w, bool) or not isinstance(raw_w, (int, float)):
                    return False
                if isinstance(raw_w, float) and not raw_w.is_integer():
                    return False
                weights_map[str(part_id)] = int(raw_w)

        before_map: dict[str, int | None] = {}
        raw_before = data.get("beforeParts")
        if raw_before is not None:
            if not isinstance(raw_before, dict):
                return False
            for part_id, raw_b in raw_before.items():
                ok_b, parsed_b = _parse_optional_nonnegative_int(raw_b)
                before_map[str(part_id)] = parsed_b if ok_b else -1

        return self._apply_command(
            self.operations.replace_custom_timing_settings,
            schedule_id,
            int(revision),
            start_time=start_text,
            end_time=end_text,
            untracked_seconds=untracked_arg,
            update_untracked=has_untracked,
            weights=weights_map if raw_weights is not None else None,
            before_parts=before_map if raw_before is not None else None,
            closing_seconds=closing_arg,
            update_closing=has_closing,
            scope="window",
        )

    # ── Exposed: pacing / pontualidade ─────────────────────────────────────

    def _format_clock_hhmm(self, epoch: float) -> str:
        cfg = self._session.clock_config
        pattern = "HH:mm" if cfg.hour_format_24h else ("h:mm a" if cfg.show_ampm else "h:mm")
        return format_time_with_seconds(epoch, pattern)

    def _compute_pacing_model(self) -> dict[str, object]:
        mode = self._session.pacing_mode
        if self._is_empty_custom_view():
            return {
                "mode": mode.value,
                "enabled": mode is not PacingMode.OFF,
                "automatic": mode is PacingMode.AUTOMATIC,
                "availability": "no_projection",
                "canConfigureMeetingWindow": False,
                "hasProjection": False,
                "status": "",
                "complete": False,
                "deviationSeconds": 0,
                "deviationMinutes": 0,
                "projectedEndLabel": "",
                "adjustedEndLabel": "",
                "targetEndLabel": "",
                "startLabel": "",
                "hasAdjustmentDifference": False,
            }
        state = self.operations.pacing_state()
        schedule = self._session.schedule
        projection = state.projection
        can_configure_meeting = (
            isinstance(schedule, MeetingSchedule)
            and self._session.week_monday >= current_monday()
            and not schedule.has_unreadable_record
            and not schedule_has_timing_data(schedule)
        )
        if projection is None:
            return {
                "mode": state.mode.value,
                "enabled": state.mode is not PacingMode.OFF,
                "automatic": state.mode is PacingMode.AUTOMATIC,
                "availability": state.availability.value,
                "canConfigureMeetingWindow": can_configure_meeting,
                "hasProjection": False,
                "status": "",
                "complete": False,
                "deviationSeconds": 0,
                "deviationMinutes": 0,
                "projectedEndLabel": "",
                "adjustedEndLabel": "",
                "targetEndLabel": "",
                "startLabel": "",
                "hasAdjustmentDifference": False,
            }
        deviation_sec = int(round(projection.deviation_seconds))
        deviation_min = max(1, int(round(abs(deviation_sec) / 60.0))) if abs(deviation_sec) > projection.tolerance_seconds else 0
        return {
            "mode": state.mode.value,
            "enabled": state.mode is not PacingMode.OFF,
            "automatic": state.mode is PacingMode.AUTOMATIC,
            "availability": state.availability.value,
            "canConfigureMeetingWindow": can_configure_meeting,
            "hasProjection": True,
            "status": projection.status.value,
            "complete": projection.complete,
            "deviationSeconds": deviation_sec,
            "deviationMinutes": deviation_min,
            "projectedEndLabel": self._format_clock_hhmm(projection.projected_end_epoch),
            "adjustedEndLabel": self._format_clock_hhmm(projection.adjusted_end_epoch),
            "targetEndLabel": self._format_clock_hhmm(projection.end_epoch),
            "startLabel": self._format_clock_hhmm(projection.start_epoch),
            "hasAdjustmentDifference": (
                abs(projection.adjusted_end_epoch - projection.projected_end_epoch) >= 30
            ),
        }

    def _emit_pacing_if_changed(self, *, force: bool) -> None:
        payload = self._compute_pacing_model()
        if force or payload != self._last_pacing_payload:
            self._last_pacing_payload = payload
            self.pacingChanged.emit()

    @Property(str, notify=pacingChanged)
    def pacingMode(self) -> str:
        return self._session.pacing_mode.value

    def _pacing_modes_model(self) -> list[str]:
        return [mode.value for mode in _PACING_MODES]

    pacingModes = Property(_QVARIANT, _pacing_modes_model, constant=True)

    def _pacing_state_model(self) -> dict[str, object]:
        return dict(self._last_pacing_payload)

    pacingState = Property(_QVARIANT, _pacing_state_model, notify=pacingChanged)

    @Slot(str, result=bool)
    def setPacingMode(self, value: str) -> bool:
        try:
            mode = PacingMode(value)
        except ValueError:
            return False
        return self._apply_command(
            self.operations.set_pacing_mode, mode, scope="settings", feedback_key="pacing_mode"
        )

    def _apply_command(self, command, *args, **kwargs) -> bool:
        success, _ = self._run_command(command, *args, **kwargs)
        return success

    def _run_command[T](
        self,
        command: Callable[..., T],
        *args,
        scope: str = "schedule",
        feedback_key: str | None = None,
        notify: bool = False,
        **kwargs,
    ) -> tuple[bool, T | None]:
        # A successful change to another setting must not dismiss an unresolved save error.
        if (
            feedback_key is None
            or self._feedback.get("scope") != scope
            or self._feedback_key == feedback_key
        ):
            self.clearFeedback()
        try:
            return True, command(*args, **kwargs)
        except TimerDurationBudgetError as error:
            log.warning("Timer command rejected (duration budget): %s", error)
            self._set_feedback(
                code=error.code,
                message=self._localize_operation_error(error),
                scope=scope,
                available_seconds=error.available_seconds,
                requested_seconds=error.requested_seconds,
                feedback_key=feedback_key,
                notify=notify,
            )
        except TimerOperationError as error:
            log.warning("Timer command rejected: %s", error)
            self._set_feedback(
                code=error.code,
                message=self._localize_operation_error(error),
                scope=scope,
                retryable=error.code == "schedule_stale",
                feedback_key=feedback_key,
                notify=notify,
            )
        except (OSError, RuntimeError):
            log.warning("Could not apply timer command", exc_info=True)
            self._set_feedback(
                code="failed",
                message=self.tr("Could not save changes. Try again."),
                scope=scope,
                feedback_key=feedback_key,
                notify=notify,
            )
        return False, None

    # ── Exposed: clock config ──────────────────────────────────────────────

    def _clock_model(self) -> dict:
        return self._session.clock_config.to_dict()

    clockConfig = Property(_QVARIANT, _clock_model, notify=clockConfigChanged)

    def _clock_modes_model(self) -> list[str]:
        return [mode.value for mode in CLOCK_MODE_OPTIONS]

    clockModes = Property(
        _QVARIANT,
        _clock_modes_model,
        notify=clockConfigChanged,
    )

    def _part_timer_displays_model(self) -> list[str]:
        return [display.value for display in PART_TIMER_DISPLAY_OPTIONS]

    partTimerDisplays = Property(
        _QVARIANT,
        _part_timer_displays_model,
        notify=clockConfigChanged,
    )

    def _analog_clock_styles_model(self) -> list[str]:
        return [style.value for style in ANALOG_CLOCK_STYLE_OPTIONS]

    analogClockStyles = Property(
        _QVARIANT,
        _analog_clock_styles_model,
        notify=clockConfigChanged,
    )

    @Slot(str, "QVariant")
    def updateClock(self, key: str, value) -> None:
        self._apply_command(self.operations.update_clock, key, value, scope="settings", feedback_key=key)

    # ── Exposed: monitors ──────────────────────────────────────────────────

    def _monitors_model(self) -> list:
        return self._monitors.model()

    monitors = Property(_QVARIANT, _monitors_model, notify=monitorsChanged)

    def _timer_visible(self) -> bool:
        return self._monitors.is_visible()

    timerVisible = Property(bool, _timer_visible, notify=monitorsChanged)

    @Slot()
    def refreshMonitors(self) -> None:
        self.monitorsChanged.emit()

    @Slot(int, result="QVariant")
    def requestReserve(self, index: int):
        """Reserve a monitor for the timer. Returns a conflict dict (for the QML
        confirm dialog) only when media is genuinely projecting there; otherwise
        reserves immediately and returns an empty dict."""
        result = self._monitors.request_reserve(
            index,
            self.tr("this monitor"),
        )
        if not result:
            self.monitorsChanged.emit()
        return result

    @Slot(int)
    def confirmReserve(self, index: int) -> None:
        if self._monitors.confirm_reserve(index):
            self.monitorsChanged.emit()

    @Slot(int)
    def unreserveMonitor(self, index: int) -> None:
        """Cancel a timer reservation. This *only* frees the monitor — it is not
        responsible for showing media (the media window's own monitor menu is).
        The screen is left empty; the operator shows media there from that menu
        ("Show") if they want it."""
        if self._monitors.unreserve(index):
            self.monitorsChanged.emit()

    @Slot(bool)
    def setTimerVisible(self, visible: bool) -> None:
        self._monitors.set_visible(bool(visible))
        self.monitorsChanged.emit()

    # ── Exposed: media countdown mode ──────────────────────────────────────

    def _media_countdown_model(self) -> dict:
        snapshot = self._media_countdown_automation.snapshot()
        occurrence = snapshot.next_occurrence
        suggestion_hour, suggestion_minute, suggestion_is_meeting = (
            self._media_countdown_automation.suggested_manual_target()
        )
        weekday_names = meeting_weekday_names()
        kind_labels = {
            "midweek": meeting_kind_label("midweek"),
            "weekend": meeting_kind_label("weekend"),
        }
        settings = snapshot.settings
        automation = settings.automation
        presentation = settings.presentation
        try:
            presentation_index = MEDIA_COUNTDOWN_PRESENTATION_OPTIONS.index(presentation)
        except ValueError:
            presentation_index = 0
        configured_count = sum(slot.is_configured for slot in snapshot.schedule.slots)
        if occurrence is None:
            next_model = {}
        else:
            trigger_at = occurrence.starts_at.timestamp() - automation.lead_seconds
            trigger = datetime.fromtimestamp(trigger_at).astimezone()
            next_model = {
                "slotId": occurrence.slot_id,
                "kind": occurrence.slot.kind,
                "label": kind_labels.get(occurrence.slot.kind, occurrence.slot.kind),
                "weekdayLabel": weekday_names[occurrence.starts_at.weekday()],
                "startTime": occurrence.starts_at.strftime("%H:%M"),
                "triggerTime": trigger.strftime("%H:%M:%S"),
            }
        return {
            "presentationIndex": presentation_index,
            "activeProjection": {
                "active": snapshot.active_countdown,
                "origin": snapshot.active_origin,
                "targetTime": (
                    snapshot.active_target.time().toString("HH:mm")
                    if snapshot.active_target is not None
                    else ""
                ),
            },
            "manualSuggestion": {
                "hour": suggestion_hour,
                "minute": suggestion_minute,
                "fromMeeting": suggestion_is_meeting,
            },
            "automation": {
                "enabled": automation.enabled,
                "leadSeconds": automation.lead_seconds,
                "status": snapshot.status.value,
                "activeAutomatic": snapshot.active_automatic,
                "blockingReason": snapshot.blocking_reason.value,
            },
            "schedule": {
                "configuredCount": configured_count,
                "next": next_model,
            },
        }

    mediaCountdown = Property(
        _QVARIANT,
        _media_countdown_model,
        notify=mediaCountdownChanged,
    )

    @staticmethod
    def _media_countdown_presentation(index: int) -> str:
        if 0 <= index < len(MEDIA_COUNTDOWN_PRESENTATION_OPTIONS):
            return MEDIA_COUNTDOWN_PRESENTATION_OPTIONS[index].value
        return MEDIA_COUNTDOWN_PRESENTATION_OPTIONS[0].value

    @Slot(bool)
    def setAutomaticCountdownEnabled(self, enabled: bool) -> None:
        self._media_countdown_automation.set_enabled(enabled)

    @Slot(int)
    def setAutomaticCountdownLeadSeconds(self, seconds: int) -> None:
        self._media_countdown_automation.set_lead_seconds(seconds)

    @Slot(int)
    def setMediaCountdownPresentation(self, presentation_index: int) -> None:
        self._media_countdown_automation.set_presentation(
            self._media_countdown_presentation(int(presentation_index))
        )

    @Slot()
    def refreshMediaCountdownPage(self) -> None:
        self._media_countdown_automation.refresh()
        self.mediaCountdownChanged.emit()

    @Slot()
    def configureMeetingSchedule(self) -> None:
        self.meetingScheduleConfigurationRequested.emit()

    def _selected_media_countdown_presentation(self) -> str:
        return self._media_countdown_automation.snapshot().settings.presentation.value

    @Slot(int, int)
    def startCountdownToTime(self, hour: int, minute: int) -> None:
        from PySide6.QtCore import QDate, QTime
        selected = QTime(int(hour), int(minute), 0)
        now = QTime.currentTime()
        if selected <= now:
            target = QDateTime(QDate.currentDate().addDays(1), selected)
        else:
            target = QDateTime.currentDateTime()
            target.setTime(selected)
        self.mediaCountdownRequested.emit(
            target,
            self._selected_media_countdown_presentation(),
        )

    @Slot(int)
    def startCountdownDuration(self, seconds: int) -> None:
        duration = max(
            _MIN_MANUAL_COUNTDOWN_SECONDS,
            min(_MAX_MANUAL_COUNTDOWN_SECONDS, int(seconds)),
        )
        target = QDateTime.currentDateTime().addSecs(duration)
        self.mediaCountdownRequested.emit(
            target,
            self._selected_media_countdown_presentation(),
        )

    # ── Exposed: QML cursor bridge ─────────────────────────────────────────

    @Slot()
    def pointerEnter(self) -> None:  # noqa: N802
        self.pointerEntered.emit()

    @Slot()
    def pointerExit(self) -> None:  # noqa: N802
        self.pointerExited.emit()
