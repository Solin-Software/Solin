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
    ATTENDEE_PREFIX,
    ClearHand,
    MeetingAction,
    MeetingCore,
    MeetingPolicy,
    RemoveParticipant,
    SetName,
    SetPermission,
    is_attendee,
)
from .provider import POST_MEETING, PRE_MEETING, SequenceResult, StepFailure
from .publisher_service import MeetingPublisherService, PublisherConfig, PublisherState
from .room_api import ParticipantNotFoundError, RoomApi, RoomApiError
from .server_binary import ServerUnavailableError, server_executable
from .server_config import ServerSettings
from .server_process import ConferenceServerService, ServerState
from .tokens import AccessTokenRequest, RoomGrants, mint_access_token

log = logging.getLogger(__name__)

ATTENDEE_TOKEN_TTL_S = 15 * 60
MAX_DEVICE_ID_LENGTH = 64


class MeetingState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    OPEN = "open"
    FAILED = "failed"


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
    _api_result = Signal(object)

    def __init__(
        self,
        server: ConferenceServerService,
        publisher: MeetingPublisherService,
        *,
        available: Callable[[], bool] = lambda: True,
        room_api_factory: Callable[[ServerSettings], RoomApi] | None = None,
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
        publisher.error.connect(self._on_failure)

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
        # The room lives only in this server, so stopping it ends the room too.
        self._server.stop(wait=wait, timeout_ms=int(timeout * 1000))
        self._api = None
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
        settings = self._settings
        if settings is None or self._state is not MeetingState.OPEN:
            return None
        device_id = device_id.strip()
        if not device_id or len(device_id) > MAX_DEVICE_ID_LENGTH or not device_id.isprintable():
            return None
        identity = ATTENDEE_PREFIX + device_id
        if not self._core.may_join(identity):
            return None
        grants = RoomGrants(settings.room, can_publish=False, can_subscribe=False,
                            can_publish_data=False, hidden=True, can_update_own_metadata=True)
        return mint_access_token(settings.server.api_key, settings.server.api_secret,
                                 AccessTokenRequest(identity, grants, name=name.strip()[:64],
                                                    ttl_seconds=ATTENDEE_TOKEN_TTL_S))

    def admit(self, identity: str) -> None:
        self._command(self._core.admit(identity))

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

        def create() -> _ApiResult:
            api.create_room(settings.room, metadata="solin")
            return _ApiResult("room_ready")

        self._submit(create, on_error=lambda: _ApiResult("room_failed"))

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
        ))

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
        self._submit(
            lambda: _ApiResult("applied", sequence=sequence,
                               failures=apply_actions(api, room, actions)),
            on_error=lambda: _ApiResult("applied", sequence=sequence,
                                        failures=(StepFailure("lock", "unavailable"),)),
        )
        return True

    def _command(self, actions: list[MeetingAction]) -> None:
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
