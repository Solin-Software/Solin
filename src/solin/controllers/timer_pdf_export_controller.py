"""Qt file-dialog adapter for timer schedule PDF export."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..core.rendering.timer_report_pdf import (
    default_pdf_filename,
    export_schedule_pdf,
)

log = logging.getLogger(__name__)


class TimerPdfExportController:
    def __init__(self, parent) -> None:
        self._parent = parent

    def export(
        self,
        schedule,
        *,
        week_label: str,
        meeting_type_label: str,
        date_format: str,
        time_format: str,
    ) -> None:
        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        base_dir = Path(documents) if documents else Path.home()
        default_path = base_dir / default_pdf_filename(schedule)
        path, _ = QFileDialog.getSaveFileName(
            self._parent,
            self._parent.tr("Export PDF"),
            str(default_path),
            self._parent.tr("PDF files (*.pdf)"),
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
            )
        except Exception as exc:  # noqa: BLE001 - PDF export adapter boundary
            log.exception("Timer PDF export failed")
            QMessageBox.critical(
                self._parent,
                self._parent.tr("Export failed"),
                self._parent.tr(
                    "Could not export the timer PDF:\n{error}"
                ).replace("{error}", str(exc)),
            )
            return

        QMessageBox.information(
            self._parent,
            self._parent.tr("PDF exported"),
            self._parent.tr("Saved to:\n{path}").replace("{path}", path),
        )
