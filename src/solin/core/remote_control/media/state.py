"""Authoritative, versioned runtime state for local media remote control."""

from __future__ import annotations

from _thread import RLock as ReentrantLock
from collections.abc import Iterable
from dataclasses import dataclass, replace
import uuid

from ..command_gate import CommandDecision, CommandDisposition, CommandIdempotencyGate
from .contracts import (
    CatalogCollection,
    CatalogSnapshot,
    CommandError,
    CommandErrorCode,
    CommandResult,
    JsonObject,
    PlayCommand,
    ProjectionCommand,
    ProjectionSnapshot,
    RemoteCommand,
    SessionProjectionCommand,
)


__all__ = [
    "CommandDecision",
    "CommandDisposition",
    "ProjectionCommandSession",
    "RemoteControlRuntimeSnapshot",
    "RemoteControlStateStore",
]


@dataclass(frozen=True, slots=True)
class RemoteControlRuntimeSnapshot:
    server_instance_id: str
    catalog: CatalogSnapshot
    playback: ProjectionSnapshot

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.server_instance_id)
        except (AttributeError, ValueError) as exc:
            raise ValueError("server_instance_id must be a UUID") from exc
        if str(parsed) != self.server_instance_id.lower():
            raise ValueError("server_instance_id must use the canonical UUID representation")
        if not isinstance(self.catalog, CatalogSnapshot):
            raise TypeError("catalog must be a CatalogSnapshot")
        if not isinstance(self.playback, ProjectionSnapshot):
            raise TypeError("playback must be a ProjectionSnapshot")

    @property
    def catalog_revision(self) -> int:
        return self.catalog.catalog_revision

    @property
    def playback_revision(self) -> int:
        return self.playback.playback_revision

    def to_dict(self) -> JsonObject:
        return {
            "serverInstanceId": self.server_instance_id,
            "catalogRevision": self.catalog_revision,
            "playbackRevision": self.playback_revision,
            "catalog": self.catalog.to_dict(),
            "playback": self.playback.to_dict(),
        }


class RemoteControlStateStore:
    """Single source of truth for catalog and playback wire snapshots."""

    def __init__(
        self,
        *,
        server_instance_id: str | None = None,
        collections: Iterable[CatalogCollection] = (),
        playback: ProjectionSnapshot | None = None,
        publication_lock: ReentrantLock | None = None,
    ) -> None:
        self._lock = publication_lock or ReentrantLock()
        self._catalog_change_token: object | None = None
        instance_id = server_instance_id or str(uuid.uuid4())
        initial_catalog = CatalogSnapshot(0, tuple(collections))
        initial_playback = replace(
            playback or ProjectionSnapshot.idle(),
            playback_revision=0,
        )
        self._snapshot = RemoteControlRuntimeSnapshot(
            server_instance_id=instance_id,
            catalog=initial_catalog,
            playback=initial_playback,
        )

    @property
    def server_instance_id(self) -> str:
        return self._snapshot.server_instance_id

    @property
    def catalog_revision(self) -> int:
        with self._lock:
            return self._snapshot.catalog_revision

    @property
    def playback_revision(self) -> int:
        with self._lock:
            return self._snapshot.playback_revision

    @property
    def catalog(self) -> CatalogSnapshot:
        with self._lock:
            return self._snapshot.catalog

    @property
    def playback(self) -> ProjectionSnapshot:
        with self._lock:
            return self._snapshot.playback

    def snapshot(self) -> RemoteControlRuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def rotate_server_instance(self) -> str:
        """Start a new wire event epoch while preserving authoritative snapshots."""
        with self._lock:
            instance_id = str(uuid.uuid4())
            self._snapshot = replace(self._snapshot, server_instance_id=instance_id)
            return instance_id

    def update_catalog(
        self,
        collections: Iterable[CatalogCollection],
        *,
        change_token: object | None = None,
    ) -> CatalogSnapshot:
        candidate_collections = tuple(collections)
        with self._lock:
            current = self._snapshot.catalog
            token_changed = change_token is not None and change_token != self._catalog_change_token
            if candidate_collections == current.collections and not token_changed:
                return current
            updated = CatalogSnapshot(
                catalog_revision=current.catalog_revision + 1,
                collections=candidate_collections,
            )
            self._snapshot = replace(self._snapshot, catalog=updated)
            self._catalog_change_token = change_token
            return updated

    def update_playback(self, playback: ProjectionSnapshot) -> ProjectionSnapshot:
        if not isinstance(playback, ProjectionSnapshot):
            raise TypeError("playback must be a ProjectionSnapshot")
        with self._lock:
            current = self._snapshot.playback
            comparable = replace(playback, playback_revision=current.playback_revision)
            if comparable == current:
                return current
            updated = replace(
                playback,
                playback_revision=current.playback_revision + 1,
            )
            self._snapshot = replace(self._snapshot, playback=updated)
            return updated

    def validate_command_preconditions(
        self,
        command: ProjectionCommand,
    ) -> CommandError | None:
        """Validate optimistic-concurrency fields against one current snapshot."""

        if not isinstance(command, ProjectionCommand):
            raise TypeError("command must be a ProjectionCommand")
        snapshot = self.snapshot()
        if isinstance(command, PlayCommand):
            if command.catalog_revision != snapshot.catalog_revision:
                return CommandError(
                    CommandErrorCode.CATALOG_STALE,
                    "The catalog changed; refresh it before starting playback.",
                    retryable=True,
                )
            return None
        if (
            isinstance(command, SessionProjectionCommand)
            and command.playback_session_id != snapshot.playback.playback_session_id
        ):
            return CommandError(
                CommandErrorCode.PLAYBACK_STALE,
                "The active playback session changed.",
                retryable=True,
            )
        return None


class ProjectionCommandSession:
    """Bounded idempotency and optimistic-concurrency gate for media commands."""

    def __init__(self, state: RemoteControlStateStore, *, deduplication_limit: int = 512) -> None:
        if not isinstance(state, RemoteControlStateStore):
            raise TypeError("state must be a RemoteControlStateStore")
        self._state = state
        self._gate: CommandIdempotencyGate[ProjectionCommand, CommandError, CommandResult] = (
            CommandIdempotencyGate(
                command_base_type=ProjectionCommand,
                error_type=CommandError,
                validate_preconditions=state.validate_command_preconditions,
                build_success=self._build_success,
                build_failure=self._build_failure,
                conflict_error=lambda: CommandError(
                    CommandErrorCode.COMMAND_ID_CONFLICT,
                    "This command ID was already used for a different command.",
                ),
                in_progress_error=lambda: CommandError(
                    CommandErrorCode.COMMAND_IN_PROGRESS,
                    "This command is still in progress.",
                    retryable=True,
                ),
                queue_full_error=lambda: CommandError(
                    CommandErrorCode.UNAVAILABLE,
                    "The command queue is temporarily full.",
                    retryable=True,
                ),
                deduplication_limit=deduplication_limit,
            )
        )

    @property
    def cached_command_count(self) -> int:
        return self._gate.cached_command_count

    def prepare(self, command: RemoteCommand) -> CommandDecision[CommandResult]:
        return self._gate.prepare(command)

    def complete_success(self, command: RemoteCommand) -> CommandResult:
        return self._gate.complete_success(command)

    def complete_failure(self, command: RemoteCommand, error: CommandError) -> CommandResult:
        return self._gate.complete_failure(command, error)

    def _build_success(self, command: ProjectionCommand) -> CommandResult:
        snapshot = self._state.snapshot()
        return CommandResult.success(
            command.command_id,
            catalog_revision=snapshot.catalog_revision,
            playback_revision=snapshot.playback_revision,
        )

    def _build_failure(
        self,
        command: ProjectionCommand,
        error: CommandError,
    ) -> CommandResult:
        snapshot = self._state.snapshot()
        return CommandResult.failure(
            command.command_id,
            catalog_revision=snapshot.catalog_revision,
            playback_revision=snapshot.playback_revision,
            error=error,
        )
