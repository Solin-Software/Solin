from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
import statistics
import threading
import time
from types import SimpleNamespace
import uuid

from aiohttp.test_utils import TestClient, TestServer
from PySide6.QtCore import QCoreApplication, QObject, Signal
import pytest

from solin.controllers.remote_timer_adapter import (
    RemoteTimerAdapter,
    RemoteTimerAdapterDependencies,
)
from solin.controllers.timer_engine import TimerEngine
from solin.controllers.timer_operations import TimerOperations
from solin.controllers.timer_hand_alert_controller import TimerHandAlertController
from solin.core.foundation.resources import application_resource_path
from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.integrations.automation.settings import ZoomSettingsStore
from solin.core.remote_control.features import (
    DEFAULT_TIMER_REMOTE_FEATURE_FLAGS,
    TimerRemoteFeatureFlags,
    TimerRemoteHandAlertPolicy,
)
from solin.core.remote_control.security import (
    InMemorySessionStore,
    LoginRateLimiter,
    RemoteControlCredentialsStore,
    RemoteScope,
    ScryptParameters,
    ScryptPasswordHasher,
)
from solin.core.remote_control.server import (
    TIMER_SESSION_COOKIE_NAME,
    TRUST_CERTIFICATE_ROUTE,
    RemoteControlHttpApplication,
)
from solin.core.timer.application import TimerSession
from solin.core.timer.custom_schedule import TimerDurationBudgetError
from solin.core.timer.hand_alert import TimerHandAlert
from solin.core.timer.infrastructure import JsonCustomTimerRepository
from solin.core.timer.models import ClockConfig, MeetingSchedule, MeetingType
from solin.core.timer.pacing import PacingSnapshot, PacingStatus
from solin.core.timer.pacing_models import PacingWindow
from solin.core.timer.schedule_factory import build_default_schedule
from solin.ui.qml.timer_bridge import TimerBridge


_APP = QCoreApplication.instance() or QCoreApplication([])
_ORIGIN = "https://192.168.1.10:8765"
_HOST = "192.168.1.10:8765"


class _MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def string(self, key: str, default: str = "") -> str:
        return str(self.values.get(key, default) or "")

    def value(self, key: str, default: bool, value_type: type[bool]) -> bool:
        return value_type(self.values.get(key, default))

    def set_value(self, key: str, value: object, *, sync: bool = True) -> None:
        del sync
        self.values[key] = value

    def remove(self, key: str, *, sync: bool = True) -> None:
        del sync
        self.values.pop(key, None)


class _InMemoryTimerRepository:
    def __init__(self) -> None:
        self.clock_config = ClockConfig()
        self.visible = False
        self.meeting_type = MeetingType.MIDWEEK
        self.schedules: dict[tuple[str, str], dict] = {}

    def load_clock_config(self) -> ClockConfig:
        return self.clock_config

    def save_clock_config(self, config: ClockConfig) -> None:
        self.clock_config = config

    def get_timer_visible(self) -> bool:
        return self.visible

    def set_timer_visible(self, visible: bool) -> None:
        self.visible = visible

    def load_last_meeting_type(self) -> MeetingType:
        return self.meeting_type

    def save_last_meeting_type(self, meeting_type: MeetingType) -> None:
        self.meeting_type = meeting_type

    def load_schedule(
        self,
        week_monday: date,
        meeting_type: MeetingType,
    ) -> MeetingSchedule | None:
        saved = self.schedules.get((week_monday.isoformat(), meeting_type.value))
        return MeetingSchedule.from_dict(saved) if saved is not None else None

    def save_schedule(self, schedule: MeetingSchedule) -> None:
        self.schedules[(schedule.week_monday, schedule.meeting_type.value)] = schedule.to_dict()


class _MediaCountdownAutomationStub(QObject):
    state_changed = Signal()


def _build_stack(
    *,
    feature_flags: TimerRemoteFeatureFlags = DEFAULT_TIMER_REMOTE_FEATURE_FLAGS,
    custom_directory: Path | None = None,
    pacing_window: PacingWindow | None = None,
):
    repo = _InMemoryTimerRepository()
    week = date(2026, 7, 13)
    if pacing_window is not None:
        meeting = build_default_schedule(week, MeetingType.MIDWEEK)
        meeting.pacing_window = pacing_window
        repo.save_schedule(meeting)
    custom_repository = (
        JsonCustomTimerRepository(custom_directory) if custom_directory is not None else None
    )
    session = TimerSession(repo, week, custom_repository=custom_repository)
    engine = TimerEngine()
    engine._timer.stop()  # Deterministic tick control in tests
    engine.set_freeze_seconds(3)
    hand_alert = TimerHandAlertController(ZoomSettingsStore(_MemorySettings()), engine)
    operations = TimerOperations(engine, session)
    bridge = TimerBridge(
        operations=operations,
        output=SimpleNamespace(set_clock_config=lambda _cfg: None),
        monitors=SimpleNamespace(
            model=lambda: [],
            is_visible=lambda: False,
            request_reserve=lambda _idx, _label: {},
            confirm_reserve=lambda _idx: True,
            unreserve=lambda _idx: True,
            set_visible=lambda _vis: None,
        ),
        hand_alert=hand_alert,
        pdf_export=SimpleNamespace(export=lambda *_a, **_kw: None),
        media_countdown_automation=_MediaCountdownAutomationStub(),
    )

    hasher = ScryptPasswordHasher(ScryptParameters(n=2**12, r=8, p=1))
    credentials = RemoteControlCredentialsStore.for_timer(
        ProfileAppSettingsStore(_MemorySettings()),
        password_hasher=hasher,
    )
    credentials.set_credentials("timer.op", "timer secret battery staple")

    adapter = RemoteTimerAdapter(
        RemoteTimerAdapterDependencies(
            active_profile_id="profile-1",
            active_profile_name="Main Auditorium",
            credentials=credentials,
            timer_operations=bridge.operations,
            feature_flags=feature_flags,
        ),
        bridge,
    )
    adapter.set_active(True)
    hand_alert.set_outputs(available=True)
    hand_alert.alert_changed.connect(adapter.set_hand_alert)

    root_assets = application_resource_path("remote_control")
    timer_sessions = InMemorySessionStore(scope=RemoteScope.TIMER)
    timer_module = adapter.build_http_module(
        sessions=timer_sessions,
        rate_limiter=LoginRateLimiter(),
        assets_directory=root_assets / "timer",
        shared_assets_directory=root_assets / "shared",
        setup_provider=lambda: {
            "locale": "en",
            "messages": {},
            "tls": {
                "installationId": "inst-timer-e2e",
                "authoritySha256": "AA:BB:CC",
                "verificationCode": "1234-5678",
                "authorityCertificateUrl": TRUST_CERTIFICATE_ROUTE,
                "authorityNotValidAfter": "2036-01-01T00:00:00+00:00",
            },
        },
        enabled_predicate=lambda: True,
    )
    app = RemoteControlHttpApplication(
        (timer_module,),
        allowed_origin=_ORIGIN,
        installation_id="inst-timer-e2e",
        trust_certificate_der=b"der-bytes",
    )
    return engine, session, bridge, adapter, app


async def _login_headers(client: TestClient) -> dict[str, str]:
    resp = await client.post(
        "/remote/timer/api/auth/login",
        headers={"Host": _HOST, "Origin": _ORIGIN},
        json={"username": "timer.op", "password": "timer secret battery staple"},
    )
    assert resp.status == 200
    body = await resp.json()
    cookie = resp.cookies[TIMER_SESSION_COOKIE_NAME].value
    return {
        "Host": _HOST,
        "Origin": _ORIGIN,
        "X-CSRF-Token": body["csrfToken"],
        "Cookie": f"{TIMER_SESSION_COOKIE_NAME}={cookie}",
    }


@pytest.mark.parametrize(
    ("policy", "public_mode", "auth_mode"),
    [
        (TimerRemoteHandAlertPolicy.PUBLIC_NAMES, "names", "names"),
        (TimerRemoteHandAlertPolicy.PUBLIC_COUNT, "count", "names"),
        (TimerRemoteHandAlertPolicy.AUTHENTICATED_ONLY, None, "names"),
        (TimerRemoteHandAlertPolicy.DISABLED, None, None),
    ],
)
def test_timer_hand_alert_policy_applies_to_bootstrap_and_live_streams(
    policy: TimerRemoteHandAlertPolicy,
    public_mode: str | None,
    auth_mode: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine, _session, bridge, adapter, app = _build_stack(
        feature_flags=TimerRemoteFeatureFlags(hand_alert_policy=policy),
    )
    initial_alert = TimerHandAlert(names=("João", "Ana"))
    reordered = TimerHandAlert(names=("Ana", "João"))

    def expected_alert(alert: TimerHandAlert, mode: str | None) -> dict[str, object]:
        if mode is not None and alert.names:
            payload: dict[str, object] = {"kind": "automatic", "count": len(alert.names)}
            if mode == "names":
                payload["names"] = list(alert.names)
            return {"handAlert": payload}
        if mode is not None and alert.manual:
            return {"handAlert": {"kind": "manual"}}
        return {}

    def alert_fields(live: dict) -> dict[str, object]:
        return {key: value for key, value in live.items() if key == "handAlert"}

    async def scenario() -> None:
        async with TestClient(TestServer(app.app)) as client:
            public_headers = {"Host": _HOST, "Origin": _ORIGIN}
            # Seed the alert while the timer remote is inactive.
            adapter.set_active(False)
            adapter.set_hand_alert(initial_alert)
            adapter.set_active(True)
            schedule_revision = adapter.state.schedule_revision

            login = await client.post(
                "/remote/timer/api/auth/login",
                headers=public_headers,
                json={"username": "timer.op", "password": "timer secret battery staple"},
            )
            assert login.status == 200
            login_payload = await login.json()
            token = login.cookies[TIMER_SESSION_COOKIE_NAME].value
            auth_headers = {
                **public_headers,
                "Cookie": f"{TIMER_SESSION_COOKIE_NAME}={token}",
                "X-CSRF-Token": login_payload["csrfToken"],
            }
            assert alert_fields(login_payload["live"]) == expected_alert(initial_alert, auth_mode)

            async def bootstrap(path: str, headers: dict[str, str]) -> dict:
                response = await client.get(f"/remote/timer/api/{path}", headers=headers)
                assert response.status == 200
                body = await response.json()
                assert body["liveRevision"] == body["live"]["liveRevision"]
                return body

            cookie_public_boot = await bootstrap("public", auth_headers)
            assert cookie_public_boot["authenticated"] is True
            assert alert_fields(cookie_public_boot["live"]) == expected_alert(
                initial_alert, public_mode
            )

            public_ws = await client.ws_connect("/remote/timer/api/ws/public", headers=auth_headers)
            auth_ws = await client.ws_connect("/remote/timer/api/ws", headers=auth_headers)
            streams = (
                (public_ws, "public", public_headers, "public.snapshot", public_mode),
                (auth_ws, "bootstrap", auth_headers, "session.snapshot", auth_mode),
            )
            revisions: dict[str, int] = {}
            for ws, path, headers, message_type, mode in streams:
                boot = await bootstrap(path, headers)
                expected = expected_alert(initial_alert, mode)
                assert alert_fields(boot["live"]) == expected
                event = await ws.receive_json(timeout=1)
                assert event["type"] == message_type
                revisions[path] = boot["liveRevision"]
                assert (
                    event["liveRevision"]
                    == event["payload"]["live"]["liveRevision"]
                    == revisions[path]
                )
                assert event["sequence"] == boot["eventSequence"]
                assert alert_fields(event["payload"]["live"]) == expected

            schedule_builds = 0
            emissions = 0
            build_schedule = adapter._build_schedule_snapshot

            def track_schedule():
                nonlocal schedule_builds
                schedule_builds += 1
                return build_schedule()

            def track_emission() -> None:
                nonlocal emissions
                emissions += 1

            monkeypatch.setattr(adapter, "_build_schedule_snapshot", track_schedule)
            adapter.snapshot_updated.connect(track_emission)

            previous_alert = initial_alert
            for alert in (
                reordered,
                reordered,
                TimerHandAlert(names=("João",)),
                TimerHandAlert(manual=True),
                TimerHandAlert(),
            ):
                if alert.names:
                    adapter.set_hand_alert(alert)
                else:
                    bridge.setManualHand(alert.manual)
                await app.publish_snapshot(RemoteScope.TIMER)
                for ws, path, headers, _, mode in streams:
                    boot = await bootstrap(path, headers)
                    expected = expected_alert(alert, mode)
                    if expected != expected_alert(previous_alert, mode):
                        revisions[path] += 1
                        event = await ws.receive_json(timeout=1)
                        assert event["type"] == "timer.live"
                        assert (
                            event["liveRevision"]
                            == event["payload"]["liveRevision"]
                            == revisions[path]
                        )
                        assert event["sequence"] == boot["eventSequence"]
                        assert alert_fields(event["payload"]) == expected
                    assert boot["liveRevision"] == revisions[path]
                    assert alert_fields(boot["live"]) == expected
                previous_alert = alert
            assert schedule_builds == 0
            assert emissions == (4 if auth_mode else 0)
            assert adapter.state.schedule_revision == schedule_revision

            bridge.startPart(adapter.state.schedule.parts[0].id)
            await app.publish_snapshot(RemoteScope.TIMER)
            for ws, path, *_ in streams:
                event = await ws.receive_json(timeout=1)
                assert event["type"] == "timer.live"
                assert (
                    event["liveRevision"] == event["payload"]["liveRevision"] == revisions[path] + 1
                )
                assert event["payload"]["active"] is True
                assert "handAlert" not in event["payload"]

    asyncio.run(scenario())


@pytest.mark.parametrize("allow_pacing_edits", (False, True))
def test_steady_state_timer_ticks_emit_zero_websocket_broadcasts(allow_pacing_edits: bool) -> None:
    start = int(time.time())
    engine, _session, _bridge, adapter, app = _build_stack(
        feature_flags=TimerRemoteFeatureFlags(allow_pacing=allow_pacing_edits),
        pacing_window=PacingWindow(start, start + 6300),
    )
    snapshot_emissions = 0

    def on_snapshot() -> None:
        nonlocal snapshot_emissions
        snapshot_emissions += 1

    adapter.snapshot_updated.connect(on_snapshot)

    async def scenario() -> None:
        client = TestClient(TestServer(app.app))
        await client.start_server()
        try:
            public_ws = await client.ws_connect(
                "/remote/timer/api/ws/public",
                headers={"Host": _HOST, "Origin": _ORIGIN},
            )
            initial = await public_ws.receive_json()
            assert initial["type"] == "public.snapshot"
            assert "pacing" not in initial["payload"]

            headers = await _login_headers(client)
            response = await client.get("/remote/timer/api/schedule", headers=headers)
            schedule = await response.json()
            assert schedule["pacing"]["end_epoch"] == start + 6300
            assert schedule["capabilities"]["managePacing"] is allow_pacing_edits
            if not allow_pacing_edits:
                edit = await client.post(
                    "/remote/timer/api/commands",
                    headers=headers,
                    json={
                        "commandId": str(uuid.uuid4()),
                        "type": "set_meeting_pacing",
                        "scheduleRevision": schedule["scheduleRevision"],
                        "automatic": True,
                    },
                )
                assert (await edit.json())["error"]["code"] == "blocked"

            post_start = await client.post(
                "/remote/timer/api/commands",
                headers=headers,
                json={
                    "commandId": str(uuid.uuid4()),
                    "type": "start_part",
                    "scheduleRevision": adapter.state.schedule_revision,
                    "partId": adapter.state.schedule.parts[0].id,
                },
            )
            assert (await post_start.json())["ok"] is True

            started_event = await public_ws.receive_json()
            assert started_event["type"] == "timer.live"
            assert started_event["payload"]["active"] is True
            assert "pacing" not in started_event["payload"]

            operator_ws = await client.ws_connect("/remote/timer/api/ws", headers=headers)
            initial = await operator_ws.receive_json()
            schedule = initial["payload"]["schedule"]
            assert schedule["pacing"]["end_epoch"] == start + 6300
            assert schedule["capabilities"]["managePacing"] is allow_pacing_edits
            emissions_after_start = snapshot_emissions
            for _ in range(25):
                engine._on_tick()

            assert snapshot_emissions == emissions_after_start
            await app.publish_snapshot(RemoteScope.TIMER)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(public_ws.receive_json(), timeout=0.05)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(operator_ws.receive_json(), timeout=0.05)

            await operator_ws.close()
            await public_ws.close()
        finally:
            await client.close()

    try:
        asyncio.run(scenario())
    finally:
        adapter.stop()
        engine._revert_timer.stop()


def test_end_to_end_timer_part_and_schedule_commands_sync_public_and_controller_views() -> None:
    engine, _session, _bridge, adapter, app = _build_stack()

    async def scenario() -> None:
        client = TestClient(TestServer(app.app))
        await client.start_server()
        try:
            public_ws = await client.ws_connect(
                "/remote/timer/api/ws/public",
                headers={"Host": _HOST, "Origin": _ORIGIN},
            )
            public_baseline = await public_ws.receive_json()
            assert public_baseline["sequence"] == 0
            assert "scheduleRevision" not in public_baseline

            headers = await _login_headers(client)
            boot = await (await client.get("/remote/timer/api/bootstrap", headers=headers)).json()
            rev = boot["scheduleRevision"]
            first_part_id = boot["schedule"]["parts"][0]["id"]
            second_part_id = boot["schedule"]["parts"][1]["id"]

            async def send(payload: dict[str, object]) -> dict:
                res = await client.post(
                    "/remote/timer/api/commands",
                    headers=headers,
                    json={"commandId": str(uuid.uuid4()), **payload},
                )
                assert res.status == 200
                data = await res.json()
                assert data["ok"] is True, data
                return data

            # 1. Adjust idle part planned duration (+1 min)
            r1 = await send(
                {
                    "type": "adjust_part",
                    "scheduleRevision": rev,
                    "partId": second_part_id,
                    "deltaSeconds": 60,
                }
            )
            rev = r1["scheduleRevision"]

            # 2. Direct planned duration edit (set_part_seconds)
            r2 = await send(
                {
                    "type": "set_part_seconds",
                    "scheduleRevision": rev,
                    "partId": second_part_id,
                    "plannedSeconds": 420,
                }
            )
            rev = r2["scheduleRevision"]
            sched = await (await client.get("/remote/timer/api/schedule", headers=headers)).json()
            assert (
                next(p for p in sched["parts"] if p["id"] == second_part_id)["plannedSeconds"]
                == 420
            )

            # 3. The last remaining Ministry part is still independently editable.
            r3_single = await send(
                {
                    "type": "set_section_count",
                    "scheduleRevision": rev,
                    "section": "ministry",
                    "count": 1,
                }
            )
            rev = r3_single["scheduleRevision"]
            sched_single = await (
                await client.get("/remote/timer/api/schedule", headers=headers)
            ).json()
            ministry_single = [p for p in sched_single["parts"] if p["section"] == "ministry"]
            assert len(ministry_single) == 1
            assert ministry_single[0]["durationEditable"] is True
            single_id = ministry_single[0]["id"]

            adjusted = await send({
                "type": "adjust_part",
                "scheduleRevision": rev,
                "partId": single_id,
                "deltaSeconds": 60,
            })
            updated = await send({
                "type": "set_part_seconds",
                "scheduleRevision": adjusted["scheduleRevision"],
                "partId": single_id,
                "plannedSeconds": 600,
            })
            rev = updated["scheduleRevision"]

            r3 = await send(
                {
                    "type": "set_section_count",
                    "scheduleRevision": rev,
                    "section": "ministry",
                    "count": 4,
                }
            )
            rev = r3["scheduleRevision"]
            sched = await (await client.get("/remote/timer/api/schedule", headers=headers)).json()
            ministry_parts = [p for p in sched["parts"] if p["section"] == "ministry"]
            assert len(ministry_parts) == 4
            assert all(p["durationEditable"] is True for p in ministry_parts)
            assert ministry_parts[0]["id"] == single_id
            assert ministry_parts[0]["plannedSeconds"] == 600

            # 4. Start part, nudge +1s / -1s while running, stop, reset
            r4 = await send(
                {
                    "type": "start_part",
                    "scheduleRevision": rev,
                    "partId": first_part_id,
                }
            )
            rev = r4["scheduleRevision"]
            live_msg = await public_ws.receive_json()
            assert live_msg["type"] == "timer.live"
            assert live_msg["sequence"] == 1
            assert "scheduleRevision" not in live_msg
            assert live_msg["payload"]["activePartId"] == first_part_id
            sched_running = await (
                await client.get("/remote/timer/api/schedule", headers=headers)
            ).json()
            assert (
                next(p for p in sched_running["parts"] if p["id"] == first_part_id)[
                    "durationEditable"
                ]
                is False
            )

            r5 = await send(
                {
                    "type": "adjust_part",
                    "scheduleRevision": rev,
                    "partId": first_part_id,
                    "deltaSeconds": 1,
                }
            )
            rev = r5["scheduleRevision"]
            nudged_msg = await public_ws.receive_json()
            assert nudged_msg["sequence"] == 2
            assert "scheduleRevision" not in nudged_msg
            assert nudged_msg["payload"]["elapsedSeconds"] >= 1.0

            r6 = await send({"type": "stop_part", "partId": first_part_id})
            rev = r6["scheduleRevision"]
            stopped_msg = await public_ws.receive_json()
            assert stopped_msg["sequence"] == 3
            assert "scheduleRevision" not in stopped_msg
            assert stopped_msg["payload"]["state"] in {"stopped", "idle"}
            sched_stopped = await (
                await client.get("/remote/timer/api/schedule", headers=headers)
            ).json()
            assert (
                next(p for p in sched_stopped["parts"] if p["id"] == first_part_id)[
                    "durationEditable"
                ]
                is False
            )

            r7 = await send(
                {
                    "type": "reset_part",
                    "scheduleRevision": rev,
                    "partId": first_part_id,
                }
            )
            rev = r7["scheduleRevision"]
            sched_reset = await (
                await client.get("/remote/timer/api/schedule", headers=headers)
            ).json()
            assert (
                next(p for p in sched_reset["parts"] if p["id"] == first_part_id)[
                    "durationEditable"
                ]
                is True
            )

            # 5. Switch meeting type to Weekend and navigate weeks
            await send({"type": "set_meeting_type", "meetingType": "weekend"})
            sched_wknd = await (
                await client.get("/remote/timer/api/schedule", headers=headers)
            ).json()
            assert sched_wknd["meetingType"] == "weekend"

            await send({"type": "navigate_week", "target": "next"})
            sched_next = await (
                await client.get("/remote/timer/api/schedule", headers=headers)
            ).json()
            assert sched_next["weekMonday"] == "2026-07-20"
            assert sched_next["isCurrentWeek"] is False

            await public_ws.close()
        finally:
            await client.close()

    try:
        asyncio.run(scenario())
    finally:
        adapter.stop()
        engine._revert_timer.stop()


def test_structure_edits_feature_flag_blocks_structural_mutations_end_to_end() -> None:
    engine, _session, _bridge, adapter, app = _build_stack(
        feature_flags=TimerRemoteFeatureFlags(allow_schedule_structure_edits=False)
    )

    async def scenario() -> None:
        client = TestClient(TestServer(app.app))
        await client.start_server()
        try:
            headers = await _login_headers(client)
            boot = await (await client.get("/remote/timer/api/bootstrap", headers=headers)).json()
            caps = boot["schedule"]["capabilities"]
            assert caps["controlParts"] is True
            assert caps["adjustPlannedMinutes"] is True
            assert caps["switchMeetingType"] is True
            assert caps["setPlannedSeconds"] is False
            assert caps["setSectionCount"] is False
            assert caps["navigateWeeks"] is False

            blocked = await client.post(
                "/remote/timer/api/commands",
                headers=headers,
                json={
                    "commandId": str(uuid.uuid4()),
                    "type": "navigate_week",
                    "target": "next",
                },
            )
            data = await blocked.json()
            assert data["ok"] is False
            assert data["error"]["code"] == "blocked"
        finally:
            await client.close()

    try:
        asyncio.run(scenario())
    finally:
        adapter.stop()
        engine._revert_timer.stop()


@pytest.mark.parametrize("pacing_enabled", (False, True))
def test_cross_thread_command_to_websocket_snapshot_p95_latency_stays_under_budget(pacing_enabled: bool) -> None:
    start = int(time.time())
    engine, _session, _bridge, adapter, app = _build_stack(
        feature_flags=TimerRemoteFeatureFlags(allow_pacing=pacing_enabled),
        pacing_window=PacingWindow(start, start + 6300) if pacing_enabled else None,
    )
    if pacing_enabled:
        assert _bridge.operations.set_meeting_pacing(True)
    adapter.snapshot_updated.connect(lambda: None)

    stop_pumping = threading.Event()
    latencies_ms: list[float] = []
    worker_error: list[BaseException] = []

    def worker() -> None:
        async def run_benchmark() -> None:
            client = TestClient(TestServer(app.app))
            await client.start_server()
            try:
                headers = await _login_headers(client)
                ws = await client.ws_connect("/remote/timer/api/ws", headers=headers)
                initial = await ws.receive_json()
                rev = initial["scheduleRevision"]
                part_id = initial["payload"]["schedule"]["parts"][0]["id"]

                start_res = await client.post(
                    "/remote/timer/api/commands",
                    headers=headers,
                    json={
                        "commandId": str(uuid.uuid4()),
                        "type": "start_part",
                        "scheduleRevision": rev,
                        "partId": part_id,
                    },
                )
                rev = (await start_res.json())["scheduleRevision"]
                while True:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
                    if msg["type"] == "timer.live" and msg["payload"]["active"]:
                        break

                # Measure 50 sequential running +/-1s nudges across threads
                for index in range(50):
                    delta = 1 if index % 2 == 0 else -1
                    t0 = time.perf_counter()
                    resp = await client.post(
                        "/remote/timer/api/commands",
                        headers=headers,
                        json={
                            "commandId": str(uuid.uuid4()),
                            "type": "adjust_part",
                            "scheduleRevision": rev,
                            "partId": part_id,
                            "deltaSeconds": delta,
                        },
                    )
                    body = await resp.json()
                    assert body["ok"] is True
                    rev = body["scheduleRevision"]
                    target_live_rev = body["liveRevision"]
                    while True:
                        event = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
                        if (
                            event["type"] == "timer.live"
                            and event["liveRevision"] >= target_live_rev
                        ):
                            break
                    latencies_ms.append((time.perf_counter() - t0) * 1000.0)

                await ws.close()
            finally:
                await client.close()

        try:
            asyncio.run(run_benchmark())
        except BaseException as exc:  # noqa: BLE001
            worker_error.append(exc)
        finally:
            stop_pumping.set()

    thread = threading.Thread(target=worker, name="timer-e2e-benchmark")
    thread.start()
    try:
        deadline = time.monotonic() + 15.0
        while not stop_pumping.is_set():
            _APP.processEvents()
            time.sleep(0.0005)
            assert time.monotonic() < deadline, "Cross-thread timer benchmark timed out"
        thread.join(timeout=2.0)
    finally:
        adapter.stop()
        engine._revert_timer.stop()

    if worker_error:
        raise worker_error[0]

    assert len(latencies_ms) == 50
    sorted_latencies = sorted(latencies_ms)
    p50 = statistics.median(sorted_latencies)
    p95 = sorted_latencies[int(len(sorted_latencies) * 0.95) - 1]
    assert p50 < 25.0, f"P50 latency too high: {p50:.2f} ms"
    assert p95 < 50.0, f"P95 latency too high: {p95:.2f} ms"


def test_abort_pending_commands_unblocks_server_waiter_without_pumping_qt() -> None:
    from PySide6.QtCore import QObject
    from solin.controllers.qt_command_bridge import QtCommandBridge

    owner = QObject()
    dispatched: list[str] = []
    bridge: QtCommandBridge[str, str] = QtCommandBridge(
        lambda cmd: dispatched.append(cmd) or None,
        lambda: "server_stopping",
        owner,
    )

    entered = threading.Event()
    outcomes: list[str | None] = []

    def server_worker() -> None:
        async def run() -> None:
            task = asyncio.create_task(bridge.execute("start_part"))
            await asyncio.sleep(0)
            entered.set()
            outcomes.append(await asyncio.wait_for(task, timeout=1.0))

        asyncio.run(run())

    worker = threading.Thread(target=server_worker, name="qt-bridge-abort-test")
    worker.start()
    assert entered.wait(2.0)
    # Main thread aborts pending commands without pumping Qt events; waiter resolves immediately.
    t0 = time.perf_counter()
    bridge.abort_pending()
    worker.join(timeout=2.0)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert not worker.is_alive()
    assert elapsed_ms < 250.0
    assert outcomes == ["server_stopping"]
    # Late queued Qt slot delivery after loop closure is a safe no-op.
    _APP.processEvents()
    assert dispatched == []


def test_custom_schedule_remote_lifecycle_and_privacy(tmp_path: Path) -> None:
    engine, _session, _bridge, adapter, app = _build_stack(
        feature_flags=TimerRemoteFeatureFlags(allow_custom_schedules=True),
        custom_directory=tmp_path / "custom-timers",
    )

    async def scenario() -> None:
        async with TestClient(TestServer(app.app)) as client:
            public_headers = {"Host": _HOST, "Origin": _ORIGIN}
            denied = await client.get("/remote/timer/api/custom-schedules", headers=public_headers)
            assert denied.status in (401, 403)
            headers = await _login_headers(client)
            boot = await (await client.get("/remote/timer/api/bootstrap", headers=headers)).json()
            assert boot["schedule"]["capabilities"]["manageCustomSchedules"] is True
            revision = boot["scheduleRevision"]

            async def send(payload: dict[str, object], *, command_id: str | None = None) -> dict:
                response = await client.post(
                    "/remote/timer/api/commands",
                    headers=headers,
                    json={"commandId": command_id or str(uuid.uuid4()), **payload},
                )
                assert response.status == 200
                return await response.json()

            async def catalog() -> list[dict]:
                response = await client.get("/remote/timer/api/custom-schedules", headers=headers)
                assert response.status == 200
                return (await response.json())["schedules"]

            original_part = boot["schedule"]["parts"][0]
            renamed = await send(
                {
                    "type": "rename_part",
                    "scheduleRevision": revision,
                    "partId": original_part["id"],
                    "title": "Opening rehearsal",
                }
            )
            assert renamed["ok"]
            weekly = await (await client.get("/remote/timer/api/schedule", headers=headers)).json()
            assert weekly["parts"][0]["id"] == original_part["id"]
            assert weekly["parts"][0]["section"] == original_part["section"]
            assert weekly["parts"][0]["title"] == "Opening rehearsal"
            restored = await send(
                {
                    "type": "rename_part",
                    "scheduleRevision": renamed["scheduleRevision"],
                    "partId": original_part["id"],
                    "title": None,
                }
            )
            assert restored["ok"]
            create = {
                "type": "create_custom_schedule",
                "scheduleRevision": restored["scheduleRevision"],
                "title": "Training",
            }
            command_id = str(uuid.uuid4())
            created = await send(create, command_id=command_id)
            assert created["ok"]
            assert await send(create, command_id=command_id) == created
            schedules = await catalog()
            assert len(schedules) == 1
            schedule = schedules[0]
            schedule_id = created["createdScheduleId"]
            assert schedule["id"] == schedule_id

            async def edit(action: str, values: dict, custom_revision: int) -> dict:
                return await send(
                    {
                        "type": "edit_custom_schedule",
                        "scheduleRevision": adapter.state.schedule_revision,
                        "scheduleId": schedule_id,
                        "customRevision": custom_revision,
                        "action": action,
                        "values": values,
                    }
                )

            assert (await edit("add_section", {"title": "Warm-up"}, schedule["revision"]))["ok"]
            schedule = (await catalog())[0]
            section_id = schedule["entries"][0]["id"]
            assert (
                await edit(
                    "add_part",
                    {
                        "title": "Stretch",
                        "planned_seconds": 20,
                        "section_id": section_id,
                    },
                    schedule["revision"],
                )
            )["ok"]
            stale = await edit(
                "add_part", {"title": "Stale", "planned_seconds": 30}, schedule["revision"]
            )
            assert stale["error"]["code"] == "schedule_stale"
            schedule = (await catalog())[0]
            selected = await send(
                {
                    "type": "select_custom_schedule",
                    "scheduleRevision": adapter.state.schedule_revision,
                    "scheduleId": schedule_id,
                    "customRevision": schedule["revision"],
                }
            )
            assert selected["ok"]
            current = await (await client.get("/remote/timer/api/schedule", headers=headers)).json()
            assert current["customSchedule"]["id"] == schedule_id
            assert current["capabilities"]["setSectionCount"] is False
            assert current["capabilities"]["switchMeetingType"] is False
            assert current["capabilities"]["navigateWeeks"] is False
            part_id = current["parts"][0]["id"]
            assert current["parts"][0]["plannedSeconds"] == 20
            started = await send(
                {
                    "type": "start_part",
                    "scheduleRevision": current["scheduleRevision"],
                    "partId": part_id,
                }
            )
            assert started["ok"]
            current = await (await client.get("/remote/timer/api/schedule", headers=headers)).json()
            running_revision = current["scheduleRevision"]
            running_part = current["customSchedule"]["entries"][0]["parts"][0]
            assert running_part["startedAtEpoch"] is not None
            adapter.refresh_all()
            assert adapter.state.schedule_revision == running_revision
            blocked = await edit(
                "move_part",
                {"part_id": part_id, "section_id": None},
                current["customSchedule"]["revision"],
            )
            assert blocked["error"]["code"] == "blocked"
            public = await (
                await client.get("/remote/timer/api/public", headers=public_headers)
            ).json()
            assert "schedule" not in public and "customSchedule" not in str(public)
            assert (await send({"type": "stop_part", "partId": part_id}))["ok"]

            current = await (await client.get("/remote/timer/api/schedule", headers=headers)).json()
            copied = await send(
                {
                    "type": "duplicate_custom_schedule",
                    "scheduleRevision": current["scheduleRevision"],
                    "scheduleId": schedule_id,
                    "customRevision": current["customSchedule"]["revision"],
                    "title": "Second group",
                }
            )
            assert copied["ok"]
            copy = next(item for item in await catalog() if item["id"] == copied["createdScheduleId"])
            assert copy["id"] != schedule_id
            assert copy["entries"][0]["parts"][0]["state"] == "idle"
            assert copy["entries"][0]["parts"][0]["elapsedSeconds"] == 0

    try:
        asyncio.run(scenario())
    finally:
        adapter.stop()
        engine._revert_timer.stop()


def test_pacing_is_authenticated_and_anchor_does_not_churn(monkeypatch) -> None:
    calls = []
    edits = []

    def pacing_snapshot(_operations):
        return PacingSnapshot(100, 200, time.time(), time.time(), PacingStatus.BEHIND, False, True, 150, 50, 0, 0)

    monkeypatch.setattr(TimerOperations, "pacing_snapshot", pacing_snapshot, raising=False)
    monkeypatch.setattr(
        TimerOperations, "set_meeting_pacing",
        lambda _operations, automatic: calls.append(automatic) or True,
        raising=False,
    )
    def edit_schedule(_operations, schedule_id, revision, action, **values):
        edits.append((schedule_id, revision, action, values))
        if action == "set_pacing":
            raise TimerDurationBudgetError(100, 120)

    monkeypatch.setattr(TimerOperations, "edit_custom_schedule", edit_schedule)
    engine, _session, _bridge, adapter, app = _build_stack(
        feature_flags=TimerRemoteFeatureFlags(allow_pacing=True, allow_custom_schedules=True)
    )

    async def scenario() -> None:
        async with TestClient(TestServer(app.app)) as client:
            public_headers = {"Host": _HOST, "Origin": _ORIGIN}
            assert (await client.get("/remote/timer/api/schedule", headers=public_headers)).status in (401, 403)
            public = await (await client.get("/remote/timer/api/public", headers=public_headers)).json()
            assert "pacing" not in str(public)
            headers = await _login_headers(client)
            schedule = await (await client.get("/remote/timer/api/schedule", headers=headers)).json()
            assert schedule["pacing"]["projection_anchor_epoch"] == 150.0
            assert "projected_end_epoch" not in schedule["pacing"]
            revision = adapter.state.schedule_revision
            adapter.refresh_all()
            assert adapter.state.schedule_revision == revision
            command = {
                "commandId": str(uuid.uuid4()),
                "type": "set_meeting_pacing",
                "scheduleRevision": revision,
                "automatic": True,
            }
            first = await (await client.post("/remote/timer/api/commands", headers=headers, json=command)).json()
            second = await (await client.post("/remote/timer/api/commands", headers=headers, json=command)).json()
            assert first["ok"] and second == first
            assert calls == [True]
            edit = {
                "commandId": str(uuid.uuid4()),
                "type": "edit_custom_schedule",
                "scheduleRevision": revision,
                "scheduleId": "schedule",
                "customRevision": 2,
                "action": "set_adjustment_weight",
                "values": {"part_id": "part", "adjustment_weight": 100},
            }
            result = await (await client.post("/remote/timer/api/commands", headers=headers, json=edit)).json()
            assert result["ok"]
            assert edits[-1] == ("schedule", 2, "set_adjustment_weight", {"part_id": "part", "adjustment_weight": 100})
            edit.update({
                "commandId": str(uuid.uuid4()),
                "action": "set_pacing",
                "values": {
                    "start_epoch": 100, "end_epoch": 200, "automatic": True,
                    "untracked_seconds": 30,
                },
            })
            result = await (await client.post("/remote/timer/api/commands", headers=headers, json=edit)).json()
            assert result["error"]["code"] == "duration_budget"
            assert result["error"]["availableSeconds"] == 100
            assert result["error"]["requestedSeconds"] == 120
            assert edits[-1][3]["untracked_seconds"] == 30

    try:
        asyncio.run(scenario())
    finally:
        adapter.stop()
        engine._revert_timer.stop()


def test_custom_pacing_round_trips_through_authenticated_remote(tmp_path: Path) -> None:
    engine, _session, bridge, adapter, app = _build_stack(
        feature_flags=TimerRemoteFeatureFlags(allow_pacing=True, allow_custom_schedules=True),
        custom_directory=tmp_path / "timers",
    )
    schedule = bridge.operations.create_custom_schedule("Training")
    bridge.operations.edit_custom_schedule(schedule.id, schedule.revision, "add_part", title="Part", planned_seconds=60)
    bridge.operations.select_custom_schedule(schedule.id)

    async def scenario() -> None:
        async with TestClient(TestServer(app.app)) as client:
            headers = await _login_headers(client)
            catalog = await (await client.get("/remote/timer/api/custom-schedules", headers=headers)).json()
            current = catalog["schedules"][0]
            start = int(time.time()) + 60
            command = {
                "commandId": str(uuid.uuid4()),
                "type": "edit_custom_schedule",
                "scheduleRevision": adapter.state.schedule_revision,
                "scheduleId": schedule.id,
                "customRevision": current["revision"],
                "action": "set_pacing",
                "values": {
                    "start_epoch": start, "end_epoch": start + 180,
                    "automatic": True, "untracked_seconds": 30,
                },
            }
            result = await (await client.post("/remote/timer/api/commands", headers=headers, json=command)).json()
            assert result["ok"]
            catalog = await (await client.get("/remote/timer/api/custom-schedules", headers=headers)).json()
            config = catalog["schedules"][0]["pacingConfig"]
            assert config == command["values"]
            part_id = catalog["schedules"][0]["entries"][0]["id"]
            for before_id, seconds in ((part_id, 45), (None, 20)):
                current = catalog["schedules"][0]
                transition_edit = {
                    "commandId": str(uuid.uuid4()),
                    "type": "edit_custom_schedule",
                    "scheduleRevision": adapter.state.schedule_revision,
                    "scheduleId": schedule.id,
                    "customRevision": current["revision"],
                    "action": "set_pacing_transition",
                    "values": {"before_part_id": before_id, "estimated_seconds": seconds},
                }
                result = await (await client.post(
                    "/remote/timer/api/commands", headers=headers, json=transition_edit
                )).json()
                assert result["ok"]
                replay = await (await client.post(
                    "/remote/timer/api/commands", headers=headers, json=transition_edit
                )).json()
                assert replay == result
                catalog = await (await client.get(
                    "/remote/timer/api/custom-schedules", headers=headers
                )).json()
            assert catalog["schedules"][0]["pacingConfig"]["transitions"] == {
                "before_parts": {part_id: 45}, "closing_seconds": 20,
            }
            transition_edit.update({
                "commandId": str(uuid.uuid4()),
                "scheduleRevision": adapter.state.schedule_revision,
                "customRevision": catalog["schedules"][0]["revision"],
                "values": {"before_part_id": part_id, "estimated_seconds": None},
            })
            assert (await (await client.post(
                "/remote/timer/api/commands", headers=headers, json=transition_edit
            )).json())["ok"]
            catalog = await (await client.get(
                "/remote/timer/api/custom-schedules", headers=headers
            )).json()
            assert catalog["schedules"][0]["pacingConfig"]["transitions"] == {
                "before_parts": {}, "closing_seconds": 20,
            }
            published = await (await client.get("/remote/timer/api/schedule", headers=headers)).json()
            assert published["pacing"]["end_epoch"] == start + 180
            revision = adapter.state.schedule_revision
            adapter.refresh_all()
            assert adapter.state.schedule_revision == revision

    try:
        asyncio.run(scenario())
    finally:
        adapter.stop()
        engine._revert_timer.stop()


def test_custom_schedule_feature_is_disabled_by_default(tmp_path: Path) -> None:
    engine, _session, _bridge, adapter, app = _build_stack(
        custom_directory=tmp_path / "custom-timers"
    )

    async def scenario() -> None:
        async with TestClient(TestServer(app.app)) as client:
            headers = await _login_headers(client)
            boot = await (await client.get("/remote/timer/api/bootstrap", headers=headers)).json()
            assert boot["schedule"]["capabilities"]["manageCustomSchedules"] is False
            assert (
                await client.get("/remote/timer/api/custom-schedules", headers=headers)
            ).status == 404
            response = await client.post(
                "/remote/timer/api/commands",
                headers=headers,
                json={
                    "commandId": str(uuid.uuid4()),
                    "type": "create_custom_schedule",
                    "scheduleRevision": boot["scheduleRevision"],
                    "title": "Training",
                },
            )
            assert (await response.json())["error"]["code"] == "blocked"

    try:
        asyncio.run(scenario())
    finally:
        adapter.stop()
        engine._revert_timer.stop()


def test_catalog_read_failure_does_not_interrupt_live_timer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _session, bridge, adapter, app = _build_stack(
        feature_flags=TimerRemoteFeatureFlags(allow_custom_schedules=True),
        custom_directory=tmp_path / "custom-timers",
    )

    def fail_catalog() -> None:
        raise OSError("catalog unavailable")

    monkeypatch.setattr(bridge.operations, "list_custom_schedules", fail_catalog)

    async def scenario() -> None:
        async with TestClient(TestServer(app.app)) as client:
            headers = await _login_headers(client)
            boot = await (await client.get("/remote/timer/api/bootstrap", headers=headers)).json()
            response = await client.post(
                "/remote/timer/api/commands",
                headers=headers,
                json={
                    "commandId": str(uuid.uuid4()),
                    "type": "start_part",
                    "scheduleRevision": boot["scheduleRevision"],
                    "partId": boot["schedule"]["parts"][0]["id"],
                },
            )
            assert (await response.json())["ok"]
            assert adapter.state.live.active
            catalog = await client.get("/remote/timer/api/custom-schedules", headers=headers)
            assert catalog.status == 503 or (await catalog.json())["error"]["code"] == "failed"
            assert adapter.state.live.active

    try:
        asyncio.run(scenario())
    finally:
        adapter.stop()
        engine._revert_timer.stop()


def test_disabled_custom_feature_blocks_active_custom_controls(tmp_path: Path) -> None:
    engine, _session, bridge, adapter, app = _build_stack(custom_directory=tmp_path / "custom-timers")
    operations = bridge.operations
    schedule = operations.create_custom_schedule("Private session")
    operations.edit_custom_schedule(schedule.id, schedule.revision, "add_part", title="Exercise", planned_seconds=20)
    operations.select_custom_schedule(schedule.id)

    async def scenario() -> None:
        async with TestClient(TestServer(app.app)) as client:
            headers = await _login_headers(client)
            boot = await (await client.get("/remote/timer/api/bootstrap", headers=headers)).json()
            assert "customSchedule" not in boot["schedule"]
            assert not boot["schedule"]["capabilities"]["controlParts"]
            part_id = boot["schedule"]["parts"][0]["id"]
            response = await client.post("/remote/timer/api/commands", headers=headers, json={
                "commandId": str(uuid.uuid4()), "type": "start_part",
                "scheduleRevision": boot["scheduleRevision"], "partId": part_id,
            })
            assert (await response.json())["error"]["code"] == "blocked"
            assert not engine.snapshot().active

    try:
        asyncio.run(scenario())
    finally:
        adapter.stop()
        engine._revert_timer.stop()
