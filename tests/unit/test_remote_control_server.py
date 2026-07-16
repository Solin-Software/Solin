from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import threading
from typing import Any, cast
import uuid

from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.remote_control import server as server_module
from solin.core.remote_control.contracts import CommandError, RemoteCommand
from solin.core.remote_control.security import (
    InMemorySessionStore,
    LoginRateLimiter,
    RemoteControlCredentialsStore,
    ScryptParameters,
    ScryptPasswordHasher,
)
from solin.core.remote_control.server import (
    SESSION_COOKIE_NAME,
    RemoteControlHttpApplication,
    RemoteControlServerDependencies,
)
from solin.core.remote_control.state import (
    ProjectionCommandSession,
    RemoteControlStateStore,
)


_ORIGIN = "https://192.168.1.10:8765"
_HOST = "192.168.1.10:8765"


class _MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def string(self, key: str, default: str = "") -> str:
        return str(self.values.get(key, default) or "")

    def set_value(self, key: str, value: object, *, sync: bool = True) -> None:
        del sync
        self.values[key] = value

    def remove(self, key: str, *, sync: bool = True) -> None:
        del sync
        self.values.pop(key, None)


@dataclass(slots=True)
class _Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


class _BlockingCredentials:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def authenticate(self, _username: str, _password: str) -> bool:
        self.started.set()
        self.release.wait(2)
        return True

    @staticmethod
    def configured_username() -> str:
        return "operator"


class _ConcurrentCredentials:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0
        self.two_active = threading.Event()
        self.release = threading.Event()

    def authenticate(self, _username: str, _password: str) -> bool:
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active == 2:
                self.two_active.set()
        self.release.wait(2)
        with self._lock:
            self.active -= 1
        return False

    @staticmethod
    def configured_username() -> str:
        return "operator"


@dataclass(slots=True)
class _Harness:
    client: TestClient
    commands: list[RemoteCommand]
    application: RemoteControlHttpApplication
    state: RemoteControlStateStore
    sessions: InMemorySessionStore
    command_session: ProjectionCommandSession
    session_counts: list[int]


async def _harness(
    tmp_path: Path,
    *,
    credentials_override: Any | None = None,
    sessions: InMemorySessionStore | None = None,
    command_handler: Any | None = None,
    thumbnail_handler: Any | None = None,
    collection_thumbnail_handler: Any | None = None,
) -> _Harness:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Remote</title>", "utf-8")
    (assets / "app.js").write_text("", "utf-8")

    hasher = ScryptPasswordHasher(
        ScryptParameters(n=1_024, r=8, p=1, max_memory_bytes=8 * 1024 * 1024)
    )
    credentials = credentials_override or RemoteControlCredentialsStore.create(
        ProfileAppSettingsStore(cast(Any, _MemorySettings())),
        password_hasher=hasher,
    )
    if credentials_override is None:
        credentials.set_credentials("operator", "correct horse battery staple")
    state = RemoteControlStateStore()
    session_store = sessions or InMemorySessionStore()
    command_session = ProjectionCommandSession(state)
    commands: list[RemoteCommand] = []
    session_counts: list[int] = []

    async def execute(command: RemoteCommand) -> CommandError | None:
        commands.append(command)
        return None

    execute_command = command_handler or execute

    application = RemoteControlHttpApplication(
        RemoteControlServerDependencies(
            state=state,
            command_session=command_session,
            credentials=credentials,
            sessions=session_store,
            rate_limiter=LoginRateLimiter(),
            assets_directory=assets,
            command_handler=execute_command,
            thumbnail_handler=thumbnail_handler,
            collection_thumbnail_handler=collection_thumbnail_handler,
            session_count_changed=session_counts.append,
            profile_id="profile-1",
            profile_name="Sala principal",
            profile_locale="pt-BR",
            meeting_week_start=lambda: date(2026, 7, 13),
        ),
        allowed_origin=_ORIGIN,
        trust_certificate_der=b"scoped authority certificate",
    )
    client = TestClient(TestServer(application.app))
    await client.start_server()
    return _Harness(
        client,
        commands,
        application,
        state,
        session_store,
        command_session,
        session_counts,
    )


async def _authenticated_headers(harness: _Harness) -> dict[str, str]:
    login = await harness.client.post(
        "/remote/api/auth/login",
        headers={"Host": _HOST, "Origin": _ORIGIN},
        json={
            "username": "operator",
            "password": "correct horse battery staple",
        },
    )
    assert login.status == 200
    payload = await login.json()
    session_token = login.cookies[SESSION_COOKIE_NAME].value
    return {
        "Host": _HOST,
        "Origin": _ORIGIN,
        "X-CSRF-Token": payload["csrfToken"],
        "Cookie": f"{SESSION_COOKIE_NAME}={session_token}",
    }


def test_static_shell_has_strict_security_headers(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            response = await harness.client.get("/remote/", headers={"Host": _HOST})

            assert response.status == 200
            assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["Cache-Control"] == "no-store"

            certificate = await harness.client.get(
                "/remote/trust-certificate.cer",
                headers={"Host": _HOST},
            )
            assert certificate.status == 200
            assert await certificate.read() == b"scoped authority certificate"
            assert certificate.headers["Content-Type"] == "application/pkix-cert"
            assert certificate.headers["Cache-Control"] == "no-store"
            assert "attachment" in certificate.headers["Content-Disposition"]
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_collection_thumbnail_is_authenticated_and_route_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: list[tuple[str, str]] = []

        async def load_thumbnail(source: str, collection_id: str) -> bytes | None:
            requests.append((source, collection_id))
            return b"\xff\xd8normalized-cover"

        harness = await _harness(
            tmp_path,
            collection_thumbnail_handler=load_thumbnail,
        )
        try:
            unauthenticated = await harness.client.get(
                "/remote/api/collection-thumbnails/meeting/mwb:2026-07-13:E:1",
                headers={"Host": _HOST},
            )
            assert unauthenticated.status == 401

            headers = await _authenticated_headers(harness)
            response = await harness.client.get(
                "/remote/api/collection-thumbnails/meeting/mwb:2026-07-13:E:1",
                headers=headers,
            )
            invalid = await harness.client.get(
                "/remote/api/collection-thumbnails/meeting/not%2Fa%2Fcollection",
                headers=headers,
            )

            assert response.status == 200
            assert response.headers["Content-Type"] == "image/jpeg"
            assert await response.read() == b"\xff\xd8normalized-cover"
            assert invalid.status == 404
            assert requests == [("meeting", "mwb:2026-07-13:E:1")]
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_login_bootstrap_and_idempotent_command_flow(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            login = await harness.client.post(
                "/remote/api/auth/login",
                headers={"Host": _HOST, "Origin": _ORIGIN},
                json={
                    "username": "operator",
                    "password": "correct horse battery staple",
                },
            )
            assert login.status == 200
            login_payload = await login.json()
            csrf_token = login_payload["csrfToken"]
            session_token = login.cookies[SESSION_COOKIE_NAME].value
            cookie = {"Cookie": f"{SESSION_COOKIE_NAME}={session_token}"}

            bootstrap = await harness.client.get(
                "/remote/api/bootstrap",
                headers={"Host": _HOST, **cookie},
            )
            assert bootstrap.status == 200
            bootstrap_payload = await bootstrap.json()
            assert bootstrap_payload["csrfToken"] == csrf_token
            assert bootstrap_payload["profile"]["name"] == "Sala principal"
            assert bootstrap_payload["profile"]["locale"] == "pt-BR"
            assert bootstrap_payload["currentMeetingWeekStart"] == "2026-07-13"

            command_id = str(uuid.uuid4())
            headers = {
                "Host": _HOST,
                "Origin": _ORIGIN,
                "X-CSRF-Token": csrf_token,
                **cookie,
            }
            first = await harness.client.post(
                "/remote/api/commands",
                headers=headers,
                json={"commandId": command_id, "type": "stop"},
            )
            replay = await harness.client.post(
                "/remote/api/commands",
                headers=headers,
                json={"commandId": command_id, "type": "stop"},
            )

            assert first.status == replay.status == 200
            assert (await first.json())["ok"] is True
            assert (await replay.json())["ok"] is True
            assert len(harness.commands) == 1
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_origin_host_session_and_csrf_checks_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            wrong_host = await harness.client.get(
                "/remote/",
                headers={"Host": "attacker.invalid"},
            )
            missing_origin = await harness.client.post(
                "/remote/api/auth/login",
                headers={"Host": _HOST},
                json={
                    "username": "operator",
                    "password": "correct horse battery staple",
                },
            )
            no_session = await harness.client.get(
                "/remote/api/catalog",
                headers={"Host": _HOST},
            )

            assert wrong_host.status == 403
            assert missing_origin.status == 403
            assert no_session.status == 401
            assert no_session.headers["Cache-Control"] == "no-store"
            assert missing_origin.headers["X-Content-Type-Options"] == "nosniff"
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_websocket_baseline_is_atomic_and_multiple_clients_keep_one_sequence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            login = await harness.client.post(
                "/remote/api/auth/login",
                headers={"Host": _HOST, "Origin": _ORIGIN},
                json={
                    "username": "operator",
                    "password": "correct horse battery staple",
                },
            )
            session_token = login.cookies[SESSION_COOKIE_NAME].value
            websocket_headers = {
                "Host": _HOST,
                "Origin": _ORIGIN,
                "Cookie": f"{SESSION_COOKIE_NAME}={session_token}",
            }

            harness.state.update_catalog((), change_token="before-first-client")
            first = await harness.client.ws_connect(
                "/remote/api/ws",
                headers=websocket_headers,
            )
            first_catalog = await first.receive_json()
            first_playback = await first.receive_json()

            assert [first_catalog["type"], first_playback["type"]] == [
                "catalog.snapshot",
                "playback.snapshot",
            ]
            assert first_catalog["sequence"] == first_playback["sequence"] == 0
            assert first_catalog["catalogRevision"] == 1
            assert first_catalog["payload"]["catalogRevision"] == 1
            assert first_playback["payload"]["playbackRevision"] == 0

            await harness.application.publish_snapshot()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(first.receive(), timeout=0.05)

            harness.state.update_catalog((), change_token="second-revision")
            await harness.application.publish_snapshot()
            first_update = await first.receive_json()
            assert first_update["type"] == "catalog.snapshot"
            assert first_update["sequence"] == 1
            assert first_update["catalogRevision"] == 2

            second = await harness.client.ws_connect(
                "/remote/api/ws",
                headers=websocket_headers,
            )
            second_catalog = await second.receive_json()
            second_playback = await second.receive_json()
            assert second_catalog["sequence"] == second_playback["sequence"] == 1

            harness.state.update_catalog((), change_token="third-revision")
            await harness.application.publish_snapshot()
            first_after_second_joined = await first.receive_json()
            second_after_joined = await second.receive_json()
            assert first_after_second_joined == second_after_joined
            assert first_after_second_joined["sequence"] == 2
            assert first_after_second_joined["catalogRevision"] == 3

            await first.close()
            await second.close()
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_logout_revokes_every_websocket_using_that_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            login = await harness.client.post(
                "/remote/api/auth/login",
                headers={"Host": _HOST, "Origin": _ORIGIN},
                json={
                    "username": "operator",
                    "password": "correct horse battery staple",
                },
            )
            payload = await login.json()
            token = login.cookies[SESSION_COOKIE_NAME].value
            session_headers = {
                "Host": _HOST,
                "Origin": _ORIGIN,
                "Cookie": f"{SESSION_COOKIE_NAME}={token}",
            }
            websocket = await harness.client.ws_connect(
                "/remote/api/ws",
                headers=session_headers,
            )
            await websocket.receive_json()
            await websocket.receive_json()

            logout = await harness.client.post(
                "/remote/api/logout",
                headers={
                    **session_headers,
                    "X-CSRF-Token": payload["csrfToken"],
                },
            )
            revoked = await websocket.receive_json()
            closed = await websocket.receive()

            assert logout.status == 200
            assert revoked["type"] == "session.revoked"
            assert closed.type.name in {"CLOSE", "CLOSED"}
            assert websocket.close_code == 4401
            bootstrap = await harness.client.get(
                "/remote/api/bootstrap",
                headers={
                    "Host": _HOST,
                    "Cookie": f"{SESSION_COOKIE_NAME}={token}",
                },
            )
            assert bootstrap.status == 401
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_inflight_login_cannot_create_session_after_global_revocation(tmp_path: Path) -> None:
    async def scenario() -> None:
        credentials = _BlockingCredentials()
        sessions = InMemorySessionStore()
        harness = await _harness(
            tmp_path,
            credentials_override=credentials,
            sessions=sessions,
        )
        try:
            login_task = asyncio.create_task(
                harness.client.post(
                    "/remote/api/auth/login",
                    headers={"Host": _HOST, "Origin": _ORIGIN},
                    json={
                        "username": "operator",
                        "password": "correct horse battery staple",
                    },
                )
            )
            assert await asyncio.to_thread(credentials.started.wait, 1)

            sessions.revoke_all()
            credentials.release.set()
            response = await login_task

            assert response.status == 401
            assert sessions.active_count() == 0
            assert SESSION_COOKIE_NAME not in response.cookies
        finally:
            credentials.release.set()
            await harness.client.close()

    asyncio.run(scenario())


def test_login_password_hash_concurrency_is_bounded_to_two(tmp_path: Path) -> None:
    async def scenario() -> None:
        credentials = _ConcurrentCredentials()
        harness = await _harness(tmp_path, credentials_override=credentials)
        try:
            requests = [
                asyncio.create_task(
                    harness.client.post(
                        "/remote/api/auth/login",
                        headers={"Host": _HOST, "Origin": _ORIGIN},
                        json={
                            "username": "operator",
                            "password": "correct horse battery staple",
                        },
                    )
                )
                for _index in range(3)
            ]
            assert await asyncio.to_thread(credentials.two_active.wait, 1)
            await asyncio.sleep(0.05)

            assert credentials.maximum_active == 2
            assert sum(not request.done() for request in requests) == 3

            credentials.release.set()
            responses = await asyncio.gather(*requests)
            assert [response.status for response in responses] == [401, 401, 401]
            assert credentials.maximum_active == 2
        finally:
            credentials.release.set()
            await harness.client.close()

    asyncio.run(scenario())


def test_cancelled_login_keeps_hash_slot_reserved_until_worker_finishes(tmp_path: Path) -> None:
    async def scenario() -> None:
        credentials = _ConcurrentCredentials()
        harness = await _harness(tmp_path, credentials_override=credentials)
        try:
            first = asyncio.create_task(
                harness.application._authenticate_credentials("operator", "password-one")
            )
            second = asyncio.create_task(
                harness.application._authenticate_credentials("operator", "password-two")
            )
            assert await asyncio.to_thread(credentials.two_active.wait, 1)

            first.cancel()
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
            third = asyncio.create_task(
                harness.application._authenticate_credentials("operator", "password-three")
            )
            await asyncio.sleep(0.05)

            assert third.done() is False
            assert credentials.maximum_active == 2

            credentials.release.set()
            assert await third is False
            assert credentials.maximum_active == 2
        finally:
            credentials.release.set()
            await harness.client.close()

    asyncio.run(scenario())


def test_strict_json_times_out_a_stalled_request_body() -> None:
    class _SlowRequest:
        content_length = None
        content_type = "application/json"

        @staticmethod
        async def read() -> bytes:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        with pytest.raises(web.HTTPRequestTimeout):
            await RemoteControlHttpApplication._strict_json(
                cast(web.Request, _SlowRequest()),
                2_048,
                timeout_seconds=0.01,
            )

    asyncio.run(scenario())


def test_command_reauthorizes_session_after_reading_body(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            headers = await _authenticated_headers(harness)
            command_id = str(uuid.uuid4())

            async def revoke_then_parse(
                _request: web.Request,
                _maximum_bytes: int,
            ) -> dict[str, object]:
                harness.sessions.revoke_all()
                return {"commandId": command_id, "type": "stop"}

            monkeypatch.setattr(harness.application, "_strict_json", revoke_then_parse)
            response = await harness.client.post(
                "/remote/api/commands",
                headers=headers,
                json={"commandId": command_id, "type": "stop"},
            )

            assert response.status == 403
            assert harness.commands == []
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_command_timeout_returns_in_progress_then_caches_background_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        executed: list[RemoteCommand] = []

        async def execute(command: RemoteCommand) -> CommandError | None:
            executed.append(command)
            started.set()
            await release.wait()
            return None

        monkeypatch.setattr(server_module, "_COMMAND_TIMEOUT_SECONDS", 0.01)
        harness = await _harness(tmp_path, command_handler=execute)
        try:
            headers = await _authenticated_headers(harness)
            command_id = str(uuid.uuid4())
            first = await harness.client.post(
                "/remote/api/commands",
                headers=headers,
                json={"commandId": command_id, "type": "stop"},
            )
            assert started.is_set()
            first_payload = await first.json()

            assert first.status == 200
            assert first_payload["ok"] is False
            assert first_payload["error"] == {
                "code": "command_in_progress",
                "message": "This command is still in progress.",
                "retryable": True,
            }
            assert len(harness.application._command_tasks) == 1

            release.set()
            for _attempt in range(100):
                if not harness.application._command_tasks:
                    break
                await asyncio.sleep(0.01)
            assert not harness.application._command_tasks

            replay = await harness.client.post(
                "/remote/api/commands",
                headers=headers,
                json={"commandId": command_id, "type": "stop"},
            )
            assert (await replay.json())["ok"] is True
            assert len(executed) == 1
        finally:
            release.set()
            await harness.client.close()

    asyncio.run(scenario())


def test_shutdown_cancels_and_collects_unfinished_background_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def execute(_command: RemoteCommand) -> CommandError | None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(server_module, "_COMMAND_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(
            server_module,
            "_COMMAND_SHUTDOWN_TIMEOUT_SECONDS",
            0.01,
        )
        harness = await _harness(tmp_path, command_handler=execute)
        headers = await _authenticated_headers(harness)
        response = await harness.client.post(
            "/remote/api/commands",
            headers=headers,
            json={"commandId": str(uuid.uuid4()), "type": "stop"},
        )

        assert response.status == 200
        assert started.is_set()
        assert harness.application._command_tasks

        await harness.client.close()

        assert cancelled.is_set()
        assert not harness.application._command_tasks

    asyncio.run(scenario())


def test_websocket_limits_and_binary_messages_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        sockets = []
        try:
            headers = await _authenticated_headers(harness)
            for _index in range(4):
                socket = await harness.client.ws_connect(
                    "/remote/api/ws",
                    headers=headers,
                )
                await socket.receive_json()
                await socket.receive_json()
                sockets.append(socket)

            with pytest.raises(WSServerHandshakeError) as limit_error:
                await harness.client.ws_connect(
                    "/remote/api/ws",
                    headers=headers,
                )
            assert limit_error.value.status == 429

            await sockets[0].send_bytes(b"commands are not accepted here")
            await sockets[0].receive()
            assert sockets[0].close_code == 1008
        finally:
            await asyncio.gather(
                *(socket.close() for socket in sockets),
                return_exceptions=True,
            )
            await harness.client.close()

    asyncio.run(scenario())


def test_global_websocket_limit_is_enforced_across_sessions(tmp_path: Path) -> None:
    async def scenario() -> None:
        sessions = InMemorySessionStore(max_sessions=9)
        credentials = [sessions.create("operator") for _index in range(9)]
        harness = await _harness(tmp_path, sessions=sessions)
        sockets = []
        try:
            for session in credentials[:8]:
                headers = {
                    "Host": _HOST,
                    "Origin": _ORIGIN,
                    "Cookie": f"{SESSION_COOKIE_NAME}={session.session_token}",
                }
                for _index in range(4):
                    socket = await harness.client.ws_connect(
                        "/remote/api/ws",
                        headers=headers,
                    )
                    await socket.receive_json()
                    await socket.receive_json()
                    sockets.append(socket)

            overflow_headers = {
                "Host": _HOST,
                "Origin": _ORIGIN,
                "Cookie": (f"{SESSION_COOKIE_NAME}={credentials[-1].session_token}"),
            }
            with pytest.raises(WSServerHandshakeError) as limit_error:
                await harness.client.ws_connect(
                    "/remote/api/ws",
                    headers=overflow_headers,
                )
            assert limit_error.value.status == 503
        finally:
            await asyncio.gather(
                *(socket.close() for socket in sockets),
                return_exceptions=True,
            )
            await harness.client.close()

    asyncio.run(scenario())


def test_websocket_snapshot_fanout_sends_to_clients_concurrently(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        sockets = tuple(web.WebSocketResponse() for _index in range(3))
        active = 0
        maximum_active = 0
        all_started = asyncio.Event()
        release = asyncio.Event()

        async def send_many(
            _socket: web.WebSocketResponse,
            _payloads: object,
        ) -> None:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            if active == len(sockets):
                all_started.set()
            await release.wait()
            active -= 1

        try:
            harness.application._websockets.update((socket, asyncio.Lock()) for socket in sockets)
            monkeypatch.setattr(harness.application, "_send_many", send_many)
            harness.state.update_catalog((), change_token="concurrent-fanout")
            publish = asyncio.create_task(harness.application.publish_snapshot())

            await asyncio.wait_for(all_started.wait(), timeout=1)
            assert maximum_active == len(sockets)

            release.set()
            await publish
        finally:
            release.set()
            harness.application._websockets.clear()
            await harness.client.close()

    asyncio.run(scenario())


def test_expired_websocket_session_publishes_updated_session_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        clock = _Clock()
        sessions = InMemorySessionStore(
            absolute_ttl_seconds=100,
            idle_ttl_seconds=10,
            clock=clock,
        )
        session = sessions.create("operator")
        monkeypatch.setattr(server_module, "_WEBSOCKET_HEARTBEAT_SECONDS", 0.01)
        harness = await _harness(tmp_path, sessions=sessions)
        try:
            socket = await harness.client.ws_connect(
                "/remote/api/ws",
                headers={
                    "Host": _HOST,
                    "Origin": _ORIGIN,
                    "Cookie": f"{SESSION_COOKIE_NAME}={session.session_token}",
                },
            )
            await socket.receive_json()
            await socket.receive_json()

            clock.value = 10
            await asyncio.wait_for(socket.receive(), timeout=1)

            assert socket.close_code == 4401
            assert harness.session_counts[-1] == 0
        finally:
            await harness.client.close()

    asyncio.run(scenario())
