"""Contracts shared by the timer presentation and PDF export."""

from PySide6.QtCore import QCoreApplication, QTranslator
from PySide6.QtPdf import QPdfDocument

from solin.core.i18n.timer_part_titles import display_part_title
from solin.core.i18n.timer_schedule_view import parts_model_for_schedule
from solin.core.rendering import timer_report_pdf
from solin.core.rendering.timer_report_pdf import (
    build_timer_pdf_rows,
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
