"""A minimal asyncio client for Galene's WebSocket protocol, used as the operator.

It joins a group with an ``op`` token, tracks the other users (id, username,
permissions, data) and sends the moderation actions Solin needs: ``present`` /
``unpresent`` (open or close someone's microphone), ``kick`` and user messages.
It never sends or receives media. Protocol: ``galene-protocol.md`` (version 2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

PING_INTERVAL_SECONDS = 15.0


@dataclass(slots=True)
class GaleneUser:
    id: str
    username: str
    permissions: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        return "present" in self.permissions


UserCallback = Callable[[str, GaleneUser], None]  # kind ("add", "change", "delete"), user


class GaleneJoinError(RuntimeError):
    pass


class GaleneClient:
    def __init__(
        self,
        url: str,
        group: str,
        token: str,
        *,
        on_user: UserCallback | None = None,
        on_closed: Callable[[str], None] | None = None,
    ) -> None:
        self.url = url
        self.group = group
        self.id = f"solin-op-{secrets.token_hex(6)}"
        self._token = token
        self._on_user = on_user
        self._on_closed = on_closed
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pinger: asyncio.Task[None] | None = None
        self.users: dict[str, GaleneUser] = {}
        self.permissions: tuple[str, ...] = ()
        self.username = ""
        self._closing = False

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def connect(self, *, timeout: float = 10.0) -> None:
        self._closing = False
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self.url, heartbeat=None, timeout=aiohttp.ClientWSTimeout(ws_close=5.0))
            await self._send({"type": "handshake", "version": ["2"], "id": self.id})
            await self._send({"type": "join", "kind": "join", "group": self.group, "token": self._token})
            joined = await asyncio.wait_for(self._await_joined(), timeout)
        except BaseException:  # noqa: BLE001 - close the socket on any failure, then re-raise
            await self._dispose()
            raise
        self.permissions = tuple(joined.get("permissions") or ())
        self.username = str(joined.get("username") or "")
        self._reader = asyncio.create_task(self._read(), name="galene-op-reader")
        self._pinger = asyncio.create_task(self._ping(), name="galene-op-ping")

    async def _await_joined(self) -> dict[str, Any]:
        assert self._ws is not None
        async for message in self._ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
            kind = data.get("kind")
            if data.get("type") == "joined":
                if kind == "join":
                    return data
                if kind == "fail":
                    raise GaleneJoinError(str(data.get("value") or data.get("error") or "join failed"))
            else:
                self._handle(data)
        raise GaleneJoinError("connection closed before joining")

    async def close(self) -> None:
        self._closing = True
        for task in (self._pinger, self._reader):
            if task is not None and not task.done():
                task.cancel()
        await self._dispose()

    async def _dispose(self) -> None:
        ws, session = self._ws, self._session
        self._ws = self._session = None
        if ws is not None and not ws.closed:
            await ws.close()
        if session is not None:
            await session.close()

    async def _send(self, message: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise ConnectionError("not connected to Galene")
        await ws.send_str(json.dumps(message))

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_SECONDS)
            try:
                await self._send({"type": "ping"})
            except ConnectionError:
                return

    async def _read(self) -> None:
        reason = "closed"
        try:
            assert self._ws is not None
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    self._handle(json.loads(message.data))
                elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - network boundary
            reason = str(error)
            log.warning("Galene operator connection failed: %s", error)
        if not self._closing and self._on_closed is not None:
            self._on_closed(reason)

    def _handle(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        if kind == "ping":
            asyncio.ensure_future(self._send({"type": "pong"}))
        elif kind == "user":
            self._handle_user(data)
        elif kind == "joined" and data.get("kind") == "change":
            self.permissions = tuple(data.get("permissions") or ())
        elif kind == "usermessage" and data.get("kind") in ("error", "warning"):
            log.warning("Galene: %s", data.get("value"))
        elif kind == "offer":
            # The operator never takes media.
            asyncio.ensure_future(self._send({"type": "abort", "id": data.get("id")}))

    def _handle_user(self, data: dict[str, Any]) -> None:
        user_id = str(data.get("id") or "")
        if not user_id or user_id == self.id:
            return
        kind = str(data.get("kind") or "")
        if kind == "delete":
            user = self.users.pop(user_id, None)
            if user is None:
                return
        else:
            user = self.users.get(user_id) or GaleneUser(id=user_id, username="")
            user.username = str(data.get("username") or user.username)
            user.permissions = tuple(data.get("permissions") or ())
            user.data = dict(data.get("data") or {})
            self.users[user_id] = user
        if self._on_user is not None:
            self._on_user(kind, user)

    # ── moderation ────────────────────────────────────────────────────────

    async def user_action(self, kind: str, dest: str, value: Any = None) -> None:
        message: dict[str, Any] = {"type": "useraction", "kind": kind, "source": self.id, "dest": dest}
        if value is not None:
            message["value"] = value
        await self._send(message)

    async def present(self, dest: str) -> None:
        await self.user_action("present", dest)

    async def unpresent(self, dest: str) -> None:
        await self.user_action("unpresent", dest)

    async def kick(self, dest: str, message: str = "") -> None:
        await self.user_action("kick", dest, message)

    async def user_message(self, dest: str, kind: str, value: Any = None) -> None:
        await self._send({
            "type": "usermessage", "kind": kind, "source": self.id, "dest": dest,
            "privileged": True, "value": value,
        })
