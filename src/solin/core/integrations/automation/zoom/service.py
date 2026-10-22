"""Asynchronous Qt facade supervising one serialized Windows automation process."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from solin.core.conference.provider import SequenceResult, StepFailure
from solin.core.foundation.constants import IS_COMPILED, ZOOM_AUTOMATION_ARGUMENT

from .sequences import SEQUENCES
from .protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Request:
    id: int
    command: str


class ZoomService(QObject):
    enabled_changed = Signal(bool)
    connection_changed = Signal(bool)
    busy_changed = Signal(bool)
    operation_finished = Signal(object)
    sequence_progress = Signal(str)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._active = False
        self._connected = False
        self._process: QProcess | None = None
        self._ready = False
        self._buffer = bytearray()
        self._pending: _Request | None = None
        self._next_id = 0
        self._queued_sequence: str | None = None
        self._restart_delay_ms = 1000
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self.refresh)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._timed_out)

    @property
    def is_available(self) -> bool:
        return sys.platform == "win32"

    @property
    def is_enabled(self) -> bool:
        return self._active

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_busy(self) -> bool:
        return self._queued_sequence is not None or bool(
            self._pending and self._pending.command in SEQUENCES
        )

    def start(self) -> None:
        if not self.is_available or self._active:
            return
        self._active = True
        self.enabled_changed.emit(True)
        self._poll.start()
        self.refresh()

    def stop(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        was_active = self._active
        self._active = False
        self._poll.stop()
        process = self._process
        self._retire(cancelled=True)
        if wait and process is not None:
            process.waitForFinished(max(0, min(2000, int(timeout * 1000))))
        if was_active:
            self.enabled_changed.emit(False)

    def cancel(self) -> None:
        if self.is_busy:
            self._retire(cancelled=True)

    def refresh(self) -> None:
        if not self._active or self._pending is not None or self.is_busy:
            return
        if self._process is None:
            self._launch()
        elif self._ready:
            self._send("meeting")

    def request_pre_meeting(self) -> bool:
        return self._request_sequence("pre_meeting")

    def request_post_meeting(self) -> bool:
        return self._request_sequence("post_meeting")

    def _request_sequence(self, sequence: str) -> bool:
        if not self._active or not self._connected or self.is_busy:
            return False
        self._queued_sequence = sequence
        self.busy_changed.emit(True)
        if self._pending is None:
            self._dispatch()
        return True

    def _launch(self) -> None:
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("SOLIN_ZOOM_PARENT_PID", str(os.getpid()))
        environment.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(environment)
        process.readyReadStandardOutput.connect(lambda: self._read(process))
        process.readyReadStandardError.connect(lambda: self._log_stderr(process))
        process.finished.connect(lambda *_: self._exited(process))
        process.errorOccurred.connect(lambda _: self._process_error(process))
        arguments = (
            [ZOOM_AUTOMATION_ARGUMENT]
            if IS_COMPILED
            else ["-m", "solin.core.integrations.automation.zoom.worker"]
        )
        self._process = process
        self._ready = False
        self._buffer.clear()
        self._deadline.start(10000)
        process.start(sys.executable, arguments)

    def _send(self, command: str) -> None:
        if self._process is None or not self._ready:
            return
        self._next_id += 1
        self._pending = _Request(self._next_id, command)
        payload = json.dumps({"id": self._next_id, "command": command}).encode() + b"\n"
        self._deadline.start(35000 if command in SEQUENCES else 3000)
        if self._process.write(payload) != len(payload):
            self._retire(code="worker_failed")

    def _dispatch(self) -> None:
        if self._pending is not None or not self._active:
            return
        if self._queued_sequence:
            sequence = self._queued_sequence
            self._queued_sequence = None
            self._send(sequence)
        else:
            self.refresh()

    def _read(self, process: QProcess) -> None:
        if process is not self._process:
            return
        self._buffer.extend(process.readAllStandardOutput().data())
        try:
            while b"\n" in self._buffer:
                line, _, rest = self._buffer.partition(b"\n")
                self._buffer = bytearray(rest)
                if len(line) > MAX_MESSAGE_BYTES:
                    raise ValueError("Oversized worker message")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Invalid worker message")
                self._receive(message)
            if len(self._buffer) > MAX_MESSAGE_BYTES:
                raise ValueError("Oversized worker message")
        except (ValueError, TypeError, KeyError):
            log.warning("Invalid Zoom worker response", exc_info=True)
            self._retire(code="worker_failed")

    def _receive(self, message: dict) -> None:
        if not self._ready:
            if message != {"ready": PROTOCOL_VERSION}:
                raise ValueError("Unexpected worker handshake")
            self._ready = True
            self._deadline.stop()
            self._dispatch()
            return
        pending = self._pending
        if pending is None or type(message.get("id")) is not int or message["id"] != pending.id:
            raise ValueError("Unexpected response identifier")
        if "step" in message:
            if pending.command not in SEQUENCES or not isinstance(message["step"], str):
                raise ValueError("Unexpected progress")
            self.sequence_progress.emit(message["step"])
            return
        if pending.command == "meeting":
            if "error" in message:
                self._set_connected(False)
            else:
                if type(message.get("connected")) is not bool:
                    raise ValueError("Invalid connection state")
                self._set_connected(message["connected"])
                self._restart_delay_ms = 1000
        else:
            if "error" in message:
                outcome = SequenceResult(
                    pending.command, (StepFailure("meeting", str(message["error"])),)
                )
            else:
                result = message["result"]
                if result["sequence"] != pending.command or type(result["cancelled"]) is not bool:
                    raise ValueError("Invalid operation result")
                failures = result["failed_steps"]
                if not isinstance(failures, list) or len(failures) > 4:
                    raise ValueError("Invalid operation failures")
                if any(
                    not isinstance(f, dict)
                    or not isinstance(f.get("step"), str)
                    or not isinstance(f.get("code"), str)
                    for f in failures
                ):
                    raise ValueError("Invalid operation failure")
                outcome = SequenceResult(
                    pending.command,
                    tuple(StepFailure(f["step"], f["code"]) for f in failures),
                    result["cancelled"],
                )
        self._deadline.stop()
        self._pending = None
        if pending.command in SEQUENCES:
            if any(f.code == "meeting_not_found" for f in outcome.failed_steps):
                self._set_connected(False)
            log.debug(
                "Zoom %s completed in %s ms: %s",
                pending.command,
                message.get("elapsed_ms"),
                outcome,
            )
            self.busy_changed.emit(False)
            self.operation_finished.emit(outcome)
        # Avoid an immediate endless chain of connection queries.
        if self._queued_sequence is not None:
            self._dispatch()

    def _set_connected(self, connected: bool) -> None:
        self._poll.setInterval(5000 if connected else 1000)
        if self._connected != connected:
            self._connected = connected
            self.connection_changed.emit(connected)

    def _timed_out(self) -> None:
        self._retire(code="worker_timeout")

    def _process_error(self, process: QProcess) -> None:
        if process is self._process:
            self._retire(code="worker_failed")

    def _exited(self, process: QProcess) -> None:
        if process is self._process:
            self._retire(code="worker_failed")
        process.deleteLater()

    @staticmethod
    def _log_stderr(process: QProcess) -> None:
        data = bytes(process.readAllStandardError().data()).decode("utf-8", errors="replace")
        if data:
            log.warning("Zoom worker: %s", data[-8192:].strip())

    def _retire(self, *, code: str | None = None, cancelled: bool = False) -> None:
        process, self._process = self._process, None
        sequence = self._queued_sequence
        if self._pending and self._pending.command in SEQUENCES:
            sequence = self._pending.command
        self._queued_sequence = None
        self._pending = None
        self._ready = False
        self._buffer.clear()
        self._deadline.stop()
        if process is not None:
            if process.state() == QProcess.ProcessState.NotRunning:
                process.deleteLater()
            else:
                process.kill()
        self._set_connected(False)
        if code:
            log.warning("Zoom automation worker retired: %s", code)
            self._poll.setInterval(self._restart_delay_ms)
            self._restart_delay_ms = min(30000, self._restart_delay_ms * 2)
        if sequence:
            self.busy_changed.emit(False)
            self.operation_finished.emit(
                SequenceResult(sequence, (StepFailure("worker", code),) if code else (), cancelled)
            )
        elif code:
            self.error.emit(code)
