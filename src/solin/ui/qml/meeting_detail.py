"""Dedicated QML host for meeting and memorial detail views."""

from __future__ import annotations

from typing import Any, cast

from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QSurfaceFormat
from PySide6.QtQuickWidgets import QQuickWidget

from solin.ui.qml.loader import load_qml_type
from solin.ui.qml.playlist.visuals import (
    PlaylistIconProvider,
    PlaylistThumbnailProvider,
)


class MeetingDetailQmlHost(QQuickWidget):
    """Compose the MeetingDetail QML scene and its presentation adapters."""

    def __init__(
        self,
        *,
        controller,
        catalog_bridge,
        songs_bridge,
        meeting_pill: str,
        meeting_date: str,
        pill_color: str,
        no_items_text: str,
        parent=None,
    ) -> None:
        super().__init__(parent)

        surface_format = QSurfaceFormat()
        surface_format.setAlphaBufferSize(8)
        self.setFormat(surface_format)
        self.setClearColor(QColor("#0d1117"))
        self.setMouseTracking(True)
        self.setAcceptDrops(False)

        self.engine().addImageProvider(
            "playlistthumbs",
            PlaylistThumbnailProvider(
                controller.thumb_cache,
                controller.disk_thumbnail,
            ),
        )
        self.engine().addImageProvider("playlisticons", PlaylistIconProvider())

        context = self.rootContext()
        context.setContextProperty("controller", controller)
        context.setContextProperty("catalogBridge", catalog_bridge)
        context.setContextProperty("songsBridge", songs_bridge)
        context.setContextProperty("pillColor", pill_color)
        self.update_shell_texts(
            meeting_pill=meeting_pill,
            meeting_date=meeting_date,
            no_items_text=no_items_text,
            retranslate=False,
        )

        load_qml_type(self, "MeetingDetailView")
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)

    def update_shell_texts(
        self,
        *,
        meeting_pill: str,
        meeting_date: str,
        no_items_text: str,
        retranslate: bool = True,
    ) -> None:
        context = self.rootContext()
        context.setContextProperty("meetingPill", meeting_pill)
        context.setContextProperty("meetingDate", meeting_date)
        context.setContextProperty("noItemsText", no_items_text)
        if retranslate and hasattr(self.engine(), "retranslate"):
            self.engine().retranslate()

    def clear_scene(self) -> None:
        self.setSource(QUrl())

    def preview_external_drop(self, source, point) -> None:
        root = cast(Any, self.rootObject())
        if root is None:
            return
        local_position = self.mapFrom(source, point)
        delegate_index = root.getIndexAt(local_position.y())
        root.setProperty("dropIndicatorIndex", delegate_index)

    def clear_external_drop_preview(self) -> None:
        root = cast(Any, self.rootObject())
        if root is not None:
            root.clearExternalDropPreview()

    def external_drop_target(self, source, point) -> tuple[str, int]:
        root = cast(Any, self.rootObject())
        if root is None:
            return "root", 2**31 - 1

        local_position = self.mapFrom(source, point)
        root.getIndexAt(local_position.y())
        list_id = root.property("externalDropListId") or "root"
        value = root.property("externalDropIndex")
        insert_index = value if isinstance(value, int) and value >= 0 else 2**31 - 1
        root.clearExternalDropPreview()
        return str(list_id), insert_index
