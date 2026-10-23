"""Supervise the isolated meeting publisher process (``--meeting-publisher``).

The service owns the two egress rings for the lifetime of a meeting: it creates them on
:meth:`MeetingPublisherService.start`, announces them through :attr:`egress_changed` (the app
hands that descriptor to the scene runtime, which tells the sidecar to fill them), and
unlinks them on :meth:`stop`. The worker process only reads. If the worker crashes it is
relaunched with backoff against the same rings, with a fresh access token each time, so
the sidecar never has to re-attach; too many crashes in a short window end in FAILED.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from solin.core.foundation.constants import IS_COMPILED, MEETING_PUBLISHER_ARGUMENT
from solin.core.scenes.engine import (
    MEETING_AUDIO_MIX_INDEX,
    MeetingEgressDescriptor,
    ParticipantIngressDescriptor,
)

from .media_ring import MediaRingReader
from .publisher_worker import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, PublishRequest
from .tokens import AccessTokenRequest, RoomGrants, mint_access_token

log = logging.getLogger(__name__)

VIDEO_RING_SLOTS = 4
PARTICIPANT_RING_SLOTS = 3
PARTICIPANT_RING_PAYLOAD = 1920 * 1080 * 3 // 2  # the largest remote camera frame shown
AUDIO_RING_SLOTS = 64
AUDIO_RING_PAYLOAD = 16 * 1024  # 1024 stereo float32 frames is 8 KiB; leave headroom
HALL_IDENTITY = "hall"


class PublisherState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    CONNECTING = "connecting"
    PUBLISHING = "publishing"
    RECONNECTING = "reconnecting"
    RESTARTING = "restarting"
    FAILED = "failed"


_WORKER_STATES = {
    "connecting": PublisherState.CONNECTING,
    "publishing": PublisherState.PUBLISHING,
    "reconnecting": PublisherState.RECONNECTING,
}


@dataclass(frozen=True, slots=True)
class PublisherConfig:
    url: str
    api_key: str
    api_secret: str
    room: str
    display_name: str = "Kingdom Hall"
    width: int = 640
    height: int = 360
    fps: int = 30
    max_bitrate: int = 800_000
    simulcast: bool = True
    audio_only: bool = False
    e2ee_key: str = ""
    hall_audio: bool = False
    recording_device: str = ""
    playout_device: str = ""
    echo_cancellation: bool = True
    noise_suppression: bool = True
    auto_gain_control: bool = False

    def publish_request(self, token: str, video_ring: str, audio_ring: str) -> PublishRequest:
        return PublishRequest(
            url=self.url, token=token, video_ring=video_ring, audio_ring=audio_ring,
            width=self.width, height=self.height, fps=self.fps, max_bitrate=self.max_bitrate,
            simulcast=self.simulcast, audio_only=self.audio_only, e2ee_key=self.e2ee_key,
            hall_audio=self.hall_audio, recording_device=self.recording_device,
            playout_device=self.playout_device, echo_cancellation=self.echo_cancellation,
            noise_suppression=self.noise_suppression, auto_gain_control=self.auto_gain_control,
        )


def _default_command() -> tuple[str, list[str]]:
    if IS_COMPILED:
        return sys.executable, [MEETING_PUBLISHER_ARGUMENT]
    return sys.executable, ["-m", "solin.core.conference.publisher_worker"]


class MeetingPublisherService(QObject):
    """One supervised publisher. Lives on the Qt thread that created it."""

    state_changed = Signal(str)
    egress_changed = Signal(object)  # MeetingEgressDescriptor | None
    participant_ingress_changed = Signal(object)  # ParticipantIngressDescriptor | None
    stats_changed = Signal(object)
    warning = Signal(str)  # e.g. "hall_audio_unavailable": the meeting goes on without it
    error = Signal(str)

    def __init__(
        self,
        *,
        command: Callable[[], tuple[str, list[str]]] = _default_command,
        handshake_timeout_ms: int = 10000,
        max_restarts: int = 5,
        restart_window_s: float = 120.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._command = command
        self._handshake_timeout_ms = handshake_timeout_ms
        self._max_restarts = max_restarts
        self._restart_window_s = restart_window_s
        self._config: PublisherConfig | None = None
        self._rings: tuple[MediaRingReader, MediaRingReader] | None = None
        self._descriptor: MeetingEgressDescriptor | None = None
        self._participant_ring: MediaRingReader | None = None
        self._participant_descriptor: ParticipantIngressDescriptor | None = None
        self._spotlight = ""
        self._generation = 0
        self._process: QProcess | None = None
        self._ready = False
        self._buffer = bytearray()
        self._state = PublisherState.STOPPED
        self._last_error = ""
        self._restart_delay_ms = 1000
        self._restarts: deque[float] = deque()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._handshake = QTimer(self)
        self._handshake.setSingleShot(True)
        self._handshake.timeout.connect(lambda: self._recover("no handshake"))
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self._launch)

    # -- public API ---------------------------------------------------------------------
    @property
    def state(self) -> PublisherState:
        return self._state

    @property
    def descriptor(self) -> MeetingEgressDescriptor | None:
        return self._descriptor

    def start(self, config: PublisherConfig) -> None:
        """Create the rings, announce them and launch the publisher."""
        config.publish_request("validation", "video", "audio")  # raises ValueError early
        self.stop()
        video = MediaRingReader(None, VIDEO_RING_SLOTS, config.width * config.height * 3 // 2,
                                create=True)
        audio = MediaRingReader(None, AUDIO_RING_SLOTS, AUDIO_RING_PAYLOAD, create=True)
        self._config = config
        self._rings = (video, audio)
        self._generation += 1
        self._descriptor = MeetingEgressDescriptor(
            channel_id="solin-meeting", generation=self._generation,
            video_ring=video.name, audio_ring=audio.name,
            width=config.width, height=config.height, fps=config.fps,
            audio_mix=MEETING_AUDIO_MIX_INDEX,
        )
        participant = MediaRingReader(None, PARTICIPANT_RING_SLOTS, PARTICIPANT_RING_PAYLOAD,
                                      create=True)
        self._participant_ring = participant
        self._participant_descriptor = ParticipantIngressDescriptor(
            channel_id="solin-participant", generation=self._generation,
            video_ring=participant.name,
        )
        self._spotlight = ""
        self._restarts.clear()
        self._restart_delay_ms = 1000
        self.egress_changed.emit(self._descriptor)
        self.participant_ingress_changed.emit(self._participant_descriptor)
        self._launch()

    @property
    def participant_descriptor(self) -> ParticipantIngressDescriptor | None:
        return self._participant_descriptor

    def set_spotlight(self, identity: str) -> None:
        """Have the publisher follow this remote participant's camera (empty: nobody)."""
        if identity == self._spotlight:
            return
        self._spotlight = identity
        process = self._process
        if process is not None and self._ready:
            payload = json.dumps({"command": "spotlight", "identity": identity}).encode() + b"\n"
            process.write(payload)

    def stop(self, *, timeout_ms: int = 3000) -> None:
        self._restart_timer.stop()
        self._stop_process(timeout_ms)
        if self._descriptor is not None:
            self._descriptor = None
            self.egress_changed.emit(None)
        if self._participant_descriptor is not None:
            self._participant_descriptor = None
            self.participant_ingress_changed.emit(None)
        rings, self._rings = self._rings, None
        participant, self._participant_ring = self._participant_ring, None
        self._spotlight = ""
        for ring in (*(rings or ()), *((participant,) if participant else ())):
            try:
                ring.close()
                ring.unlink()
            except (OSError, FileNotFoundError):
                log.debug("meeting ring cleanup errored", exc_info=True)
        self._config = None
        self._set_state(PublisherState.STOPPED)

    # -- process lifecycle --------------------------------------------------------------
    def _launch(self) -> None:
        if self._config is None or self._rings is None:
            return
        program, arguments = self._command()
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(environment)
        process.readyReadStandardOutput.connect(lambda: self._read(process))
        process.readyReadStandardError.connect(lambda: self._read_stderr(process))
        process.finished.connect(lambda *_: self._exited(process))
        process.errorOccurred.connect(lambda _: self._process_error(process))
        self._process = process
        self._ready = False
        self._buffer.clear()
        self._last_error = ""
        self._set_state(PublisherState.STARTING)
        self._handshake.start(self._handshake_timeout_ms)
        process.start(program, arguments)

    def _send_start(self) -> None:
        config, rings, process = self._config, self._rings, self._process
        if config is None or rings is None or process is None:
            return
        token = mint_access_token(config.api_key, config.api_secret, AccessTokenRequest(
            identity=HALL_IDENTITY, name=config.display_name,
            grants=RoomGrants(config.room, can_subscribe=True, can_update_own_metadata=True),
            attributes={"role": "hall"},
        ))
        request = replace(
            config.publish_request(token, rings[0].name, rings[1].name),
            participant_ring=self._participant_ring.name if self._participant_ring else "",
            spotlight=self._spotlight,  # a restarted worker resumes following them
        )
        message = {"command": "start", **{
            name: getattr(request, name) for name in PublishRequest.__dataclass_fields__
        }}
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if process.write(payload) != len(payload):
            self._recover("could not send start")

    def _stop_process(self, timeout_ms: int = 3000) -> None:
        self._handshake.stop()
        process, self._process = self._process, None
        if process is None:
            return
        if process.state() != QProcess.ProcessState.NotRunning:
            process.write(b'{"command":"stop"}\n')
            process.closeWriteChannel()
            if not process.waitForFinished(timeout_ms):
                process.kill()
                process.waitForFinished(1000)
        process.deleteLater()

    # -- protocol -----------------------------------------------------------------------
    def _read(self, process: QProcess) -> None:
        if process is not self._process:
            return
        self._buffer.extend(process.readAllStandardOutput().data())
        try:
            while b"\n" in self._buffer:
                line, _, rest = self._buffer.partition(b"\n")
                self._buffer = bytearray(rest)
                if len(line) > MAX_MESSAGE_BYTES:
                    raise ValueError("Oversized publisher message")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Invalid publisher message")
                self._receive(message)
            if len(self._buffer) > MAX_MESSAGE_BYTES:
                raise ValueError("Oversized publisher message")
        except (ValueError, TypeError):
            log.warning("Invalid meeting publisher output", exc_info=True)
            self._recover("protocol error")

    def _receive(self, message: dict) -> None:
        if not self._ready:
            if message != {"ready": PROTOCOL_VERSION}:
                raise ValueError("Unexpected publisher handshake")
            self._ready = True
            self._handshake.stop()
            self._send_start()
            return
        if "state" in message:
            state = _WORKER_STATES.get(message["state"])
            if state is None:
                raise ValueError("Unknown publisher state")
            if state is PublisherState.PUBLISHING:
                self._restart_delay_ms = 1000
            self._set_state(state)
        elif "stats" in message:
            if not isinstance(message["stats"], dict):
                raise ValueError("Invalid publisher stats")
            self.stats_changed.emit(message["stats"])
        elif "warning" in message:
            code = str(message["warning"])[:64]
            log.warning("Meeting publisher: %s", code)
            self.warning.emit(code)
        elif "error" in message:
            self._last_error = str(message["error"])[:64]
        else:
            raise ValueError("Unknown publisher message")

    def _read_stderr(self, process: QProcess) -> None:
        text = bytes(process.readAllStandardError().data()).decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.strip():
                self._stderr_tail.append(line)
                log.debug("meeting-publisher: %s", line)

    # -- failures -----------------------------------------------------------------------
    def _exited(self, process: QProcess) -> None:
        if process is not self._process:
            process.deleteLater()
            return
        code = process.exitCode()
        self._process = None
        process.deleteLater()
        reason = self._last_error or f"exited with code {code}"
        self._recover(reason)

    def _process_error(self, process: QProcess) -> None:
        if process is self._process and process.error() == QProcess.ProcessError.FailedToStart:
            self._process = None
            process.deleteLater()
            self._recover("failed to start")

    def _recover(self, reason: str) -> None:
        if self._config is None:
            return
        tail = " | ".join(list(self._stderr_tail)[-3:])
        log.warning("Meeting publisher %s%s", reason, f": {tail}" if tail else "")
        self._stop_process(timeout_ms=1000)
        now = time.monotonic()
        while self._restarts and now - self._restarts[0] > self._restart_window_s:
            self._restarts.popleft()
        if len(self._restarts) >= self._max_restarts:
            self._set_state(PublisherState.FAILED)
            self.error.emit(f"Meeting publisher keeps failing ({reason})")
            return
        self._restarts.append(now)
        self._set_state(PublisherState.RESTARTING)
        self._restart_timer.start(self._restart_delay_ms)
        self._restart_delay_ms = min(30000, self._restart_delay_ms * 2)

    def _set_state(self, state: PublisherState) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state.value)


class AudioDeviceLister(QObject):
    """Asks a short-lived publisher process which meeting microphones and speakers exist.

    The device list comes from the same WebRTC device module the meeting will use, so the
    ids match what the publisher can open.
    """

    finished = Signal(object)  # {"recording": [...], "playout": [...]}, empty lists on failure

    def __init__(self, *, command: Callable[[], tuple[str, list[str]]] = _default_command,
                 timeout_ms: int = 15000, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._command = command
        self._timeout_ms = timeout_ms
        self._process: QProcess | None = None
        self._buffer = bytearray()
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(lambda: self._finish(None))

    def start(self) -> None:
        if self._process is not None:
            return
        program, arguments = self._command()
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(lambda: self._read(process))
        process.finished.connect(lambda *_: self._finish(None) if process is self._process else None)
        process.errorOccurred.connect(lambda _: self._finish(None) if process is self._process else None)
        self._process = process
        self._buffer.clear()
        self._deadline.start(self._timeout_ms)
        process.start(program, arguments)
        process.write(b'{"command":"list_devices"}\n')

    def _read(self, process: QProcess) -> None:
        self._buffer.extend(process.readAllStandardOutput().data())
        while b"\n" in self._buffer:
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and isinstance(message.get("devices"), dict):
                self._finish(message["devices"])
                return

    def _finish(self, devices: dict | None) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        self._deadline.stop()
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
            process.waitForFinished(1000)
        process.deleteLater()
        self.finished.emit(_clean_devices(devices))


def _clean_devices(devices: dict | None) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {"recording": [], "playout": []}
    for kind in result:
        for device in (devices or {}).get(kind) or ():
            if isinstance(device, dict) and isinstance(device.get("name"), str):
                result[kind].append({"id": str(device.get("id") or ""), "name": device["name"][:200]})
    return result
