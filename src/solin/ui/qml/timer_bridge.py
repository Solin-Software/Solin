"""QML adapter for the profile timer and its projection controls."""

from __future__ import annotations

from datetime import date, datetime
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QDateTime, QObject, Property, Signal, Slot

from solin.controllers.timer_operations import TimerOperations
from solin.core.i18n.date import week_label
from solin.core.i18n.meeting_schedule import (
    meeting_kind_label,
    meeting_weekday_names,
)
from solin.core.i18n.timer_schedule_view import parts_model_for_schedule
from solin.core.jw.language_context import jw_media_language_context
from solin.core.meetings.meeting_weeks import current_monday
from solin.core.timer.application import (
    DEFAULT_ASSIGNMENT_AUTOFILL_SCOPE,
    AssignmentAutofillScope,
    DetectedPartTiming,
)
from solin.core.timer.custom_schedule import TimerOperationError
from solin.core.timer.models import (
    ANALOG_CLOCK_STYLE_OPTIONS,
    CLOCK_MODE_OPTIONS,
    MEDIA_COUNTDOWN_PRESENTATION_OPTIONS,
    PART_TIMER_DISPLAY_OPTIONS,
    MeetingType,
    PartKind,
    Section,
)
from solin.controllers.timer_monitor_controller import TimerMonitorController
from solin.controllers.timer_pdf_export_controller import TimerPdfExportController

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


class TimerBridge(QObject):
    """Context object bound to the QML timer tab as ``timer``."""

    scheduleChanged = Signal()
    liveStateChanged = Signal()
    clockConfigChanged = Signal()
    monitorsChanged = Signal()
    manualHandChanged = Signal()
    weekChanged = Signal()
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
        self._timer_page_visible = False
        self._assignments_mode_active = False
        self._assignment_lookup_deferred = False
        self._assignment_context = (self._session.week_monday, self._session.meeting_type)
        self._assignment_target: tuple[date, str] | None = None
        self._assignment_generation = 0
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
        operations.clock_changed.connect(self.clockConfigChanged)
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
        context = (self._session.week_monday, self._session.meeting_type)
        if context != self._assignment_context:
            self._assignment_lookup_deferred = False
            self._assignment_context = context
        self.weekChanged.emit()
        self.weekShift.emit(direction)

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
        self.clockConfigChanged.emit()
        self.mediaCountdownChanged.emit()

    def refresh_theme(self) -> None:
        self.scheduleChanged.emit()
        self.clockConfigChanged.emit()
        self.monitorsChanged.emit()

    def _on_engine_state(self, snapshot: dict) -> None:
        self._live = snapshot
        self._refresh_assignment_lookup()
        self.scheduleChanged.emit()
        self.liveStateChanged.emit()

    def _on_engine_tick(self, snapshot: dict) -> None:
        self._live = snapshot
        self.liveStateChanged.emit()

    # ── Exposed: schedule model ────────────────────────────────────────────

    def _parts_model(self) -> list:
        return parts_model_for_schedule(self._session.schedule, self.operations.time_format)

    parts = Property(_QVARIANT, _parts_model, notify=scheduleChanged)

    def _live_model(self) -> dict:
        return self._live

    liveState = Property(_QVARIANT, _live_model, notify=liveStateChanged)

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
        self._apply_command(self.operations.navigate_week, "previous")

    @Slot()
    def nextWeek(self) -> None:
        self._apply_command(self.operations.navigate_week, "next")

    @Slot()
    def goToCurrentWeek(self) -> None:
        self._apply_command(self.operations.navigate_week, "current")

    @Slot(str)
    def setMeetingType(self, value: str) -> None:
        try:
            mt = MeetingType(value)
        except ValueError:
            return
        self._apply_command(self.operations.set_meeting_type, mt)

    @Slot()
    def exportSchedulePdf(self) -> None:
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

    # ── Exposed: part editing ──────────────────────────────────────────────

    @Slot(str)
    def startPart(self, part_id: str) -> None:
        self._apply_command(self.operations.start_part, part_id)

    @Slot(str)
    def stopPart(self, part_id: str) -> None:
        self._apply_command(self.operations.stop_part, part_id)

    @Slot(str)
    def resetPart(self, part_id: str) -> None:
        self._apply_command(self.operations.reset_part, part_id)

    @Slot(str, int)
    def adjustPart(self, part_id: str, delta_seconds: int) -> None:
        self._apply_command(self.operations.adjust_part, part_id, delta_seconds)

    @Slot(str, int)
    def setPartSeconds(self, part_id: str, seconds: int) -> None:
        self._apply_command(self.operations.set_part_seconds, part_id, seconds)

    @Slot(str, int)
    def setSectionCount(self, section: str, count: int) -> None:
        try:
            sec = Section(section)
        except ValueError:
            return
        self._apply_command(self.operations.set_section_count, sec, count)

    def _apply_command(self, command, *args) -> None:
        try:
            command(*args)
        except TimerOperationError as error:
            log.warning("Timer command rejected: %s", error)
        except (OSError, RuntimeError):
            log.warning("Could not apply timer command", exc_info=True)

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
        self._apply_command(self.operations.update_clock, key, value)

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
