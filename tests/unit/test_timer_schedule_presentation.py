"""Contracts shared by the timer presentation and PDF export."""

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QTranslator
from PySide6.QtPdf import QPdfDocument

from solin.core.i18n.date import format_datetime, format_time_with_seconds
from solin.core.i18n.timer_part_titles import display_part_title
from solin.core.i18n.timer_schedule_view import parts_model_for_schedule
from solin.core.rendering import timer_report_pdf
from solin.core.rendering.timer_report_pdf import (
    build_timer_pdf_rows,
    build_timer_pdf_summary,
    default_pdf_filename,
    export_schedule_pdf,
)
from solin.core.timer.models import (
    CustomTimerSchedule,
    MeetingSchedule,
    MeetingType,
    PartState,
    Section,
    TimerPart,
    TimerSection,
)
from solin.core.timer.pacing_models import PacingMode, PacingWindow


class _TimerTranslator(QTranslator):
    def translate(self, context, sourceText, disambiguation=None, n=-1):
        if context == "_TimerPart" and sourceText == "Part {number}":
            return "Translated {number}"
        return ""


def test_custom_titles_are_literal_and_meeting_overrides_take_precedence():
    translator = _TimerTranslator()
    app = QCoreApplication.instance()
    app.installTranslator(translator)
    try:
        official = TimerPart.new(Section.MINISTRY, "Part {number}", 60)
        custom = TimerPart.new(None, "Part {number}", 90)
        assert display_part_title(official, indexed_number=2) == "Translated 2"
        assert display_part_title(custom, indexed_number=2) == "Part {number}"
        official.title_override = "Part {number}"
        assert display_part_title(official, indexed_number=2) == "Part {number}"
    finally:
        app.removeTranslator(translator)


def test_custom_presentation_and_pdf_share_order_titles_and_sections():
    section = TimerSection.new("Workshop")
    first = TimerPart.new(None, "Part {number}", 90)
    second = TimerPart.new(section.id, "Discussion", 30)
    second.state = PartState.STOPPED
    second.accumulated_seconds = 35
    third = TimerPart.new(None, "Closing", 60)
    section.parts.append(second)
    schedule = CustomTimerSchedule.new("Training / morning")
    schedule.entries = [first, section, third]

    views = parts_model_for_schedule(schedule)
    rows = build_timer_pdf_rows(schedule, now_epoch=100)
    assert [row["id"] for row in views] == [first.id, second.id, third.id]
    assert [row.title for row in rows] == [view["title"] for view in views]
    assert [row.section_label for row in rows] == [view["sectionTitle"] for view in views]
    assert [row["showSectionHeader"] for row in views] == [False, True, False]
    assert views[1]["section"] == section.id
    assert views[1]["sectionTitle"] == "Workshop"
    assert views[1]["sectionTotalLabel"] == "00:30"
    assert [view["durationEditable"] for view in views] == [True, False, True]
    assert rows[1].delta_label == "+00:05"
    assert default_pdf_filename(schedule) == "Solin timer Training _ morning.pdf"


def test_meeting_presentation_keeps_canonical_section_and_editability():
    part = TimerPart.new(Section.MINISTRY, "Part {number}", 60)
    schedule = MeetingSchedule("2026-11-23", MeetingType.MIDWEEK, [part])
    row = parts_model_for_schedule(schedule)[0]
    assert row["section"] == "ministry"
    assert row["sectionKey"] == "ministry"
    assert row["sectionTitle"]
    assert row["title"] == "Part 1"
    assert row["durationEditable"]
    assert row["configurableCount"]


def test_custom_schedule_exports_pdf_with_schedule_and_section_titles(tmp_path, monkeypatch):
    section = TimerSection.new("Workshop")
    section.parts.append(TimerPart.new(section.id, "Discussion", 90))
    schedule = CustomTimerSchedule.new("Training")
    schedule.entries = [section]
    target = tmp_path / "report.pdf"
    headers = []
    sections = []
    draw_header = timer_report_pdf._PdfPainter.draw_header
    draw_section = timer_report_pdf._PdfPainter.draw_section

    def capture_header(painter, title, meta, **kwargs):
        headers.append((title, meta))
        draw_header(painter, title, meta, **kwargs)

    def capture_section(painter, label, color, total):
        sections.append((label, total))
        draw_section(painter, label, color, total)

    monkeypatch.setattr(timer_report_pdf._PdfPainter, "draw_header", capture_header)
    monkeypatch.setattr(timer_report_pdf._PdfPainter, "draw_section", capture_section)
    export_schedule_pdf(schedule, target, week_label="", meeting_type_label="")

    document = QPdfDocument()
    assert document.load(str(target)) == QPdfDocument.Error.None_
    assert document.pageCount() >= 1
    assert headers[0] == ("Training", "")
    assert sections == [("Workshop", "Total 01:30")]


def test_pdf_uses_observed_instants_and_adjusted_target():
    part = TimerPart.new(None, "Discussion", 120)
    part.state = PartState.STOPPED
    part.first_started_epoch = 900
    part.actual_started_epoch = 1000
    part.actual_stopped_epoch = 1090
    part.accumulated_seconds = 95
    part.run_target_seconds = 90
    schedule = CustomTimerSchedule.new("Training")
    schedule.entries = [part]
    schedule.pacing_window = PacingWindow(900, 2000)

    row = build_timer_pdf_rows(schedule, now_epoch=1200, include_pacing=True)[0]
    assert row.started_label == format_time_with_seconds(1000)
    assert row.finished_label == format_time_with_seconds(1090)
    assert row.planned_label == "02:00"
    assert row.adjusted_label == "01:30"
    assert row.result_label == "01:35"
    assert row.delta_label == "+00:05"
    assert build_timer_pdf_summary(schedule, now_epoch=1200, include_pacing=True).over_time_count == 1

    default_row = build_timer_pdf_rows(schedule, now_epoch=1200)[0]
    assert default_row.started_label == format_time_with_seconds(900)
    assert default_row.finished_label == format_time_with_seconds(995)
    assert default_row.adjusted_label == ""
    assert default_row.delta_label == "-00:25"
    assert build_timer_pdf_summary(schedule, now_epoch=1200).within_time_count == 1

    schedule.pacing_window = None
    assert build_timer_pdf_rows(schedule, now_epoch=1200, include_pacing=True)[0] == default_row
    assert build_timer_pdf_summary(schedule, now_epoch=1200, include_pacing=True).within_time_count == 1


def test_pdf_uses_legacy_time_fallback_when_observations_are_absent():
    part = TimerPart.new(None, "Discussion", 120)
    part.state = PartState.STOPPED
    part.first_started_epoch = 900
    part.accumulated_seconds = 95
    schedule = CustomTimerSchedule.new("Training")
    schedule.entries = [part]
    schedule.pacing_window = PacingWindow(900, 2000)

    row = build_timer_pdf_rows(schedule, now_epoch=1200, include_pacing=True)[0]
    assert row.started_label == format_time_with_seconds(900)
    assert row.finished_label == format_time_with_seconds(995)
    assert row.adjusted_label == ""


def test_pdf_shows_planned_window_and_labels_the_projected_end(tmp_path, monkeypatch):
    schedule = CustomTimerSchedule.new("Training")
    schedule.pacing_window = PacingWindow(1000, 2000)
    snapshot = SimpleNamespace(start_epoch=1000, end_epoch=2000, adjusted_end_epoch=2100)
    calls = []
    summaries = []

    def snapshot_for_schedule(actual_schedule, now, *, automatic):
        calls.append((actual_schedule, now, automatic))
        return snapshot

    def capture_summary(painter, items):
        summaries.append(items)

    monkeypatch.setattr(timer_report_pdf, "pacing_snapshot", snapshot_for_schedule)
    monkeypatch.setattr(timer_report_pdf._PdfPainter, "draw_summary", capture_summary)
    export_schedule_pdf(
        schedule, tmp_path / "report.pdf", week_label="", meeting_type_label="", now_epoch=1500,
        pacing_mode=PacingMode.AUTOMATIC,
    )

    assert calls == [(schedule, 1500, True)]
    assert summaries[1] == [
        ("Planned start", format_datetime(1000)),
        ("Planned end", format_datetime(2000)),
        ("Estimated end", format_datetime(2100)),
    ]


def test_pdf_exports_a_real_pacing_snapshot(tmp_path, monkeypatch):
    schedule = CustomTimerSchedule.new("Training")
    schedule.entries = [TimerPart.new(None, "Discussion", 600)]
    schedule.pacing_window = PacingWindow(1000, 2000)
    summaries = []
    draw_summary = timer_report_pdf._PdfPainter.draw_summary

    def capture_summary(painter, items):
        summaries.append(items)
        draw_summary(painter, items)

    monkeypatch.setattr(timer_report_pdf._PdfPainter, "draw_summary", capture_summary)
    target = tmp_path / "paced-report.pdf"
    export_schedule_pdf(
        schedule, target, week_label="", meeting_type_label="", now_epoch=1000, pacing_mode=PacingMode.MONITOR
    )

    document = QPdfDocument()
    assert document.load(str(target)) == QPdfDocument.Error.None_
    assert len(summaries) == 2
    assert summaries[1][0][1] == format_datetime(1000)
    assert summaries[1][1][1] == format_datetime(2000)
    assert summaries[1][2][1] == format_datetime(2000)


def test_pdf_omits_pacing_summary_by_default_and_without_window(tmp_path, monkeypatch):
    schedule = CustomTimerSchedule.new("Training")
    schedule.entries = [TimerPart.new(None, "Discussion", 600)]
    schedule.pacing_window = PacingWindow(1000, 2000)
    summaries = []
    monkeypatch.setattr(timer_report_pdf._PdfPainter, "draw_summary", lambda painter, items: summaries.append(items))

    export_schedule_pdf(schedule, tmp_path / "default.pdf", week_label="", meeting_type_label="", now_epoch=1000)
    assert len(summaries) == 1

    summaries.clear()
    schedule.pacing_window = None
    export_schedule_pdf(
        schedule, tmp_path / "no-window.pdf", week_label="", meeting_type_label="", now_epoch=1000,
        pacing_mode=PacingMode.MONITOR,
    )
    assert len(summaries) == 1
