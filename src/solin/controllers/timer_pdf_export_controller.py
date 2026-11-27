"""Qt file-dialog adapter for timer schedule PDF export."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QStandardPaths
from PySide6.QtWidgets import QFileDialog, QWidget

from ..core.rendering.timer_report_pdf import (
    default_pdf_filename,
    export_schedule_pdf,
)
from ..core.timer.models import TimerSchedule
from ..core.timer.pacing_models import PacingMode
from ..ui.notifications import NotificationCenter

log = logging.getLogger(__name__)

class TimerPdfExportController:
    def __init__(self, parent: QWidget | None, notifications: NotificationCenter) -> None:
        self._parent = parent
        self._notifications = notifications

    def export(
        self,
        schedule: TimerSchedule,
        *,
        week_label: str,
        meeting_type_label: str,
        date_format: str,
        time_format: str,
        pacing_mode: PacingMode = PacingMode.OFF,
    ) -> None:
        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        base_dir = Path(documents) if documents else Path.home()
        default_path = base_dir / default_pdf_filename(schedule)
        path, _ = QFileDialog.getSaveFileName(
            self._parent,
            QCoreApplication.translate("TimerPdfExport", "Export PDF"),
            str(default_path),
            QCoreApplication.translate("TimerPdfExport", "PDF files (*.pdf)"),
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"

        try:
            export_schedule_pdf(
                schedule,
                path,
                week_label=week_label,
                meeting_type_label=meeting_type_label,
                date_format=date_format,
                time_format=time_format,
                pacing_mode=pacing_mode,
            )
        except Exception as exc:  # noqa: BLE001 - PDF export adapter boundary
            log.exception("Timer PDF export failed")
            self._notifications.error(
                QCoreApplication.translate(
                    "TimerPdfExport", "Could not export the timer PDF:\n{error}",
                ).format(error=exc),
                title=QCoreApplication.translate("TimerPdfExport", "Export failed"),
            )
            return

        self._notifications.success(
            QCoreApplication.translate("TimerPdfExport", "Saved to:\n{path}").format(path=path),
            title=QCoreApplication.translate("TimerPdfExport", "PDF exported"),
        )
