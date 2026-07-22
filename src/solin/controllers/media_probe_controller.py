"""Asynchronous presentation probes with stale-result rejection and retry."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import uuid

from PySide6.QtCore import QObject, QTimer

from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
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


@dataclass(frozen=True, slots=True)
class _ProbeIntent:
    owner_id: str
    node_id: str
    source: str
    thumbnail_path: Path | None
    thumbnail_source: str
    attempt: int = 0


class MediaProbeController(QObject):
    """Populate a MediaStateRegistry without running filesystem I/O on Qt."""

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
            return probe_media_presentation(request)

        def commit(value: object) -> None:
            if not isinstance(value, MediaPresentationProbeResult):
                return
            if self._operation_ids.get(identity) != operation_id:
                return
            self._operation_ids.pop(identity, None)
            accepted = self._registry.accept(
                MediaProbeResult(
                    key,
                    _presentation_state(value, intent.thumbnail_source),
                )
            )
            if accepted and value.availability == ProbedMediaAvailability.TEMPORARILY_UNAVAILABLE:
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
            conflict_key=f"probe:{_source_key(intent.source)}",
            presentation=MediaOperationPresentation.BACKGROUND,
            runner=run,
            commit=commit,
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
        timer.setInterval(_RETRY_DELAYS_MS[min(intent.attempt, len(_RETRY_DELAYS_MS) - 1)])
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
        thumbnail_source=thumbnail_source if result.thumbnail_exists else "",
        cached=result.cached,
        error=result.error,
    )


def _source_key(source: str) -> str:
    if source.startswith(("http://", "https://")):
        return source
    return os.path.normcase(os.path.abspath(source)) if source else "empty"
