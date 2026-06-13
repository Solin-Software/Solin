"""
timer_bridge.py — Solin
=======================
The QObject that exposes the timer domain to the QML timer tab and receives the
operator's actions. It is the single seam between the pure domain
(:mod:`app.core.timer`) and the UI — and the natural place a *future* local
network adapter would hook in, since every action funnels through here and every
view is built from serializable domain objects.

It owns the currently-selected ``MeetingSchedule`` (shared by reference with the
``TimerEngine``), persists edits per profile via ``TimerStore``, and brokers
monitor reservation against the shared ``MonitorAllocationStore`` (delegating the
actual fullscreen windows to the media and timer-output controllers).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import QDateTime, QObject, Property, Signal, Slot, QStandardPaths
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..core.i18n.date import format_time_with_seconds, week_label
from ..core.meetings.publications import current_monday
from ..core.meetings.section_meta import SECTION_META
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
    adjust_part,
    build_default_schedule,
    configurable_count_for,
    normalize_schedule,
    redistribute_section,
    set_section_part_count,
)
from ..core.i18n.timer_part_titles import display_part_title
from ..core.meetings.colors import section_colors
from ..core.timer.part_titles import is_indexed_part_title_source
from ..core.ui.monitor_allocation import OWNER_MEDIA, OWNER_OFF, OWNER_TIMER

log = logging.getLogger(__name__)


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

    def __init__(self, window, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._engine = window._timer_engine
        self._store = window._timer_store
        self._output = window._timer_output
        self._allocation = window._monitor_allocation

        self._week_monday: date = current_monday()
        self._meeting_type: MeetingType = self._store.load_last_meeting_type()
        self._clock_config: ClockConfig = self._output.clock_config
        self._live: dict = {}

        self._engine.state_changed.connect(self._on_engine_state)
        self._engine.tick.connect(self._on_engine_tick)

        self._load_schedule()

    # ── Schedule lifecycle ─────────────────────────────────────────────────

    def _load_schedule(self) -> None:
        sch = self._store.load_schedule(self._week_monday, self._meeting_type)
        if sch is None:
            sch = build_default_schedule(self._week_monday, self._meeting_type)
        elif normalize_schedule(sch):
            self._store.save_schedule(sch)
        self._schedule = sch
        self._engine.set_schedule(sch)
        self.scheduleChanged.emit()
        self.liveStateChanged.emit()

    def _persist(self) -> None:
        self._store.save_schedule(self._schedule)
        self.scheduleChanged.emit()

    def refresh_language(self) -> None:
        self.scheduleChanged.emit()
        self.clockConfigChanged.emit()

    def _on_engine_state(self, snapshot: dict) -> None:
        # Engine transitions mutate run-state in place → persist so a meeting in
        # progress survives a restart, then refresh the views.
        self._live = snapshot
        if self._schedule is not None:
            self._store.save_schedule(self._schedule)
        self.scheduleChanged.emit()
        self.liveStateChanged.emit()

    def _on_engine_tick(self, snapshot: dict) -> None:
        self._live = snapshot
        self.liveStateChanged.emit()

    # ── Exposed: schedule model ────────────────────────────────────────────

    def _parts_model(self) -> list:
        lang = getattr(self._window, "lang", None)
        time_format = getattr(lang, "time_with_seconds_format", "HH:mm:ss")
        return _parts_model_for_schedule(self._schedule, time_format)

    parts = Property("QVariant", _parts_model, notify=scheduleChanged)

    def _live_model(self) -> dict:
        return self._live

    liveState = Property("QVariant", _live_model, notify=liveStateChanged)

    # ── Exposed: week / meeting type ───────────────────────────────────────

    def _week_label(self) -> str:
        return week_label(self._week_monday)

    weekLabel = Property(str, _week_label, notify=weekChanged)

    def _meeting_type_value(self) -> str:
        return self._meeting_type.value

    meetingType = Property(str, _meeting_type_value, notify=weekChanged)

    def _is_current_week(self) -> bool:
        return self._week_monday == current_monday()

    isCurrentWeek = Property(bool, _is_current_week, notify=weekChanged)

    @Slot()
    def previousWeek(self) -> None:
        self._week_monday -= timedelta(days=7)
        self.weekChanged.emit()
        self.weekShift.emit(-1)
        self._load_schedule()

    @Slot()
    def nextWeek(self) -> None:
        self._week_monday += timedelta(days=7)
        self.weekChanged.emit()
        self.weekShift.emit(+1)
        self._load_schedule()

    @Slot()
    def goToCurrentWeek(self) -> None:
        target = current_monday()
        direction = (target > self._week_monday) - (target < self._week_monday)
        self._week_monday = target
        self.weekChanged.emit()
        self.weekShift.emit(int(direction))
        self._load_schedule()

    @Slot(str)
    def setMeetingType(self, value: str) -> None:
        try:
            mt = MeetingType(value)
        except ValueError:
            return
        if mt is self._meeting_type:
            return
        self._meeting_type = mt
        self._store.save_last_meeting_type(mt)
        self.weekChanged.emit()
        self.weekShift.emit(0)   # switch midweek/weekend with a pure fade
        self._load_schedule()

    @Slot()
    def exportSchedulePdf(self) -> None:
        from ..core.rendering.timer_report_pdf import default_pdf_filename, export_schedule_pdf

        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        base_dir = Path(documents) if documents else Path.home()
        default_path = base_dir / default_pdf_filename(self._schedule)
        path, _ = QFileDialog.getSaveFileName(
            self._window,
            self.tr("Export PDF"),
            str(default_path),
            self.tr("PDF files (*.pdf)"),
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"

        lang = getattr(self._window, "lang", None)
        time_format = getattr(lang, "time_with_seconds_format", "HH:mm:ss")
        date_format = getattr(lang, "date_format", "dd/MM/yyyy HH:mm")
        meeting_type_label = (
            self.tr("Weekend") if self._meeting_type is MeetingType.WEEKEND
            else self.tr("Midweek")
        )
        try:
            export_schedule_pdf(
                self._schedule,
                path,
                week_label=self._week_label(),
                meeting_type_label=meeting_type_label,
                date_format=date_format,
                time_format=time_format,
            )
        except Exception as exc:  # noqa: BLE001 - PDF export adapter boundary
            log.exception("Timer PDF export failed")
            QMessageBox.critical(
                self._window,
                self.tr("Export failed"),
                self.tr("Could not export the timer PDF:\n{error}").replace("{error}", str(exc)),
            )
            return

        QMessageBox.information(
            self._window,
            self.tr("PDF exported"),
            self.tr("Saved to:\n{path}").replace("{path}", path),
        )

    # ── Exposed: part editing ──────────────────────────────────────────────

    def _part_is_editable(self, part_id: str) -> bool:
        part = self._schedule.part_by_id(part_id) if self._schedule is not None else None
        return part is not None and part.state is PartState.IDLE

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
        if not self._part_is_editable(part_id):
            return
        adjust_part(self._schedule, part_id, int(delta_seconds))
        self._persist()

    @Slot(str, int)
    def setPartSeconds(self, part_id: str, seconds: int) -> None:
        if not self._part_is_editable(part_id):
            return
        redistribute_section(self._schedule, part_id, int(seconds))
        self._persist()

    @Slot(str, int)
    def setSectionCount(self, section: str, count: int) -> None:
        try:
            sec = Section(section)
        except ValueError:
            return
        set_section_part_count(self._schedule, sec, int(count))
        self._engine.set_schedule(self._schedule)
        self._persist()

    # ── Exposed: clock config ──────────────────────────────────────────────

    def _clock_model(self) -> dict:
        return self._clock_config.to_dict()

    clockConfig = Property("QVariant", _clock_model, notify=clockConfigChanged)

    def _clock_modes_model(self) -> list[str]:
        return [mode.value for mode in CLOCK_MODE_OPTIONS]

    clockModes = Property(
        "QVariant",
        _clock_modes_model,
        notify=clockConfigChanged,
    )

    def _part_timer_displays_model(self) -> list[str]:
        return [display.value for display in PART_TIMER_DISPLAY_OPTIONS]

    partTimerDisplays = Property(
        "QVariant",
        _part_timer_displays_model,
        notify=clockConfigChanged,
    )

    def _analog_clock_styles_model(self) -> list[str]:
        return [style.value for style in ANALOG_CLOCK_STYLE_OPTIONS]

    analogClockStyles = Property(
        "QVariant",
        _analog_clock_styles_model,
        notify=clockConfigChanged,
    )

    @Slot(str, "QVariant")
    def updateClock(self, key: str, value) -> None:
        data = self._clock_config.to_dict()
        if key not in data:
            return
        data[key] = value
        self._clock_config = ClockConfig.from_dict(data).clamped()
        self._output.set_clock_config(self._clock_config)
        self.clockConfigChanged.emit()

    # ── Exposed: monitors ──────────────────────────────────────────────────

    def _secondary_screens(self) -> list:
        return self._window.screen_mgr.secondary_screens()

    def _monitors_model(self) -> list:
        rows = []
        for i, s in enumerate(self._secondary_screens()):
            owner = self._allocation.owner_of(s)
            geo = s.geometry()
            rows.append({
                "index": i,
                "name": s.name() or f"Monitor {i + 1}",
                "resolution": f"{geo.width()} × {geo.height()}",
                "owner": owner,
                "reserved": owner == OWNER_TIMER,
                "usedByMedia": owner == OWNER_MEDIA,
            })
        return rows

    monitors = Property("QVariant", _monitors_model, notify=monitorsChanged)

    def _timer_visible(self) -> bool:
        return self._output.is_visible()

    timerVisible = Property(bool, _timer_visible, notify=monitorsChanged)

    @Slot()
    def refreshMonitors(self) -> None:
        self.monitorsChanged.emit()

    def _media_present_on(self, screen) -> bool:
        """True when a media projection window is *actually* shown on ``screen``.

        The conflict prompt must reflect reality, not the default ownership — a
        monitor defaults to ``media`` even when no media window is there, so
        keying off ownership alone would warn about displacing media that isn't
        present.
        """
        for win in getattr(self._window, "projection_windows", []):
            try:
                if win.screen() == screen:
                    return True
            except Exception:  # noqa: BLE001 - Qt screen-lifecycle boundary
                continue
        return False

    @Slot(int, result="QVariant")
    def requestReserve(self, index: int):
        """Reserve a monitor for the timer. Returns a conflict dict (for the QML
        confirm dialog) only when media is genuinely projecting there; otherwise
        reserves immediately and returns an empty dict."""
        screens = self._secondary_screens()
        if index < 0 or index >= len(screens):
            return {}
        screen = screens[index]
        if self._media_present_on(screen):
            return {
                "conflict": True,
                "index": index,
                "screenName": screen.name() or self.tr("this monitor"),
            }
        self._allocation.set_owner(screen, OWNER_TIMER)
        self._apply_timer_takeover()
        return {}

    @Slot(int)
    def confirmReserve(self, index: int) -> None:
        screens = self._secondary_screens()
        if index < 0 or index >= len(screens):
            return
        self._allocation.set_owner(screens[index], OWNER_TIMER)
        self._apply_timer_takeover()

    @Slot(int)
    def unreserveMonitor(self, index: int) -> None:
        """Cancel a timer reservation. This *only* frees the monitor — it is not
        responsible for showing media (the media window's own monitor menu is).
        The screen is left empty; the operator shows media there from that menu
        ("Show") if they want it."""
        screens = self._secondary_screens()
        if index < 0 or index >= len(screens):
            return
        screen = screens[index]
        self._allocation.set_owner(screen, OWNER_OFF)
        # Keep the media side's in-memory "hidden" set in sync so its monitor
        # menu shows this screen as available to "Show".
        self._window._deactivated_screens.add(screen.name())
        self._output.reconcile()   # fade the clock out
        self.monitorsChanged.emit()

    @Slot(bool)
    def setTimerVisible(self, visible: bool) -> None:
        self._output.set_visible(bool(visible))
        self.monitorsChanged.emit()

    def _apply_timer_takeover(self) -> None:
        # Media must drop the screen first, then the clock claims it.
        self._reconcile_media()
        self._output.reconcile()
        self.monitorsChanged.emit()

    def _reconcile_media(self) -> None:
        media = getattr(self._window, "_projection_targets", None)
        if media is not None and hasattr(media, "reconcile_projection_windows"):
            media.reconcile_projection_windows()

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
