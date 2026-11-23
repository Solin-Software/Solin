"""
timer_bridge.py — Solin
=======================
The QObject that exposes the timer domain to the QML timer tab and receives the
operator's actions. It is the single seam between the pure domain
(:mod:`solin.core.timer`) and the UI — and the natural place a *future* local
network adapter would hook in, since every action funnels through here and every
view is built from serializable domain objects.

It presents a ``TimerSession`` and delegates scheduling, persistence, monitor
reservation, PDF export, and fullscreen output to explicitly injected adapters.
"""

from __future__ import annotations

from datetime import date, datetime
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QDateTime, QObject, Property, Signal, Slot

from solin.core.i18n.date import format_time_with_seconds, week_label
from solin.core.i18n.meeting_schedule import (
    meeting_kind_label,
    meeting_weekday_names,
)
from solin.core.jw.language_context import jw_media_language_context
from solin.core.meetings.meeting_weeks import current_monday
from solin.core.meetings.section_meta import SECTION_META
from solin.core.timer.application import (
    DEFAULT_ASSIGNMENT_AUTOFILL_SCOPE,
    AssignmentAutofillScope,
    DetectedPartTiming,
    TimerSession,
)
from solin.core.timer.models import (
    ANALOG_CLOCK_STYLE_OPTIONS,
    CONFIGURABLE_PART_SECTIONS,
    CLOCK_MODE_OPTIONS,
    MEDIA_COUNTDOWN_PRESENTATION_OPTIONS,
    PART_TIMER_DISPLAY_OPTIONS,
    ClockConfig,
    MeetingType,
    PartKind,
    PartState,
    Section,
)
from solin.core.timer.schedule_factory import (
    configurable_count_for,
)
from solin.core.i18n.timer_part_titles import display_part_title
from solin.core.meetings.colors import section_colors
from solin.core.timer.part_titles import is_indexed_part_title_source
from solin.controllers.timer_monitor_controller import TimerMonitorController
from solin.controllers.timer_pdf_export_controller import TimerPdfExportController
from solin.styles.theme import current_theme_scheme

if TYPE_CHECKING:
    from solin.controllers.timer_hand_alert_controller import TimerHandAlertController
    from solin.controllers.timer_workbook_lookup import TimerWorkbookLookup
    from solin.core.meetings.workbook import Workbook

    _QVARIANT = object
else:
    _QVARIANT = "QVariant"


# Stable key per section — used by the QML to translate the header (via the
# shared ``_Section`` translation context) and to pick its colour + SVG icon.
_SECTION_KEYS = {
    Section.OPENING_COMMENTS: "opening_comments",
    Section.TREASURES: "treasures",
    Section.MINISTRY: "ministry",
    Section.LIVING: "living",
    Section.CONCLUDING_COMMENTS: "concluding_comments",
    Section.PUBLIC_TALK: "public_talk",
    Section.WATCHTOWER: "watchtower",
}

_SECTION_CODES = {
    Section.OPENING_COMMENTS: "opening_comments",
    Section.TREASURES: "tgw",
    Section.MINISTRY: "ayfm",
    Section.LIVING: "lac",
    Section.CONCLUDING_COMMENTS: "concluding_comments",
    Section.PUBLIC_TALK: "public_talk",
    Section.WATCHTOWER: "wt",
}

_STANDALONE_SECTIONS = {
    Section.OPENING_COMMENTS,
    Section.CONCLUDING_COMMENTS,
}

log = logging.getLogger(__name__)

_MIN_MANUAL_COUNTDOWN_SECONDS = 10
_MAX_MANUAL_COUNTDOWN_SECONDS = 24 * 60 * 60 - 1


def section_palette(section: Section) -> dict[str, str]:
    code = _SECTION_CODES.get(section, "")
    hue = SECTION_META.get(code, ("", 215))[1]
    colors = section_colors(hue, current_theme_scheme())
    return {
        "accent": colors["accent"],
        "text": colors["text"],
        "badge": colors["badge"],
        "border": colors["border"],
    }


def _fmt_mmss(seconds: float) -> str:
    total = int(round(abs(seconds)))
    sign = "-" if seconds < 0 else ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{sign}{h}:{m:02d}:{s:02d}"
    return f"{sign}{m:02d}:{s:02d}"


def _fmt_total(seconds: float) -> str:
    """A compact section-total label: '30 min' on whole minutes, else 'MM:SS'."""
    s = int(round(seconds))
    if s % 60 == 0:
        return f"{s // 60} min"
    return _fmt_mmss(s)


def _started_label(epoch: float | None, time_format: str = "HH:mm:ss") -> str:
    if epoch is None:
        return ""
    return format_time_with_seconds(epoch, time_format)


def parts_model_for_schedule(schedule, time_format: str = "HH:mm:ss") -> list[dict]:
    rows = []
    seen_sections: set[str] = set()
    section_positions: dict[Section, int] = {}
    for number, p in enumerate(schedule.parts, start=1):
        section = p.section
        section_position = section_positions.get(section, 0) + 1
        section_positions[section] = section_position
        first = section.value not in seen_sections
        seen_sections.add(section.value)
        palette = section_palette(section)
        indexed_number: int | None = None
        if is_indexed_part_title_source(p.title):
            indexed_number = section_position
        show_section_header = section not in _STANDALONE_SECTIONS
        rows.append({
            "id": p.id,
            "section": section.value,
            "sectionKey": _SECTION_KEYS.get(section, section.value),
            "sectionColor": palette["accent"],
            "sectionTextColor": palette["text"],
            "sectionBadgeBg": palette["badge"],
            "sectionBorderColor": palette["border"],
            "title": display_part_title(p, indexed_number=indexed_number),
            "displayNumber": number,
            "plannedSeconds": p.planned_seconds,
            "plannedLabel": _fmt_mmss(p.planned_seconds),
            "state": p.state.value,
            "firstOfSection": first,
            "showSectionHeader": show_section_header,
            "configurableCount": section in CONFIGURABLE_PART_SECTIONS,
            "sectionCount": configurable_count_for(schedule, section),
            "sectionTotalLabel": (
                _fmt_total(schedule.section_total_seconds(section))
                if show_section_header
                else ""
            ),
            "startedLabel": _started_label(p.first_started_epoch, time_format),
            "resultLabel": (
                _fmt_mmss(p.accumulated_seconds) if p.state is PartState.STOPPED else ""
            ),
        })
    return rows


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
        engine,
        session: TimerSession,
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
        self._engine = engine
        self._session = session
        self._output = output
        self._monitors = monitors
        self._hand_alert = hand_alert
        self._pdf_export = pdf_export
        self._media_countdown_automation = media_countdown_automation
        self._language_manager = language_manager
        self._clock_config: ClockConfig = session.clock_config
        self._schedule = session.schedule
        self._live: dict = {}
        self._workbook_lookup = workbook_lookup
        self._assignment_scope = assignment_scope
        self._timer_page_visible = False
        self._assignments_mode_active = False
        self._assignment_lookup_deferred = False
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

        self._engine.state_changed.connect(self._on_engine_state)
        self._engine.schedule_mutated.connect(self._record_live_schedule_change)
        self._engine.tick.connect(self._on_engine_tick)
        self._hand_alert.controls_changed.connect(self.manualHandChanged)
        self._media_countdown_automation.state_changed.connect(
            self.mediaCountdownChanged
        )

        self._engine.set_schedule(self._schedule)

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

    def _replace_schedule(self, schedule) -> None:
        if (
            schedule.week_monday != self._schedule.week_monday
            or schedule.meeting_type is not self._schedule.meeting_type
        ):
            self._assignment_lookup_deferred = False
        self._schedule = schedule
        self._engine.set_schedule(schedule)
        self._refresh_assignment_lookup()

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
            applied = self._session.apply_detected_assignments(
                current_week, timings, self._assignment_scope,
            )
        except (OSError, RuntimeError, ValueError):
            log.warning("Could not adopt detected timer assignments for %s", self._assignment_target,
                        exc_info=True)
            return
        if applied:
            self._replace_schedule(self._session.schedule)

    def refresh_language(self) -> None:
        self.scheduleChanged.emit()
        self.clockConfigChanged.emit()
        self.mediaCountdownChanged.emit()

    def refresh_theme(self) -> None:
        self.scheduleChanged.emit()
        self.clockConfigChanged.emit()
        self.monitorsChanged.emit()

    def _record_live_schedule_change(self) -> None:
        self._session.record_schedule_change()
        self._refresh_assignment_lookup()

    def _on_engine_state(self, snapshot: dict) -> None:
        self._live = snapshot
        self.scheduleChanged.emit()
        self.liveStateChanged.emit()

    def _on_engine_tick(self, snapshot: dict) -> None:
        self._live = snapshot
        self.liveStateChanged.emit()

    # ── Exposed: schedule model ────────────────────────────────────────────

    def _parts_model(self) -> list:
        time_format = getattr(
            self._language_manager,
            "time_with_seconds_format",
            "HH:mm:ss",
        )
        return parts_model_for_schedule(self._schedule, time_format)

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
        schedule = self._session.previous_week()
        self.weekChanged.emit()
        self.weekShift.emit(-1)
        self._replace_schedule(schedule)

    @Slot()
    def nextWeek(self) -> None:
        schedule = self._session.next_week()
        self.weekChanged.emit()
        self.weekShift.emit(+1)
        self._replace_schedule(schedule)

    @Slot()
    def goToCurrentWeek(self) -> None:
        direction = self._session.go_to_current_week(current_monday())
        self.weekChanged.emit()
        self.weekShift.emit(direction)
        self._replace_schedule(self._session.schedule)

    @Slot(str)
    def setMeetingType(self, value: str) -> None:
        try:
            mt = MeetingType(value)
        except ValueError:
            return
        if not self._session.set_meeting_type(mt):
            return
        self.weekChanged.emit()
        self.weekShift.emit(0)   # switch midweek/weekend with a pure fade
        self._replace_schedule(self._session.schedule)

    @Slot()
    def exportSchedulePdf(self) -> None:
        time_format = getattr(
            self._language_manager,
            "time_with_seconds_format",
            "HH:mm:ss",
        )
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
            self._schedule,
            week_label=self._week_label(),
            meeting_type_label=meeting_type_label,
            date_format=date_format,
            time_format=time_format,
        )

    # ── Exposed: part editing ──────────────────────────────────────────────

    @Slot(str)
    def startPart(self, part_id: str) -> None:
        self._engine.start_part(part_id)

    @Slot(str)
    def stopPart(self, part_id: str) -> None:
        self._engine.stop_part(part_id)

    @Slot(str)
    def resetPart(self, part_id: str) -> None:
        self._engine.reset_part(part_id)

    @Slot(str, int)
    def adjustPart(self, part_id: str, delta_seconds: int) -> None:
        part = self._schedule.part_by_id(part_id)
        if part is not None and part.state is PartState.RUNNING:
            self._engine.adjust_elapsed(part_id, int(delta_seconds))
        elif self._session.adjust_part(part_id, int(delta_seconds)):
            self._refresh_assignment_lookup()
            self.scheduleChanged.emit()

    @Slot(str, int)
    def setPartSeconds(self, part_id: str, seconds: int) -> None:
        if self._session.set_part_seconds(part_id, int(seconds)):
            self._refresh_assignment_lookup()
            self.scheduleChanged.emit()

    @Slot(str, int)
    def setSectionCount(self, section: str, count: int) -> None:
        try:
            sec = Section(section)
        except ValueError:
            return
        if self._session.set_section_count(sec, int(count)):
            self._replace_schedule(self._session.schedule)

    # ── Exposed: clock config ──────────────────────────────────────────────

    def _clock_model(self) -> dict:
        return self._clock_config.to_dict()

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
        config = self._session.update_clock(key, value)
        if config is None:
            return
        self._clock_config = config
        self._output.set_clock_config(self._clock_config)
        self.clockConfigChanged.emit()

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
