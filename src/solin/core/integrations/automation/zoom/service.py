"""Asynchronous Qt facade supervising one serialized Windows automation process."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from solin.core.foundation.constants import IS_COMPILED, ZOOM_AUTOMATION_ARGUMENT

from ..settings import ZoomSettingsStore
from .protocol import (
    MAX_MESSAGE_BYTES,
    PARTICIPANT_ACTIONS,
    PLAYBACK_MICROPHONE,
    PROTOCOL_VERSION,
    RAISED_HANDS,
    PlaybackMicrophoneOwner,
    parse_participant_result,
    parse_participant_step,
    parse_playback_microphone_owner,
    parse_snapshot,
    valid_reference,
)
from .sequences import SEQUENCES
from .state import (
    ParticipantAction,
    ParticipantActionResult,
    ParticipantStepResult,
    RaisedHandsSnapshot,
    RaisedHandsStatus,
    SequenceResult,
    StepFailure,
)

log = logging.getLogger(__name__)
_WRITES = SEQUENCES | PARTICIPANT_ACTIONS
_RAISED_HANDS_INTERVAL_MS = 1500
_PANEL_RECHECK_INTERVAL_MS = 5000
_WORKER_STOP_MS = 2000


@dataclass(frozen=True, slots=True)
class _Request:
    id: int
    command: str
    reference: str | None = None
    monitor_generation: int = 0


class ZoomService(QObject):
    enabled_changed = Signal(bool)
    connection_changed = Signal(bool)
    busy_changed = Signal(bool)
    operation_finished = Signal(object)
    sequence_progress = Signal(str)
    raised_hands_changed = Signal(object)
    participant_action_finished = Signal(object)
    mute_during_playback_changed = Signal(bool)
    error = Signal(str)

    def __init__(
        self,
        settings: ZoomSettingsStore,
        parent: QObject | None = None,
        *,
        raised_hands_enabled: bool = False,
    ):
        super().__init__(parent)
        self._settings = settings
        self._active = False
        self._connected = False
        self._process: QProcess | None = None
        self._closing_process: QProcess | None = None
        self._ready = False
        self._buffer = bytearray()
        self._pending: _Request | None = None
        self._next_id = 0
        self._queued_operation: _Request | None = None
        self._participant_steps: list[ParticipantStepResult] = []
        self._media_playing = False
        self._mute_during_playback = settings.mute_during_playback()
        self._playback_mute_sent: bool | None = None
        # Confirmed by the worker. Keep it through forced retirement so a
        # replacement can restore the microphone in the same meeting.
        self._playback_microphone_owner: PlaybackMicrophoneOwner | None = None
        self._playback_microphone_used = False
        self._raised_hands_enabled = raised_hands_enabled
        self._monitoring = False
        self._monitor_generation = 0
        self._open_panel_once = False
        self._snapshot = RaisedHandsSnapshot()
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
        return self._queued_operation is not None or bool(
            self._pending and self._pending.command in _WRITES
        )

    @property
    def raised_hands_snapshot(self) -> RaisedHandsSnapshot:
        return self._snapshot

    @property
    def raised_hands_available(self) -> bool:
        return self.is_available and self._raised_hands_enabled

    @property
    def mute_during_playback(self) -> bool:
        return self._mute_during_playback

    def set_mute_during_playback(self, enabled: bool) -> None:
        if not self.is_available or enabled == self._mute_during_playback:
            return
        self._settings.set_mute_during_playback(enabled)
        self._mute_during_playback = enabled
        self.mute_during_playback_changed.emit(enabled)
        self._dispatch_playback_microphone()

    def set_media_playing(self, playing: bool) -> None:
        if playing != self._media_playing:
            self._media_playing = playing
            self._dispatch_playback_microphone()

    def start(self) -> None:
        if not self.is_available or self._active:
            return
        self._active = True
        if self._raised_hands_enabled and self._settings.raised_hands_monitoring_enabled():
            self._apply_raised_hands_monitoring(True)
        self.enabled_changed.emit(True)
        self._poll.start()
        self.refresh()

    def stop(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        was_active = self._active
        self._active = False
        self._poll.stop()
        self._apply_raised_hands_monitoring(False)
        process = self._process or self._closing_process
        interactive_pending = self._pending is not None and self._pending.command in _WRITES
        self._retire(
            cancelled=True,
            restore_microphone=self._playback_microphone_used and not interactive_pending,
        )
        if wait and process is not None:
            if not process.waitForFinished(max(0, min(_WORKER_STOP_MS, int(timeout * 1000)))):
                self._kill_closing_process(process)
        if was_active:
            self.enabled_changed.emit(False)

    def cancel(self) -> None:
        if self.is_busy:
            self._retire(cancelled=True)

    def refresh(self) -> None:
        if (
            not self._active or self._closing_process is not None
            or self._pending is not None or self.is_busy
        ):
            return
        if self._process is None:
            self._launch()
        elif self._ready:
            if self._dispatch_playback_microphone():
                return
            if self._monitoring and self._connected:
                open_panel, self._open_panel_once = self._open_panel_once, False
                self._send(RAISED_HANDS, open_panel=open_panel)
            else:
                self._send("meeting")

    def request_pre_meeting(self) -> bool:
        return self._queue_operation("pre_meeting")

    def request_post_meeting(self) -> bool:
        return self._queue_operation("post_meeting")

    def set_raised_hands_monitoring(self, enabled: bool) -> bool:
        if not self._active or not self._raised_hands_enabled:
            return False
        if enabled != self._monitoring:
            self._settings.set_raised_hands_monitoring_enabled(enabled)
            self._apply_raised_hands_monitoring(enabled)
            if enabled:
                self.refresh()
        return True

    def _apply_raised_hands_monitoring(self, enabled: bool) -> None:
        if self._monitoring == enabled:
            return
        self._monitoring = self._open_panel_once = enabled
        self._monitor_generation += 1
        status = RaisedHandsStatus.INACTIVE
        if enabled:
            status = RaisedHandsStatus.UNAVAILABLE if self._connected else RaisedHandsStatus.NO_MEETING
        self._publish_hands(RaisedHandsSnapshot(status))

    def request_participant_action(self, action: ParticipantAction, reference: str) -> bool:
        if not isinstance(action, ParticipantAction) or not self._has_reference(reference):
            return False
        return self._queue_operation(action.value, reference)

    def _has_reference(self, reference: str) -> bool:
        return (
            self._monitoring
            and valid_reference(reference)
            and any(hand.reference == reference for hand in self._snapshot.hands)
        )

    def _queue_operation(self, command: str, reference: str | None = None) -> bool:
        if not self._active or not self._connected or self.is_busy:
            return False
        self._queued_operation = _Request(0, command, reference)
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

    def _send(
        self, command: str, *, reference: str | None = None, open_panel: bool = False,
        muted: bool = False,
    ) -> None:
        if self._process is None or not self._ready:
            return
        self._next_id += 1
        self._pending = _Request(self._next_id, command, reference, self._monitor_generation)
        request: dict[str, object] = {"id": self._next_id, "command": command}
        if command == RAISED_HANDS:
            request["open_panel"] = open_panel
        elif command == PLAYBACK_MICROPHONE:
            request["muted"] = muted
            request["owner"] = self._playback_microphone_owner
        if reference is not None:
            request["reference"] = reference
        payload = json.dumps(request).encode() + b"\n"
        timeout = 3000
        if command in SEQUENCES:
            timeout = 35000
        elif command in PARTICIPANT_ACTIONS or command == PLAYBACK_MICROPHONE:
            timeout = 18000
        elif open_panel:
            timeout = 10000
        self._deadline.start(timeout)
        if self._process.write(payload) != len(payload):
            self._retire(code="worker_failed")

    def _dispatch_playback_microphone(self) -> bool:
        wanted = self._mute_during_playback and self._media_playing
        if (
            not self._active or not self._connected or not self._ready
            or self._pending is not None or self.is_busy or wanted == self._playback_mute_sent
        ):
            return False
        if not wanted and self._playback_mute_sent is None and self._playback_microphone_owner is None:
            return False
        self._playback_mute_sent = wanted
        self._playback_microphone_used = True
        self._send(PLAYBACK_MICROPHONE, muted=wanted)
        return True

    def _dispatch(self) -> None:
        if self._pending is not None or not self._active:
            return
        operation, self._queued_operation = self._queued_operation, None
        if operation is None:
            self.refresh()
        elif operation.reference is None:
            self._send(operation.command)
        elif self._has_reference(operation.reference):
            self._send(operation.command, reference=operation.reference)
        else:
            self.busy_changed.emit(False)
            self.participant_action_finished.emit(
                ParticipantActionResult(
                    ParticipantAction(operation.command),
                    operation.reference,
                    failure=StepFailure("target", "participant_changed"),
                )
            )

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
                if process is not self._process:
                    return
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
        if "participant_step" in message:
            if pending.reference is None:
                raise ValueError("Unexpected participant progress")
            self._participant_steps.append(
                parse_participant_step(
                    message["participant_step"],
                    ParticipantAction(pending.command),
                    len(self._participant_steps),
                )
            )
            return
        error = message.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError("Invalid worker error")
        command = pending.command
        if command == "meeting":
            connected = False if error else message.get("connected")
            if type(connected) is not bool:
                raise ValueError("Invalid connection state")
        elif command == RAISED_HANDS:
            snapshot = (
                self._snapshot_from_error(error) if error else parse_snapshot(message["snapshot"])
            )
        elif command == PLAYBACK_MICROPHONE:
            if not error:
                owner = parse_playback_microphone_owner(message["owner"])
                if self._playback_mute_sent is False and owner is not None:
                    raise ValueError("Unexpected playback microphone owner after restore")
        elif pending.reference is not None:
            action = ParticipantAction(command)
            if error:
                completed = tuple(self._participant_steps)
                participant_result = ParticipantActionResult(
                    action, pending.reference, completed, StepFailure("worker", error)
                )
            else:
                participant_result = parse_participant_result(
                    message["result"], action, pending.reference
                )
                if tuple(self._participant_steps) != participant_result.completed_steps:
                    raise ValueError("Inconsistent participant action progress")
        else:
            outcome = self._sequence_result(command, message, error)

        self._deadline.stop()
        self._pending = None
        self._participant_steps.clear()
        if command == "meeting":
            self._set_connected(connected)
            if not error:
                self._restart_delay_ms = 1000
        elif command == RAISED_HANDS:
            if error == "meeting_not_found":
                self._set_connected(False)
            elif self._monitoring and pending.monitor_generation == self._monitor_generation:
                self._publish_hands(snapshot)
        elif command == PLAYBACK_MICROPHONE:
            if error == "meeting_not_found":
                # Reconcile the unfulfilled request when Zoom is connected again.
                self._playback_mute_sent = None
                self._set_connected(False)
            elif error:
                log.warning("Zoom playback microphone action failed: %s", error)
                self.error.emit(error)
            else:
                self._playback_microphone_owner = owner
        elif pending.reference is not None:
            failure = participant_result.failure
            if failure is not None:
                if failure.code == "meeting_not_found":
                    self._set_connected(False)
                elif failure.code == "panel_closed" and self._monitoring:
                    self._publish_hands(RaisedHandsSnapshot(RaisedHandsStatus.PANEL_CLOSED))
            self.busy_changed.emit(False)
            self.participant_action_finished.emit(participant_result)
        else:
            if any(f.code == "meeting_not_found" for f in outcome.failed_steps):
                self._set_connected(False)
            if command == "pre_meeting" and self._mute_during_playback and self._media_playing:
                # Meeting start can turn the microphone on during playback.
                self._playback_mute_sent = None
            log.debug(
                "Zoom %s completed in %s ms: %s",
                command,
                message.get("elapsed_ms"),
                outcome,
            )
            self.busy_changed.emit(False)
            self.operation_finished.emit(outcome)
        # Avoid an immediate endless chain of connection queries.
        if self._queued_operation is not None or (
            self._monitoring and self._open_panel_once and self._connected
        ):
            self._dispatch()
        else:
            self._dispatch_playback_microphone()

    @staticmethod
    def _sequence_result(command: str, message: dict, error: str | None) -> SequenceResult:
        if error:
            return SequenceResult(command, (StepFailure("meeting", error),))
        result = message["result"]
        if result["sequence"] != command or type(result["cancelled"]) is not bool:
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
        return SequenceResult(
            command,
            tuple(StepFailure(f["step"], f["code"]) for f in failures),
            result["cancelled"],
        )

    @staticmethod
    def _snapshot_from_error(error: str) -> RaisedHandsSnapshot:
        if error == "meeting_not_found":
            return RaisedHandsSnapshot(RaisedHandsStatus.NO_MEETING)
        if error == "panel_closed":
            return RaisedHandsSnapshot(RaisedHandsStatus.PANEL_CLOSED)
        return RaisedHandsSnapshot(RaisedHandsStatus.UNAVAILABLE, error=error)

    def _publish_hands(self, snapshot: RaisedHandsSnapshot) -> None:
        previous, self._snapshot = self._snapshot, snapshot
        self._set_poll_interval()
        if snapshot != previous:
            self.raised_hands_changed.emit(snapshot)

    def _set_poll_interval(self) -> None:
        if not self._connected:
            interval = 1000
        elif self._monitoring and self._snapshot.status is RaisedHandsStatus.READY:
            interval = _RAISED_HANDS_INTERVAL_MS
        else:
            interval = _PANEL_RECHECK_INTERVAL_MS
        if interval != self._poll.interval():
            self._poll.setInterval(interval)

    def _set_connected(self, connected: bool, *, error: str | None = None) -> None:
        changed = self._connected != connected
        self._connected = connected
        if self._monitoring and not connected:
            self._publish_hands(self._snapshot_from_error(error or "meeting_not_found"))
        else:
            self._set_poll_interval()
        if changed:
            if connected:
                self._playback_mute_sent = None
            self.connection_changed.emit(connected)

    def _timed_out(self) -> None:
        self._retire(code="worker_timeout")

    def _process_error(self, process: QProcess) -> None:
        if process is self._process:
            self._retire(code="worker_failed")

    def _exited(self, process: QProcess) -> None:
        if process is self._process:
            self._retire(code="worker_failed")
        if process is self._closing_process:
            self._closing_process = None
            self.refresh()
        process.deleteLater()

    def _kill_closing_process(self, process: QProcess) -> None:
        if process is self._closing_process:
            process.kill()

    @staticmethod
    def _log_stderr(process: QProcess) -> None:
        data = bytes(process.readAllStandardError().data()).decode("utf-8", errors="replace")
        if data:
            log.warning("Zoom worker: %s", data[-8192:].strip())

    def _retire(
        self, *, code: str | None = None, cancelled: bool = False, restore_microphone: bool = False
    ) -> None:
        process, self._process = self._process, None
        operation = self._queued_operation
        if self._pending and self._pending.command in _WRITES:
            operation = self._pending
        completed = tuple(self._participant_steps)
        self._participant_steps.clear()
        self._queued_operation = None
        self._pending = None
        self._ready = False
        self._playback_mute_sent = None
        self._playback_microphone_used = False
        self._buffer.clear()
        self._deadline.stop()
        if process is not None:
            if process.state() == QProcess.ProcessState.NotRunning:
                process.deleteLater()
            elif restore_microphone:
                # EOF releases this worker's mute before a new worker can take over.
                self._playback_microphone_owner = None
                self._closing_process = process
                QTimer.singleShot(_WORKER_STOP_MS, self, lambda: self._kill_closing_process(process))
                process.closeWriteChannel()
            else:
                process.kill()
        self._set_connected(False, error=code)
        if code:
            log.warning("Zoom automation worker retired: %s", code)
            self._poll.setInterval(self._restart_delay_ms)
            self._restart_delay_ms = min(30000, self._restart_delay_ms * 2)
        if operation is not None:
            failure = StepFailure("worker", code) if code else None
            self.busy_changed.emit(False)
            if operation.reference is None:
                self.operation_finished.emit(
                    SequenceResult(operation.command, (failure,) if failure else (), cancelled)
                )
            else:
                self.participant_action_finished.emit(
                    ParticipantActionResult(
                        ParticipantAction(operation.command),
                        operation.reference,
                        completed,
                        failure,
                        cancelled,
                    )
                )
        elif code:
            self.error.emit(code)
