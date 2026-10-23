"""The live meeting: web server, Galene operator client and meeting rules on one loop.

``MeetingRuntime`` owns a dedicated asyncio thread. On it run:

* the **meeting web server** (aiohttp, plain HTTP on the hall; the hub terminates TLS):

  ========================== ======================================================
  ``GET  /j/<slug>``         attendee app (name → lobby → program)
  ``GET  /stage/<slug>``     the conductor's raised-hands page (``#<stage key>``)
  ``GET  /assets/<file>``    the pages' scripts and styles
  ``POST /api/<slug>/join``  ask to join (device id + secret + name)
  ``POST /api/<slug>/token`` a fresh Galene join token for an admitted device
  ``GET  /api/<slug>/events`` WebSocket: lobby/admit/hand/rename/removed/ended
  ``GET  /api/<slug>/stage`` WebSocket: hands queue for the stage page
  ``GET  /ws``               Galene's WebSocket, proxied (one origin for attendees)
  ========================== ======================================================

* the **Galene operator client** (``op`` token), reconnecting if Galene restarts;
* the :class:`~solin.core.hosted_meeting.meeting_core.MeetingCore`.

Operator commands from Qt are thread-safe (:meth:`MeetingRuntime.command`); the
operator snapshot is delivered through ``on_snapshot`` on the loop thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web

from ..foundation.resources import application_resource_path
from .galene_client import GaleneClient, GaleneUser
from .meeting_core import JoinStatus, MeetingCore
from .tokens import OPERATOR, sign_token

log = logging.getLogger(__name__)

_MAX_BODY = 4096
_SNAPSHOT_DEBOUNCE_S = 0.15
_OPERATOR_RETRY_S = 2.0

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "media-src 'self' blob:; connect-src 'self' ws: wss:; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(self), microphone=(self), screen-wake-lock=(self)",
    "Cache-Control": "no-store",
}
_ASSET_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}


def default_assets_dir() -> Path:
    return application_resource_path("hosted_meeting_web")


@dataclass(frozen=True, slots=True)
class MeetingRuntimeConfig:
    slug: str
    title: str
    group_key: bytes
    galene_port: int
    web_port: int
    bind_host: str = "0.0.0.0"
    assets_dir: Path = field(default_factory=default_assets_dir)

    @property
    def galene_ws(self) -> str:
        return f"ws://127.0.0.1:{self.galene_port}/ws"

    @property
    def galene_origin(self) -> str:
        return f"http://127.0.0.1:{self.galene_port}"

    @property
    def audience(self) -> str:
        # Galene matches only the path when no canonical host is configured.
        return f"https://solin.invalid/group/{self.slug}/"


class _Effects:
    """CoreEffects on the loop thread."""

    def __init__(self, runtime: MeetingRuntime) -> None:
        self._rt = runtime

    def notify(self, device_id: str, message: dict[str, Any]) -> None:
        self._rt._push_device(device_id, message)

    def present(self, galene_id: str) -> None:
        self._rt._galene_action("present", galene_id)

    def unpresent(self, galene_id: str) -> None:
        self._rt._galene_action("unpresent", galene_id)

    def kick(self, galene_id: str, reason: str) -> None:
        self._rt._galene_action("kick", galene_id, reason)

    def changed(self) -> None:
        self._rt._schedule_snapshot()


class MeetingRuntime:
    def __init__(
        self,
        config: MeetingRuntimeConfig,
        *,
        on_snapshot: Callable[[dict[str, Any]], None] | None = None,
        on_operator_state: Callable[[bool], None] | None = None,
    ) -> None:
        self.config = config
        self._on_snapshot = on_snapshot
        self._on_operator_state = on_operator_state
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._start_error: BaseException | None = None
        self._runner: web.AppRunner | None = None
        self._stopping: asyncio.Event | None = None
        self.core: MeetingCore | None = None
        self._client: GaleneClient | None = None
        self._operator_task: asyncio.Task[None] | None = None
        self._device_sockets: dict[str, set[web.WebSocketResponse]] = {}
        self._stage_sockets: set[web.WebSocketResponse] = set()
        self._snapshot_handle: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.operator_connected = False

    # ── lifecycle (any thread) ────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._start_error is None

    def start(self, timeout: float = 10.0) -> None:
        if self.running:
            return
        self._started.clear()
        self._start_error = None
        self._thread = threading.Thread(target=self._thread_main, name="solin-hosted-meeting", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            self.stop()
            raise TimeoutError("timed out starting the meeting web server")
        if self._start_error is not None:
            error = self._start_error
            self.stop()
            raise RuntimeError(f"could not start the meeting web server: {error}") from error

    def stop(self, timeout: float = 5.0) -> None:
        loop, stopping = self._loop, self._stopping
        if loop is not None and stopping is not None and loop.is_running():
            loop.call_soon_threadsafe(stopping.set)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def command(self, name: str, *args: Any) -> Future[Any]:
        """Run ``MeetingCore.<name>(*args)`` on the loop; the future holds its result."""

        future: Future[Any] = Future()
        loop = self._loop
        if loop is None or not loop.is_running() or self.core is None:
            future.set_exception(RuntimeError("the meeting is not running"))
            return future

        def run() -> None:
            try:
                future.set_result(getattr(self.core, name)(*args))
            except Exception as error:  # noqa: BLE001 - delivered to the caller
                future.set_exception(error)

        loop.call_soon_threadsafe(run)
        return future

    # ── loop thread ───────────────────────────────────────────────────────

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except BaseException as error:  # noqa: BLE001 - reported to start()
            if not self._started.is_set():
                self._start_error = error
                self._started.set()
            else:
                log.exception("meeting runtime crashed")
        finally:
            loop.close()
            self._loop = None

    async def _main(self) -> None:
        self._stopping = asyncio.Event()
        self.core = MeetingCore(_Effects(self), self._mint)
        app = web.Application(client_max_size=_MAX_BODY, middlewares=[self._security_headers])
        slug = self.config.slug
        app.router.add_get(f"/j/{slug}", self._page("index.html"))
        app.router.add_get(f"/stage/{slug}", self._page("stage.html"))
        app.router.add_get("/assets/{name}", self._asset)
        app.router.add_post(f"/api/{slug}/join", self._join)
        app.router.add_post(f"/api/{slug}/token", self._token)
        app.router.add_get(f"/api/{slug}/events", self._events)
        app.router.add_get(f"/api/{slug}/stage", self._stage_events)
        app.router.add_get(f"/api/{slug}/info", self._info)
        app.router.add_get("/ws", self._galene_proxy)
        app.router.add_get("/healthz", self._health)
        self._runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.bind_host, self.config.web_port, reuse_address=True)
        await site.start()
        self._operator_task = asyncio.create_task(self._operator_loop())
        self._started.set()
        await self._stopping.wait()
        await self._shutdown()

    async def _shutdown(self) -> None:
        if self.core is not None:
            self.core.end()
            await asyncio.sleep(0.2)  # let "ended" reach the browsers
        if self._operator_task is not None:
            self._operator_task.cancel()
        if self._client is not None:
            await self._client.close()
        for sockets in list(self._device_sockets.values()) + [self._stage_sockets]:
            for ws in list(sockets):
                await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    def _mint(self, username: str, permissions: tuple[str, ...]) -> str:
        return sign_token(self.config.group_key, username=username, audience=self.config.audience,
                          permissions=permissions)

    # ── operator client ───────────────────────────────────────────────────

    async def _operator_loop(self) -> None:
        while True:
            token = sign_token(self.config.group_key, username="Solin", audience=self.config.audience,
                               permissions=OPERATOR)
            closed = asyncio.Event()
            client = GaleneClient(self.config.galene_ws, self.config.slug, token,
                                  on_user=self._on_galene_user,
                                  on_closed=lambda _r, event=closed: event.set())
            try:
                await client.connect()
            except Exception as error:  # noqa: BLE001 - Galene may still be starting
                log.debug("operator client could not join yet: %s", error)
                await asyncio.sleep(_OPERATOR_RETRY_S)
                continue
            self._client = client
            self._set_operator(True)
            for user in list(client.users.values()):
                self._on_galene_user("add", user)
            await closed.wait()
            self._client = None
            self._set_operator(False)
            await client.close()
            await asyncio.sleep(_OPERATOR_RETRY_S)

    def _set_operator(self, connected: bool) -> None:
        self.operator_connected = connected
        if self._on_operator_state is not None:
            self._on_operator_state(connected)
        self._schedule_snapshot()

    def _on_galene_user(self, kind: str, user: GaleneUser) -> None:
        if self.core is not None:
            self.core.on_galene_user(kind, user)

    def _galene_action(self, kind: str, galene_id: str, value: Any = None) -> None:
        client = self._client
        if client is None:
            return

        async def act() -> None:
            try:
                if kind == "kick":
                    await client.kick(galene_id, str(value or ""))
                else:
                    await client.user_action(kind, galene_id)
            except ConnectionError:
                log.debug("Galene action %s dropped: not connected", kind)

        self._spawn(act())

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── snapshots ─────────────────────────────────────────────────────────

    def _schedule_snapshot(self) -> None:
        loop = self._loop
        if loop is None or self._snapshot_handle is not None:
            return
        self._snapshot_handle = loop.call_later(_SNAPSHOT_DEBOUNCE_S, self._emit_snapshot)

    def _emit_snapshot(self) -> None:
        self._snapshot_handle = None
        if self.core is None:
            return
        snapshot = self.core.snapshot()
        snapshot["operatorConnected"] = self.operator_connected
        if self._on_snapshot is not None:
            try:
                self._on_snapshot(snapshot)
            except Exception:  # noqa: BLE001 - observer boundary
                log.exception("snapshot observer failed")
        stage = json.dumps({"type": "stage", **self.core.stage_view()})
        for ws in list(self._stage_sockets):
            self._spawn(self._send_quiet(ws, stage))

    @staticmethod
    async def _send_quiet(ws: web.WebSocketResponse, text: str) -> None:
        try:
            await ws.send_str(text)
        except (ConnectionError, RuntimeError):
            pass

    def _push_device(self, device_id: str, message: dict[str, Any]) -> None:
        text = json.dumps(message)
        for ws in list(self._device_sockets.get(device_id, ())):
            self._spawn(self._send_quiet(ws, text))

    # ── HTTP ──────────────────────────────────────────────────────────────

    @web.middleware
    async def _security_headers(self, request: web.Request, handler: Any) -> web.StreamResponse:
        response = await handler(request)
        if not isinstance(response, web.WebSocketResponse):
            for key, value in _HEADERS.items():
                response.headers.setdefault(key, value)
        return response

    def _page(self, name: str) -> Any:
        async def handler(_request: web.Request) -> web.StreamResponse:
            return await self._file_response(name)

        return handler

    async def _asset(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if "/" in name or "\\" in name or name.startswith(".") or name.endswith(".html"):
            raise web.HTTPNotFound()
        return await self._file_response(name)

    async def _file_response(self, name: str) -> web.StreamResponse:
        path = self.config.assets_dir / name
        suffix = path.suffix.lower()
        if suffix not in _ASSET_TYPES or not path.is_file():
            raise web.HTTPNotFound()
        return web.Response(body=path.read_bytes(), headers={"Content-Type": _ASSET_TYPES[suffix]})

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "operator": self.operator_connected})

    async def _info(self, _request: web.Request) -> web.Response:
        return web.json_response({"title": self.config.title, "group": self.config.slug,
                                  "ended": bool(self.core and self.core.ended)})

    async def _read_json(self, request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, aiohttp.ContentTypeError) as error:
            raise web.HTTPBadRequest(text="invalid JSON") from error
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="invalid body")
        return body

    def _decision_response(self, decision: Any) -> web.Response:
        payload: dict[str, Any] = {"status": decision.status.value}
        if decision.token:
            payload.update(token=decision.token, group=self.config.slug, ws="/ws")
        if decision.reason:
            payload["reason"] = decision.reason
        status = 403 if decision.status is JoinStatus.REFUSED else 200
        return web.json_response(payload, status=status)

    async def _join(self, request: web.Request) -> web.Response:
        body = await self._read_json(request)
        assert self.core is not None
        decision = self.core.request_join(str(body.get("device") or ""), str(body.get("secret") or ""),
                                          str(body.get("name") or ""))
        return self._decision_response(decision)

    async def _token(self, request: web.Request) -> web.Response:
        body = await self._read_json(request)
        assert self.core is not None
        decision = self.core.token_for(str(body.get("device") or ""), str(body.get("secret") or ""))
        return self._decision_response(decision)

    async def _events(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=_MAX_BODY)
        await ws.prepare(request)
        device_id = ""
        try:
            first = await asyncio.wait_for(ws.receive(), 10.0)
            if first.type != WSMsgType.TEXT:
                return ws
            auth = json.loads(first.data)
            device_id = str(auth.get("device") or "")
            assert self.core is not None
            if auth.get("type") != "auth" or not self.core.is_authenticated(device_id, str(auth.get("secret") or "")):
                await ws.close(code=4401, message=b"unauthorized")
                return ws
            self._device_sockets.setdefault(device_id, set()).add(ws)
            await ws.send_str(json.dumps({"type": "hello"}))
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                self._device_message(device_id, json.loads(message.data))
        except (TimeoutError, json.JSONDecodeError, ConnectionError):
            pass
        finally:
            if device_id:
                sockets = self._device_sockets.get(device_id)
                if sockets is not None:
                    sockets.discard(ws)
                    if not sockets:
                        self._device_sockets.pop(device_id, None)
        return ws

    def _device_message(self, device_id: str, message: dict[str, Any]) -> None:
        # The browser reports "done speaking" itself; everything else goes through Galene.
        if message.get("type") == "done" and self.core is not None:
            self.core.done(device_id)

    async def _stage_events(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=_MAX_BODY)
        await ws.prepare(request)
        try:
            first = await asyncio.wait_for(ws.receive(), 10.0)
            auth = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
            assert self.core is not None
            if auth.get("type") != "auth" or auth.get("key") != self.core.stage_key:
                await ws.close(code=4401, message=b"unauthorized")
                return ws
            self._stage_sockets.add(ws)
            await ws.send_str(json.dumps({"type": "stage", **self.core.stage_view()}))
            async for _message in ws:
                pass
        except (TimeoutError, json.JSONDecodeError, ConnectionError):
            pass
        finally:
            self._stage_sockets.discard(ws)
        return ws

    async def _galene_proxy(self, request: web.Request) -> web.WebSocketResponse:
        client_ws = web.WebSocketResponse(max_msg_size=1 << 20)
        await client_ws.prepare(request)
        async with aiohttp.ClientSession() as session:
            try:
                upstream = await session.ws_connect(
                    self.config.galene_ws, headers={"Origin": self.config.galene_origin},
                    max_msg_size=1 << 20,
                )
            except (aiohttp.ClientError, OSError):
                await client_ws.close(code=1011, message=b"media server unavailable")
                return client_ws

            async def pump(source: Any, sink: Any) -> None:
                async for message in source:
                    if message.type == WSMsgType.TEXT:
                        await sink.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await sink.send_bytes(message.data)
                    else:
                        break

            tasks = [asyncio.create_task(pump(client_ws, upstream)), asyncio.create_task(pump(upstream, client_ws))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await upstream.close()
                await client_ws.close()
        return client_ws
