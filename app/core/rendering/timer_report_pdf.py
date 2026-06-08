"""PDF export for the advanced timer schedule."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPageLayout,
    QPageSize,
    QPainter,
    QPainterPath,
    QPdfWriter,
    QPen,
)

from ..i18n.date import format_datetime, format_time_with_seconds
from ..i18n.timer_part_titles import display_part_title
from ..meetings.colors import _section_colors
from ..meetings.section_meta import SECTION_META
from ..timer.models import MeetingSchedule, MeetingType, PartState, Section
from ..timer.part_titles import is_indexed_part_title_source


_TR_CTX = "TimerPdfExport"

# Literal source markers for lupdate. Runtime calls go through _tr(), but
# lupdate does not understand that local wrapper.
_TIMER_PDF_EXPORT_SOURCES = (
    QT_TRANSLATE_NOOP("TimerPdfExport", "Meeting timer report"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "{meeting_type} - {week}"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Total {time}"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "In progress"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Running"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Completed"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Not started"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Page {page}"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Part"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Planned"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Started"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Finished"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Duration"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Status"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Generated"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Within time"),
    QT_TRANSLATE_NOOP("TimerPdfExport", "Over time"),
)

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


@dataclass(frozen=True)
class TimerPdfRow:
    number: int
    section: Section
    show_section_header: bool
    section_label: str
    section_color: str
    title: str
    planned_label: str
    started_label: str
    finished_label: str
    result_label: str
    delta_label: str
    status_label: str


@dataclass(frozen=True)
class TimerPdfSummary:
    within_time_count: int
    over_time_count: int


def _tr(text: str) -> str:
    return QCoreApplication.translate(_TR_CTX, text)


def _section_label(section: Section) -> str:
    code = _SECTION_CODES.get(section, "")
    source = SECTION_META.get(code, (section.value, 215))[0]
    return QCoreApplication.translate("_Section", source)


def _section_color(section: Section) -> str:
    code = _SECTION_CODES.get(section, "")
    hue = SECTION_META.get(code, ("", 215))[1]
    return _section_colors(hue)["accent"]


def _fmt_duration(seconds: float) -> str:
    total = int(round(abs(seconds)))
    sign = "-" if seconds < 0 else ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{sign}{h}:{m:02d}:{s:02d}"
    return f"{sign}{m:02d}:{s:02d}"


def _fmt_delta(seconds: float) -> str:
    total = int(round(seconds))
    if total == 0:
        return "0:00"
    sign = "+" if total > 0 else "-"
    return sign + _fmt_duration(abs(total))


def _rounded_delta_seconds(elapsed_seconds: float, planned_seconds: int) -> int:
    return int(round(elapsed_seconds - planned_seconds))


def build_timer_pdf_summary(
    schedule: MeetingSchedule,
    *,
    now_epoch: float | None = None,
) -> TimerPdfSummary:
    """Count completed parts that stayed within the planned time vs. over time."""
    now = time.time() if now_epoch is None else float(now_epoch)
    within_time = 0
    over_time = 0

    for part in schedule.parts:
        if part.state is not PartState.STOPPED:
            continue
        if _rounded_delta_seconds(part.elapsed(now), part.planned_seconds) <= 0:
            within_time += 1
        else:
            over_time += 1

    return TimerPdfSummary(within_time_count=within_time, over_time_count=over_time)


def build_timer_pdf_rows(
    schedule: MeetingSchedule,
    *,
    now_epoch: float | None = None,
    time_format: str = "HH:mm:ss",
) -> list[TimerPdfRow]:
    """Build stable, testable rows for the timer PDF export."""
    now = time.time() if now_epoch is None else float(now_epoch)
    rows: list[TimerPdfRow] = []
    section_positions: dict[Section, int] = {}
    for number, part in enumerate(schedule.parts, start=1):
        section_position = section_positions.get(part.section, 0) + 1
        section_positions[part.section] = section_position
        indexed_number: int | None = None
        if is_indexed_part_title_source(part.title):
            indexed_number = section_position
        show_section_header = part.section not in _STANDALONE_SECTIONS

        started_epoch = part.first_started_epoch
        elapsed = part.elapsed(now)

        started_label = (
            format_time_with_seconds(started_epoch, time_format)
            if started_epoch is not None
            else ""
        )
        finished_label = ""
        result_label = ""
        delta_label = ""

        if part.state is PartState.RUNNING:
            result_label = _fmt_duration(elapsed)
            delta_label = _fmt_delta(_rounded_delta_seconds(elapsed, part.planned_seconds))
            finished_label = _tr("In progress")
            status_label = _tr("Running")
        elif part.state is PartState.STOPPED:
            result_label = _fmt_duration(elapsed)
            delta_label = _fmt_delta(_rounded_delta_seconds(elapsed, part.planned_seconds))
            if started_epoch is not None:
                finished_label = format_time_with_seconds(started_epoch + elapsed, time_format)
            status_label = _tr("Completed")
        else:
            status_label = _tr("Not started")

        rows.append(TimerPdfRow(
            number=number,
            section=part.section,
            show_section_header=show_section_header,
            section_label=_section_label(part.section),
            section_color=_section_color(part.section),
            title=display_part_title(part, indexed_number=indexed_number),
            planned_label=_fmt_duration(part.planned_seconds),
            started_label=started_label,
            finished_label=finished_label,
            result_label=result_label,
            delta_label=delta_label,
            status_label=status_label,
        ))
    return rows


def default_pdf_filename(schedule: MeetingSchedule) -> str:
    meeting = "midweek" if schedule.meeting_type is MeetingType.MIDWEEK else "weekend"
    return f"Solin timer {schedule.week_monday} {meeting}.pdf"


class _PdfPainter:
    def __init__(self, path: str | Path) -> None:
        self.writer = QPdfWriter(str(path))
        self.writer.setResolution(96)
        self.writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        self.writer.setPageOrientation(QPageLayout.Orientation.Landscape)
        self.painter = QPainter(self.writer)
        self.painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.page_no = 1
        self.margin = 38.0
        self.width = float(self.writer.width())
        self.height = float(self.writer.height())
        self.y = self.margin

    def end(self) -> None:
        self.painter.end()

    def new_page(self, title: str, meta: str) -> None:
        self._draw_footer()
        self.writer.newPage()
        self.page_no += 1
        self.y = self.margin
        self.draw_header(title, meta, compact=True)

    def draw_header(self, title: str, meta: str, *, compact: bool = False) -> None:
        p = self.painter
        p.fillRect(QRectF(0, 0, self.width, self.height), QColor("#ffffff"))

        title_font = QFont("Segoe UI", 22 if not compact else 17, QFont.Weight.Bold)
        title_h = QFontMetricsF(title_font).height() + 8
        p.setPen(QColor("#111827"))
        p.setFont(title_font)
        p.drawText(QRectF(self.margin, self.y, self.width - self.margin * 2, title_h), title)
        self.y += title_h + (5 if not compact else 3)

        meta_font = QFont("Segoe UI", 10)
        meta_h = QFontMetricsF(meta_font).height() + 4
        p.setPen(QColor("#5b6472"))
        p.setFont(meta_font)
        p.drawText(QRectF(self.margin, self.y, self.width - self.margin * 2, meta_h), meta)
        self.y += meta_h + (14 if compact else 22)

    def draw_summary(self, items: list[tuple[str, str]]) -> None:
        gap = 10.0
        card_count = max(1, len(items))
        card_w = (self.width - self.margin * 2 - gap * (card_count - 1)) / card_count
        x = self.margin
        for label, value in items:
            rect = QRectF(x, self.y, card_w, 54)
            self.rounded_rect(rect, "#f8fafc", "#e5e7eb", 8)
            self.painter.setPen(QColor("#6b7280"))
            self.painter.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            self.painter.drawText(rect.adjusted(14, 9, -14, -30), label.upper())
            self.painter.setPen(QColor("#111827"))
            self.painter.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
            self.painter.drawText(rect.adjusted(14, 24, -14, -8), value)
            x += card_w + gap
        self.y += 70

    def draw_table_header(self, columns: list[tuple[str, float]]) -> None:
        rect = QRectF(self.margin, self.y, self.width - self.margin * 2, 28)
        self.rounded_rect(rect, "#111827", "#111827", 7)
        self.painter.setPen(QColor("#ffffff"))
        self.painter.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        x = self.margin
        for label, width in columns:
            self.painter.drawText(
                QRectF(x + 8, self.y + 5, width - 16, 18),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                label,
            )
            x += width
        self.y += 34

    def ensure_space(self, needed: float, title: str, meta: str, columns: list[tuple[str, float]]) -> None:
        if self.y + needed <= self.height - self.margin - 26:
            return
        self.new_page(title, meta)
        self.draw_table_header(columns)

    def draw_section(self, label: str, color: str, total_label: str) -> None:
        accent = QColor(color)
        rect = QRectF(self.margin, self.y, self.width - self.margin * 2, 24)
        self.rounded_rect(rect, QColor(accent.red(), accent.green(), accent.blue(), 24), "#e5e7eb", 6)
        self.painter.setPen(accent)
        self.painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.painter.drawText(rect.adjusted(10, 2, -10, -2), Qt.AlignmentFlag.AlignVCenter, label)
        self.painter.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        self.painter.drawText(
            rect.adjusted(10, 2, -10, -2),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            total_label,
        )
        self.y += 29

    def draw_row(self, row: TimerPdfRow, columns: list[tuple[str, float]], *, shaded: bool) -> None:
        row_h = 34.0
        bg = "#fbfdff" if shaded else "#ffffff"
        self.painter.fillRect(QRectF(self.margin, self.y, self.width - self.margin * 2, row_h), QColor(bg))
        self.painter.setPen(QPen(QColor("#e5e7eb"), 1))
        self.painter.drawLine(int(self.margin), int(self.y + row_h), int(self.width - self.margin), int(self.y + row_h))

        values = [
            str(row.number),
            row.title,
            row.planned_label,
            row.started_label or "-",
            row.finished_label or "-",
            row.result_label or "-",
            row.delta_label or "-",
            row.status_label,
        ]
        self.painter.setFont(QFont("Segoe UI", 9))
        metrics = QFontMetricsF(self.painter.font())
        x = self.margin
        for idx, ((_, width), value) in enumerate(zip(columns, values, strict=False)):
            color = "#111827"
            if idx in {3, 4, 5, 6, 7}:
                color = "#4b5563"
            if idx == 6 and value.startswith("+"):
                color = "#b42318"
            elif idx == 6 and (value.startswith("-") or value == "0:00"):
                color = "#047857"
            self.painter.setPen(QColor(color))
            text = metrics.elidedText(value, Qt.TextElideMode.ElideRight, max(10, int(width - 16)))
            self.painter.drawText(
                QRectF(x + 8, self.y + 3, width - 16, row_h - 6),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
            x += width
        self.y += row_h

    def rounded_rect(self, rect: QRectF, fill, stroke, radius: float) -> None:
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        self.painter.fillPath(path, QColor(fill) if isinstance(fill, str) else fill)
        self.painter.setPen(QPen(QColor(stroke), 1))
        self.painter.drawPath(path)

    def _draw_footer(self) -> None:
        self.painter.setPen(QColor("#9ca3af"))
        self.painter.setFont(QFont("Segoe UI", 8))
        self.painter.drawText(
            QRectF(self.margin, self.height - self.margin + 8, self.width - self.margin * 2, 16),
            Qt.AlignmentFlag.AlignRight,
            _tr("Page {page}").replace("{page}", str(self.page_no)),
        )


def export_schedule_pdf(
    schedule: MeetingSchedule,
    path: str | Path,
    *,
    week_label: str,
    meeting_type_label: str,
    date_format: str = "dd/MM/yyyy HH:mm",
    time_format: str = "HH:mm:ss",
    generated_epoch: float | None = None,
    now_epoch: float | None = None,
) -> None:
    """Render ``schedule`` to a polished PDF at ``path``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    generated = time.time() if generated_epoch is None else float(generated_epoch)
    rows = build_timer_pdf_rows(schedule, now_epoch=now_epoch, time_format=time_format)

    summary = build_timer_pdf_summary(schedule, now_epoch=now_epoch)

    title = _tr("Meeting timer report")
    meta = (
        _tr("{meeting_type} - {week}")
        .replace("{meeting_type}", meeting_type_label)
        .replace("{week}", week_label)
    )
    generated_label = format_datetime(generated, date_format)
    section_totals = {
        section: _tr("Total {time}").replace(
            "{time}",
            _fmt_duration(schedule.section_total_seconds(section)),
        )
        for section in {row.section for row in rows}
    }

    pdf = _PdfPainter(target)
    try:
        table_w = pdf.width - pdf.margin * 2
        fixed = [36, 76, 84, 90, 76, 62, 88]
        title_w = table_w - sum(fixed)
        columns = [
            ("#", 36),
            (_tr("Part"), title_w),
            (_tr("Planned"), 76),
            (_tr("Started"), 84),
            (_tr("Finished"), 90),
            (_tr("Duration"), 76),
            ("+/-", 62),
            (_tr("Status"), 88),
        ]

        pdf.draw_header(title, meta)
        pdf.draw_summary([
            (_tr("Generated"), generated_label),
            (_tr("Within time"), str(summary.within_time_count)),
            (_tr("Over time"), str(summary.over_time_count)),
        ])
        pdf.draw_table_header(columns)

        last_section: Section | None = None
        shaded = False
        for row in rows:
            if row.show_section_header and row.section is not last_section:
                pdf.ensure_space(66, title, meta, columns)
                pdf.draw_section(
                    row.section_label,
                    row.section_color,
                    section_totals.get(row.section, ""),
                )
                last_section = row.section
                shaded = False
            pdf.ensure_space(38, title, meta, columns)
            pdf.draw_row(row, columns, shaded=shaded)
            shaded = not shaded
        pdf._draw_footer()
    finally:
        pdf.end()
