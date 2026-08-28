from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

OFFLINE_DOWNLOAD_CONTEXT = "_OfflineDownload"
OFFLINE_DOWNLOAD_SOURCE = QT_TRANSLATE_NOOP(
    "_OfflineDownload",
    "Download for offline playback",
)
OFFLINE_DOWNLOADING_SOURCE = QT_TRANSLATE_NOOP("_OfflineDownload", "Downloading…")
OFFLINE_DOWNLOADING_PROGRESS_SOURCE = QT_TRANSLATE_NOOP(
    "_OfflineDownload",
    "Downloading… {pct}%",
)
OFFLINE_QUEUED_SOURCE = QT_TRANSLATE_NOOP(
    "_OfflineDownload",
    "Queued for download",
)
JW_PLAYLIST_CONTEXT = "JwPlaylistImport"
JW_PLAYLIST_TITLE_SOURCE = QT_TRANSLATE_NOOP(
    "JwPlaylistImport",
    "JW Library Playlist",
)
JW_PLAYLIST_UNRESOLVED_SOURCE = QT_TRANSLATE_NOOP(
    "JwPlaylistImport",
    "Some items could not be resolved. Check the internet connection:\n\n{items}",
)
DOCUMENT_PAGES_CONTEXT = "DocumentPages"
DOCUMENT_PAGE_TITLE_SOURCE = QT_TRANSLATE_NOOP(
    "DocumentPages",
    "{name} — page {page}",
)


def tr_offline_download() -> str:
    return QCoreApplication.translate(
        OFFLINE_DOWNLOAD_CONTEXT,
        OFFLINE_DOWNLOAD_SOURCE,
    )


def tr_offline_downloading() -> str:
    return QCoreApplication.translate(
        OFFLINE_DOWNLOAD_CONTEXT,
        OFFLINE_DOWNLOADING_SOURCE,
    )


def tr_offline_downloading_progress(pct: int | str) -> str:
    return QCoreApplication.translate(
        OFFLINE_DOWNLOAD_CONTEXT,
        OFFLINE_DOWNLOADING_PROGRESS_SOURCE,
    ).format(pct=pct)


def tr_offline_queued() -> str:
    return QCoreApplication.translate(
        OFFLINE_DOWNLOAD_CONTEXT,
        OFFLINE_QUEUED_SOURCE,
    )


def tr_item_count(count: int) -> str:
    """Return the application-wide standalone item-count label."""

    normalized_count = max(0, int(count))
    return QCoreApplication.translate(
        "CommonCounts",
        "%n item(s)",
        "",
        normalized_count,
    )


def tr_media_item_count(count: int) -> str:
    """Return the established application-wide media-item count."""

    normalized_count = max(0, int(count))
    return QCoreApplication.translate(
        "CommonCounts",
        "%n media item(s)",
        "",
        normalized_count,
    )


def tr_jw_playlist_title() -> str:
    return QCoreApplication.translate(
        JW_PLAYLIST_CONTEXT,
        JW_PLAYLIST_TITLE_SOURCE,
    )


def tr_jw_playlist_unresolved(item_names: str) -> str:
    return QCoreApplication.translate(
        JW_PLAYLIST_CONTEXT,
        JW_PLAYLIST_UNRESOLVED_SOURCE,
    ).format(items=item_names)


def tr_document_page_title(name: str, page: int) -> str:
    return QCoreApplication.translate(
        DOCUMENT_PAGES_CONTEXT,
        DOCUMENT_PAGE_TITLE_SOURCE,
    ).format(name=name, page=max(1, int(page)))
