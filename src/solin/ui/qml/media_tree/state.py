"""In-memory presentation state populated by asynchronous media probes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math

from PySide6.QtCore import QObject, Signal


class MediaAvailability(StrEnum):
    UNKNOWN = "unknown"
    CHECKING = "checking"
    AVAILABLE = "available"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    MISSING = "missing"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class MediaProbeKey:
    owner_id: str
    node_id: str
    source: str
    purpose: str
    generation: int


@dataclass(frozen=True, slots=True)
class MediaPresentationState:
    availability: MediaAvailability = MediaAvailability.UNKNOWN
    local_path: str = ""
    thumbnail_source: str = ""
    source_signature: str = ""
    cached: bool = False
    cloud_progress: float = -1.0
    duration_ticks: int = 0
    image_aspect_ratio: float = 0.0
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.availability, MediaAvailability):
            raise TypeError("availability must be a MediaAvailability")
        for value in (
            self.local_path,
            self.thumbnail_source,
            self.source_signature,
            self.error,
        ):
            if not isinstance(value, str):
                raise TypeError("Media presentation paths and errors must be strings")
        if not isinstance(self.cached, bool):
            raise TypeError("cached must be a boolean")
        if (
            not isinstance(self.cloud_progress, (int, float))
            or isinstance(self.cloud_progress, bool)
            or not math.isfinite(float(self.cloud_progress))
            or not -1.0 <= float(self.cloud_progress) <= 1.0
        ):
            raise ValueError("cloud_progress must be between -1 and 1")
        if (
            not isinstance(self.duration_ticks, int)
            or isinstance(self.duration_ticks, bool)
            or self.duration_ticks < 0
        ):
            raise ValueError("duration_ticks must be a non-negative integer")
        if (
            not isinstance(self.image_aspect_ratio, (int, float))
            or isinstance(self.image_aspect_ratio, bool)
            or not math.isfinite(float(self.image_aspect_ratio))
            or self.image_aspect_ratio < 0
        ):
            raise ValueError("image_aspect_ratio must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class MediaProbeResult:
    key: MediaProbeKey
    state: MediaPresentationState


class MediaStateRegistry(QObject):
    """Reject stale probe completions and expose only memory-backed state."""

    stateChanged = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._generations: dict[tuple[str, str, str], int] = {}
        self._keys: dict[tuple[str, str, str], MediaProbeKey] = {}
        self._states: dict[tuple[str, str], MediaPresentationState] = {}

    def begin_probe(
        self,
        owner_id: str,
        node_id: str,
        source: str,
        *,
        purpose: str = "presentation",
    ) -> MediaProbeKey:
        if not owner_id or not node_id or not purpose:
            raise ValueError("Probe owner, node, and purpose must not be empty")
        identity = (owner_id, node_id, purpose)
        previous_key = self._keys.get(identity)
        source_changed = previous_key is not None and previous_key.source != source
        generation = self._generations.get(identity, 0) + 1
        self._generations[identity] = generation
        key = MediaProbeKey(owner_id, node_id, source, purpose, generation)
        self._keys[identity] = key
        state_key = (owner_id, node_id)
        previous = self._states.get(state_key)
        checking = MediaPresentationState(
            availability=MediaAvailability.CHECKING,
            local_path=previous.local_path if previous and not source_changed else "",
            thumbnail_source=(
                previous.thumbnail_source if previous and not source_changed else ""
            ),
            source_signature=(
                previous.source_signature if previous and not source_changed else ""
            ),
            cached=previous.cached if previous and not source_changed else False,
            cloud_progress=-1.0,
            duration_ticks=(
                previous.duration_ticks if previous and not source_changed else 0
            ),
            image_aspect_ratio=(
                previous.image_aspect_ratio if previous and not source_changed else 0.0
            ),
        )
        self._states[state_key] = checking
        self.stateChanged.emit(owner_id, node_id)
        return key

    def accept(self, result: MediaProbeResult) -> bool:
        key = result.key
        identity = (key.owner_id, key.node_id, key.purpose)
        if self._keys.get(identity) != key:
            return False
        state_key = (key.owner_id, key.node_id)
        if self._states.get(state_key) == result.state:
            return True
        self._states[state_key] = result.state
        self.stateChanged.emit(key.owner_id, key.node_id)
        return True

    def state(self, owner_id: str, node_id: str) -> MediaPresentationState:
        return self._states.get((owner_id, node_id), MediaPresentationState())

    def patch(
        self,
        owner_id: str,
        node_id: str,
        **changes: object,
    ) -> MediaPresentationState:
        """Merge UI-known progress into memory without probing the filesystem."""

        state_key = (owner_id, node_id)
        current = self._states.get(state_key, MediaPresentationState())
        allowed = {
            "availability",
            "local_path",
            "thumbnail_source",
            "source_signature",
            "cached",
            "cloud_progress",
            "duration_ticks",
            "image_aspect_ratio",
            "error",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unknown media presentation fields: {sorted(unknown)}")
        updated = replace(current, **changes)
        if updated != current:
            self._states[state_key] = updated
            self.stateChanged.emit(owner_id, node_id)
        return updated

    def remove(self, owner_id: str, node_id: str) -> None:
        identities = [
            identity
            for identity in self._keys
            if identity[0] == owner_id and identity[1] == node_id
        ]
        for identity in identities:
            self._keys.pop(identity, None)
            self._generations.pop(identity, None)
        if self._states.pop((owner_id, node_id), None) is not None:
            self.stateChanged.emit(owner_id, node_id)

    def clear_owner(self, owner_id: str) -> None:
        node_ids = {
            node_id
            for current_owner, node_id in self._states
            if current_owner == owner_id
        }
        for node_id in node_ids:
            self.remove(owner_id, node_id)
