"""Generic thread-safe asyncio-to-Qt-main-thread command dispatch bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading
from typing import Generic, TypeVar

from PySide6.QtCore import QObject, Signal, Slot


log = logging.getLogger(__name__)

CommandT = TypeVar("CommandT")
ErrorT = TypeVar("ErrorT")


@dataclass(eq=False, slots=True)
class _CommandEnvelope(Generic[CommandT, ErrorT]):
    command: CommandT
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[ErrorT | None]
    cancelled: threading.Event


class QtCommandBridge(QObject, Generic[CommandT, ErrorT]):
    """Marshal async HTTP commands onto the Qt main thread and await completion."""

    requested = Signal(object)

    def __init__(
        self,
        handler: Callable[[CommandT], ErrorT | None],
        unexpected_error_factory: Callable[[], ErrorT],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._handler = handler
        self._unexpected_error_factory = unexpected_error_factory
        self._lock = threading.Lock()
        self._pending: set[_CommandEnvelope[CommandT, ErrorT]] = set()
        self.requested.connect(self._dispatch)

    async def execute(self, command: CommandT) -> ErrorT | None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ErrorT | None] = loop.create_future()
        envelope = _CommandEnvelope(command, loop, future, threading.Event())
        with self._lock:
            self._pending.add(envelope)
        self.requested.emit(envelope)
        try:
            return await future
        except asyncio.CancelledError:
            envelope.cancelled.set()
            raise
        finally:
            with self._lock:
                self._pending.discard(envelope)

    def abort_pending(self) -> None:
        """Resolve in-flight waiters immediately so server stop never deadlocks on Qt."""

        with self._lock:
            pending = tuple(self._pending)
            self._pending.clear()
        if not pending:
            return
        fallback_error = self._unexpected_error_factory()
        for envelope in pending:
            envelope.cancelled.set()
            self._schedule_completion(envelope, fallback_error)

    @Slot(object)
    def _dispatch(self, envelope: _CommandEnvelope[CommandT, ErrorT]) -> None:
        if envelope.cancelled.is_set():
            return
        try:
            result = self._handler(envelope.command)
        except Exception:  # noqa: BLE001 - Qt command boundary
            log.exception("Remote-control command dispatch failed")
            result = self._unexpected_error_factory()
        self._schedule_completion(envelope, result)

    @staticmethod
    def _schedule_completion(
        envelope: _CommandEnvelope[CommandT, ErrorT],
        result: ErrorT | None,
    ) -> None:
        def complete() -> None:
            if not envelope.future.done():
                envelope.future.set_result(result)

        try:
            envelope.loop.call_soon_threadsafe(complete)
        except RuntimeError:
            return
