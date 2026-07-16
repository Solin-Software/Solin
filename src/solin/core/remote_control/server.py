from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
import json
import logging
from pathlib import Path
import re
import ssl
import threading
from typing import Final

from aiohttp import WSMsgType, web

from ..meetings.meeting_weeks import current_monday
from .certificates import TLSIdentity
from .contracts import (
    CommandError,
    CommandErrorCode,
    CommandResult,
    JsonObject,
    RemoteCommand,
    parse_projection_command,
)
from .security import (
    DEFAULT_SESSION_ABSOLUTE_TTL_SECONDS,
    InMemorySessionStore,
    LoginRateLimiter,
    RemoteControlCredentialsStore,
    SessionGenerationChangedError,
    SessionLimitReachedError,
)
from .settings import REMOTE_CONTROL_PORT
from .state import (
    ProjectionCommandSession,
    RemoteControlRuntimeSnapshot,
    RemoteControlStateStore,
)


log = logging.getLogger(__name__)

REMOTE_CONTROL_PREFIX: Final = "/remote"
SESSION_COOKIE_NAME: Final = "__Host-solin_remote"
_MAX_LOGIN_BODY_BYTES: Final = 2_048
_MAX_COMMAND_BODY_BYTES: Final = 8_192
_COMMAND_TIMEOUT_SECONDS: Final = 10.0
_COMMAND_SHUTDOWN_TIMEOUT_SECONDS: Final = 5.0
_REQUEST_BODY_TIMEOUT_SECONDS: Final = 5.0
_MAX_THUMBNAIL_BYTES: Final = 2 * 1024 * 1024
_MAX_LOGIN_HASHES: Final = 2
_MAX_WEBSOCKETS: Final = 32
_MAX_WEBSOCKETS_PER_SESSION: Final = 4
_WEBSOCKET_HEARTBEAT_SECONDS: Final = 20.0
_OPAQUE_ROUTE_ID: Final = re.compile(r"[A-Za-z0-9._~:-]{1,512}\Z")
_SECURITY_HEADERS: Final = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
_CSP: Final = (
    "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "object-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'"
)
_STATIC_CONTENT_TYPES: Final = {
    ".css": "text/css",
    ".html": "text/html",
    ".ico": "image/x-icon",
    ".js": "text/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}

CommandHandler = Callable[[RemoteCommand], Awaitable[CommandError | None]]
ThumbnailHandler = Callable[[str, str, str], Awaitable[bytes | None]]
CollectionThumbnailHandler = Callable[[str, str], Awaitable[bytes | None]]


@dataclass(frozen=True, slots=True)
class RemoteControlServerDependencies:
    state: RemoteControlStateStore
    command_session: ProjectionCommandSession
    credentials: RemoteControlCredentialsStore
    sessions: InMemorySessionStore
    rate_limiter: LoginRateLimiter
    assets_directory: Path
    command_handler: CommandHandler
    thumbnail_handler: ThumbnailHandler | None = None
    collection_thumbnail_handler: CollectionThumbnailHandler | None = None
    session_count_changed: Callable[[int], None] | None = None
    profile_id: str = ""
    profile_name: str = ""
    profile_locale: str = "pt-BR"
    meeting_week_start: Callable[[], date] = current_monday


@dataclass(frozen=True, slots=True)
class RemoteControlServerBinding:
    host: str
    port: int
    tls_identity: TLSIdentity

    def __post_init__(self) -> None:
        if self.host != str(self.tls_identity.ipv4_address):
            raise ValueError("Server host must match the certificate IPv4 address")
        if self.port != REMOTE_CONTROL_PORT:
            raise ValueError(f"Remote control must use fixed port {REMOTE_CONTROL_PORT}")

    @property
    def origin(self) -> str:
        return f"https://{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.origin}{REMOTE_CONTROL_PREFIX}/"


class RemoteControlHttpApplication:
    """Authenticated aiohttp adapter around immutable remote-control contracts."""

    def __init__(
        self,
        dependencies: RemoteControlServerDependencies,
        *,
        allowed_origin: str,
        trust_certificate_der: bytes | None = None,
    ) -> None:
        self._dependencies = dependencies
        self._allowed_origin = allowed_origin.rstrip("/")
        self._allowed_host = self._allowed_origin.removeprefix("https://").casefold()
        self._trust_certificate_der = trust_certificate_der
        self._websockets: dict[web.WebSocketResponse, asyncio.Lock] = {}
        self._websocket_sessions: dict[web.WebSocketResponse, str] = {}
        self._websocket_reservations: dict[str, int] = {}
        self._websocket_reservation_count = 0
        self._command_tasks: set[asyncio.Task[CommandResult]] = set()
        self._login_hash_tasks: set[asyncio.Task[bool]] = set()
        self._login_hash_slots = asyncio.Semaphore(_MAX_LOGIN_HASHES)
        self._event_sequence = 0
        self._last_catalog_revision = dependencies.state.catalog_revision
        self._last_playback_revision = dependencies.state.playback_revision
        self._app = web.Application(
            client_max_size=_MAX_COMMAND_BODY_BYTES,
            middlewares=[self._security_headers_middleware],
        )
        self._app.on_shutdown.append(self._shutdown_background_tasks)
        self._configure_routes()

    @property
    def app(self) -> web.Application:
        return self._app

    async def publish_snapshot(self) -> None:
        if not self._websockets:
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
        sockets = tuple(self._websockets)
        deliveries = await asyncio.gather(
            *(self._send_many(socket, events) for socket in sockets),
            return_exceptions=True,
        )
        stale = [
            socket
            for socket, delivery in zip(sockets, deliveries, strict=True)
            if isinstance(delivery, BaseException)
        ]
        for socket in stale:
            self._websockets.pop(socket, None)
            self._websocket_sessions.pop(socket, None)
        if stale:
            await asyncio.gather(
                *(socket.close() for socket in stale),
                return_exceptions=True,
            )

    @web.middleware
    async def _security_headers_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        error: web.HTTPException | None = None
        try:
            if request.headers.get("Host", "").casefold() != self._allowed_host:
                raise web.HTTPForbidden(text="Host rejected")
            else:
                response = await handler(request)
        except web.HTTPException as caught:
            response = caught
            error = caught
        if error is not None and request.path.startswith(f"{REMOTE_CONTROL_PREFIX}/api/"):
            code = {
                400: "invalid",
                401: "unauthorized",
                403: "forbidden",
                404: "not_found",
                408: "unavailable",
                413: "invalid",
                415: "invalid",
                429: "rate_limited",
                503: "unavailable",
            }.get(error.status, "failed")
            response = web.json_response(
                {
                    "error": {
                        "code": code,
                        "message": error.text or error.reason,
                        "retryable": error.status in (408, 429, 503),
                    }
                },
                status=error.status,
                headers={
                    name: value
                    for name, value in error.headers.items()
                    if name.casefold() not in {"content-type", "content-length"}
                },
            )
            error = None
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.path.startswith(f"{REMOTE_CONTROL_PREFIX}/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        if error is not None:
            raise error
        return response

    def _configure_routes(self) -> None:
        prefix = REMOTE_CONTROL_PREFIX
        self._app.router.add_post(f"{prefix}/api/auth/login", self._login)
        self._app.router.add_post(f"{prefix}/api/logout", self._logout)
        self._app.router.add_get(f"{prefix}/api/bootstrap", self._bootstrap)
        self._app.router.add_get(f"{prefix}/api/catalog", self._catalog)
        self._app.router.add_get(f"{prefix}/api/playback", self._playback)
        self._app.router.add_get(
            f"{prefix}/api/thumbnails/{{source}}/{{collection_id}}/{{node_id}}",
            self._thumbnail,
        )
        self._app.router.add_get(
            f"{prefix}/api/collection-thumbnails/{{source}}/{{collection_id}}",
            self._collection_thumbnail,
        )
        self._app.router.add_post(f"{prefix}/api/commands", self._command)
        self._app.router.add_get(f"{prefix}/api/ws", self._websocket)
        self._app.router.add_get(
            f"{prefix}/trust-certificate.cer",
            self._trust_certificate,
        )
        self._app.router.add_get(f"{prefix}/", self._index)
        self._app.router.add_get(f"{prefix}/index.html", self._index)
        self._app.router.add_get(f"{prefix}/{{asset:.*}}", self._asset)

    async def _index(self, request: web.Request) -> web.StreamResponse:
        return await self._serve_asset("index.html")

    async def _trust_certificate(self, request: web.Request) -> web.Response:
        del request
        if not self._trust_certificate_der:
            raise web.HTTPNotFound()
        return web.Response(
            body=self._trust_certificate_der,
            content_type="application/pkix-cert",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": ('attachment; filename="solin-remote-authority.cer"'),
            },
        )

    async def _asset(self, request: web.Request) -> web.StreamResponse:
        requested = request.match_info.get("asset", "")
        if requested.startswith("api/") or requested == "ws":
            raise web.HTTPNotFound()
        return await self._serve_asset(requested)

    async def _serve_asset(self, requested: str) -> web.StreamResponse:
        root = self._dependencies.assets_directory.resolve()
        relative = requested.strip("/")
        if not relative or "\\" in relative:
            raise web.HTTPNotFound()
        candidate = (root / relative).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise web.HTTPNotFound()
        content_type = _STATIC_CONTENT_TYPES.get(candidate.suffix.lower())
        if content_type is None:
            raise web.HTTPNotFound()
        response = web.FileResponse(candidate, headers={"Content-Type": content_type})
        if candidate.name == "service-worker.js":
            response.headers["Service-Worker-Allowed"] = f"{REMOTE_CONTROL_PREFIX}/"
            response.headers["Cache-Control"] = "no-cache"
        elif candidate.suffix.lower() == ".html":
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
        return response

    async def _login(self, request: web.Request) -> web.Response:
        self._require_same_origin(request)
        payload = await self._strict_json(request, _MAX_LOGIN_BODY_BYTES)
        if set(payload) != {"username", "password"}:
            raise web.HTTPBadRequest(text="Invalid login request")
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise web.HTTPBadRequest(text="Invalid login request")

        client_address = request.remote or "<unknown>"
        rate_limit = self._dependencies.rate_limiter.attempt(client_address, username)
        if not rate_limit.allowed:
            raise web.HTTPTooManyRequests(
                headers={"Retry-After": str(max(1, int(rate_limit.retry_after_seconds + 0.999)))},
                text="Too many login attempts",
            )
        session_generation = self._dependencies.sessions.generation
        authenticated = await self._authenticate_credentials(username, password)
        if not authenticated:
            raise web.HTTPUnauthorized(text="Invalid username or password")
        self._dependencies.rate_limiter.reset_identity(client_address, username)
        existing_token = request.cookies.get(SESSION_COOKIE_NAME, "")
        if existing_token and self._dependencies.sessions.revoke(existing_token):
            await self._close_session_websockets(existing_token)
        try:
            session_credentials = self._dependencies.sessions.create(
                self._dependencies.credentials.configured_username(),
                expected_generation=session_generation,
            )
        except SessionGenerationChangedError as error:
            raise web.HTTPUnauthorized(text="Invalid username or password") from error
        except SessionLimitReachedError as error:
            raise web.HTTPServiceUnavailable(text="Session limit reached") from error
        self._publish_session_count()

        response = self._json_response(
            {
                "ok": True,
                "csrfToken": session_credentials.csrf_token,
                **self._bootstrap_payload(session_credentials.csrf_token),
            }
        )
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_credentials.session_token,
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/",
            max_age=DEFAULT_SESSION_ABSOLUTE_TTL_SECONDS,
        )
        return response

    async def _authenticate_credentials(self, username: str, password: str) -> bool:
        await self._login_hash_slots.acquire()
        try:
            task = asyncio.create_task(
                asyncio.to_thread(
                    self._dependencies.credentials.authenticate,
                    username,
                    password,
                )
            )
        except BaseException:  # noqa: BLE001 - release a reserved hash slot on setup failure
            self._login_hash_slots.release()
            raise
        self._login_hash_tasks.add(task)
        task.add_done_callback(self._login_hash_finished)
        return await asyncio.shield(task)

    def _login_hash_finished(self, task: asyncio.Task[bool]) -> None:
        self._login_hash_tasks.discard(task)
        self._login_hash_slots.release()
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error(
                "Remote-control password verification failed unexpectedly",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _logout(self, request: web.Request) -> web.Response:
        token = self._authorize_mutation(request)
        self._dependencies.sessions.revoke(token)
        await self._close_session_websockets(token)
        self._publish_session_count()
        response = self._json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE_NAME, path="/", secure=True, httponly=True)
        return response

    async def _bootstrap(self, request: web.Request) -> web.Response:
        token = self._require_session(request)
        session = self._dependencies.sessions.touch(token)
        if session is None:
            raise web.HTTPUnauthorized()
        csrf_token = self._dependencies.sessions.csrf_token(token)
        if csrf_token is None:
            raise web.HTTPUnauthorized()
        return self._json_response(self._bootstrap_payload(csrf_token, session.principal))

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
        if any(_OPAQUE_ROUTE_ID.fullmatch(part) is None for part in route_parts):
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
        if any(_OPAQUE_ROUTE_ID.fullmatch(part) is None for part in route_parts):
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
            payload = await self._strict_json(request, _MAX_COMMAND_BODY_BYTES)
            command = parse_projection_command(payload)
        except (TypeError, ValueError) as error:
            raise web.HTTPBadRequest(text="Invalid command") from error
        self._authorize_mutation(request)

        decision = self._dependencies.command_session.prepare(command)
        if not decision.should_execute:
            assert decision.result is not None
            return self._json_response(decision.result.to_dict())

        task = asyncio.create_task(self._execute_reserved_command(command))
        self._command_tasks.add(task)
        task.add_done_callback(self._command_task_finished)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_COMMAND_TIMEOUT_SECONDS,
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
            log.exception("Remote-control command failed")
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
        try:
            await self.publish_snapshot()
        except Exception:  # noqa: BLE001 - snapshot fan-out must not alter command result
            log.exception("Could not publish the completed remote-control command")
        return result

    def _command_task_finished(self, task: asyncio.Task[CommandResult]) -> None:
        self._command_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error(
                "Background remote-control command failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _shutdown_background_tasks(self, _app: web.Application) -> None:
        login_hash_tasks = tuple(self._login_hash_tasks)
        if login_hash_tasks:
            await asyncio.gather(*login_hash_tasks, return_exceptions=True)
        tasks = tuple(self._command_tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(
            tasks,
            timeout=_COMMAND_SHUTDOWN_TIMEOUT_SECONDS,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        self._require_same_origin(request)
        session_token = self._require_session(request)
        self._reserve_websocket(session_token)
        socket = web.WebSocketResponse(
            heartbeat=_WEBSOCKET_HEARTBEAT_SECONDS,
            receive_timeout=65.0,
            max_msg_size=1_024,
            compress=False,
        )
        try:
            await socket.prepare(request)
        finally:
            self._release_websocket_reservation(session_token)
        first_socket = not self._websockets
        self._websockets[socket] = asyncio.Lock()
        self._websocket_sessions[socket] = session_token
        heartbeat_task: asyncio.Task[None] | None = None
        try:
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
            await self._send_many(
                socket,
                (
                    self._event(
                        "catalog.snapshot",
                        snapshot.catalog.to_dict(),
                        advance=False,
                        snapshot=snapshot,
                        sequence=baseline_sequence,
                    ),
                    self._event(
                        "playback.snapshot",
                        snapshot.playback.to_dict(),
                        advance=False,
                        snapshot=snapshot,
                        sequence=baseline_sequence,
                    ),
                ),
            )
            heartbeat_task = asyncio.create_task(self._heartbeat(socket, session_token))
            async for message in socket:
                if message.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                    await socket.close(
                        code=1008,
                        message=b"Commands must use the authenticated HTTP endpoint",
                    )
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                    break
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            self._websockets.pop(socket, None)
            self._websocket_sessions.pop(socket, None)
        return socket

    def _reserve_websocket(self, session_token: str) -> None:
        active_for_session = sum(
            token == session_token for token in self._websocket_sessions.values()
        )
        reserved_for_session = self._websocket_reservations.get(session_token, 0)
        if len(self._websockets) + self._websocket_reservation_count >= _MAX_WEBSOCKETS:
            raise web.HTTPServiceUnavailable(text="WebSocket connection limit reached")
        if active_for_session + reserved_for_session >= _MAX_WEBSOCKETS_PER_SESSION:
            raise web.HTTPTooManyRequests(text="Session WebSocket limit reached")
        self._websocket_reservation_count += 1
        self._websocket_reservations[session_token] = reserved_for_session + 1

    def _release_websocket_reservation(self, session_token: str) -> None:
        reserved = self._websocket_reservations.get(session_token, 0)
        if reserved <= 1:
            self._websocket_reservations.pop(session_token, None)
        else:
            self._websocket_reservations[session_token] = reserved - 1
        self._websocket_reservation_count = max(
            0,
            self._websocket_reservation_count - 1,
        )

    async def _close_session_websockets(self, session_token: str) -> None:
        sockets = [
            socket for socket, token in self._websocket_sessions.items() if token == session_token
        ]
        if not sockets:
            return
        event = self._event("session.revoked", {}, advance=False)
        for socket in sockets:
            try:
                await self._send(socket, event)
            except (TimeoutError, ConnectionError, RuntimeError) as error:
                log.debug("Could not notify a revoked remote session: %s", error)
            finally:
                await socket.close(code=4401, message=b"Session revoked")

    async def _heartbeat(
        self,
        socket: web.WebSocketResponse,
        session_token: str,
    ) -> None:
        while not socket.closed:
            await asyncio.sleep(_WEBSOCKET_HEARTBEAT_SECONDS)
            self._publish_expired_session_count()
            if self._dependencies.sessions.resolve(session_token) is None:
                await socket.close(code=4401, message=b"Session expired")
                return
            await self._send(
                socket,
                {
                    "type": "heartbeat",
                    "serverInstanceId": self._dependencies.state.server_instance_id,
                    "sequence": self._event_sequence,
                    "currentMeetingWeekStart": self._current_meeting_week_start(),
                },
            )

    async def _send(self, socket: web.WebSocketResponse, payload: JsonObject) -> None:
        await self._send_many(socket, (payload,))

    async def _send_many(
        self,
        socket: web.WebSocketResponse,
        payloads: tuple[JsonObject, ...] | list[JsonObject],
    ) -> None:
        lock = self._websockets.get(socket)
        if lock is None:
            raise ConnectionError("WebSocket is no longer registered")
        async with lock:
            for payload in payloads:
                await asyncio.wait_for(socket.send_json(payload), timeout=1.0)

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

    def _bootstrap_payload(self, csrf_token: str, principal: str = "") -> JsonObject:
        snapshot = self._dependencies.state.snapshot()
        return {
            "authenticated": True,
            "serverInstanceId": snapshot.server_instance_id,
            "eventSequence": self._event_sequence,
            "csrfToken": csrf_token,
            "principal": principal or self._dependencies.credentials.configured_username(),
            "currentMeetingWeekStart": self._current_meeting_week_start(),
            "profile": {
                "id": self._dependencies.profile_id,
                "name": self._dependencies.profile_name,
                "locale": self._dependencies.profile_locale,
            },
            "catalogRevision": snapshot.catalog_revision,
            "playbackRevision": snapshot.playback_revision,
            "playback": snapshot.playback.to_dict(),
        }

    def _current_meeting_week_start(self) -> str:
        value = self._dependencies.meeting_week_start()
        if not isinstance(value, date):
            raise TypeError("meeting_week_start must return a date")
        return value.isoformat()

    def _publish_session_count(self) -> None:
        callback = self._dependencies.session_count_changed
        if callback is not None:
            callback(self._dependencies.sessions.active_count())

    def _require_same_origin(self, request: web.Request) -> None:
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin != self._allowed_origin:
            raise web.HTTPForbidden(text="Cross-origin request rejected")

    def _require_session(self, request: web.Request) -> str:
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        self._publish_expired_session_count()
        if not token or self._dependencies.sessions.resolve(token) is None:
            raise web.HTTPUnauthorized()
        return token

    def _authorize_mutation(self, request: web.Request) -> str:
        self._require_same_origin(request)
        self._publish_expired_session_count()
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        csrf_token = request.headers.get("X-CSRF-Token", "")
        if not token or self._dependencies.sessions.authorize_mutation(token, csrf_token) is None:
            raise web.HTTPForbidden(text="Invalid session or CSRF token")
        return token

    def _publish_expired_session_count(self) -> None:
        if self._dependencies.sessions.purge_expired():
            self._publish_session_count()

    @staticmethod
    async def _strict_json(
        request: web.Request,
        maximum_bytes: int,
        *,
        timeout_seconds: float = _REQUEST_BODY_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        if request.content_length is not None and request.content_length > maximum_bytes:
            raise web.HTTPRequestEntityTooLarge(
                max_size=maximum_bytes,
                actual_size=request.content_length,
            )
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Expected application/json")
        try:
            raw = await asyncio.wait_for(
                request.read(),
                timeout=timeout_seconds,
            )
        except TimeoutError as error:
            raise web.HTTPRequestTimeout(text="Request body timed out") from error
        if len(raw) > maximum_bytes:
            raise web.HTTPRequestEntityTooLarge(max_size=maximum_bytes, actual_size=len(raw))
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise web.HTTPBadRequest(text="Invalid JSON") from error
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object")
        return value

    @staticmethod
    def _json_response(payload: JsonObject) -> web.Response:
        return web.json_response(
            payload,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )


class RemoteControlServer:
    """Own the HTTPS/aiohttp event loop on a dedicated, stoppable thread."""

    def __init__(self, dependencies: RemoteControlServerDependencies) -> None:
        self._dependencies = dependencies
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._application: RemoteControlHttpApplication | None = None
        self._runner: web.AppRunner | None = None
        self._started = threading.Event()
        self._start_error: BaseException | None = None
        self._binding: RemoteControlServerBinding | None = None
        self._lock = threading.RLock()

    @property
    def binding(self) -> RemoteControlServerBinding | None:
        with self._lock:
            return self._binding

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and self._start_error is None

    def start(self, binding: RemoteControlServerBinding, *, timeout_seconds: float = 8.0) -> None:
        with self._lock:
            if self.is_running:
                if self._binding == binding:
                    return
                raise RuntimeError("Remote-control server is already running")
            self._binding = binding
            self._started.clear()
            self._start_error = None
            self._thread = threading.Thread(
                target=self._thread_main,
                name="solin-remote-control",
                daemon=False,
            )
            self._thread.start()
        if not self._started.wait(timeout_seconds):
            self.stop()
            raise TimeoutError("Timed out starting the remote-control server")
        if self._start_error is not None:
            error = self._start_error
            self.stop()
            raise RuntimeError("Could not start the remote-control server") from error

    def publish_snapshot(self) -> None:
        loop = self._loop
        application = self._application
        if loop is None or application is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(application.publish_snapshot(), loop)

    def stop(self, *, timeout_seconds: float = 8.0) -> None:
        with self._lock:
            thread = self._thread
            loop = self._loop
        if thread is None:
            return
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not threading.current_thread():
            thread.join(timeout_seconds)
        if thread.is_alive():
            raise TimeoutError("Timed out stopping the remote-control server")
        with self._lock:
            self._thread = None
            self._loop = None
            self._application = None
            self._runner = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._start_async())
            self._started.set()
            loop.run_forever()
        except BaseException as error:  # noqa: BLE001 - thread startup boundary
            self._start_error = error
            self._started.set()
            log.exception("Remote-control server thread failed")
        finally:
            try:
                loop.run_until_complete(self._cleanup_async())
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
            finally:
                loop.close()

    async def _start_async(self) -> None:
        binding = self._binding
        if binding is None:
            raise RuntimeError("Missing remote-control binding")
        application = RemoteControlHttpApplication(
            self._dependencies,
            allowed_origin=binding.origin,
            trust_certificate_der=binding.tls_identity.authority_certificate_der(),
        )
        runner = web.AppRunner(
            application.app,
            access_log=log,
            keepalive_timeout=30.0,
        )
        await runner.setup()
        ssl_context = _build_ssl_context(binding.tls_identity)
        site = web.TCPSite(
            runner,
            binding.host,
            binding.port,
            ssl_context=ssl_context,
            shutdown_timeout=5.0,
        )
        await site.start()
        self._application = application
        self._runner = runner

    async def _cleanup_async(self) -> None:
        runner = self._runner
        if runner is not None:
            await runner.cleanup()


def _build_ssl_context(identity: TLSIdentity) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_COMPRESSION
    context.load_cert_chain(
        certfile=str(identity.certificate_path),
        keyfile=str(identity.private_key_path),
    )
    return context
