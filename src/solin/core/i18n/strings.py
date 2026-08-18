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
_ITEM_COUNT_SINGULAR_SOURCE = "1 item"
_ITEM_COUNT_PLURAL_SOURCE = "%n items"


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


def tr_item_count(count: int) -> str:
    """Return the established application-wide singular/plural item count."""

    normalized_count = max(0, int(count))
    # Keep the long-established context so every locale reuses the same
    # vocabulary. MediaDestinationBridge owns the lupdate-visible numerus call.
    if normalized_count == 1:
        return QCoreApplication.translate(
            "MediaDestinationBridge",
            _ITEM_COUNT_SINGULAR_SOURCE,
        )
    return QCoreApplication.translate(
        "MediaDestinationBridge",
        _ITEM_COUNT_PLURAL_SOURCE,
        "",
        normalized_count,
    )
