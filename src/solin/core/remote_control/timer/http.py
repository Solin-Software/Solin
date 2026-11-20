"""Public-viewer and authenticated-controller HTTP/WS module for Solin Timer (/remote/timer)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import Final

from aiohttp import WSCloseCode, web

from ..security import (
    InMemorySessionStore,
    LoginRateLimiter,
    RemoteControlCredentialsStore,
    RemoteScope,
)
from ..server import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_BODY_BYTES,
    REMOTE_TIMER_PREFIX,
    SetupProvider,
    ScopedRemoteHttpModule,
    TIMER_SESSION_COOKIE_NAME,
    WEBSOCKET_CLOSE_TIMEOUT_SECONDS,
    WEBSOCKET_HEARTBEAT_SECONDS,
)
from ..validation import JsonObject
from .contracts import (
    TimerCommandError,
    TimerCommandErrorCode,
    TimerCommandResult,
    TimerRemoteCommand,
    parse_timer_command,
)
from .state import (
    RemoteTimerRuntimeSnapshot,
    RemoteTimerStateStore,
    TimerCommandSession,
)


log = logging.getLogger(__name__)

_MAX_AUTH_WEBSOCKETS: Final = 16
_MAX_PUBLIC_WEBSOCKETS: Final = 32
_MAX_PUBLIC_WEBSOCKETS_PER_ADDRESS: Final = 6

TimerCommandHandler = Callable[[TimerRemoteCommand], Awaitable[TimerCommandError | None]]


def _default_clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class TimerRemoteDependencies:
    state: RemoteTimerStateStore
    command_session: TimerCommandSession
    credentials: RemoteControlCredentialsStore
    sessions: InMemorySessionStore
    rate_limiter: LoginRateLimiter
    assets_directory: Path
    command_handler: TimerCommandHandler
    setup_provider: SetupProvider
    shared_assets_directory: Path | None = None
    enabled_predicate: Callable[[], bool] | None = None
    theme_provider: Callable[[], str] | None = None
    profile_id: str = ""
    profile_name: str = ""
    clock_ms: Callable[[], int] = _default_clock_ms


class TimerRemoteHttpModule(ScopedRemoteHttpModule):
    """Mounted route module serving /remote/timer with public fullscreen + authenticated control."""

    def __init__(self, dependencies: TimerRemoteDependencies) -> None:
        super().__init__(
            scope=RemoteScope.TIMER,
            url_prefix=REMOTE_TIMER_PREFIX,
            cookie_name=TIMER_SESSION_COOKIE_NAME,
            assets_directory=dependencies.assets_directory,
            shared_assets_directory=dependencies.shared_assets_directory,
            credentials=dependencies.credentials,
            sessions=dependencies.sessions,
            rate_limiter=dependencies.rate_limiter,
            setup_provider=dependencies.setup_provider,
            enabled_predicate=dependencies.enabled_predicate,
            allow_login_predicate=dependencies.credentials.has_credentials,
            theme_provider=dependencies.theme_provider,
            profile_id=dependencies.profile_id,
            profile_name=dependencies.profile_name,
            max_auth_websockets=_MAX_AUTH_WEBSOCKETS,
        )
        self._dependencies = dependencies
        self._public_websockets: dict[web.WebSocketResponse, asyncio.Lock] = {}
        self._public_websocket_addresses: dict[web.WebSocketResponse, str] = {}
        self._public_reservations: dict[str, int] = {}
        self._public_reservation_count = 0
        self._public_event_sequence = 0
        self._last_public_live_revision = dependencies.state.live_revision
        self._last_auth_live_revision = dependencies.state.live_revision
        self._last_auth_schedule_revision = dependencies.state.schedule_revision

    def public_viewer_count(self) -> int:
        return len(self._public_websockets) if self.is_enabled() else 0

    def control_available(self) -> bool:
        return self._dependencies.credentials.has_credentials()

    def register_routes(self, app: web.Application) -> None:
        self._register_common_shell_and_auth_routes(
            app,
            register_specific_routes=self._register_timer_routes,
        )

    def _register_timer_routes(self, app: web.Application) -> None:
        prefix = self.url_prefix
        app.router.add_get(f"{prefix}/api/public", self._public_bootstrap)
        app.router.add_get(f"{prefix}/api/ws/public", self._public_websocket)
        app.router.add_get(f"{prefix}/api/schedule", self._schedule)
        app.router.add_post(f"{prefix}/api/commands", self._command)
        app.router.add_get(f"{prefix}/api/ws", self._auth_websocket)

    async def publish_snapshot(self) -> None:
        async with self._publication_lock:
            if not self.is_enabled():
                return
            has_public = bool(self._public_websockets)
            has_auth = bool(self._auth_websockets)
            if not has_public and not has_auth:
                return

            now_ms = self._dependencies.clock_ms()
            snapshot = self._dependencies.state.snapshot(now_epoch_ms=now_ms)
            public_events: list[JsonObject] = []
            auth_events: list[JsonObject] = []
            live_payload: JsonObject | None = None

            if has_public and snapshot.live_revision != self._last_public_live_revision:
                self._last_public_live_revision = snapshot.live_revision
                live_payload = snapshot.live.to_dict()
                public_events.append(
                    self._public_event("timer.live", live_payload, snapshot=snapshot)
                )

            if has_auth:
                if snapshot.live_revision != self._last_auth_live_revision:
                    self._last_auth_live_revision = snapshot.live_revision
                    if live_payload is None:
                        live_payload = snapshot.live.to_dict()
                    auth_events.append(
                        self._auth_event("timer.live", live_payload, snapshot=snapshot)
                    )

                if snapshot.schedule_revision != self._last_auth_schedule_revision:
                    self._last_auth_schedule_revision = snapshot.schedule_revision
                    auth_events.append(
                        self._auth_event(
                            "timer.schedule",
                            snapshot.schedule.to_dict(),
                            snapshot=snapshot,
                        )
                    )

            if public_events:
                await self._broadcast_public(public_events)
            if auth_events:
                await self._broadcast_auth(auth_events)

    async def publish_profile(self) -> None:
        async with self._publication_lock:
            if not self.is_enabled():
                return
            has_public = bool(self._public_websockets)
            has_auth = bool(self._auth_websockets)
            if not has_public and not has_auth:
                return
            snapshot = self._dependencies.state.snapshot(
                now_epoch_ms=self._dependencies.clock_ms()
            )
            payload: JsonObject = {
                **self._profile_payload(),
                "controlAvailable": self.control_available(),
            }
            if has_public:
                await self._broadcast_public(
                    [self._public_event("profile.snapshot", payload, snapshot=snapshot)]
                )
            if has_auth:
                await self._broadcast_auth(
                    [self._auth_event("profile.snapshot", payload, snapshot=snapshot)]
                )

    async def close_disabled_connections(self) -> None:
        await super().close_disabled_connections()
        public_sockets = tuple(self._public_websockets)
        if public_sockets:
            await asyncio.gather(
                *(
                    socket.close(
                        code=WSCloseCode.GOING_AWAY,
                        message=b"Remote service disabled",
                        drain=False,
                    )
                    for socket in public_sockets
                ),
                return_exceptions=True,
            )
        self._notify_public_viewers_changed()

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(
                socket.close(
                    code=WSCloseCode.GOING_AWAY,
                    message=b"Server shutting down",
                    drain=False,
                )
                for socket in tuple(self._public_websockets)
            ),
            return_exceptions=True,
        )
        await super().shutdown()

    def _bootstrap_payload(self, csrf_token: str, principal: str = "") -> JsonObject:
        snapshot = self._dependencies.state.snapshot(
            now_epoch_ms=self._dependencies.clock_ms()
        )
        return {
            "authenticated": True,
            "controlAvailable": self.control_available(),
            "serverInstanceId": snapshot.server_instance_id,
            "eventSequence": self._event_sequence,
            "csrfToken": csrf_token,
            "principal": principal or self._dependencies.credentials.configured_username(),
            "profile": self._profile_payload(),
            "liveRevision": snapshot.live_revision,
            "scheduleRevision": snapshot.schedule_revision,
            "live": snapshot.live.to_dict(),
            "schedule": snapshot.schedule.to_dict(),
        }

    def _revocation_event(self, reason: str) -> JsonObject:
        return self._auth_event("session.revoked", {"reason": reason}, advance=False)

    async def _public_bootstrap(self, request: web.Request) -> web.Response:
        self._require_enabled()
        self._publish_expired_sessions()
        token = request.cookies.get(self.cookie_name, "")
        has_valid_session = bool(token and self._sessions.resolve(token) is not None)
        setup = self._setup_payload()
        snapshot = self._dependencies.state.snapshot(
            now_epoch_ms=self._dependencies.clock_ms()
        )
        return self._json_response(
            {
                "authenticated": has_valid_session,
                "controlAvailable": self.control_available(),
                "serverInstanceId": snapshot.server_instance_id,
                "eventSequence": self._public_event_sequence,
                "profile": self._profile_payload(),
                "tls": setup["tls"],
                "liveRevision": snapshot.live_revision,
                "live": snapshot.live.to_dict(),
            }
        )

    async def _schedule(self, request: web.Request) -> web.Response:
        self._require_session(request)
        return self._json_response(self._dependencies.state.schedule.to_dict())

    async def _command(self, request: web.Request) -> web.Response:
        self._authorize_mutation(request)
        try:
            payload = await self._strict_json(request, MAX_COMMAND_BODY_BYTES)
            command = parse_timer_command(payload)
        except (TypeError, ValueError) as error:
            raise web.HTTPBadRequest(text="Invalid command") from error
        self._authorize_mutation(request)

        decision = self._dependencies.command_session.prepare(command)
        if not decision.should_execute:
            assert decision.result is not None
            return self._json_response(decision.result.to_dict())

        task = self._track_command_task(self._execute_reserved_command(command))
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            pending = self._dependencies.command_session.prepare(command)
            assert pending.result is not None
            result = pending.result
        return self._json_response(result.to_dict())

    async def _execute_reserved_command(
        self,
        command: TimerRemoteCommand,
    ) -> TimerCommandResult:
        try:
            command_error = await self._dependencies.command_handler(command)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - desktop command adapter boundary
            log.exception("Remote timer command failed")
            command_error = TimerCommandError(
                TimerCommandErrorCode.FAILED,
                "The timer command could not be completed.",
            )
        if command_error is None:
            result = self._dependencies.command_session.complete_success(command)
        else:
            result = self._dependencies.command_session.complete_failure(
                command,
                command_error,
            )
        try:
            await self.publish_snapshot()
        except Exception:  # noqa: BLE001 - snapshot fan-out must not alter command result
            log.exception("Could not publish the completed remote timer command")
        return result

    async def _public_websocket(self, request: web.Request) -> web.StreamResponse:
        self._require_enabled()
        self._require_same_origin(request)
        address = (request.remote or "<unknown>").strip().casefold() or "<unknown>"
        self._reserve_public_websocket(address)
        socket = web.WebSocketResponse(
            timeout=WEBSOCKET_CLOSE_TIMEOUT_SECONDS,
            heartbeat=WEBSOCKET_HEARTBEAT_SECONDS,
            receive_timeout=65.0,
            max_msg_size=1_024,
            compress=False,
        )
        try:
            await socket.prepare(request)
        finally:
            self._release_public_websocket_reservation(address)

        heartbeat_task: asyncio.Task[None] | None = None
        try:
            async with self._publication_lock:
                first_public = not self._public_websockets
                self._public_websockets[socket] = asyncio.Lock()
                self._public_websocket_addresses[socket] = address
                self._notify_public_viewers_changed()
                snapshot = self._dependencies.state.snapshot(
                    now_epoch_ms=self._dependencies.clock_ms()
                )
                if first_public:
                    self._last_public_live_revision = max(
                        self._last_public_live_revision,
                        snapshot.live_revision,
                    )
                initial_event = self._public_event(
                    "public.snapshot",
                    {
                        "live": snapshot.live.to_dict(),
                        "profile": self._profile_payload(),
                        "controlAvailable": self.control_available(),
                    },
                    advance=False,
                    snapshot=snapshot,
                    sequence=self._public_event_sequence,
                )
                await self._send_many_to_map(
                    self._public_websockets,
                    socket,
                    (initial_event,),
                )
            heartbeat_task = asyncio.create_task(self._public_heartbeat(socket))
            await self._consume_unidirectional_socket(socket)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            self._public_websockets.pop(socket, None)
            self._public_websocket_addresses.pop(socket, None)
            self._notify_public_viewers_changed()
        return socket

    async def _auth_websocket(self, request: web.Request) -> web.StreamResponse:
        self._require_enabled()
        self._require_same_origin(request)
        session_token = self._require_session(request)
        self._reserve_auth_websocket(session_token)
        socket = web.WebSocketResponse(
            timeout=WEBSOCKET_CLOSE_TIMEOUT_SECONDS,
            heartbeat=WEBSOCKET_HEARTBEAT_SECONDS,
            receive_timeout=65.0,
            max_msg_size=1_024,
            compress=False,
        )
        try:
            await socket.prepare(request)
        finally:
            self._release_auth_websocket_reservation(session_token)

        heartbeat_task: asyncio.Task[None] | None = None
        try:
            async with self._publication_lock:
                first_auth = not self._auth_websockets
                self._auth_websockets[socket] = asyncio.Lock()
                self._websocket_sessions[socket] = session_token
                self._notify_inventory_changed()
                snapshot = self._dependencies.state.snapshot(
                    now_epoch_ms=self._dependencies.clock_ms()
                )
                if first_auth:
                    self._last_auth_live_revision = max(
                        self._last_auth_live_revision,
                        snapshot.live_revision,
                    )
                    self._last_auth_schedule_revision = max(
                        self._last_auth_schedule_revision,
                        snapshot.schedule_revision,
                    )
                await self._send_auth(
                    socket,
                    self._auth_event(
                        "session.snapshot",
                        {
                            "live": snapshot.live.to_dict(),
                            "schedule": snapshot.schedule.to_dict(),
                            "profile": self._profile_payload(),
                            "controlAvailable": self.control_available(),
                        },
                        advance=False,
                        snapshot=snapshot,
                        sequence=self._event_sequence,
                    ),
                )
            heartbeat_task = asyncio.create_task(self._auth_heartbeat(socket, session_token))
            await self._consume_unidirectional_socket(socket)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            self._auth_websockets.pop(socket, None)
            self._websocket_sessions.pop(socket, None)
            self._notify_inventory_changed()
        return socket

    def _reserve_public_websocket(self, address: str) -> None:
        active_for_address = sum(
            current == address for current in self._public_websocket_addresses.values()
        )
        reserved_for_address = self._public_reservations.get(address, 0)
        if (
            len(self._public_websockets) + self._public_reservation_count
            >= _MAX_PUBLIC_WEBSOCKETS
        ):
            raise web.HTTPServiceUnavailable(text="Public viewer connection limit reached")
        if active_for_address + reserved_for_address >= _MAX_PUBLIC_WEBSOCKETS_PER_ADDRESS:
            raise web.HTTPTooManyRequests(text="Device viewer connection limit reached")
        self._public_reservation_count += 1
        self._public_reservations[address] = reserved_for_address + 1

    def _release_public_websocket_reservation(self, address: str) -> None:
        reserved = self._public_reservations.get(address, 0)
        if reserved <= 1:
            self._public_reservations.pop(address, None)
        else:
            self._public_reservations[address] = reserved - 1
        self._public_reservation_count = max(0, self._public_reservation_count - 1)

    async def _broadcast_public(self, events: Sequence[JsonObject]) -> None:
        stale = await self._broadcast_to_map(self._public_websockets, events)
        for socket in stale:
            self._public_websocket_addresses.pop(socket, None)
        if stale:
            self._notify_public_viewers_changed()

    async def _public_heartbeat(self, socket: web.WebSocketResponse) -> None:
        while not socket.closed:
            await asyncio.sleep(WEBSOCKET_HEARTBEAT_SECONDS)
            if not self.is_enabled():
                await socket.close(
                    code=WSCloseCode.GOING_AWAY,
                    message=b"Remote service disabled",
                )
                return
            snapshot = self._dependencies.state.snapshot(
                now_epoch_ms=self._dependencies.clock_ms()
            )
            await self._send_many_to_map(
                self._public_websockets,
                socket,
                (
                    {
                        "type": "heartbeat",
                        "serverInstanceId": snapshot.server_instance_id,
                        "sequence": self._public_event_sequence,
                        "liveRevision": snapshot.live_revision,
                        "serverEpochMs": snapshot.live.server_epoch_ms,
                        "utcOffsetMinutes": snapshot.live.utc_offset_minutes,
                    },
                ),
            )

    async def _auth_heartbeat(
        self,
        socket: web.WebSocketResponse,
        session_token: str,
    ) -> None:
        while not socket.closed:
            await asyncio.sleep(WEBSOCKET_HEARTBEAT_SECONDS)
            self._publish_expired_sessions()
            if not self.is_enabled():
                await socket.close(code=4401, message=b"Remote service disabled")
                return
            if self._sessions.resolve(session_token) is None:
                await socket.close(code=4401, message=b"Session expired")
                return
            snapshot = self._dependencies.state.snapshot(
                now_epoch_ms=self._dependencies.clock_ms()
            )
            await self._send_auth(
                socket,
                {
                    "type": "heartbeat",
                    "serverInstanceId": snapshot.server_instance_id,
                    "sequence": self._event_sequence,
                    "liveRevision": snapshot.live_revision,
                    "scheduleRevision": snapshot.schedule_revision,
                    "serverEpochMs": snapshot.live.server_epoch_ms,
                    "utcOffsetMinutes": snapshot.live.utc_offset_minutes,
                },
            )

    def _public_event(
        self,
        event_type: str,
        payload: JsonObject,
        *,
        advance: bool = True,
        snapshot: RemoteTimerRuntimeSnapshot | None = None,
        sequence: int | None = None,
    ) -> JsonObject:
        if advance:
            self._public_event_sequence += 1
        elif sequence is None:
            sequence = self._public_event_sequence
        runtime_snapshot = snapshot or self._dependencies.state.snapshot(
            now_epoch_ms=self._dependencies.clock_ms()
        )
        return {
            "type": event_type,
            "serverInstanceId": runtime_snapshot.server_instance_id,
            "sequence": self._public_event_sequence if sequence is None else sequence,
            "liveRevision": runtime_snapshot.live_revision,
            "payload": payload,
        }

    def _auth_event(
        self,
        event_type: str,
        payload: JsonObject,
        *,
        advance: bool = True,
        snapshot: RemoteTimerRuntimeSnapshot | None = None,
        sequence: int | None = None,
    ) -> JsonObject:
        if advance:
            self._event_sequence += 1
        elif sequence is None:
            sequence = self._event_sequence
        runtime_snapshot = snapshot or self._dependencies.state.snapshot(
            now_epoch_ms=self._dependencies.clock_ms()
        )
        return {
            "type": event_type,
            "serverInstanceId": runtime_snapshot.server_instance_id,
            "sequence": self._event_sequence if sequence is None else sequence,
            "liveRevision": runtime_snapshot.live_revision,
            "scheduleRevision": runtime_snapshot.schedule_revision,
            "payload": payload,
        }
