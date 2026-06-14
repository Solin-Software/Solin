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

from typing import TYPE_CHECKING

from PySide6.QtCore import QDateTime, QObject, Property, Signal, Slot

from ..core.i18n.date import format_time_with_seconds, week_label
from ..core.meetings.publications import current_monday
from ..core.meetings.section_meta import SECTION_META
from ..core.timer.application import TimerSession
from ..core.timer.models import (
    ANALOG_CLOCK_STYLE_OPTIONS,
    CLOCK_MODE_OPTIONS,
    PART_TIMER_DISPLAY_OPTIONS,
    ClockConfig,
    MeetingType,
    PartState,
    Section,
)
from ..core.timer.schedule_factory import (
    configurable_count_for,
)
from ..core.i18n.timer_part_titles import display_part_title
from ..core.meetings.colors import section_colors
from ..core.timer.part_titles import is_indexed_part_title_source
from ..controllers.timer_monitor_controller import TimerMonitorController
from ..controllers.timer_pdf_export_controller import TimerPdfExportController

if TYPE_CHECKING:
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

# Sections whose part count the operator may configure. Treasures is always its
# 3 fixed parts, and the weekend (public talk + Watchtower study) is fixed too —
# only the midweek Ministry and Living sections vary in number of parts.
_CONFIGURABLE_COUNT = {Section.MINISTRY, Section.LIVING}


def _section_palette(section: Section) -> dict[str, str]:
    code = _SECTION_CODES.get(section, "")
    hue = SECTION_META.get(code, ("", 215))[1]
    colors = section_colors(hue)
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


def _parts_model_for_schedule(schedule, time_format: str = "HH:mm:ss") -> list[dict]:
    rows = []
    seen_sections: set[str] = set()
    section_positions: dict[Section, int] = {}
    for number, p in enumerate(schedule.parts, start=1):
        section = p.section
        section_position = section_positions.get(section, 0) + 1
        section_positions[section] = section_position
        first = section.value not in seen_sections
        seen_sections.add(section.value)
        palette = _section_palette(section)
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
            "configurableCount": section in _CONFIGURABLE_COUNT,
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
    weekChanged = Signal()
    # Direction hint for the QML parts-list transition: -1 = earlier (slide in
    # from the left), +1 = later (from the right), 0 = no direction (fade only).
    weekShift = Signal(int)

    # Relayed to TimerWidget.project_timer_signal for the media-countdown mode.
    mediaCountdownRequested = Signal(QDateTime)

    def __init__(
        self,
        *,
        engine,
        session: TimerSession,
        output,
        monitors: TimerMonitorController,
        pdf_export: TimerPdfExportController,
        language_manager=None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._session = session
        self._output = output
        self._monitors = monitors
        self._pdf_export = pdf_export
        self._language_manager = language_manager
        self._clock_config: ClockConfig = session.clock_config
        self._schedule = session.schedule
        self._live: dict = {}

        self._engine.state_changed.connect(self._on_engine_state)
        self._engine.tick.connect(self._on_engine_tick)

        self._engine.set_schedule(self._schedule)

    # ── Schedule lifecycle ─────────────────────────────────────────────────

    def _replace_schedule(self, schedule) -> None:
        self._schedule = schedule
        self._engine.set_schedule(schedule)
        self.scheduleChanged.emit()
        self.liveStateChanged.emit()

    def refresh_language(self) -> None:
        self.scheduleChanged.emit()
        self.clockConfigChanged.emit()

    def _on_engine_state(self, snapshot: dict) -> None:
        # Engine transitions mutate run-state in place → persist so a meeting in
        # progress survives a restart, then refresh the views.
        self._live = snapshot
        if self._schedule is not None:
            self._session.persist_live_state()
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
        return _parts_model_for_schedule(self._schedule, time_format)

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
        if self._session.adjust_part(part_id, int(delta_seconds)):
            self.scheduleChanged.emit()

    @Slot(str, int)
    def setPartSeconds(self, part_id: str, seconds: int) -> None:
        if self._session.set_part_seconds(part_id, int(seconds)):
            self.scheduleChanged.emit()

    @Slot(str, int)
    def setSectionCount(self, section: str, count: int) -> None:
        try:
            sec = Section(section)
        except ValueError:
            return
        if self._session.set_section_count(sec, int(count)):
            self._engine.set_schedule(self._schedule)
            self.scheduleChanged.emit()

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
        self.mediaCountdownRequested.emit(target)

    @Slot(int)
    def startCountdownMinutes(self, minutes: int) -> None:
        target = QDateTime.currentDateTime().addSecs(int(minutes) * 60)
        self.mediaCountdownRequested.emit(target)
