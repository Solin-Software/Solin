"""Qt-independent contracts for asynchronous media operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import math

from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.foundation.resource_keys import ResourceClaim


class MediaOperationState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    COPYING = "copying"
    PROCESSING = "processing"
    FINALIZING = "finalizing"
    FAILED = "failed"
    READY = "ready"
    CANCELLED = "cancelled"


class MediaOperationPresentation(StrEnum):
    TREE_LOCAL = "tree_local"
    WINDOW_MODAL = "window_modal"
    BACKGROUND = "background"


class MediaOperationCancelled(RuntimeError):
    """Raised by runners when cooperative cancellation is observed."""


@dataclass(frozen=True, slots=True)
class MediaOperationProgress:
    state: MediaOperationState
    stage: str = ""
    detail: str = ""
    completed: int = 0
    total: int = 0
    cancellable: bool = True

    def __post_init__(self) -> None:
        if self.state not in {
            MediaOperationState.PREPARING,
            MediaOperationState.COPYING,
            MediaOperationState.PROCESSING,
            MediaOperationState.FINALIZING,
        }:
            raise ValueError("Worker progress must describe an active operation state")
        if not isinstance(self.stage, str) or not isinstance(self.detail, str):
            raise TypeError("Operation progress labels must be strings")
        for name, value in (("completed", self.completed), ("total", self.total)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.cancellable, bool):
            raise TypeError("cancellable must be a boolean")

    @property
    def ratio(self) -> float:
        if self.total <= 0:
            return -1.0
        return min(1.0, self.completed / self.total)


ProgressReporter = Callable[[MediaOperationProgress], None]
MediaOperationRunner = Callable[[ProgressReporter, CancellationFlag], object]
MediaOperationCommit = Callable[[object], None]
MediaOperationFailure = Callable[[str, bool], None]
MediaOperationCancellation = Callable[[], None]
MediaOperationDiscard = Callable[[object | None], None]


@dataclass(frozen=True, slots=True)
class MediaOperationSpec:
    operation_id: str
    scope_id: str
    operation_type: str
    conflict_key: str | ResourceClaim
    presentation: MediaOperationPresentation
    runner: MediaOperationRunner
    commit: MediaOperationCommit
    subject_id: str = ""
    priority: int = 0
    initial_stage: str = ""
    retryable: bool = False
    failed: MediaOperationFailure | None = None
    cancelled: MediaOperationCancellation | None = None
    discarded: MediaOperationDiscard | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("operation_id", self.operation_id),
            ("scope_id", self.scope_id),
            ("operation_type", self.operation_type),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.conflict_key, (str, ResourceClaim)):
            raise TypeError("conflict_key must be a string or ResourceClaim")
        if isinstance(self.conflict_key, str) and not self.conflict_key.strip():
            raise ValueError("conflict_key must not be empty")
        if not isinstance(self.presentation, MediaOperationPresentation):
            raise TypeError("presentation must be a MediaOperationPresentation")
        if not isinstance(self.subject_id, str):
            raise TypeError("subject_id must be a string")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise TypeError("priority must be an integer")
        if not callable(self.runner) or not callable(self.commit):
            raise TypeError("runner and commit must be callable")
        if not isinstance(self.initial_stage, str):
            raise TypeError("initial_stage must be a string")


@dataclass(frozen=True, slots=True)
class MediaOperationRecord:
    operation_id: str
    scope_id: str
    operation_type: str
    presentation: MediaOperationPresentation
    state: MediaOperationState
    subject_id: str = ""
    stage: str = ""
    detail: str = ""
    completed: int = 0
    total: int = 0
    cancellable: bool = False
    retryable: bool = False
    error: str = ""

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return -1.0
        ratio = self.completed / self.total
        return min(1.0, max(0.0, ratio)) if math.isfinite(ratio) else -1.0
