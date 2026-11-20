"""Authenticated HTTP/WebSocket route module for Solin Media Remote (/remote/media)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
import logging
from pathlib import Path
from typing import Final

from aiohttp import web

from ...meetings.meeting_weeks import current_monday
from ..security import (
    InMemorySessionStore,
    LoginRateLimiter,
    RemoteControlCredentialsStore,
    RemoteScope,
)
from ..server import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_BODY_BYTES,
    MEDIA_SESSION_COOKIE_NAME,
    OPAQUE_ROUTE_ID,
    REMOTE_MEDIA_PREFIX,
    SetupProvider,
    ScopedRemoteHttpModule,
    WEBSOCKET_CLOSE_TIMEOUT_SECONDS,
    WEBSOCKET_HEARTBEAT_SECONDS,
)
from ..validation import JsonObject
from .contracts import (
    CommandError,
    CommandErrorCode,
    CommandResult,
    RemoteCommand,
    parse_projection_command,
)
from .state import (
    ProjectionCommandSession,
    RemoteControlRuntimeSnapshot,
    RemoteControlStateStore,
)


log = logging.getLogger(__name__)

_MAX_THUMBNAIL_BYTES: Final = 2 * 1024 * 1024

MediaCommandHandler = Callable[[RemoteCommand], Awaitable[CommandError | None]]
ThumbnailHandler = Callable[[str, str, str], Awaitable[bytes | None]]
CollectionThumbnailHandler = Callable[[str, str], Awaitable[bytes | None]]


@dataclass(frozen=True, slots=True)
class MediaRemoteDependencies:
    state: RemoteControlStateStore
    command_session: ProjectionCommandSession
    credentials: RemoteControlCredentialsStore
    sessions: InMemorySessionStore
    rate_limiter: LoginRateLimiter
    assets_directory: Path
    command_handler: MediaCommandHandler
    setup_provider: SetupProvider
    shared_assets_directory: Path | None = None
    enabled_predicate: Callable[[], bool] | None = None
    theme_provider: Callable[[], str] | None = None
    thumbnail_handler: ThumbnailHandler | None = None
    collection_thumbnail_handler: CollectionThumbnailHandler | None = None
    profile_id: str = ""
    profile_name: str = ""
    meeting_week_start: Callable[[], date] = current_monday


class MediaRemoteHttpModule(ScopedRemoteHttpModule):
    """Mounted route module serving /remote/media with dedicated sessions and cookies."""

    def __init__(self, dependencies: MediaRemoteDependencies) -> None:
        super().__init__(
            scope=RemoteScope.MEDIA,
            url_prefix=REMOTE_MEDIA_PREFIX,
            cookie_name=MEDIA_SESSION_COOKIE_NAME,
            assets_directory=dependencies.assets_directory,
            shared_assets_directory=dependencies.shared_assets_directory,
            credentials=dependencies.credentials,
            sessions=dependencies.sessions,
            rate_limiter=dependencies.rate_limiter,
            setup_provider=dependencies.setup_provider,
            enabled_predicate=dependencies.enabled_predicate,
            theme_provider=dependencies.theme_provider,
            profile_id=dependencies.profile_id,
            profile_name=dependencies.profile_name,
        )
        self._dependencies = dependencies
        self._last_catalog_revision = dependencies.state.catalog_revision
        self._last_playback_revision = dependencies.state.playback_revision

    def register_routes(self, app: web.Application) -> None:
        self._register_common_shell_and_auth_routes(
            app,
            register_specific_routes=self._register_media_routes,
        )

    def _register_media_routes(self, app: web.Application) -> None:
        prefix = self.url_prefix
        app.router.add_get(f"{prefix}/api/catalog", self._catalog)
        app.router.add_get(f"{prefix}/api/playback", self._playback)
        app.router.add_get(
            f"{prefix}/api/thumbnails/{{source}}/{{collection_id}}/{{node_id}}",
            self._thumbnail,
        )
        app.router.add_get(
            f"{prefix}/api/collection-thumbnails/{{source}}/{{collection_id}}",
            self._collection_thumbnail,
        )
        app.router.add_post(f"{prefix}/api/commands", self._command)
        app.router.add_get(f"{prefix}/api/ws", self._websocket)

    async def publish_snapshot(self) -> None:
        async with self._publication_lock:
            if not self._auth_websockets or not self.is_enabled():
                return
            snapshot = self._dependencies.state.snapshot()
            events: list[JsonObject] = []
            if snapshot.catalog_revision != self._last_catalog_revision:
                self._last_catalog_revision = snapshot.catalog_revision
                events.append(
                    self._event(
                        "catalog.snapshot",
                        snapshot.catalog.to_dict(),
                        snapshot=snapshot,
                    )
                )
            if snapshot.playback_revision != self._last_playback_revision:
                self._last_playback_revision = snapshot.playback_revision
                events.append(
                    self._event(
                        "playback.snapshot",
                        snapshot.playback.to_dict(),
                        snapshot=snapshot,
                    )
                )
            if not events:
                return
            await self._broadcast_auth(events)

    async def publish_profile(self) -> None:
        async with self._publication_lock:
            if not self._auth_websockets or not self.is_enabled():
                return
            await self._broadcast_auth([self._event("profile.snapshot", self._profile_payload())])

    def _bootstrap_payload(self, csrf_token: str, principal: str = "") -> JsonObject:
        snapshot = self._dependencies.state.snapshot()
        return {
            "authenticated": True,
            "serverInstanceId": snapshot.server_instance_id,
            "eventSequence": self._event_sequence,
            "csrfToken": csrf_token,
            "principal": principal or self._dependencies.credentials.configured_username(),
            "currentMeetingWeekStart": self._current_meeting_week_start(),
            "profile": self._profile_payload(),
            "catalogRevision": snapshot.catalog_revision,
            "playbackRevision": snapshot.playback_revision,
            "playback": snapshot.playback.to_dict(),
        }

    def _revocation_event(self, reason: str) -> JsonObject:
        return self._event("session.revoked", {"reason": reason}, advance=False)

    async def _catalog(self, request: web.Request) -> web.Response:
        self._require_session(request)
        return self._json_response(self._dependencies.state.catalog.to_dict())

    async def _playback(self, request: web.Request) -> web.Response:
        self._require_session(request)
        return self._json_response(self._dependencies.state.playback.to_dict())

    async def _thumbnail(self, request: web.Request) -> web.Response:
        self._require_session(request)
        handler = self._dependencies.thumbnail_handler
        if handler is None:
            raise web.HTTPNotFound()
        route_parts = (
            request.match_info["source"],
            request.match_info["collection_id"],
            request.match_info["node_id"],
        )
        if any(OPAQUE_ROUTE_ID.fullmatch(part) is None for part in route_parts):
            raise web.HTTPNotFound()
        data = await handler(*route_parts)
        return self._thumbnail_response(data)

    async def _collection_thumbnail(self, request: web.Request) -> web.Response:
        self._require_session(request)
        handler = self._dependencies.collection_thumbnail_handler
        if handler is None:
            raise web.HTTPNotFound()
        route_parts = (
            request.match_info["source"],
            request.match_info["collection_id"],
        )
        if any(OPAQUE_ROUTE_ID.fullmatch(part) is None for part in route_parts):
            raise web.HTTPNotFound()
        data = await handler(*route_parts)
        return self._thumbnail_response(data)

    @staticmethod
    def _thumbnail_response(data: bytes | None) -> web.Response:
        if not data or len(data) > _MAX_THUMBNAIL_BYTES:
            raise web.HTTPNotFound()
        return web.Response(
            body=data,
            content_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=300"},
        )

    async def _command(self, request: web.Request) -> web.Response:
        self._authorize_mutation(request)
        try:
            payload = await self._strict_json(request, MAX_COMMAND_BODY_BYTES)
            command = parse_projection_command(payload)
        except (TypeError, ValueError) as error:
            raise web.HTTPBadRequest(text="Invalid command") from error
        self._authorize_mutation(request)

        decision = self._dependencies.command_session.prepare(command)
        if not decision.should_execute:
            assert decision.result is not None
            self._log_stale_command(command, decision.result, stage="precondition")
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

    async def _execute_reserved_command(self, command: RemoteCommand) -> CommandResult:
        try:
            command_error = await self._dependencies.command_handler(command)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - desktop command adapter boundary
            log.exception("Remote media command failed")
            command_error = CommandError(
                CommandErrorCode.FAILED,
                "The command could not be completed.",
            )
        if command_error is None:
            result = self._dependencies.command_session.complete_success(command)
        else:
            result = self._dependencies.command_session.complete_failure(
                command,
                command_error,
            )
            self._log_stale_command(command, result, stage="execution")
        try:
            await self.publish_snapshot()
        except Exception:  # noqa: BLE001 - snapshot fan-out must not alter command result
            log.exception("Could not publish the completed remote media command")
        return result

    @staticmethod
    def _log_stale_command(
        command: RemoteCommand,
        result: CommandResult,
        *,
        stage: str,
    ) -> None:
        error = result.error
        if error is None or error.code not in {
            CommandErrorCode.CATALOG_STALE,
            CommandErrorCode.PLAYBACK_STALE,
        }:
            return
        log.info(
            "Remote media command state conflict: type=%s code=%s stage=%s "
            "submitted_catalog_revision=%s current_catalog_revision=%d "
            "current_playback_revision=%d",
            command.type.value,
            error.code.value,
            stage,
            getattr(command, "catalog_revision", None),
            result.catalog_revision,
            result.playback_revision,
        )

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
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
                first_socket = not self._auth_websockets
                self._auth_websockets[socket] = asyncio.Lock()
                self._websocket_sessions[socket] = session_token
                self._notify_inventory_changed()
                snapshot = self._dependencies.state.snapshot()
                if first_socket:
                    self._last_catalog_revision = max(
                        self._last_catalog_revision,
                        snapshot.catalog_revision,
                    )
                    self._last_playback_revision = max(
                        self._last_playback_revision,
                        snapshot.playback_revision,
                    )
                baseline_sequence = self._event_sequence
                await self._send_auth(
                    socket,
                    self._event(
                        "session.snapshot",
                        {
                            "catalog": snapshot.catalog.to_dict(),
                            "playback": snapshot.playback.to_dict(),
                            "profile": self._profile_payload(),
                            "currentMeetingWeekStart": self._current_meeting_week_start(),
                        },
                        advance=False,
                        snapshot=snapshot,
                        sequence=baseline_sequence,
                    ),
                )
            heartbeat_task = asyncio.create_task(self._heartbeat(socket, session_token))
            await self._consume_unidirectional_socket(socket)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            self._auth_websockets.pop(socket, None)
            self._websocket_sessions.pop(socket, None)
            self._notify_inventory_changed()
        return socket

    async def _heartbeat(
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
            await self._send_auth(
                socket,
                {
                    "type": "heartbeat",
                    "serverInstanceId": self._dependencies.state.server_instance_id,
                    "sequence": self._event_sequence,
                    "currentMeetingWeekStart": self._current_meeting_week_start(),
                },
            )

    def _event(
        self,
        event_type: str,
        payload: JsonObject,
        *,
        advance: bool = True,
        snapshot: RemoteControlRuntimeSnapshot | None = None,
        sequence: int | None = None,
    ) -> JsonObject:
        if advance:
            self._event_sequence += 1
        elif sequence is None:
            sequence = self._event_sequence
        runtime_snapshot = snapshot or self._dependencies.state.snapshot()
        return {
            "type": event_type,
            "serverInstanceId": runtime_snapshot.server_instance_id,
            "sequence": self._event_sequence if sequence is None else sequence,
            "catalogRevision": runtime_snapshot.catalog_revision,
            "playbackRevision": runtime_snapshot.playback_revision,
            "payload": payload,
        }

    def _current_meeting_week_start(self) -> str:
        value = self._dependencies.meeting_week_start()
        if not isinstance(value, date):
            raise TypeError("meeting_week_start must return a date")
        return value.isoformat()
