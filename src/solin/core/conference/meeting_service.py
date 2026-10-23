"""A Solin-hosted meeting: the :class:`~solin.core.conference.provider.MeetingProvider`
that runs the room on the hall PC instead of automating Zoom.

It composes the supervised LiveKit server, the isolated program publisher and the
:class:`~solin.core.conference.meeting_core.MeetingCore` rules. Server-API calls run on one
worker thread; their results come back to the Qt thread through a queued signal, so the
core is only ever touched from the thread that owns this object. Participants are polled
once a second (cheap on loopback) and immediately after every operator command.

``request_pre_meeting`` locks the meeting (no self-unmute) and ``request_post_meeting``
unlocks it, matching what the Zoom sequences do, so the existing meeting workflow drives
either provider the same way.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .meeting_core import (
    ClearHand,
    MeetingAction,
    MeetingCore,
    MeetingPolicy,
    ParticipantStatus,
    RemoveParticipant,
    SetName,
    SetPermission,
    is_attendee,
)
from .attendee_access import AttendeeAccess, JoinRefusedError, StageHand, attendee_token
from .attendee_server import AttendeeHttpApplication, AttendeeServer
from .provider import POST_MEETING, PRE_MEETING, SequenceResult, StepFailure
from .publisher_service import MeetingPublisherService, PublisherConfig, PublisherState
from .room_api import ParticipantNotFoundError, RoomApi, RoomApiError
from .server_binary import ServerUnavailableError, server_executable
from .server_config import ServerSettings
from .server_process import ConferenceServerService, ServerState

log = logging.getLogger(__name__)

DEFAULT_WEB_PORT = 7890


class MeetingState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    OPEN = "open"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HallAudioSettings:
    """The meeting microphone (hall feed) and speaker, through WebRTC's device module."""

    enabled: bool = True
    recording_device: str = ""  # device id; empty = system default
    playout_device: str = ""
    echo_cancellation: bool = True
    noise_suppression: bool = True


@dataclass(frozen=True, slots=True)
class MeetingSettings:
    server: ServerSettings
    room: str = "solin-meeting"
    display_name: str = "Kingdom Hall"
    width: int = 640
    height: int = 360
    fps: int = 30
    audio_only: bool = False
    e2ee: bool = True
    policy: MeetingPolicy = field(default_factory=MeetingPolicy)
    web_port: int = DEFAULT_WEB_PORT
    hall_audio: HallAudioSettings = field(default_factory=HallAudioSettings)
    web_bind: str = "0.0.0.0"


@dataclass(frozen=True, slots=True)
class _ApiResult:
    kind: str  # "snapshot", "applied", "room_ready"
    value: Any = None
    sequence: str | None = None
    failures: tuple[StepFailure, ...] = ()


def apply_actions(api: RoomApi, room: str, actions: list[MeetingAction]) -> tuple[StepFailure, ...]:
    """Apply ``actions`` in order; a participant who already left is not a failure."""
    failures: list[StepFailure] = []
    for action in actions:
        try:
            if isinstance(action, SetPermission):
                api.update_participant(room, action.identity, permission=action.permission)
            elif isinstance(action, SetName):
                api.update_participant(room, action.identity, name=action.name)
            elif isinstance(action, ClearHand):
                api.update_participant(room, action.identity, attributes={"hand": ""})
            elif isinstance(action, RemoveParticipant):
                api.remove_participant(room, action.identity)
        except ParticipantNotFoundError:
            continue
        except RoomApiError as exc:
            failures.append(StepFailure(type(action).__name__, exc.code))
    return tuple(failures)


class SolinMeetingService(QObject):
    # MeetingProvider signals
    enabled_changed = Signal(bool)
    connection_changed = Signal(bool)
    busy_changed = Signal(bool)
    operation_finished = Signal(object)
    error = Signal(str)
    # Meeting-specific
    state_changed = Signal(str)
    participants_changed = Signal(object)  # list[MeetingParticipant]
    hands_changed = Signal(object)  # list[MeetingParticipant]
    attendance_changed = Signal(int)
    egress_changed = Signal(object)  # MeetingEgressDescriptor | None
    hall_audio_available_changed = Signal(bool)
    participant_ingress_changed = Signal(object)  # ParticipantIngressDescriptor | None
    _api_result = Signal(object)

    def __init__(
        self,
        server: ConferenceServerService,
        publisher: MeetingPublisherService,
        *,
        available: Callable[[], bool] = lambda: True,
        room_api_factory: Callable[[ServerSettings], RoomApi] | None = None,
        attendee_server_factory: Callable[[str], AttendeeServer] | None = None,
        lan_address: Callable[[], str] | None = None,
        poll_interval_ms: int = 1000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._server = server
        self._publisher = publisher
        for owned in (server, publisher):
            if owned.parent() is None:  # adopt what nobody else owns
                owned.setParent(self)
        self._available = available
        self._room_api_factory = room_api_factory or (
            lambda s: RoomApi(f"http://127.0.0.1:{s.http_port}", s.api_key, s.api_secret)
        )
        self._attendee_server_factory = attendee_server_factory or default_attendee_server
        self._lan_address = lan_address or first_lan_address
        self._attendee_server: AttendeeServer | None = None
        self._meeting_id = ""
        self._stage_secret = ""
        self._origin = ""
        self._settings: MeetingSettings | None = None
        self._api: RoomApi | None = None
        self._core = MeetingCore()
        self._e2ee_key = ""
        self._state = MeetingState.IDLE
        self._connected = False
        self._busy = False
        self._poll_in_flight = False
        self._last_views: tuple[Any, ...] = ()
        self._executor: ThreadPoolExecutor | None = None
        self._poll = QTimer(self)
        self._poll.setInterval(poll_interval_ms)
        self._poll.timeout.connect(self.refresh)
        self._api_result.connect(self._on_api_result)
        server.state_changed.connect(self._on_server_state)
        server.error.connect(self._on_failure)
        publisher.state_changed.connect(self._on_publisher_state)
        publisher.egress_changed.connect(self.egress_changed)
        publisher.participant_ingress_changed.connect(self.participant_ingress_changed)
        publisher.error.connect(self._on_failure)
        publisher.warning.connect(self._on_publisher_warning)
        self._hall_audio_available = True

    # -- MeetingProvider ----------------------------------------------------------------
    @property
    def is_available(self) -> bool:
        return self._available()

    @property
    def is_enabled(self) -> bool:
        return self._state in (MeetingState.STARTING, MeetingState.OPEN)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_busy(self) -> bool:
        return self._busy

    def start(self) -> None:
        """Open the meeting with the configured settings."""
        settings = self._settings
        if settings is None:
            raise RuntimeError("Configure the meeting before starting it")
        if self.is_enabled:
            return
        self._core = MeetingCore(policy=settings.policy)
        self._e2ee_key = secrets.token_urlsafe(24) if settings.e2ee else ""
        self._meeting_id = secrets.token_urlsafe(8)
        self._stage_secret = secrets.token_urlsafe(18)
        self._api = self._room_api_factory(settings.server)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="solin-meeting")
        self._set_state(MeetingState.STARTING)
        self.enabled_changed.emit(True)
        if self._server.state is ServerState.RUNNING and self._server.settings == settings.server:
            self._create_room()
        else:
            self._server.start(settings.server)

    def stop(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        was_enabled = self.is_enabled
        self._poll.stop()
        self._publisher.stop()
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)
        attendee_server, self._attendee_server = self._attendee_server, None
        if attendee_server is not None:
            attendee_server.stop()
        self._origin = ""
        # The room lives only in this server, so stopping it ends the room too.
        self._server.stop(wait=wait, timeout_ms=int(timeout * 1000))
        self._api = None
        if not self._hall_audio_available:
            self._hall_audio_available = True
            self.hall_audio_available_changed.emit(True)
        self._poll_in_flight = False
        self._set_connected(False)
        self._set_busy(False)
        self._set_state(MeetingState.IDLE)
        if was_enabled:
            self.enabled_changed.emit(False)

    def request_pre_meeting(self) -> bool:
        return self._run_sequence(PRE_MEETING, locked=True)

    def request_post_meeting(self) -> bool:
        return self._run_sequence(POST_MEETING, locked=False)

    # -- meeting --------------------------------------------------------------------------
    def configure(self, settings: MeetingSettings) -> None:
        if self.is_enabled:
            raise RuntimeError("Stop the meeting before changing its settings")
        self._settings = settings

    @property
    def state(self) -> MeetingState:
        return self._state

    @property
    def locked(self) -> bool:
        return self._core.locked

    @property
    def e2ee_key(self) -> str:
        return self._e2ee_key

    @property
    def room(self) -> str:
        return self._settings.room if self._settings else ""

    def participants(self):
        return self._core.participants()

    def hands(self):
        return self._core.hands()

    def attendance(self) -> int:
        return self._core.attendance()

    def attendee_token(self, device_id: str, name: str = "") -> str | None:
        """A short-lived lobby token for one attendee device, or None if it may not join."""
        access = self._access()
        if access is None:
            return None
        try:
            return attendee_token(access, device_id, name)
        except JoinRefusedError:
            return None

    @property
    def join_url(self) -> str:
        """The link attendees open; the encryption key rides in the never-sent fragment."""
        if not self._origin:
            return ""
        return f"{self._origin}/join" + (f"#k={self._e2ee_key}" if self._e2ee_key else "")

    @property
    def stage_url(self) -> str:
        return f"{self._origin}/stage/{self._stage_secret}" if self._origin else ""

    def _access(self) -> AttendeeAccess | None:
        settings = self._settings
        if settings is None or self._state is not MeetingState.OPEN:
            return None
        return AttendeeAccess(
            meeting_id=self._meeting_id, hall_name=settings.display_name, room=settings.room,
            api_key=settings.server.api_key, api_secret=settings.server.api_secret,
            stage_secret=self._stage_secret, removed=self._core.removed(),
            e2ee=bool(self._e2ee_key),
        )

    def admit(self, identity: str) -> None:
        self._command(self._core.admit(identity))

    def spotlight(self, identity: str) -> None:
        """Show this remote part-holder in "Remote participant (meeting)" layers."""
        self._command(self._core.spotlight(identity))

    def clear_spotlight(self) -> None:
        self._command(self._core.clear_spotlight())

    @property
    def spotlighted(self) -> str:
        return self._core.spotlighted

    def admit_all(self) -> None:
        self._command(self._core.admit_all())

    def to_lobby(self, identity: str) -> None:
        self._command(self._core.to_lobby(identity))

    def call_on(self, identity: str) -> None:
        self._command(self._core.call_on(identity))

    def finish(self, identity: str) -> None:
        self._command(self._core.finish(identity))

    def lower_hand(self, identity: str) -> None:
        self._command(self._core.lower_hand(identity))

    def rename(self, identity: str, name: str) -> None:
        self._command(self._core.rename(identity, name))

    def remove(self, identity: str) -> None:
        if is_attendee(identity):
            self._command(self._core.remove(identity))
            self._publish_access()

    def refresh(self) -> None:
        if self._poll_in_flight or self._state is not MeetingState.OPEN:
            return
        api, settings = self._api, self._settings
        if api is None or settings is None:
            return
        self._poll_in_flight = True
        self._submit(lambda: _ApiResult("snapshot", api.list_participants(settings.room)),
                     on_error=lambda: _ApiResult("snapshot", None))

    # -- lifecycle ----------------------------------------------------------------------
    def _on_server_state(self, state: str) -> None:
        if self._state is MeetingState.STARTING and state == ServerState.RUNNING:
            self._create_room()
        elif self._state is MeetingState.OPEN and state == ServerState.RESTARTING:
            self._set_connected(False)
        elif self._state is MeetingState.OPEN and state == ServerState.RUNNING:
            # The server lost the room with its restart; recreate it. The publisher
            # reconnects by itself and attendees are re-admitted by identity.
            self._create_room()

    def _create_room(self) -> None:
        api, settings = self._api, self._settings
        if api is None or settings is None:
            return

        metadata = room_metadata(self._core.locked)

        def create() -> _ApiResult:
            api.create_room(settings.room, metadata=metadata)
            return _ApiResult("room_ready")

        self._submit(create, on_error=lambda: _ApiResult("room_failed"))

    def _start_attendee_server(self) -> bool:
        settings = self._settings
        if settings is None:
            return False
        server = self._attendee_server_factory(f"http://127.0.0.1:{settings.server.http_port}")
        try:
            server.start(settings.web_bind, settings.web_port)
        except (OSError, RuntimeError) as exc:
            log.warning("The attendee page could not start: %s", exc)
            self._on_failure("The attendee page could not start")
            return False
        self._attendee_server = server
        self._origin = f"http://{self._lan_address()}:{server.port}"
        self._publish_access()
        self._publish_hands()
        return True

    def _publish_access(self) -> None:
        if self._attendee_server is not None:
            self._attendee_server.publish_access(self._access())

    def _publish_hands(self) -> None:
        if self._attendee_server is None:
            return
        hands = [StageHand(p.name, index + 1, p.status is ParticipantStatus.SPEAKING)
                 for index, p in enumerate(self._core.hands())]
        self._attendee_server.publish_hands(hands, self._core.attendance())

    def _start_publishing(self) -> None:
        settings = self._settings
        if settings is None:
            return
        server = settings.server
        self._publisher.start(PublisherConfig(
            url=f"ws://127.0.0.1:{server.http_port}", api_key=server.api_key,
            api_secret=server.api_secret, room=settings.room,
            display_name=settings.display_name, width=settings.width,
            height=settings.height, fps=settings.fps, audio_only=settings.audio_only,
            e2ee_key=self._e2ee_key,
            hall_audio=settings.hall_audio.enabled,
            recording_device=settings.hall_audio.recording_device,
            playout_device=settings.hall_audio.playout_device,
            echo_cancellation=settings.hall_audio.echo_cancellation,
            noise_suppression=settings.hall_audio.noise_suppression,
        ))

    @property
    def hall_audio_available(self) -> bool:
        return self._hall_audio_available

    def _on_publisher_warning(self, code: str) -> None:
        if code == "hall_audio_unavailable" and self._hall_audio_available:
            self._hall_audio_available = False
            self.hall_audio_available_changed.emit(False)

    def _on_publisher_state(self, state: str) -> None:
        self._set_connected(state == PublisherState.PUBLISHING)

    def _on_failure(self, message: str) -> None:
        if not self.is_enabled:
            return
        log.warning("Solin-hosted meeting failure: %s", message)
        self._set_state(MeetingState.FAILED)
        self.error.emit(message)

    # -- commands and sequences ---------------------------------------------------------
    def _run_sequence(self, sequence: str, *, locked: bool) -> bool:
        if self._state is not MeetingState.OPEN or self._busy or self._api is None:
            return False
        self._set_busy(True)
        actions = self._core.set_locked(locked)
        self._emit_views()
        api, room = self._api, self.room

        def apply() -> _ApiResult:
            # The attendee page reads the lock from the room: called on (locked) switches
            # the microphone on by itself; before the meeting attendees unmute themselves.
            failures = list(apply_actions(api, room, actions))
            try:
                api.update_room_metadata(room, room_metadata(locked))
            except RoomApiError as exc:
                failures.append(StepFailure("room_lock", exc.code))
            return _ApiResult("applied", sequence=sequence, failures=tuple(failures))

        self._submit(
            apply,
            on_error=lambda: _ApiResult("applied", sequence=sequence,
                                        failures=(StepFailure("lock", "unavailable"),)),
        )
        return True

    def _command(self, actions: list[MeetingAction]) -> None:
        # The publisher follows the core's spotlight (it may end by lobby or removal).
        self._publisher.set_spotlight(self._core.spotlighted)
        self._emit_views()
        api = self._api
        if not actions or api is None or self._state is not MeetingState.OPEN:
            return
        room = self.room
        self._submit(lambda: _ApiResult("applied", failures=apply_actions(api, room, actions)))

    def _submit(self, work: Callable[[], _ApiResult],
                on_error: Callable[[], _ApiResult] | None = None) -> None:
        executor = self._executor
        if executor is None:
            return

        def run() -> None:
            try:
                result = work()
            except Exception:  # noqa: BLE001 - surfaced through the result
                log.warning("Meeting server call failed", exc_info=True)
                if on_error is None:
                    return
                result = on_error()
            self._api_result.emit(result)

        try:
            executor.submit(run)
        except RuntimeError:  # shut down while stopping
            return

    def _on_api_result(self, result: _ApiResult) -> None:
        if self._executor is None:
            return  # stopped meanwhile
        if result.kind == "room_ready":
            if self._state is MeetingState.STARTING:
                self._set_state(MeetingState.OPEN)
                if not self._start_attendee_server():
                    return
                self._start_publishing()
                self._poll.start()
            self.refresh()
        elif result.kind == "room_failed":
            self._on_failure("Could not create the meeting room")
        elif result.kind == "snapshot":
            self._poll_in_flight = False
            if result.value is not None:
                actions = self._core.observe(result.value)
                self._emit_views()
                if actions:
                    self._command(actions)
        elif result.kind == "applied":
            if result.failures:
                log.warning("Some meeting actions failed: %s", result.failures)
            if result.sequence is not None:
                self._set_busy(False)
                self.operation_finished.emit(SequenceResult(result.sequence, result.failures))
            self.refresh()

    # -- state ----------------------------------------------------------------------------
    def _emit_views(self) -> None:
        participants = self._core.participants()
        hands = self._core.hands()
        attendance = self._core.attendance()
        views = (tuple(participants), tuple(hands), attendance)
        if views == self._last_views:
            return
        previous = self._last_views
        self._last_views = views
        if not previous or previous[1:] != views[1:]:
            self._publish_hands()
        if not previous or previous[0] != views[0]:
            self.participants_changed.emit(participants)
        if not previous or previous[1] != views[1]:
            self.hands_changed.emit(hands)
        if not previous or previous[2] != attendance:
            self.attendance_changed.emit(attendance)

    def _set_state(self, state: MeetingState) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state.value)

    def _set_connected(self, connected: bool) -> None:
        if connected != self._connected:
            self._connected = connected
            self.connection_changed.emit(connected)

    def _set_busy(self, busy: bool) -> None:
        if busy != self._busy:
            self._busy = busy
            self.busy_changed.emit(busy)


def _server_available() -> bool:
    try:
        server_executable()
    except ServerUnavailableError:
        return False
    return True


def create_hosted_meeting_service(config_dir: Path, parent: QObject | None = None) -> SolinMeetingService:
    """The Solin-hosted provider with its own supervised server and publisher."""
    return SolinMeetingService(
        ConferenceServerService(config_dir),
        MeetingPublisherService(),
        available=_server_available,
        parent=parent,
    )


def room_metadata(locked: bool) -> str:
    return json.dumps({"solin": 1, "locked": bool(locked)}, separators=(",", ":"))


def default_attendee_server(livekit_http_url: str) -> AttendeeServer:
    from solin.core.foundation.resources import application_resource_path

    from .web_client import WebClientUnavailableError, client_directory

    try:
        client = client_directory()
    except WebClientUnavailableError as exc:
        log.warning("%s; attendees cannot join from a browser", exc)
        client = None
    return AttendeeServer(AttendeeHttpApplication(
        pages_directory=application_resource_path("conference_web"),
        client_directory=client,
        livekit_http_url=livekit_http_url,
    ))


def first_lan_address() -> str:
    """The address attendees on this network use to reach the hall (loopback if none)."""
    from solin.core.remote_control.network import discover_lan_interfaces

    interfaces = discover_lan_interfaces()
    return str(interfaces[0].ipv4_address) if interfaces else "127.0.0.1"
