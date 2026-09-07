"""Shared, I/O-free media role composition for playlist and meeting trees."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from PySide6.QtCore import QUrl

from solin.core.i18n.strings import (
    tr_offline_download,
    tr_offline_downloading,
    tr_offline_downloading_progress,
)
from solin.core.media.duration import format_effective_duration_ticks
from solin.core.media.operations import MediaOperationRecord, MediaOperationState
from solin.core.projection.idle_media import supports_idle_media_source
from solin.core.projection.image_framing import (
    image_transform_from_record,
    image_transform_to_record,
)
from solin.ui.qml.media_tree.state import MediaAvailability, MediaPresentationState
from solin.ui.qml.media_tree.snapshot import MutableRoleValue


@dataclass(frozen=True, slots=True)
class MediaRoleInput:
    node_id: str
    title: str
    media_type: str
    badge: str
    url: str
    start_trim_ticks: int = 0
    end_trim_ticks: int = 0
    base_duration_ticks: int = 0
    image_framing: dict[str, Any] | None = None


def media_roles(
    media: MediaRoleInput,
    state: MediaPresentationState,
    operation: MediaOperationRecord | None,
    *,
    source_revision: int = 0,
) -> dict[str, MutableRoleValue]:
    """Return every QML role for one media node using only in-memory inputs."""

    remote = media.url.startswith(("http://", "https://"))
    local_available = state.availability == MediaAvailability.AVAILABLE
    trim_source = ""
    if state.local_path:
        trim_source = QUrl.fromLocalFile(os.path.abspath(state.local_path)).toString()
    elif remote:
        trim_source = media.url
    elif local_available and media.url:
        trim_source = QUrl.fromLocalFile(os.path.abspath(media.url)).toString()
    cloud_visible = remote and not state.cached
    cloud_active = cloud_visible and (
        state.availability == MediaAvailability.CHECKING
        or state.cloud_progress >= 0
    )
    cloud_tooltip = ""
    if cloud_visible:
        if cloud_active and state.cloud_progress >= 0:
            cloud_tooltip = tr_offline_downloading_progress(
                int(round(state.cloud_progress * 100))
            )
        elif cloud_active:
            cloud_tooltip = tr_offline_downloading()
        else:
            cloud_tooltip = tr_offline_download()

    base_ticks = media.base_duration_ticks or state.duration_ticks
    operation_state = (
        operation.state.value
        if operation is not None and operation.state != MediaOperationState.CANCELLED
        else MediaOperationState.READY.value
    )
    active = operation_state not in {
        MediaOperationState.READY.value,
        MediaOperationState.FAILED.value,
    }
    operation_message = ""
    if operation is not None:
        operation_message = operation.error or operation.stage or operation.detail
    thumbnail_source = state.thumbnail_source
    if thumbnail_source.startswith("image://") and source_revision > 0:
        thumbnail_source = f"{thumbnail_source}/{source_revision}"
    if not thumbnail_source and media.media_type == "image" and state.local_path:
        thumbnail_source = QUrl.fromLocalFile(
            os.path.abspath(state.local_path)
        ).toString()
    return {
        "title": media.title,
        "mediaType": media.media_type,
        "badge": media.badge,
        "duration": format_effective_duration_ticks(
            base_ticks,
            media.start_trim_ticks,
            media.end_trim_ticks,
        ),
        "thumbSource": thumbnail_source,
        "url": media.url,
        "trimSource": trim_source,
        "trimAvailable": bool(trim_source and not active),
        "cloudVisible": cloud_visible,
        "cloudActive": cloud_active,
        "cloudProgress": float(state.cloud_progress),
        "cloudTooltip": cloud_tooltip,
        "isMissing": state.availability == MediaAvailability.MISSING,
        "startTrimTicks": media.start_trim_ticks,
        "endTrimTicks": media.end_trim_ticks,
        "baseDurationTicks": base_ticks,
        "hasCustomTrim": bool(media.start_trim_ticks or media.end_trim_ticks),
        "imageFraming": image_transform_to_record(
            image_transform_from_record(media.image_framing)
        ),
        "operationId": operation.operation_id if operation else "",
        "operationState": operation_state,
        "operationProgress": operation.progress if operation else -1.0,
        "operationMessage": operation_message,
        "operationCancellable": bool(operation and operation.cancellable),
        "operationRetryable": bool(operation and operation.retryable),
        "canDrag": not active,
        "canEdit": not active,
        "canProject": not active and state.availability == MediaAvailability.AVAILABLE,
        "canRemove": not active or operation_state == MediaOperationState.FAILED.value,
        "canDownload": cloud_visible and not active,
        "canSetAsIdle": bool(
            not active
            and local_available
            and supports_idle_media_source(media.media_type, media.url)
        ),
    }
