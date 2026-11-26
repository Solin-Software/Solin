"""Generic bounded command idempotency and optimistic-concurrency gate."""

from __future__ import annotations

from _thread import RLock as ReentrantLock
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Generic, Protocol, TypeVar

from .validation import JsonObject


class WireCommand(Protocol):
    @property
    def command_id(self) -> str: ...

    def to_dict(self) -> JsonObject: ...


class WireCommandResult(Protocol):
    @property
    def command_id(self) -> str: ...


CommandT = TypeVar("CommandT", bound=WireCommand)
ErrorT = TypeVar("ErrorT")
ResultT = TypeVar("ResultT", bound=WireCommandResult)


class CommandDisposition(StrEnum):
    ACCEPTED = "accepted"
    REPLAYED = "replayed"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True, slots=True)
class CommandDecision(Generic[ResultT]):
    disposition: CommandDisposition
    result: ResultT | None = None

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
class _CommandRecord(Generic[ResultT]):
    fingerprint: str
    result: ResultT | None = None


class CommandIdempotencyGate(Generic[CommandT, ErrorT, ResultT]):
    """Bounded idempotency and optimistic-concurrency gate for remote commands."""

    def __init__(
        self,
        *,
        command_base_type: type[CommandT],
        error_type: type[ErrorT],
        validate_preconditions: Callable[[CommandT], ErrorT | None],
        build_success: Callable[[CommandT], ResultT],
        build_failure: Callable[[CommandT, ErrorT], ResultT],
        conflict_error: Callable[[], ErrorT],
        in_progress_error: Callable[[], ErrorT],
        queue_full_error: Callable[[], ErrorT],
        deduplication_limit: int = 512,
    ) -> None:
        if isinstance(deduplication_limit, bool) or not isinstance(deduplication_limit, int):
            raise TypeError("deduplication_limit must be an integer")
        if deduplication_limit < 1:
            raise ValueError("deduplication_limit must be at least one")
        self._command_base_type = command_base_type
        self._error_type = error_type
        self._validate_preconditions = validate_preconditions
        self._build_success = build_success
        self._build_failure = build_failure
        self._conflict_error = conflict_error
        self._in_progress_error = in_progress_error
        self._queue_full_error = queue_full_error
        self._limit = deduplication_limit
        self._records: OrderedDict[str, _CommandRecord[ResultT]] = OrderedDict()
        self._lock = ReentrantLock()

    @property
    def cached_command_count(self) -> int:
        with self._lock:
            return len(self._records)

    def prepare(self, command: CommandT) -> CommandDecision[ResultT]:
        self._require_command(command)
        fingerprint = _command_fingerprint(command)
        with self._lock:
            existing = self._records.get(command.command_id)
            if existing is not None:
                self._records.move_to_end(command.command_id)
                if existing.fingerprint != fingerprint:
                    return CommandDecision(
                        CommandDisposition.REJECTED,
                        self._build_failure(command, self._conflict_error()),
                    )
                if existing.result is None:
                    return CommandDecision(
                        CommandDisposition.IN_PROGRESS,
                        self._build_failure(command, self._in_progress_error()),
                    )
                return CommandDecision(CommandDisposition.REPLAYED, existing.result)

            if not self._make_room():
                return CommandDecision(
                    CommandDisposition.REJECTED,
                    self._build_failure(command, self._queue_full_error()),
                )

            record: _CommandRecord[ResultT] = _CommandRecord(fingerprint=fingerprint)
            self._records[command.command_id] = record
            precondition_error = self._validate_preconditions(command)
            if precondition_error is not None:
                record.result = self._build_failure(command, precondition_error)
                return CommandDecision(CommandDisposition.REJECTED, record.result)
            return CommandDecision(CommandDisposition.ACCEPTED)

    def complete_success(self, command: CommandT, *, result: ResultT | None = None) -> ResultT:
        self._require_command(command)
        return self._complete(command, self._build_success(command) if result is None else result)

    def complete_failure(self, command: CommandT, error: ErrorT) -> ResultT:
        self._require_command(command)
        if not isinstance(error, self._error_type):
            raise TypeError(f"error must be a {self._error_type.__name__}")
        return self._complete(command, self._build_failure(command, error))

    def _complete(self, command: CommandT, result: ResultT) -> ResultT:
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

    def _require_command(self, command: object) -> None:
        if not isinstance(command, self._command_base_type):
            raise TypeError(f"command must be a {self._command_base_type.__name__}")

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


def _command_fingerprint(command: WireCommand) -> str:
    payload = json.dumps(
        command.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
