"""Background adapter for resolving remote JW meeting media."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from solin.core.jw.publication_archive import resolve_meeting_media


class MeetingMediaResolutionWorker(QObject):
    """Resolve one queued media reference without blocking the UI thread."""

    resolved = Signal(str, object)

    @Slot(str, str, int, int, int, str, str, bool)
    def resolve(
        self,
        request_id: str,
        key_symbol: str,
        track: int,
        issue_tag: int,
        meps_doc_id: int,
        media_type: str,
        language: str,
        is_sign_language: bool,
    ) -> None:
        result = resolve_meeting_media(
            key_symbol,
            track,
            issue_tag,
            meps_doc_id,
            language,
            is_sign_language=is_sign_language,
            media_type=media_type,
        )
        self.resolved.emit(request_id, result)


__all__ = ["MeetingMediaResolutionWorker"]
