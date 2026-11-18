"""Authoritative, versioned runtime state for local meeting-timer remote control."""

from __future__ import annotations

from _thread import RLock as ReentrantLock
from dataclasses import dataclass, replace
import time
import uuid

from ..command_gate import CommandDecision, CommandDisposition, CommandIdempotencyGate
from ..validation import JsonObject
from .contracts import (
    AdjustPartCommand,
    ResetPartCommand,
    SetPartSecondsCommand,
    SetSectionCountCommand,
    StartPartCommand,
    TimerCommand,
    TimerCommandError,
    TimerCommandErrorCode,
    TimerCommandResult,
    TimerLiveWireSnapshot,
    TimerRemoteCommand,
    TimerScheduleWireSnapshot,
)


__all__ = [
    "CommandDecision",
    "CommandDisposition",
    "RemoteTimerRuntimeSnapshot",
    "RemoteTimerStateStore",
    "TimerCommandSession",
]


@dataclass(frozen=True, slots=True)
class RemoteTimerRuntimeSnapshot:
    server_instance_id: str
    live: TimerLiveWireSnapshot
    schedule: TimerScheduleWireSnapshot

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.server_instance_id)
        except (AttributeError, ValueError) as exc:
            raise ValueError("server_instance_id must be a UUID") from exc
        if str(parsed) != self.server_instance_id.lower():
            raise ValueError("server_instance_id must use the canonical UUID representation")
        if not isinstance(self.live, TimerLiveWireSnapshot):
            raise TypeError("live must be a TimerLiveWireSnapshot")
        if not isinstance(self.schedule, TimerScheduleWireSnapshot):
            raise TypeError("schedule must be a TimerScheduleWireSnapshot")

    @property
    def live_revision(self) -> int:
        return self.live.live_revision

    @property
    def schedule_revision(self) -> int:
        return self.schedule.schedule_revision

    def to_dict(self) -> JsonObject:
        return {
            "serverInstanceId": self.server_instance_id,
            "liveRevision": self.live_revision,
            "scheduleRevision": self.schedule_revision,
            "live": self.live.to_dict(),
            "schedule": self.schedule.to_dict(),
        }


class RemoteTimerStateStore:
    """Single source of truth for live timer and schedule wire snapshots."""

    def __init__(
        self,
        *,
        server_instance_id: str | None = None,
        live: TimerLiveWireSnapshot | None = None,
        schedule: TimerScheduleWireSnapshot | None = None,
        publication_lock: ReentrantLock | None = None,
    ) -> None:
        self._lock = publication_lock or ReentrantLock()
        instance_id = server_instance_id or str(uuid.uuid4())
        initial_live = replace(
            live
            or TimerLiveWireSnapshot.idle(
                server_epoch_ms=int(time.time() * 1000),
            ),
            live_revision=0,
        )
        initial_schedule = replace(
            schedule or TimerScheduleWireSnapshot.empty(),
            schedule_revision=0,
        )
        self._snapshot = RemoteTimerRuntimeSnapshot(
            server_instance_id=instance_id,
            live=initial_live,
            schedule=initial_schedule,
        )

    @property
    def server_instance_id(self) -> str:
        return self._snapshot.server_instance_id

    @property
    def live_revision(self) -> int:
        with self._lock:
            return self._snapshot.live_revision

    @property
    def schedule_revision(self) -> int:
        with self._lock:
            return self._snapshot.schedule_revision

    @property
    def live(self) -> TimerLiveWireSnapshot:
        with self._lock:
            return self._snapshot.live

    @property
    def schedule(self) -> TimerScheduleWireSnapshot:
        with self._lock:
            return self._snapshot.schedule

    def snapshot(self, *, now_epoch_ms: int | None = None) -> RemoteTimerRuntimeSnapshot:
        with self._lock:
            current = self._snapshot
        if now_epoch_ms is None:
            return current
        return replace(current, live=current.live.rebased_at(now_epoch_ms))

    def rotate_server_instance(self) -> str:
        with self._lock:
            instance_id = str(uuid.uuid4())
            self._snapshot = replace(self._snapshot, server_instance_id=instance_id)
            return instance_id

    def update_live(self, live: TimerLiveWireSnapshot) -> TimerLiveWireSnapshot:
        if not isinstance(live, TimerLiveWireSnapshot):
            raise TypeError("live must be a TimerLiveWireSnapshot")
        with self._lock:
            current = self._snapshot.live
            if live.comparable_key() == current.comparable_key():
                return current
            updated = replace(live, live_revision=current.live_revision + 1)
            self._snapshot = replace(self._snapshot, live=updated)
            return updated

    def update_schedule(self, schedule: TimerScheduleWireSnapshot) -> TimerScheduleWireSnapshot:
        if not isinstance(schedule, TimerScheduleWireSnapshot):
            raise TypeError("schedule must be a TimerScheduleWireSnapshot")
        with self._lock:
            current = self._snapshot.schedule
            comparable = replace(schedule, schedule_revision=current.schedule_revision)
            if comparable == current:
                return current
            updated = replace(schedule, schedule_revision=current.schedule_revision + 1)
            self._snapshot = replace(self._snapshot, schedule=updated)
            return updated

    def validate_command_preconditions(
        self,
        command: TimerCommand,
    ) -> TimerCommandError | None:
        if not isinstance(command, TimerCommand):
            raise TypeError("command must be a TimerCommand")
        snapshot = self.snapshot()
        if not snapshot.schedule.capabilities.allows_command(command):
            return TimerCommandError(
                TimerCommandErrorCode.BLOCKED,
                "This timer control action is disabled by policy.",
            )
        if isinstance(
            command,
            (
                StartPartCommand,
                ResetPartCommand,
                AdjustPartCommand,
                SetPartSecondsCommand,
                SetSectionCountCommand,
            ),
        ):
            if command.schedule_revision != snapshot.schedule_revision:
                return TimerCommandError(
                    TimerCommandErrorCode.SCHEDULE_STALE,
                    "The meeting schedule changed; refresh before retrying.",
                    retryable=True,
                )
        return None


class TimerCommandSession:
    """Bounded idempotency and optimistic-concurrency gate for timer commands."""

    def __init__(self, state: RemoteTimerStateStore, *, deduplication_limit: int = 512) -> None:
        if not isinstance(state, RemoteTimerStateStore):
            raise TypeError("state must be a RemoteTimerStateStore")
        self._state = state
        self._gate: CommandIdempotencyGate[
            TimerCommand,
            TimerCommandError,
            TimerCommandResult,
        ] = CommandIdempotencyGate(
            command_base_type=TimerCommand,
            error_type=TimerCommandError,
            validate_preconditions=state.validate_command_preconditions,
            build_success=self._build_success,
            build_failure=self._build_failure,
            conflict_error=lambda: TimerCommandError(
                TimerCommandErrorCode.COMMAND_ID_CONFLICT,
                "This command ID was already used for a different command.",
            ),
            in_progress_error=lambda: TimerCommandError(
                TimerCommandErrorCode.COMMAND_IN_PROGRESS,
                "This command is still in progress.",
                retryable=True,
            ),
            queue_full_error=lambda: TimerCommandError(
                TimerCommandErrorCode.UNAVAILABLE,
                "The command queue is temporarily full.",
                retryable=True,
            ),
            deduplication_limit=deduplication_limit,
        )

    @property
    def cached_command_count(self) -> int:
        return self._gate.cached_command_count

    def prepare(self, command: TimerRemoteCommand) -> CommandDecision[TimerCommandResult]:
        return self._gate.prepare(command)

    def complete_success(self, command: TimerRemoteCommand) -> TimerCommandResult:
        return self._gate.complete_success(command)

    def complete_failure(
        self,
        command: TimerRemoteCommand,
        error: TimerCommandError,
    ) -> TimerCommandResult:
        return self._gate.complete_failure(command, error)

    def _build_success(self, command: TimerCommand) -> TimerCommandResult:
        snapshot = self._state.snapshot()
        return TimerCommandResult.success(
            command.command_id,
            live_revision=snapshot.live_revision,
            schedule_revision=snapshot.schedule_revision,
        )

    def _build_failure(
        self,
        command: TimerCommand,
        error: TimerCommandError,
    ) -> TimerCommandResult:
        snapshot = self._state.snapshot()
        return TimerCommandResult.failure(
            command.command_id,
            live_revision=snapshot.live_revision,
            schedule_revision=snapshot.schedule_revision,
            error=error,
        )
