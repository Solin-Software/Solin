from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import threading
from typing import Any, cast
import uuid
from unittest.mock import patch

from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.remote_control import server as server_module
from solin.core.remote_control.certificates import TLSCertificateStore
from solin.core.remote_control.features import (
    TIMER_REMOTE_HAND_ALERT_POLICY,
    TimerRemoteHandAlertPolicy,
)
from solin.core.remote_control.media.contracts import CommandError, RemoteCommand
from solin.core.remote_control.media.http import (
    MediaRemoteDependencies,
    MediaRemoteHttpModule,
)
from solin.core.remote_control.media.state import (
    ProjectionCommandSession,
    RemoteControlStateStore,
)
from solin.core.remote_control.security import (
    InMemorySessionStore,
    LoginRateLimiter,
    RemoteControlCredentialsStore,
    RemoteScope,
    RemoteSessionInfo,
    ScryptParameters,
    ScryptPasswordHasher,
)
from solin.core.remote_control.server import (
    MEDIA_SESSION_COOKIE_NAME,
    TIMER_SESSION_COOKIE_NAME,
    RemoteControlHttpApplication,
    RemoteControlServer,
    ScopedRemoteHttpModule,
)
from solin.core.remote_control.timer.contracts import (
    TimerCommandError,
    TimerLiveWireSnapshot,
    TimerRemoteCommand,
    TimerScheduleWireSnapshot,
)
from solin.core.remote_control.timer.http import (
    TimerRemoteDependencies,
    TimerRemoteHttpModule,
)
from solin.core.remote_control.timer.state import (
    RemoteTimerStateStore,
    TimerCommandSession,
)
from solin.core.timer.hand_alert import TimerHandAlert
from solin.core.timer.models import PartState


_ORIGIN = "https://192.168.1.10:8765"
_HOST = "192.168.1.10:8765"


def test_tls_hot_reload_validates_candidate_before_mutating_live_context(tmp_path: Path) -> None:
    identity = TLSCertificateStore(tmp_path / "tls").load_or_create("192.168.1.10")

    class _LiveContext:
        def __init__(self) -> None:
            self.loaded: tuple[str, str] | None = None

        def load_cert_chain(self, *, certfile: str, keyfile: str) -> None:
            self.loaded = (certfile, keyfile)

    class _LiveApp:
        def __init__(self) -> None:
            self.certificate_der: bytes | None = None

        def set_trust_certificate_der(self, data: bytes | None) -> None:
            self.certificate_der = data

    context = _LiveContext()
    app = _LiveApp()
    with patch.object(server_module, "_build_ssl_context") as validate:
        asyncio.run(
            RemoteControlServer._reload_tls_identity(
                cast(Any, context),
                cast(Any, app),
                identity,
            )
        )

    validate.assert_called_once_with(identity)
    assert context.loaded == (str(identity.certificate_path), str(identity.private_key_path))
    assert app.certificate_der == identity.trust_certificate_der()


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

    @staticmethod
    def has_credentials() -> bool:
        return True

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

    @staticmethod
    def has_credentials() -> bool:
        return True

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
    timer_commands: list[TimerRemoteCommand]
    application: RemoteControlHttpApplication
    media_module: MediaRemoteHttpModule
    timer_module: TimerRemoteHttpModule
    state: RemoteControlStateStore
    timer_state: RemoteTimerStateStore
    sessions: InMemorySessionStore
    timer_sessions: InMemorySessionStore
    command_session: ProjectionCommandSession
    session_inventories: list[tuple[RemoteSessionInfo, ...]]
    public_viewer_counts: list[dict[RemoteScope, int]]
    media_enabled: list[bool]
    timer_enabled: list[bool]


async def _harness(
    tmp_path: Path,
    *,
    credentials_override: Any | None = None,
    timer_credentials_configured: bool = True,
    timer_hand_alert_policy: TimerRemoteHandAlertPolicy = TIMER_REMOTE_HAND_ALERT_POLICY,
    sessions: InMemorySessionStore | None = None,
    timer_sessions: InMemorySessionStore | None = None,
    command_handler: Any | None = None,
    thumbnail_handler: Any | None = None,
    collection_thumbnail_handler: Any | None = None,
    setup_provider: Any | None = None,
    installation_id: str = "installation-1",
) -> _Harness:
    media_assets = tmp_path / "media_assets"
    media_assets.mkdir()
    (media_assets / "index.html").write_text(
        '<!doctype html><title>Media</title><script src="./app.js"></script>',
        "utf-8",
    )
    (media_assets / "app.js").write_text("", "utf-8")
    (media_assets / "service-worker.js").write_text(
        'const SHELL_REVISION = "__SOLIN_REMOTE_SHELL_REVISION__";\n'
        "const SHELL_RESOURCES = __SOLIN_REMOTE_SHELL_RESOURCES__;\n",
        "utf-8",
    )

    timer_assets = tmp_path / "timer_assets"
    timer_assets.mkdir()
    (timer_assets / "index.html").write_text(
        '<!doctype html><title>Timer</title><script src="./app.js"></script>',
        "utf-8",
    )
    (timer_assets / "app.js").write_text("", "utf-8")
    (timer_assets / "service-worker.js").write_text(
        'const SHELL_REVISION = "__SOLIN_REMOTE_SHELL_REVISION__";\n'
        "const SHELL_RESOURCES = __SOLIN_REMOTE_SHELL_RESOURCES__;\n",
        "utf-8",
    )

    hasher = ScryptPasswordHasher(
        ScryptParameters(n=1_024, r=8, p=1, max_memory_bytes=8 * 1024 * 1024)
    )
    profile_settings = ProfileAppSettingsStore(cast(Any, _MemorySettings()))
    credentials = credentials_override or RemoteControlCredentialsStore.for_media(
        profile_settings,
        password_hasher=hasher,
    )
    if credentials_override is None:
        credentials.set_credentials("operator", "correct horse battery staple")

    timer_credentials = RemoteControlCredentialsStore.for_timer(
        profile_settings,
        password_hasher=hasher,
    )
    if timer_credentials_configured:
        timer_credentials.set_credentials("timer.op", "timer secret battery staple")

    state = RemoteControlStateStore()
    timer_state = RemoteTimerStateStore(hand_alert_policy=timer_hand_alert_policy)
    session_store = sessions or InMemorySessionStore(scope=RemoteScope.MEDIA)
    timer_session_store = timer_sessions or InMemorySessionStore(scope=RemoteScope.TIMER)
    command_session = ProjectionCommandSession(state)
    timer_command_session = TimerCommandSession(timer_state)
    commands: list[RemoteCommand] = []
    timer_commands: list[TimerRemoteCommand] = []
    session_inventories: list[tuple[RemoteSessionInfo, ...]] = []
    public_viewer_counts: list[dict[RemoteScope, int]] = []
    media_enabled = [True]
    timer_enabled = [True]

    async def execute(command: RemoteCommand) -> CommandError | None:
        commands.append(command)
        return None

    async def execute_timer(command: TimerRemoteCommand) -> TimerCommandError | None:
        timer_commands.append(command)
        return None

    default_setup = setup_provider or (
        lambda: {
            "locale": "pt-BR",
            "messages": {"app.mediaControl": "Controle de mídia"},
            "tls": {
                "installationId": "installation-1",
                "authoritySha256": "AA:BB:CC:DD",
                "verificationCode": "AABB · CCDD",
                "authorityCertificateUrl": "/remote/trust-certificate.cer",
                "authorityNotValidAfter": "2036-07-16T12:00:00+00:00",
            },
        }
    )

    media_module = MediaRemoteHttpModule(
        MediaRemoteDependencies(
            state=state,
            command_session=command_session,
            credentials=credentials,
            sessions=session_store,
            rate_limiter=LoginRateLimiter(),
            assets_directory=media_assets,
            command_handler=command_handler or execute,
            setup_provider=default_setup,
            enabled_predicate=lambda: media_enabled[0],
            theme_provider=lambda: "dark",
            thumbnail_handler=thumbnail_handler,
            collection_thumbnail_handler=collection_thumbnail_handler,
            profile_id="profile-1",
            profile_name="Sala principal",
            meeting_week_start=lambda: date(2026, 7, 13),
        )
    )
    timer_module = TimerRemoteHttpModule(
        TimerRemoteDependencies(
            state=timer_state,
            command_session=timer_command_session,
            credentials=timer_credentials,
            sessions=timer_session_store,
            rate_limiter=LoginRateLimiter(),
            assets_directory=timer_assets,
            command_handler=execute_timer,
            setup_provider=default_setup,
            enabled_predicate=lambda: timer_enabled[0],
            theme_provider=lambda: "dark",
            profile_id="profile-1",
            profile_name="Sala principal",
            clock_ms=lambda: 1_721_130_000_000,
        )
    )

    application = RemoteControlHttpApplication(
        (media_module, timer_module),
        allowed_origin=_ORIGIN,
        installation_id=installation_id,
        trust_certificate_der=b"scoped authority certificate",
        session_inventory_changed=session_inventories.append,
        public_viewer_count_changed=public_viewer_counts.append,
    )
    client = TestClient(TestServer(application.app))
    await client.start_server()
    return _Harness(
        client=client,
        commands=commands,
        timer_commands=timer_commands,
        application=application,
        media_module=media_module,
        timer_module=timer_module,
        state=state,
        timer_state=timer_state,
        sessions=session_store,
        timer_sessions=timer_session_store,
        command_session=command_session,
        session_inventories=session_inventories,
        public_viewer_counts=public_viewer_counts,
        media_enabled=media_enabled,
        timer_enabled=timer_enabled,
    )


async def _authenticated_headers(harness: _Harness) -> dict[str, str]:
    login = await harness.client.post(
        "/remote/media/api/auth/login",
        headers={"Host": _HOST, "Origin": _ORIGIN},
        json={
            "username": "operator",
            "password": "correct horse battery staple",
        },
    )
    assert login.status == 200
    payload = await login.json()
    session_token = login.cookies[MEDIA_SESSION_COOKIE_NAME].value
    return {
        "Host": _HOST,
        "Origin": _ORIGIN,
        "X-CSRF-Token": payload["csrfToken"],
        "Cookie": f"{MEDIA_SESSION_COOKIE_NAME}={session_token}",
    }


async def _authenticated_timer_headers(harness: _Harness) -> dict[str, str]:
    login = await harness.client.post(
        "/remote/timer/api/auth/login",
        headers={"Host": _HOST, "Origin": _ORIGIN},
        json={
            "username": "timer.op",
            "password": "timer secret battery staple",
        },
    )
    assert login.status == 200
    payload = await login.json()
    session_token = login.cookies[TIMER_SESSION_COOKIE_NAME].value
    return {
        "Host": _HOST,
        "Origin": _ORIGIN,
        "X-CSRF-Token": payload["csrfToken"],
        "Cookie": f"{TIMER_SESSION_COOKIE_NAME}={session_token}",
    }


def test_static_shell_has_strict_security_headers(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            response = await harness.client.get("/remote/media/", headers={"Host": _HOST})

            assert response.status == 200
            assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["Cache-Control"] == "no-store"

            script = await harness.client.get(
                "/remote/media/app.js",
                headers={"Host": _HOST},
            )
            assert script.status == 200
            assert script.headers["Cache-Control"] == "no-cache"

            service_worker = await harness.client.get(
                "/remote/media/service-worker.js",
                headers={"Host": _HOST},
            )
            assert service_worker.status == 200
            assert service_worker.headers["Cache-Control"] == (
                "no-cache, no-store, must-revalidate"
            )
            assert service_worker.headers["Pragma"] == "no-cache"
            assert service_worker.headers["Expires"] == "0"
            assert service_worker.headers["Service-Worker-Allowed"] == "/remote/media/"
            worker_source = await service_worker.text()
            assert "__SOLIN_REMOTE_" not in worker_source

            index = await response.text()
            versioned_script = re.search(
                r'src="(?P<url>/remote/media/_assets/[a-f0-9]{32}/app\.js)"',
                index,
            )
            assert versioned_script is not None
            immutable_script = await harness.client.get(
                versioned_script.group("url"),
                headers={"Host": _HOST},
            )
            assert immutable_script.status == 200
            assert immutable_script.headers["Cache-Control"] == (
                "public, max-age=31536000, immutable"
            )

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


def test_setup_is_public_no_store_and_does_not_expose_profile(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            response = await harness.client.get(
                "/remote/media/api/setup",
                headers={"Host": _HOST},
            )
            payload = await response.json()

            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert payload["locale"] == "pt-BR"
            assert payload["messages"] == {"app.mediaControl": "Controle de mídia"}
            assert payload["tls"]["installationId"] == "installation-1"
            assert payload["tls"]["authorityCertificateUrl"] == ("/remote/trust-certificate.cer")
            assert "profile" not in payload
            assert "name" not in payload
            assert "id" not in payload

            removed_endpoint = await harness.client.get(
                "/remote/media/api/localization",
                headers={"Host": _HOST},
            )
            assert removed_endpoint.status == 404
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_profile_snapshot_publishes_updated_localization(tmp_path: Path) -> None:
    async def scenario() -> None:
        localization = {
            "locale": "en",
            "messages": {"app.mediaControl": "Media control"},
            "tls": {
                "installationId": "installation-1",
                "authoritySha256": "AA:BB:CC:DD",
                "verificationCode": "AABB · CCDD",
                "authorityCertificateUrl": "/remote/trust-certificate.cer",
                "authorityNotValidAfter": "2036-07-16T12:00:00+00:00",
            },
        }
        harness = await _harness(
            tmp_path,
            setup_provider=lambda: localization,
        )
        try:
            headers = await _authenticated_headers(harness)
            socket = await harness.client.ws_connect(
                "/remote/media/api/ws",
                headers=headers,
            )
            await socket.receive_json()

            localization = {
                "locale": "pt-BR",
                "messages": {"app.mediaControl": "Controle de mídia"},
            }
            await harness.application.publish_profile()
            event = await socket.receive_json()

            assert event["type"] == "profile.snapshot"
            assert event["sequence"] == 1
            assert event["payload"] == {
                "id": "profile-1",
                "name": "Sala principal",
                "displayName": "Sala principal",
                "theme": "dark",
                **localization,
            }
            await socket.close()
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
                "/remote/media/api/collection-thumbnails/meeting/mwb:2026-07-13:E:1",
                headers={"Host": _HOST},
            )
            assert unauthenticated.status == 401

            headers = await _authenticated_headers(harness)
            response = await harness.client.get(
                "/remote/media/api/collection-thumbnails/meeting/mwb:2026-07-13:E:1",
                headers=headers,
            )
            invalid = await harness.client.get(
                "/remote/media/api/collection-thumbnails/meeting/not%2Fa%2Fcollection",
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
                "/remote/media/api/auth/login",
                headers={"Host": _HOST, "Origin": _ORIGIN},
                json={
                    "username": "operator",
                    "password": "correct horse battery staple",
                },
            )
            assert login.status == 200
            login_payload = await login.json()
            csrf_token = login_payload["csrfToken"]
            session_token = login.cookies[MEDIA_SESSION_COOKIE_NAME].value
            cookie = {"Cookie": f"{MEDIA_SESSION_COOKIE_NAME}={session_token}"}

            bootstrap = await harness.client.get(
                "/remote/media/api/bootstrap",
                headers={"Host": _HOST, **cookie},
            )
            assert bootstrap.status == 200
            bootstrap_payload = await bootstrap.json()
            assert bootstrap_payload["csrfToken"] == csrf_token
            assert bootstrap_payload["profile"]["name"] == "Sala principal"
            assert bootstrap_payload["profile"]["locale"] == "pt-BR"
            assert bootstrap_payload["profile"]["messages"] == {
                "app.mediaControl": "Controle de mídia"
            }
            assert bootstrap_payload["currentMeetingWeekStart"] == "2026-07-13"

            command_id = str(uuid.uuid4())
            headers = {
                "Host": _HOST,
                "Origin": _ORIGIN,
                "X-CSRF-Token": csrf_token,
                **cookie,
            }
            first = await harness.client.post(
                "/remote/media/api/commands",
                headers=headers,
                json={"commandId": command_id, "type": "stop"},
            )
            replay = await harness.client.post(
                "/remote/media/api/commands",
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


def test_timer_custom_catalog_auth_policy_and_command_replay(tmp_path: Path) -> None:
    from dataclasses import replace
    from solin.core.remote_control.timer.contracts import (
        CustomScheduleCatalogWireSnapshot,
        CustomScheduleWireItem,
        TimerControlCapabilities,
    )

    async def scenario() -> None:
        harness = await _harness(tmp_path)
        calls = []

        async def catalog(query):
            calls.append(query)
            return CustomScheduleCatalogWireSnapshot(
                (CustomScheduleWireItem("s", "Private", 1, ()),)
            )

        try:
            harness.timer_module._dependencies = replace(
                harness.timer_module._dependencies, catalog_handler=catalog
            )
            path = "/remote/timer/api/custom-schedules"
            anonymous = await harness.client.get(path, headers={"Host": _HOST})
            assert anonymous.status == 401
            headers = await _authenticated_timer_headers(harness)
            assert (await harness.client.get(path, headers=headers)).status == 404
            assert calls == []
            capabilities = TimerControlCapabilities(can_manage_custom_schedules=True)
            harness.timer_state.update_schedule(
                replace(harness.timer_state.schedule, capabilities=capabilities)
            )
            response = await harness.client.get(path, headers=headers)
            assert (await response.json())["schedules"][0]["title"] == "Private"
            assert len(calls) == 1
            assert (await harness.client.get(path, headers=headers)).status == 200
            assert len(calls) == 2
            harness.timer_module._dependencies = replace(
                harness.timer_module._dependencies, catalog_handler=None
            )
            assert (await harness.client.get(path, headers=headers)).status == 503

            async def empty_result(_query):
                return None

            harness.timer_module._dependencies = replace(
                harness.timer_module._dependencies, catalog_handler=empty_result
            )
            assert (await harness.client.get(path, headers=headers)).status == 503
            harness.timer_module._dependencies = replace(
                harness.timer_module._dependencies, catalog_handler=catalog
            )
            payload = {
                "type": "create_custom_schedule",
                "commandId": str(uuid.uuid4()),
                "scheduleRevision": harness.timer_state.schedule_revision,
                "title": "Event",
            }
            first = await harness.client.post(
                "/remote/timer/api/commands", headers=headers, json=payload
            )
            replay = await harness.client.post(
                "/remote/timer/api/commands", headers=headers, json=payload
            )
            assert (await first.json())["ok"] and (await replay.json())["ok"]
            assert len(harness.timer_commands) == 1
            conflict = await harness.client.post(
                "/remote/timer/api/commands", headers=headers, json={**payload, "title": "Other"}
            )
            assert (await conflict.json())["error"]["code"] == "command_id_conflict"
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_timer_catalog_rechecks_session_after_main_thread_query(tmp_path: Path) -> None:
    from dataclasses import replace
    from solin.core.remote_control.timer.contracts import (
        CustomScheduleCatalogWireSnapshot,
        TimerControlCapabilities,
    )

    async def scenario() -> None:
        harness = await _harness(tmp_path)
        started = asyncio.Event()
        finish = asyncio.Event()

        async def catalog(_query):
            started.set()
            await finish.wait()
            return CustomScheduleCatalogWireSnapshot(())

        try:
            harness.timer_module._dependencies = replace(
                harness.timer_module._dependencies, catalog_handler=catalog
            )
            harness.timer_state.update_schedule(
                replace(
                    harness.timer_state.schedule,
                    capabilities=TimerControlCapabilities(can_manage_custom_schedules=True),
                )
            )
            headers = await _authenticated_timer_headers(harness)
            pending = asyncio.create_task(
                harness.client.get("/remote/timer/api/custom-schedules", headers=headers)
            )
            await started.wait()
            harness.timer_sessions.revoke_all()
            finish.set()
            assert (await pending).status == 401
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_media_and_timer_sessions_cookies_and_csrf_never_cross_contaminate(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            media_headers = await _authenticated_headers(harness)
            timer_headers = await _authenticated_timer_headers(harness)
            harness.client.session.cookie_jar.clear()

            media_on_timer_schedule = await harness.client.get(
                "/remote/timer/api/schedule",
                headers=media_headers,
            )
            media_on_timer_cmd = await harness.client.post(
                "/remote/timer/api/commands",
                headers=media_headers,
                json={"commandId": str(uuid.uuid4()), "type": "stop_part"},
            )
            timer_cookie_with_media_csrf = await harness.client.post(
                "/remote/timer/api/commands",
                headers={
                    **timer_headers,
                    "X-CSRF-Token": media_headers["X-CSRF-Token"],
                },
                json={"commandId": str(uuid.uuid4()), "type": "stop_part"},
            )
            timer_on_media_catalog = await harness.client.get(
                "/remote/media/api/catalog",
                headers=timer_headers,
            )
            timer_on_media_cmd = await harness.client.post(
                "/remote/media/api/commands",
                headers=timer_headers,
                json={"commandId": str(uuid.uuid4()), "type": "stop"},
            )

            assert media_on_timer_schedule.status == 401
            assert media_on_timer_cmd.status == 403
            assert timer_cookie_with_media_csrf.status == 403
            assert timer_on_media_catalog.status == 401
            assert timer_on_media_cmd.status == 403

            with pytest.raises(WSServerHandshakeError) as ws_error:
                await harness.client.ws_connect(
                    "/remote/timer/api/ws",
                    headers=media_headers,
                )
            assert ws_error.value.status == 401
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_timer_public_viewer_works_without_login_and_isolates_public_ws_pool(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            public_boot = await harness.client.get(
                "/remote/timer/api/public",
                headers={"Host": _HOST},
            )
            assert public_boot.status == 200
            boot_json = await public_boot.json()
            assert boot_json["authenticated"] is False
            assert boot_json["controlAvailable"] is True
            assert "schedule" not in boot_json
            assert "scheduleRevision" not in boot_json

            public_ws = await harness.client.ws_connect(
                "/remote/timer/api/ws/public",
                headers={"Host": _HOST, "Origin": _ORIGIN},
            )
            initial = await public_ws.receive_json()
            assert initial["type"] == "public.snapshot"
            assert "scheduleRevision" not in initial
            assert harness.timer_module.public_viewer_count() == 1
            assert harness.public_viewer_counts[-1] == {
                RemoteScope.MEDIA: 0,
                RemoteScope.TIMER: 1,
            }

            auth_headers = await _authenticated_timer_headers(harness)
            auth_ws = await harness.client.ws_connect(
                "/remote/timer/api/ws",
                headers=auth_headers,
            )
            auth_initial = await auth_ws.receive_json()
            assert auth_initial["type"] == "session.snapshot"
            assert "scheduleRevision" in auth_initial

            harness.timer_state.update_live(
                TimerLiveWireSnapshot(
                    live_revision=0,
                    server_epoch_ms=1_721_130_005_000,
                    utc_offset_minutes=-180,
                    active=True,
                    state=PartState.RUNNING,
                    active_part_id="part-1",
                    active_part_title="Opening Comments",
                    planned_seconds=60,
                    elapsed_seconds=5.0,
                )
            )
            harness.timer_state.update_schedule(
                TimerScheduleWireSnapshot.empty(week_monday="2026-07-13")
            )
            await harness.application.publish_snapshot(RemoteScope.TIMER)

            pub_live = await public_ws.receive_json()
            assert pub_live["type"] == "timer.live"
            assert "scheduleRevision" not in pub_live
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(public_ws.receive_json(), timeout=0.04)

            auth_first = await auth_ws.receive_json()
            auth_second = await auth_ws.receive_json()
            assert {auth_first["type"], auth_second["type"]} == {
                "timer.live",
                "timer.schedule",
            }

            await public_ws.close()
            await auth_ws.close()
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_timer_revocation_blocks_a_queued_hand_alert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _harness(
            tmp_path,
            timer_hand_alert_policy=TimerRemoteHandAlertPolicy.AUTHENTICATED_ONLY,
        )
        async with harness.client:
            headers = await _authenticated_timer_headers(harness)
            socket = await harness.client.ws_connect("/remote/timer/api/ws", headers=headers)
            assert (await socket.receive_json(timeout=1))["type"] == "session.snapshot"
            module = harness.timer_module
            (server_socket,) = module._auth_websockets
            (session,) = module.active_session_inventory()

            queued = asyncio.Event()
            send = module._send_many_to_map

            async def observe_send(registry, server_socket, payloads):
                if payloads[0]["type"] == "timer.live":
                    queued.set()
                await send(registry, server_socket, payloads)

            revoked = asyncio.Event()
            close_session = module._close_session_websockets

            async def observe_revocation(token, reason):
                revoked.set()
                await close_session(token, reason)

            monkeypatch.setattr(module, "_send_many_to_map", observe_send)
            monkeypatch.setattr(module, "_close_session_websockets", observe_revocation)
            harness.timer_state.update_hand_alert(TimerHandAlert(names=("Private name",)))

            async with asyncio.TaskGroup() as tasks:
                async with module._auth_websockets[server_socket]:
                    tasks.create_task(harness.application.publish_snapshot(RemoteScope.TIMER))
                    await asyncio.wait_for(queued.wait(), timeout=1)
                    task = tasks.create_task(module.revoke_session(session.management_id))
                    await asyncio.wait_for(revoked.wait(), timeout=1)

                event = await socket.receive_json(timeout=1)
                assert event["type"] == "session.revoked"
                assert event["payload"] == {"reason": "revoked_device"}
                await socket.close()
            assert task.result()

            auth_boot = await harness.client.get("/remote/timer/api/bootstrap", headers=headers)
            assert auth_boot.status == 401
            public_boot = await harness.client.get("/remote/timer/api/public", headers=headers)
            body = await public_boot.json()
            assert body["authenticated"] is False
            assert "handAlert" not in body["live"]

    asyncio.run(scenario())


def test_timer_view_only_mode_rejects_login_while_serving_public_timer(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path, timer_credentials_configured=False)
        try:
            public_boot = await harness.client.get(
                "/remote/timer/api/public",
                headers={"Host": _HOST},
            )
            payload = await public_boot.json()
            assert public_boot.status == 200
            assert payload["controlAvailable"] is False

            login = await harness.client.post(
                "/remote/timer/api/auth/login",
                headers={"Host": _HOST, "Origin": _ORIGIN},
                json={"username": "timer.op", "password": "timer secret battery staple"},
            )
            assert login.status == 403
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_dynamic_module_disable_closes_only_disabled_scope_connections(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            media_headers = await _authenticated_headers(harness)
            timer_headers = await _authenticated_timer_headers(harness)
            media_ws = await harness.client.ws_connect(
                "/remote/media/api/ws",
                headers=media_headers,
            )
            timer_ws = await harness.client.ws_connect(
                "/remote/timer/api/ws",
                headers=timer_headers,
            )
            await media_ws.receive_json()
            await timer_ws.receive_json()

            harness.media_enabled[0] = False
            await harness.application.sync_module_states()

            disabled_page = await harness.client.get(
                "/remote/media/",
                headers={"Host": _HOST},
            )
            active_timer_page = await harness.client.get(
                "/remote/timer/",
                headers={"Host": _HOST},
            )
            assert disabled_page.status == 404
            assert active_timer_page.status == 200

            revoked = await asyncio.wait_for(media_ws.receive_json(), timeout=1.0)
            assert revoked["type"] == "session.revoked"
            assert revoked["payload"]["reason"] == "service_disabled"
            await asyncio.wait_for(media_ws.receive(), timeout=1.0)
            assert media_ws.closed
            assert not timer_ws.closed

            await timer_ws.close()
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_origin_host_session_and_csrf_checks_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            wrong_host = await harness.client.get(
                "/remote/media/",
                headers={"Host": "attacker.invalid"},
            )
            missing_origin = await harness.client.post(
                "/remote/media/api/auth/login",
                headers={"Host": _HOST},
                json={
                    "username": "operator",
                    "password": "correct horse battery staple",
                },
            )
            no_session = await harness.client.get(
                "/remote/media/api/catalog",
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
                "/remote/media/api/auth/login",
                headers={"Host": _HOST, "Origin": _ORIGIN},
                json={
                    "username": "operator",
                    "password": "correct horse battery staple",
                },
            )
            session_token = login.cookies[MEDIA_SESSION_COOKIE_NAME].value
            websocket_headers = {
                "Host": _HOST,
                "Origin": _ORIGIN,
                "Cookie": f"{MEDIA_SESSION_COOKIE_NAME}={session_token}",
            }

            harness.state.update_catalog((), change_token="before-first-client")
            first = await harness.client.ws_connect(
                "/remote/media/api/ws",
                headers=websocket_headers,
            )
            first_snapshot = await first.receive_json()

            assert first_snapshot["type"] == "session.snapshot"
            assert first_snapshot["sequence"] == 0
            assert first_snapshot["catalogRevision"] == 1
            assert first_snapshot["payload"]["catalog"]["catalogRevision"] == 1
            assert first_snapshot["payload"]["playback"]["playbackRevision"] == 0
            assert first_snapshot["payload"]["profile"]["name"] == "Sala principal"

            await harness.application.publish_snapshot(RemoteScope.MEDIA)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(first.receive(), timeout=0.05)

            harness.state.update_catalog((), change_token="second-revision")
            await harness.application.publish_snapshot(RemoteScope.MEDIA)
            first_update = await first.receive_json()
            assert first_update["type"] == "catalog.snapshot"
            assert first_update["sequence"] == 1
            assert first_update["catalogRevision"] == 2

            second = await harness.client.ws_connect(
                "/remote/media/api/ws",
                headers=websocket_headers,
            )
            second_snapshot = await second.receive_json()
            assert second_snapshot["type"] == "session.snapshot"
            assert second_snapshot["sequence"] == 1

            harness.state.update_catalog((), change_token="third-revision")
            await harness.application.publish_snapshot(RemoteScope.MEDIA)
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


def test_server_shutdown_does_not_wait_for_connected_websocket_grace_period(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            headers = await _authenticated_headers(harness)
            socket = await harness.client.ws_connect(
                "/remote/media/api/ws",
                headers=headers,
            )
            await socket.receive_json()

            await asyncio.wait_for(harness.client.server.close(), timeout=1.0)
            await asyncio.wait_for(socket.receive(), timeout=1.0)

            assert socket.closed
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_logout_revokes_every_websocket_using_that_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            login = await harness.client.post(
                "/remote/media/api/auth/login",
                headers={"Host": _HOST, "Origin": _ORIGIN},
                json={
                    "username": "operator",
                    "password": "correct horse battery staple",
                },
            )
            payload = await login.json()
            token = login.cookies[MEDIA_SESSION_COOKIE_NAME].value
            session_headers = {
                "Host": _HOST,
                "Origin": _ORIGIN,
                "Cookie": f"{MEDIA_SESSION_COOKIE_NAME}={token}",
            }
            websockets = []
            for _ in range(2):
                websocket = await harness.client.ws_connect(
                    "/remote/media/api/ws",
                    headers=session_headers,
                )
                websockets.append(websocket)
                await websocket.receive_json()

            async def receive_revocation(websocket) -> None:
                revoked = await websocket.receive_json()
                closed = await websocket.receive()
                assert revoked["type"] == "session.revoked"
                assert revoked["payload"] == {"reason": "signed_out"}
                assert closed.type.name == "CLOSE"
                assert closed.data == 4401
                assert websocket.close_code == 4401
                assert websocket.exception() is None

            async with asyncio.TaskGroup() as group:
                for websocket in websockets:
                    group.create_task(receive_revocation(websocket))
                logout = await harness.client.post(
                    "/remote/media/api/logout",
                    headers={
                        **session_headers,
                        "X-CSRF-Token": payload["csrfToken"],
                    },
                )
                assert logout.status == 200
            bootstrap = await harness.client.get(
                "/remote/media/api/bootstrap",
                headers={
                    "Host": _HOST,
                    "Cookie": f"{MEDIA_SESSION_COOKIE_NAME}={token}",
                },
            )
            assert bootstrap.status == 401
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_inflight_login_cannot_create_session_after_global_revocation(tmp_path: Path) -> None:
    async def scenario() -> None:
        credentials = _BlockingCredentials()
        sessions = InMemorySessionStore(scope=RemoteScope.MEDIA)
        harness = await _harness(
            tmp_path,
            credentials_override=credentials,
            sessions=sessions,
        )
        try:
            login_task = asyncio.create_task(
                harness.client.post(
                    "/remote/media/api/auth/login",
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
            assert MEDIA_SESSION_COOKIE_NAME not in response.cookies
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
                        "/remote/media/api/auth/login",
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
                harness.application._authenticate_credentials(
                    credentials, "operator", "password-one"
                )
            )
            second = asyncio.create_task(
                harness.application._authenticate_credentials(
                    credentials, "operator", "password-two"
                )
            )
            assert await asyncio.to_thread(credentials.two_active.wait, 1)

            first.cancel()
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
            third = asyncio.create_task(
                harness.application._authenticate_credentials(
                    credentials, "operator", "password-three"
                )
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
            await ScopedRemoteHttpModule._strict_json(
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

            monkeypatch.setattr(harness.media_module, "_strict_json", revoke_then_parse)
            response = await harness.client.post(
                "/remote/media/api/commands",
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
    from solin.core.remote_control.media import http as media_http_module

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        executed: list[RemoteCommand] = []

        async def execute(command: RemoteCommand) -> CommandError | None:
            executed.append(command)
            started.set()
            await release.wait()
            return None

        monkeypatch.setattr(media_http_module, "COMMAND_TIMEOUT_SECONDS", 0.01)
        harness = await _harness(tmp_path, command_handler=execute)
        try:
            headers = await _authenticated_headers(harness)
            command_id = str(uuid.uuid4())
            first = await harness.client.post(
                "/remote/media/api/commands",
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
            assert len(harness.media_module._command_tasks) == 1

            release.set()
            for _attempt in range(100):
                if not harness.media_module._command_tasks:
                    break
                await asyncio.sleep(0.01)
            assert not harness.media_module._command_tasks

            replay = await harness.client.post(
                "/remote/media/api/commands",
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
    from solin.core.remote_control.media import http as media_http_module

    async def scenario() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def execute(_command: RemoteCommand) -> CommandError | None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(media_http_module, "COMMAND_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(server_module, "COMMAND_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
        harness = await _harness(tmp_path, command_handler=execute)
        headers = await _authenticated_headers(harness)
        response = await harness.client.post(
            "/remote/media/api/commands",
            headers=headers,
            json={"commandId": str(uuid.uuid4()), "type": "stop"},
        )

        assert response.status == 200
        assert started.is_set()
        assert harness.media_module._command_tasks

        await harness.client.close()

        assert cancelled.is_set()
        assert not harness.media_module._command_tasks

    asyncio.run(scenario())


def test_websocket_limits_and_binary_messages_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        sockets = []
        try:
            headers = await _authenticated_headers(harness)
            for _index in range(4):
                socket = await harness.client.ws_connect(
                    "/remote/media/api/ws",
                    headers=headers,
                )
                await socket.receive_json()
                sockets.append(socket)

            with pytest.raises(WSServerHandshakeError) as limit_error:
                await harness.client.ws_connect(
                    "/remote/media/api/ws",
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
        sessions = InMemorySessionStore(max_sessions=9, scope=RemoteScope.MEDIA)
        credentials = [sessions.create("operator") for _index in range(9)]
        harness = await _harness(tmp_path, sessions=sessions)
        sockets = []
        try:
            for session in credentials[:8]:
                headers = {
                    "Host": _HOST,
                    "Origin": _ORIGIN,
                    "Cookie": f"{MEDIA_SESSION_COOKIE_NAME}={session.session_token}",
                }
                for _index in range(4):
                    socket = await harness.client.ws_connect(
                        "/remote/media/api/ws",
                        headers=headers,
                    )
                    await socket.receive_json()
                    sockets.append(socket)

            overflow_headers = {
                "Host": _HOST,
                "Origin": _ORIGIN,
                "Cookie": (f"{MEDIA_SESSION_COOKIE_NAME}={credentials[-1].session_token}"),
            }
            with pytest.raises(WSServerHandshakeError) as limit_error:
                await harness.client.ws_connect(
                    "/remote/media/api/ws",
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
            _socket_map: object,
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
            harness.media_module._auth_websockets.update(
                (socket, asyncio.Lock()) for socket in sockets
            )
            monkeypatch.setattr(harness.media_module, "_send_many_to_map", send_many)
            harness.state.update_catalog((), change_token="concurrent-fanout")
            publish = asyncio.create_task(harness.application.publish_snapshot(RemoteScope.MEDIA))

            await asyncio.wait_for(all_started.wait(), timeout=1)
            assert maximum_active == len(sockets)

            release.set()
            await publish
        finally:
            release.set()
            harness.media_module._auth_websockets.clear()
            await harness.client.close()

    asyncio.run(scenario())


def test_login_publishes_safe_client_metadata_for_desktop_inventory(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _harness(tmp_path)
        try:
            response = await harness.client.post(
                "/remote/media/api/auth/login",
                headers={
                    "Host": _HOST,
                    "Origin": _ORIGIN,
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 "
                        "Chrome/130.0 Mobile Safari/537.36"
                    ),
                    "X-Solin-Display-Mode": "standalone",
                },
                json={
                    "username": "operator",
                    "password": "correct horse battery staple",
                },
            )

            assert response.status == 200
            inventory = harness.session_inventories[-1]
            assert len(inventory) == 1
            session = inventory[0]
            assert session.scope is RemoteScope.MEDIA
            assert session.browser == "Chrome"
            assert session.platform == "Android"
            assert session.client_mode == "standalone"
            assert session.remote_address
            assert session.connected_socket_count == 0
            assert not hasattr(session, "session_token")
            assert not hasattr(session, "csrf_token")
        finally:
            await harness.client.close()

    asyncio.run(scenario())


def test_desktop_can_revoke_one_or_all_sessions_across_scopes_without_restarting_server(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sessions = InMemorySessionStore(scope=RemoteScope.MEDIA)
        timer_sessions = InMemorySessionStore(scope=RemoteScope.TIMER)
        first = sessions.create("operator", browser="Chrome", platform="Android")
        second = timer_sessions.create("timer.op", browser="Safari", platform="iPhone")
        harness = await _harness(
            tmp_path,
            sessions=sessions,
            timer_sessions=timer_sessions,
        )
        first_socket = None
        second_socket = None
        try:
            first_socket = await harness.client.ws_connect(
                "/remote/media/api/ws",
                headers={
                    "Host": _HOST,
                    "Origin": _ORIGIN,
                    "Cookie": f"{MEDIA_SESSION_COOKIE_NAME}={first.session_token}",
                },
            )
            second_socket = await harness.client.ws_connect(
                "/remote/timer/api/ws",
                headers={
                    "Host": _HOST,
                    "Origin": _ORIGIN,
                    "Cookie": f"{TIMER_SESSION_COOKIE_NAME}={second.session_token}",
                },
            )
            for socket in (first_socket, second_socket):
                await socket.receive_json()

            inventory = harness.session_inventories[-1]
            assert {item.global_management_id for item in inventory} == {
                f"media:{first.management_id}",
                f"timer:{second.management_id}",
            }
            assert all(item.connected_socket_count == 1 for item in inventory)
            server_instance = harness.state.server_instance_id

            assert await harness.application.revoke_session(f"media:{first.management_id}") is True
            revoked_event = await first_socket.receive_json()
            assert revoked_event["type"] == "session.revoked"
            assert revoked_event["payload"] == {"reason": "revoked_device"}
            assert sessions.resolve(first.session_token) is None
            assert timer_sessions.resolve(second.session_token) is not None
            assert harness.state.server_instance_id == server_instance
            assert [item.global_management_id for item in harness.session_inventories[-1]] == [
                f"timer:{second.management_id}"
            ]

            assert await harness.application.revoke_all_sessions() == 1
            revoked_all_event = await second_socket.receive_json()
            assert revoked_all_event["type"] == "session.revoked"
            assert revoked_all_event["payload"] == {"reason": "revoked_all"}
            assert sessions.active_sessions() == ()
            assert timer_sessions.active_sessions() == ()
            assert harness.state.server_instance_id == server_instance
            assert harness.session_inventories[-1] == ()
        finally:
            if first_socket is not None:
                await first_socket.close()
            if second_socket is not None:
                await second_socket.close()
            await harness.client.close()

    asyncio.run(scenario())


def test_expired_websocket_session_publishes_updated_session_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from solin.core.remote_control.media import http as media_http_module

    async def scenario() -> None:
        clock = _Clock()
        sessions = InMemorySessionStore(
            absolute_ttl_seconds=100,
            idle_ttl_seconds=10,
            clock=clock,
            scope=RemoteScope.MEDIA,
        )
        session = sessions.create("operator")
        monkeypatch.setattr(media_http_module, "WEBSOCKET_HEARTBEAT_SECONDS", 0.01)
        harness = await _harness(tmp_path, sessions=sessions)
        try:
            socket = await harness.client.ws_connect(
                "/remote/media/api/ws",
                headers={
                    "Host": _HOST,
                    "Origin": _ORIGIN,
                    "Cookie": f"{MEDIA_SESSION_COOKIE_NAME}={session.session_token}",
                },
            )
            await socket.receive_json()

            clock.value = 10
            await asyncio.wait_for(socket.receive(), timeout=1)

            assert socket.close_code == 4401
            assert harness.session_inventories[-1] == ()
        finally:
            await harness.client.close()

    asyncio.run(scenario())
