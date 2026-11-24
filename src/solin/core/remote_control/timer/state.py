"""Authoritative, versioned runtime state for local meeting-timer remote control."""

from __future__ import annotations

from _thread import RLock as ReentrantLock
from dataclasses import dataclass, replace
import time
import uuid

from ...timer.hand_alert import TimerHandAlert
from ..command_gate import CommandDecision, CommandDisposition, CommandIdempotencyGate
from ..features import TIMER_REMOTE_HAND_ALERT_POLICY, TimerRemoteHandAlertPolicy
from ..validation import JsonObject
from .contracts import (
    AdjustPartCommand,
    CreateCustomScheduleCommand,
    EditCustomScheduleCommand,
    DeleteCustomScheduleCommand,
    DuplicateCustomScheduleCommand,
    SelectCustomScheduleCommand,
    SelectMeetingScheduleCommand,
    RenamePartCommand,
    ResetAllCommand,
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
    hand_alert_payload,
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
    # Public revisions track only changes a public viewer can observe.
    public_live_revision: int = 0
    hand_alert: TimerHandAlert = TimerHandAlert()

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
        """Serialize timer state without audience-specific hand alert data."""

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
        hand_alert_policy: TimerRemoteHandAlertPolicy = TIMER_REMOTE_HAND_ALERT_POLICY,
    ) -> None:
        if not isinstance(hand_alert_policy, TimerRemoteHandAlertPolicy):
            raise TypeError("hand_alert_policy must be a TimerRemoteHandAlertPolicy")
        self._lock = publication_lock or ReentrantLock()
        self._hand_alert_policy = hand_alert_policy
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
    def hand_alert_policy(self) -> TimerRemoteHandAlertPolicy:
        return self._hand_alert_policy

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
            return self.update_snapshots(self._snapshot.schedule, live).live

    def update_hand_alert(self, alert: TimerHandAlert) -> bool:
        """Update the live alert and its audience revisions in one publication."""

        policy = self._hand_alert_policy
        if policy is TimerRemoteHandAlertPolicy.DISABLED:
            return False
        if alert.names and alert.manual:
            alert = TimerHandAlert(names=alert.names)
        with self._lock:
            current = self._snapshot
            if alert == current.hand_alert:
                return False
            public_changed = hand_alert_payload(
                alert, policy, authenticated=False
            ) != hand_alert_payload(current.hand_alert, policy, authenticated=False)
            self._snapshot = replace(
                current,
                hand_alert=alert,
                live=replace(current.live, live_revision=current.live_revision + 1),
                public_live_revision=current.public_live_revision + int(public_changed),
            )
            return True

    def update_schedule(self, schedule: TimerScheduleWireSnapshot) -> TimerScheduleWireSnapshot:
        if not isinstance(schedule, TimerScheduleWireSnapshot):
            raise TypeError("schedule must be a TimerScheduleWireSnapshot")
        with self._lock:
            return self.update_snapshots(schedule, self._snapshot.live).schedule

    def update_snapshots(
        self,
        schedule: TimerScheduleWireSnapshot,
        live: TimerLiveWireSnapshot,
    ) -> RemoteTimerRuntimeSnapshot:
        """Publish schedule and live state atomically."""
        if not isinstance(schedule, TimerScheduleWireSnapshot):
            raise TypeError("schedule must be a TimerScheduleWireSnapshot")
        if not isinstance(live, TimerLiveWireSnapshot):
            raise TypeError("live must be a TimerLiveWireSnapshot")
        with self._lock:
            current = self._snapshot
            schedule_changed = (
                replace(schedule, schedule_revision=current.schedule_revision) != current.schedule
            )
            live_changed = live.comparable_key() != current.live.comparable_key()
            if not schedule_changed and not live_changed:
                return current
            updated = replace(
                current,
                schedule=replace(schedule, schedule_revision=current.schedule_revision + 1)
                if schedule_changed
                else current.schedule,
                live=replace(live, live_revision=current.live_revision + 1)
                if live_changed
                else current.live,
                public_live_revision=current.public_live_revision + int(live_changed),
            )
            self._snapshot = updated
            return updated

    def validate_command_preconditions(
        self,
        command: TimerCommand,
    ) -> TimerCommandError | None:
        if not isinstance(command, TimerCommand):
            raise TypeError("command must be a TimerCommand")
        snapshot = self.snapshot()
        if (
            snapshot.schedule.custom_schedule is not None
            and not snapshot.schedule.capabilities.can_manage_custom_schedules
        ):
            return TimerCommandError(
                TimerCommandErrorCode.BLOCKED,
                "Custom timer control is disabled by policy.",
            )
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
                CreateCustomScheduleCommand,
                EditCustomScheduleCommand,
                DeleteCustomScheduleCommand,
                DuplicateCustomScheduleCommand,
                SelectCustomScheduleCommand,
                SelectMeetingScheduleCommand,
                RenamePartCommand,
                ResetAllCommand,
            ),
        ):
            if command.schedule_revision != snapshot.schedule_revision:
                return TimerCommandError(
                    TimerCommandErrorCode.SCHEDULE_STALE,
                    "The timer schedule changed; refresh before retrying.",
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

    def complete_success(
        self, command: TimerRemoteCommand, *, created_schedule_id: str | None = None,
    ) -> TimerCommandResult:
        result = self._build_success(command)
        if created_schedule_id is not None:
            result = replace(result, created_schedule_id=created_schedule_id)
        return self._gate.complete_success(command, result=result)

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
