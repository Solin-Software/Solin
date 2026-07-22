"""Asynchronous presentation probes with stale-result rejection and retry."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import uuid

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage, QImageReader

from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.core.foundation.resource_keys import file_resource_key
from solin.core.media.operations import MediaOperationPresentation, MediaOperationSpec
from solin.core.media.presentation_probe import (
    MediaPresentationProbeRequest,
    MediaPresentationProbeResult,
    ProbedMediaAvailability,
    probe_media_presentation,
)
from solin.ui.qml.media_tree.state import (
    MediaAvailability,
    MediaPresentationState,
    MediaProbeKey,
    MediaProbeResult,
    MediaStateRegistry,
)


_RETRY_DELAYS_MS = (250, 500, 1_000, 2_000, 5_000, 10_000, 30_000)
_MISSING_GRACE_ATTEMPTS = 5
_THUMBNAIL_DECODE_GRACE_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class _ProbeIntent:
    owner_id: str
    node_id: str
    source: str
    thumbnail_path: Path | None
    thumbnail_source: str
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class _ProbePayload:
    result: MediaPresentationProbeResult
    thumbnail: QImage | None = None
    image_aspect_ratio: float = 0.0


class MediaProbeController(QObject):
    """Populate a MediaStateRegistry without running filesystem I/O on Qt."""

    thumbnailReady = Signal(str, str, object)

    def __init__(
        self,
        *,
        coordinator: MediaOperationCoordinator,
        registry: MediaStateRegistry,
        media_cache_dir: str | Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._coordinator = coordinator
        self._registry = registry
        self._media_cache_dir = Path(media_cache_dir)
        self._intents: dict[tuple[str, str], _ProbeIntent] = {}
        self._operation_ids: dict[tuple[str, str], str] = {}
        self._retry_timers: dict[tuple[str, str], QTimer] = {}
        self._closing = False

    def request(
        self,
        *,
        owner_id: str,
        node_id: str,
        source: str,
        thumbnail_path: str | Path | None = None,
        thumbnail_source: str = "",
    ) -> MediaProbeKey | None:
        if self._closing:
            return None
        identity = (owner_id, node_id)
        previous = self._intents.get(identity)
        attempt = (
            previous.attempt
            if previous is not None
            and previous.source == source
            and previous.thumbnail_path == (Path(thumbnail_path) if thumbnail_path else None)
            else 0
        )
        intent = _ProbeIntent(
            owner_id=owner_id,
            node_id=node_id,
            source=source,
            thumbnail_path=Path(thumbnail_path) if thumbnail_path else None,
            thumbnail_source=thumbnail_source,
            attempt=attempt,
        )
        self._intents[identity] = intent
        self._stop_retry(identity)
        active_operation = self._operation_ids.get(identity)
        if active_operation:
            self._coordinator.cancel(active_operation)
        return self._submit(intent)

    def remove(self, owner_id: str, node_id: str) -> None:
        identity = (owner_id, node_id)
        self._intents.pop(identity, None)
        self._stop_retry(identity)
        operation_id = self._operation_ids.pop(identity, "")
        if operation_id:
            self._coordinator.cancel(operation_id)
        self._registry.remove(owner_id, node_id)

    def clear_owner(self, owner_id: str) -> None:
        identities = [identity for identity in self._intents if identity[0] == owner_id]
        for _owner_id, node_id in identities:
            self.remove(owner_id, node_id)

    def shutdown(self) -> None:
        self._closing = True
        for timer in self._retry_timers.values():
            timer.stop()
        self._retry_timers.clear()
        for operation_id in tuple(self._operation_ids.values()):
            self._coordinator.cancel(operation_id)
        self._operation_ids.clear()
        self._intents.clear()

    def _submit(self, intent: _ProbeIntent) -> MediaProbeKey | None:
        key = self._registry.begin_probe(
            intent.owner_id,
            intent.node_id,
            intent.source,
        )
        operation_id = f"probe:{uuid.uuid4().hex}"
        identity = (intent.owner_id, intent.node_id)
        self._operation_ids[identity] = operation_id
        request = MediaPresentationProbeRequest(
            source=intent.source,
            media_cache_dir=self._media_cache_dir,
            thumbnail_path=intent.thumbnail_path,
        )

        def run(_progress, cancellation):
            if cancellation.is_set():
                return None
            result = probe_media_presentation(request)
            if cancellation.is_set():
                return None
            thumbnail = _read_thumbnail(request.thumbnail_path)
            aspect_ratio = _image_aspect_ratio(result.local_path)
            return _ProbePayload(result, thumbnail, aspect_ratio)

        def commit(value: object) -> None:
            if not isinstance(value, _ProbePayload):
                return
            if self._operation_ids.get(identity) != operation_id:
                return
            self._operation_ids.pop(identity, None)
            result = value.result
            thumbnail_matches_source = bool(
                not result.source_signature
                or result.thumbnail_source_signature == result.source_signature
            )
            thumbnail_decoded = value.thumbnail is not None
            thumbnail_usable = (
                result.thumbnail_exists
                and thumbnail_decoded
                and thumbnail_matches_source
            )
            thumbnail_temporarily_unreadable = (
                result.thumbnail_exists
                and not thumbnail_decoded
                and thumbnail_matches_source
                and intent.attempt < _THUMBNAIL_DECODE_GRACE_ATTEMPTS
            )
            if (
                result.availability == ProbedMediaAvailability.MISSING
                and not intent.source.startswith(("http://", "https://"))
                and intent.attempt < _MISSING_GRACE_ATTEMPTS
            ):
                result = replace(
                    result,
                    availability=ProbedMediaAvailability.TEMPORARILY_UNAVAILABLE,
                    error=result.error or "Local media is still stabilizing",
                )
            accepted = self._registry.accept(
                MediaProbeResult(
                    key,
                    _presentation_state(
                        result,
                        intent.thumbnail_source,
                        value.image_aspect_ratio,
                        thumbnail_usable=thumbnail_usable,
                    ),
                )
            )
            if accepted:
                self.thumbnailReady.emit(
                    intent.owner_id,
                    intent.node_id,
                    value.thumbnail if thumbnail_usable else None,
                )
            if (
                accepted
                and (
                    result.availability
                    == ProbedMediaAvailability.TEMPORARILY_UNAVAILABLE
                    or thumbnail_temporarily_unreadable
                )
            ):
                self._schedule_retry(intent)
            elif accepted:
                self._intents[identity] = _ProbeIntent(
                    owner_id=intent.owner_id,
                    node_id=intent.node_id,
                    source=intent.source,
                    thumbnail_path=intent.thumbnail_path,
                    thumbnail_source=intent.thumbnail_source,
                )

        def finished_without_result() -> None:
            if self._operation_ids.get(identity) == operation_id:
                self._operation_ids.pop(identity, None)

        spec = MediaOperationSpec(
            operation_id=operation_id,
            scope_id=intent.owner_id,
            operation_type="presentation_probe",
            conflict_key=(
                file_resource_key(intent.thumbnail_path)
                if intent.thumbnail_path is not None
                else f"probe:{_source_key(intent.source)}"
            ),
            presentation=MediaOperationPresentation.BACKGROUND,
            runner=run,
            commit=commit,
            priority=-100,
            failed=lambda _message, _retryable: finished_without_result(),
            cancelled=finished_without_result,
        )
        if not self._coordinator.submit(spec):
            self._operation_ids.pop(identity, None)
            return None
        return key

    def _schedule_retry(self, intent: _ProbeIntent) -> None:
        identity = (intent.owner_id, intent.node_id)
        current = self._intents.get(identity)
        if current is None or current.source != intent.source or self._closing:
            return
        retry = _ProbeIntent(
            owner_id=intent.owner_id,
            node_id=intent.node_id,
            source=intent.source,
            thumbnail_path=intent.thumbnail_path,
            thumbnail_source=intent.thumbnail_source,
            attempt=intent.attempt + 1,
        )
        self._intents[identity] = retry
        timer = QTimer(self)
        timer.setSingleShot(True)
        base_delay = _RETRY_DELAYS_MS[
            min(intent.attempt, len(_RETRY_DELAYS_MS) - 1)
        ]
        timer.setInterval(_jittered_delay_ms(base_delay, retry))
        timer.timeout.connect(lambda: self._retry(identity, retry, timer))
        self._retry_timers[identity] = timer
        timer.start()

    def _retry(
        self,
        identity: tuple[str, str],
        intent: _ProbeIntent,
        timer: QTimer,
    ) -> None:
        if self._retry_timers.get(identity) is timer:
            self._retry_timers.pop(identity, None)
        timer.deleteLater()
        if self._closing or self._intents.get(identity) != intent:
            return
        self._submit(intent)

    def _stop_retry(self, identity: tuple[str, str]) -> None:
        timer = self._retry_timers.pop(identity, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()


def _presentation_state(
    result: MediaPresentationProbeResult,
    thumbnail_source: str,
    image_aspect_ratio: float = 0.0,
    *,
    thumbnail_usable: bool = False,
) -> MediaPresentationState:
    availability = {
        ProbedMediaAvailability.AVAILABLE: MediaAvailability.AVAILABLE,
        ProbedMediaAvailability.TEMPORARILY_UNAVAILABLE: (
            MediaAvailability.TEMPORARILY_UNAVAILABLE
        ),
        ProbedMediaAvailability.MISSING: MediaAvailability.MISSING,
    }[result.availability]
    return MediaPresentationState(
        availability=availability,
        local_path=result.local_path,
        thumbnail_source=thumbnail_source if thumbnail_usable else "",
        source_signature=result.source_signature,
        cached=result.cached,
        image_aspect_ratio=image_aspect_ratio,
        error=result.error,
    )


def _read_thumbnail(path: Path | None) -> QImage | None:
    if path is None:
        return None
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image = reader.read()
    return image if not image.isNull() else None


def _image_aspect_ratio(path: str) -> float:
    if not path:
        return 0.0
    reader = QImageReader(path)
    size = reader.size()
    if not size.isValid() or size.height() <= 0:
        return 0.0
    return size.width() / size.height()


def _source_key(source: str) -> str:
    if source.startswith(("http://", "https://")):
        return source
    return os.path.normcase(os.path.abspath(source)) if source else "empty"


def _jittered_delay_ms(base_delay: int, intent: _ProbeIntent) -> int:
    identity = (
        f"{intent.owner_id}\0{intent.node_id}\0{intent.source}\0{intent.attempt}"
    ).encode("utf-8", errors="surrogatepass")
    sample = hashlib.blake2s(identity, digest_size=1).digest()[0] / 255
    return max(1, int(round(base_delay * (0.85 + sample * 0.30))))
