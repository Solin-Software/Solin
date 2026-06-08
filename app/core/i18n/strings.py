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
    ).replace("{pct}", str(pct))


def tr_offline_queued() -> str:
    return QCoreApplication.translate(
        OFFLINE_DOWNLOAD_CONTEXT,
        OFFLINE_QUEUED_SOURCE,
    )
