"""The hall's web server for meeting attendees and the stage.

* ``/join`` — the attendee page; ``/api/join`` hands it a lobby token;
* ``/stage/<secret>`` — the read-only raised-hands list for the conductor's device;
* ``/rtc…`` — LiveKit signaling, proxied to the local server so the page, the token API and
  signaling share one origin (one port, one TLS setup — what a hub tunnel forwards later).
  Media still flows directly between browsers and the LiveKit server over ICE.

:class:`AttendeeHttpApplication` is the aiohttp app (Qt-free, tested with ``TestClient``);
:class:`AttendeeServer` runs it on its own thread and event loop. The app never calls into
the Qt thread: the meeting service publishes immutable snapshots
(:class:`~solin.core.conference.attendee_access.AttendeeAccess` and the hands list).

The end-to-end encryption key travels only in the join link's ``#fragment``, which browsers
never send to a server, so neither this server nor a relay in front of it ever sees it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import ssl
import threading
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

from .attendee_access import AttendeeAccess, JoinRefusedError, StageHand, attendee_token, clean_name
from .web_client import CLIENT_FILES

log = logging.getLogger(__name__)

MAX_JSON_BYTES = 2048
JOIN_ATTEMPTS_PER_MINUTE = 20
_PAGE_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
               ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png"}
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; worker-src 'self' blob:; media-src 'self' blob: mediastream:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(self), microphone=(self), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
_HOP_BY_HOP = {"connection", "keep-alive", "upgrade", "transfer-encoding", "host",
               "proxy-connection", "te", "trailer"}


class _RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        if len(self._hits) > 4096:  # forget idle clients
            self._hits = {k: v for k, v in self._hits.items() if v}
        return True


class AttendeeHttpApplication:
    def __init__(self, *, pages_directory: Path, client_directory: Path | None,
                 livekit_http_url: str) -> None:
        self._pages = Path(pages_directory)
        self._client = Path(client_directory) if client_directory else None
        self._upstream = livekit_http_url.rstrip("/")
        self._access: AttendeeAccess | None = None
        self._hands: tuple[StageHand, ...] = ()
        self._attendance = 0
        self._join_limiter = _RateLimiter(JOIN_ATTEMPTS_PER_MINUTE)
        self._sockets: set[web.WebSocketResponse] = set()
        self._session: aiohttp.ClientSession | None = None
        self.app = web.Application(client_max_size=MAX_JSON_BYTES * 2,
                                   middlewares=[self._headers_middleware])
        routes = self.app.router
        routes.add_get("/", self._root)
        routes.add_get("/join", self._join_page)
        routes.add_get("/stage/{secret}", self._stage_page)
        routes.add_get("/assets/{name}", self._asset)
        routes.add_get("/vendor/{name}", self._vendor)
        routes.add_get("/api/meeting", self._meeting)
        routes.add_post("/api/join", self._join)
        routes.add_get("/api/stage/{secret}/hands", self._stage_hands)
        routes.add_route("*", "/rtc", self._rtc_proxy)
        routes.add_route("*", "/rtc/{tail:.*}", self._rtc_proxy)
        self.app.on_shutdown.append(self._close_sockets)
        self.app.on_cleanup.append(self._close_session)

    # -- published state (call on the server loop) --------------------------------------
    def publish_access(self, access: AttendeeAccess | None) -> None:
        self._access = access

    def publish_hands(self, hands: Sequence[StageHand], attendance: int) -> None:
        self._hands = tuple(hands)
        self._attendance = int(attendance)

    # -- middleware -------------------------------------------------------------------
    @web.middleware
    async def _headers_middleware(self, request: web.Request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        if not request.path.startswith("/rtc"):
            response.headers.update(_SECURITY_HEADERS)
            if request.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
        if isinstance(response, web.HTTPException):
            raise response
        return response

    # -- pages and files ----------------------------------------------------------------
    async def _root(self, _request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/join")

    async def _join_page(self, _request: web.Request) -> web.StreamResponse:
        return self._file(self._pages / "join.html", "no-store")

    async def _stage_page(self, request: web.Request) -> web.StreamResponse:
        if not self._stage_allowed(request.match_info["secret"]):
            raise web.HTTPNotFound()
        return self._file(self._pages / "stage.html", "no-store")

    async def _asset(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        path = self._pages / name
        if "/" in name or name.startswith(".") or path.suffix not in _PAGE_TYPES:
            raise web.HTTPNotFound()
        return self._file(path, "no-cache")

    async def _vendor(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if self._client is None or name not in CLIENT_FILES:
            raise web.HTTPNotFound()
        return self._file(self._client / name, "public, max-age=86400",
                          content_type="text/javascript")

    def _file(self, path: Path, cache: str, *, content_type: str | None = None) -> web.StreamResponse:
        if not path.is_file():
            raise web.HTTPNotFound()
        response = web.FileResponse(path, headers={
            "Cache-Control": cache,
            "Content-Type": content_type or _PAGE_TYPES.get(path.suffix, "application/octet-stream"),
        })
        return response

    # -- API ----------------------------------------------------------------------------------
    async def _meeting(self, _request: web.Request) -> web.Response:
        access = self._access
        if access is None:
            return web.json_response({"open": False})
        return web.json_response({"open": True, "meetingId": access.meeting_id,
                                  "hallName": access.hall_name, "e2ee": access.e2ee})

    async def _join(self, request: web.Request) -> web.Response:
        peer = request.remote or "unknown"
        if not self._join_limiter.allow(peer):
            return web.json_response({"error": "too_many_attempts"}, status=429,
                                     headers={"Retry-After": "60"})
        body = await self._json_body(request)
        access = self._access
        if access is None:
            return web.json_response({"error": "closed"}, status=409)
        try:
            token = attendee_token(access, body.get("deviceId"), clean_name(body.get("name")))
        except JoinRefusedError as exc:
            status = 403 if exc.code == "removed" else 400
            return web.json_response({"error": exc.code}, status=status)
        return web.json_response({"token": token, "room": access.room,
                                  "meetingId": access.meeting_id})

    async def _stage_hands(self, request: web.Request) -> web.Response:
        if not self._stage_allowed(request.match_info["secret"]):
            raise web.HTTPNotFound()
        return web.json_response({
            "open": self._access is not None,
            "attendance": self._attendance,
            "hands": [{"name": h.name, "position": h.position, "speaking": h.speaking}
                      for h in self._hands],
        })

    def _stage_allowed(self, secret: str) -> bool:
        access = self._access
        return access is not None and hmac.compare_digest(secret.encode(), access.stage_secret.encode())

    @staticmethod
    async def _json_body(request: web.Request) -> dict:
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        raw = await request.content.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise web.HTTPRequestEntityTooLarge(MAX_JSON_BYTES, len(raw))
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise web.HTTPBadRequest() from exc
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        return body

    # -- LiveKit signaling proxy ---------------------------------------------------------
    def _client_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=5))
        return self._session

    async def _rtc_proxy(self, request: web.Request) -> web.StreamResponse:
        target = self._upstream + request.rel_url.path_qs
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._proxy_websocket(request, target.replace("http", "ws", 1), headers)
        try:
            async with self._client_session().request(
                request.method, target, headers=headers, data=await request.read(),
                allow_redirects=False,
            ) as upstream:
                body = await upstream.read()
                reply = {k: v for k, v in upstream.headers.items()
                         if k.lower() not in _HOP_BY_HOP | {"content-length", "content-encoding"}}
                return web.Response(status=upstream.status, body=body, headers=reply)
        except aiohttp.ClientError as exc:
            log.debug("LiveKit signaling unavailable: %s", exc)
            raise web.HTTPBadGateway() from exc

    async def _proxy_websocket(self, request: web.Request, target: str,
                               headers: dict[str, str]) -> web.StreamResponse:
        for name in ("Sec-WebSocket-Key", "Sec-WebSocket-Version", "Sec-WebSocket-Extensions"):
            headers.pop(name, None)
        try:
            upstream = await self._client_session().ws_connect(
                target, headers=headers, autoping=False, max_msg_size=0)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug("LiveKit signaling unavailable: %s", exc)
            raise web.HTTPBadGateway() from exc
        client = web.WebSocketResponse(autoping=False, max_msg_size=0)
        await client.prepare(request)
        self._sockets.add(client)

        async def pump(source, sink) -> None:
            async for message in source:
                if message.type == WSMsgType.BINARY:
                    await sink.send_bytes(message.data)
                elif message.type == WSMsgType.TEXT:
                    await sink.send_str(message.data)
                elif message.type == WSMsgType.PING:
                    await sink.ping(message.data)
                elif message.type == WSMsgType.PONG:
                    await sink.pong(message.data)
                else:
                    break

        try:
            tasks = [asyncio.ensure_future(pump(client, upstream)),
                     asyncio.ensure_future(pump(upstream, client))]
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            self._sockets.discard(client)
            await upstream.close()
            await client.close()
        return client

    async def _close_sockets(self, _app: web.Application) -> None:
        for socket in list(self._sockets):
            await socket.close(code=aiohttp.WSCloseCode.GOING_AWAY)

    async def _close_session(self, _app: web.Application) -> None:
        if self._session is not None:
            await self._session.close()


class AttendeeServer:
    """Runs :class:`AttendeeHttpApplication` on its own thread and asyncio loop."""

    def __init__(self, application: AttendeeHttpApplication) -> None:
        self.application = application
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self.port = 0

    def start(self, host: str, port: int, ssl_context: ssl.SSLContext | None = None) -> None:
        if self._thread is not None:
            raise RuntimeError("The attendee server is already running")
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._serve, args=(host, port, ssl_context),
                                        name="solin-attendee-web", daemon=True)
        self._thread.start()
        if not self._ready.wait(8) or self._error is not None:
            error = self._error
            self.stop()
            raise RuntimeError(f"The attendee server could not start: {error}") from error

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(8)
        self._thread = None
        self._loop = None

    def publish_access(self, access: AttendeeAccess | None) -> None:
        self._call(self.application.publish_access, access)

    def publish_hands(self, hands: Sequence[StageHand], attendance: int) -> None:
        self._call(self.application.publish_hands, tuple(hands), attendance)

    def _call(self, function, *args) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(function, *args)
        else:
            function(*args)

    def _serve(self, host: str, port: int, ssl_context: ssl.SSLContext | None) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._runner = web.AppRunner(self.application.app, access_log=None)
            loop.run_until_complete(self._runner.setup())
            site = web.TCPSite(self._runner, host, port, ssl_context=ssl_context)
            loop.run_until_complete(site.start())
            sockets = getattr(site._server, "sockets", None) or ()  # noqa: SLF001 - bound port
            self.port = sockets[0].getsockname()[1] if sockets else port
        except BaseException as exc:  # noqa: BLE001 - reported to start()
            self._error = exc
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(self._runner.cleanup())
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
