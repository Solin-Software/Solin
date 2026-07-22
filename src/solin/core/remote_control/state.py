"""Authoritative, versioned runtime state for local remote control."""

from __future__ import annotations

from _thread import RLock as ReentrantLock
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import json
import uuid

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
    """Single source of truth for catalog and playback wire snapshots.

    Revisions are owned by this store.  Callers provide snapshot contents and
    cannot reset or skip a revision.  Publishing identical contents is a no-op,
    which prevents spurious reconnect work for every remote client.
    """

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
        command: RemoteCommand,
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


class CommandDisposition(StrEnum):
    ACCEPTED = "accepted"
    REPLAYED = "replayed"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True, slots=True)
class CommandDecision:
    disposition: CommandDisposition
    result: CommandResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, CommandDisposition):
            raise TypeError("disposition must be a CommandDisposition")
        if self.disposition is CommandDisposition.ACCEPTED:
            if self.result is not None:
                raise ValueError("accepted command decisions cannot have an immediate result")
        elif self.result is None:
            raise ValueError("non-accepted command decisions require a result")

    @property
    def should_execute(self) -> bool:
        return self.disposition is CommandDisposition.ACCEPTED


@dataclass(slots=True)
class _CommandRecord:
    fingerprint: str
    result: CommandResult | None = None


class ProjectionCommandSession:
    """Bounded idempotency and optimistic-concurrency gate for commands.

    ``prepare`` reserves a new command ID before it can be dispatched.  The
    caller must then use ``complete_success`` or ``complete_failure`` after the
    main-thread action finishes.  Duplicate retries replay the exact cached
    result and can therefore never execute the action twice.
    """

    def __init__(self, state: RemoteControlStateStore, *, deduplication_limit: int = 512) -> None:
        if not isinstance(state, RemoteControlStateStore):
            raise TypeError("state must be a RemoteControlStateStore")
        if isinstance(deduplication_limit, bool) or not isinstance(deduplication_limit, int):
            raise TypeError("deduplication_limit must be an integer")
        if deduplication_limit < 1:
            raise ValueError("deduplication_limit must be at least one")
        self._state = state
        self._limit = deduplication_limit
        self._records: OrderedDict[str, _CommandRecord] = OrderedDict()
        self._lock = ReentrantLock()

    @property
    def cached_command_count(self) -> int:
        with self._lock:
            return len(self._records)

    def prepare(self, command: RemoteCommand) -> CommandDecision:
        if not isinstance(command, ProjectionCommand):
            raise TypeError("command must be a ProjectionCommand")
        fingerprint = _command_fingerprint(command)
        with self._lock:
            existing = self._records.get(command.command_id)
            if existing is not None:
                self._records.move_to_end(command.command_id)
                if existing.fingerprint != fingerprint:
                    return CommandDecision(
                        CommandDisposition.REJECTED,
                        self._failure_result(
                            command,
                            CommandError(
                                CommandErrorCode.COMMAND_ID_CONFLICT,
                                "This command ID was already used for a different command.",
                            ),
                        ),
                    )
                if existing.result is None:
                    return CommandDecision(
                        CommandDisposition.IN_PROGRESS,
                        self._failure_result(
                            command,
                            CommandError(
                                CommandErrorCode.COMMAND_IN_PROGRESS,
                                "This command is still in progress.",
                                retryable=True,
                            ),
                        ),
                    )
                return CommandDecision(CommandDisposition.REPLAYED, existing.result)

            if not self._make_room():
                return CommandDecision(
                    CommandDisposition.REJECTED,
                    self._failure_result(
                        command,
                        CommandError(
                            CommandErrorCode.UNAVAILABLE,
                            "The command queue is temporarily full.",
                            retryable=True,
                        ),
                    ),
                )

            record = _CommandRecord(fingerprint=fingerprint)
            self._records[command.command_id] = record
            precondition_error = self._state.validate_command_preconditions(command)
            if precondition_error is not None:
                record.result = self._failure_result(command, precondition_error)
                return CommandDecision(CommandDisposition.REJECTED, record.result)
            return CommandDecision(CommandDisposition.ACCEPTED)

    def complete_success(self, command: RemoteCommand) -> CommandResult:
        snapshot = self._state.snapshot()
        result = CommandResult.success(
            command.command_id,
            catalog_revision=snapshot.catalog_revision,
            playback_revision=snapshot.playback_revision,
        )
        return self._complete(command, result)

    def complete_failure(self, command: RemoteCommand, error: CommandError) -> CommandResult:
        if not isinstance(error, CommandError):
            raise TypeError("error must be a CommandError")
        return self._complete(command, self._failure_result(command, error))

    def _complete(self, command: RemoteCommand, result: CommandResult) -> CommandResult:
        if not isinstance(command, ProjectionCommand):
            raise TypeError("command must be a ProjectionCommand")
        if result.command_id != command.command_id:
            raise ValueError("result command_id does not match the command")
        fingerprint = _command_fingerprint(command)
        with self._lock:
            record = self._records.get(command.command_id)
            if record is None or record.fingerprint != fingerprint:
                raise ValueError("command was not reserved by this session")
            if record.result is not None:
                raise ValueError("command was already completed")
            record.result = result
            self._records.move_to_end(command.command_id)
            return result

    def _failure_result(
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

    def _make_room(self) -> bool:
        while len(self._records) >= self._limit:
            completed_id = next(
                (
                    command_id
                    for command_id, record in self._records.items()
                    if record.result is not None
                ),
                None,
            )
            if completed_id is None:
                return False
            del self._records[completed_id]
        return True


def _command_fingerprint(command: ProjectionCommand) -> str:
    payload = json.dumps(
        command.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
