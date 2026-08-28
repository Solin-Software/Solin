"""Qt file-dialog adapter for timer schedule PDF export."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP, QStandardPaths
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..core.rendering.timer_report_pdf import (
    default_pdf_filename,
    export_schedule_pdf,
)

log = logging.getLogger(__name__)

_TR_CONTEXT = "TimerPdfExport"
_EXPORT_PDF_SOURCE = QT_TRANSLATE_NOOP("TimerPdfExport", "Export PDF")
_PDF_FILES_SOURCE = QT_TRANSLATE_NOOP("TimerPdfExport", "PDF files (*.pdf)")
_EXPORT_FAILED_SOURCE = QT_TRANSLATE_NOOP("TimerPdfExport", "Export failed")
_EXPORT_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "TimerPdfExport",
    "Could not export the timer PDF:\n{error}",
)
_EXPORTED_SOURCE = QT_TRANSLATE_NOOP("TimerPdfExport", "PDF exported")
_SAVED_TO_SOURCE = QT_TRANSLATE_NOOP("TimerPdfExport", "Saved to:\n{path}")


def _tr(source: str) -> str:
    return QCoreApplication.translate(_TR_CONTEXT, source)


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
            _tr(_EXPORT_PDF_SOURCE),
            str(default_path),
            _tr(_PDF_FILES_SOURCE),
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
                _tr(_EXPORT_FAILED_SOURCE),
                _tr(_EXPORT_ERROR_SOURCE).format(error=exc),
            )
            return

        QMessageBox.information(
            self._parent,
            _tr(_EXPORTED_SOURCE),
            _tr(_SAVED_TO_SOURCE).format(path=path),
        )
