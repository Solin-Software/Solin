"""HostedMeetingService: one hosted meeting from start to end, for the Qt UI.

Start (on a worker thread, so the UI never blocks):

1. Galene (with the hub's TURN when a hub is configured) and a fresh group for this
   meeting (random slug, per-meeting HS256 key);
2. the meeting runtime: web server + operator client + rules;
3. the hub tunnel (frpc), giving the hall its public HTTPS address — optional: without
   a hub the meeting is reachable on the LAN only;
4. the commenter-audio player (``solin-galene-bridge receive`` → remote-audio device);
5. the program output in the libobs sidecar (``start_meeting_output``), on the Qt thread.

End reverses the order. Operator actions are forwarded to the runtime's meeting rules.
"""

from __future__ import annotations

import logging
import re
import secrets
import socket
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .binaries import Helper, find_helper
from .galene_config import TurnServer
from .galene_server import GaleneServer, GaleneUnavailable
from .processes import ProcessState, SupervisedProcess
from .runtime import MeetingRuntime, MeetingRuntimeConfig
from .settings import HostedMeetingSettingsStore, HubConnection, MeetingQuality
from .tokens import OBSERVE, PRESENT, new_group_key, sign_token
from .tunnel import FrpTunnel, TunnelState

log = logging.getLogger(__name__)

PROGRAM_USERNAME = "Solin program"
HALL_LISTENER_USERNAME = "Solin hall"
_LONG_TOKEN_TTL = 24 * 3600


class MeetingState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    LIVE = "live"
    ENDING = "ending"
    FAILED = "failed"


@dataclass(slots=True)
class MeetingInfo:
    state: MeetingState = MeetingState.IDLE
    slug: str = ""
    invitation_url: str = ""
    lan_url: str = ""
    stage_url: str = ""
    output: str = "idle"           # program output: idle/starting/live/reconnecting/failed
    tunnel: str = "off"            # off/connecting/online/failed
    galene: str = "stopped"
    commenter_audio: str = "stopped"
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value, "slug": self.slug, "invitationUrl": self.invitation_url,
            "lanUrl": self.lan_url, "stageUrl": self.stage_url, "output": self.output,
            "tunnel": self.tunnel, "galene": self.galene, "commenterAudio": self.commenter_audio,
            "error": self.error, "warnings": list(self.warnings),
        }


def meeting_slug(hall_name: str) -> str:
    base = unicodedata.normalize("NFKD", hall_name or "meeting").encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:24] or "meeting"
    return f"{base}-{secrets.token_hex(4)}"


def lan_address() -> str:
    """The LAN IPv4 address other devices on the hall network can reach."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no traffic is sent (TEST-NET-1)
            return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"


class HostedMeetingService(QObject):
    info_changed = Signal(object)       # dict (MeetingInfo.as_dict)
    snapshot_changed = Signal(object)   # dict (MeetingCore.snapshot)
    _call_on_main = Signal(object)

    def __init__(
        self,
        settings: HostedMeetingSettingsStore,
        data_dir: Path,
        *,
        scene_runtime: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._data_dir = Path(data_dir) / "hosted_meeting"
        self._scene_runtime = scene_runtime
        self._info = MeetingInfo()
        self._lock = threading.RLock()
        self._galene: GaleneServer | None = None
        self._runtime: MeetingRuntime | None = None
        self._tunnel: FrpTunnel | None = None
        self._receiver: SupervisedProcess | None = None
        self._group_key = b""
        self._snapshot: dict[str, Any] = {}
        self._worker: threading.Thread | None = None
        self._unsubscribe_engine: Callable[[], None] | None = None
        self._call_on_main.connect(self._run_on_main, Qt.ConnectionType.QueuedConnection)

    # ── state ─────────────────────────────────────────────────────────────

    @property
    def info(self) -> dict[str, Any]:
        with self._lock:
            return self._info.as_dict()

    @property
    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot)

    @property
    def state(self) -> MeetingState:
        return self._info.state

    @staticmethod
    def available() -> bool:
        return find_helper(Helper.GALENE) is not None

    def _update(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self._info, key, value)
            info = self._info.as_dict()
        self.info_changed.emit(info)

    def _warn(self, message: str) -> None:
        with self._lock:
            warnings = [*self._info.warnings, message]
        self._update(warnings=warnings)

    @Slot(object)
    def _run_on_main(self, fn: Callable[[], None]) -> None:
        fn()

    def _main(self, fn: Callable[[], None]) -> None:
        self._call_on_main.emit(fn)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start_meeting(self) -> bool:
        with self._lock:
            if self._info.state in (MeetingState.STARTING, MeetingState.LIVE, MeetingState.ENDING):
                return False
            self._info = MeetingInfo(state=MeetingState.STARTING)
        self.info_changed.emit(self.info)
        self._worker = threading.Thread(target=self._start_worker, name="solin-meeting-start", daemon=True)
        self._worker.start()
        return True

    def _start_worker(self) -> None:
        try:
            self._start_services()
        except Exception as error:  # noqa: BLE001 - reported to the operator
            log.exception("could not start the hosted meeting")
            self._stop_services()
            self._update(state=MeetingState.FAILED, error=str(error))

    def _start_services(self) -> None:
        settings = self._settings
        hub = settings.hub()
        turn = TurnServer(urls=hub.turn_urls, secret=hub.turn_secret) if hub and hub.turn_urls else None
        self._galene = GaleneServer(self._data_dir / "galene", on_state=self._on_galene_state)
        try:
            self._galene.start(turn=turn)
        except GaleneUnavailable as error:
            raise RuntimeError(str(error)) from error
        title = settings.hall_name() or "Meeting"
        slug = meeting_slug(title)
        self._group_key = new_group_key()
        self._galene.layout.clear_groups()
        self._galene.layout.write_group(slug, self._group_key, display_name=title)

        port = settings.web_port()
        self._runtime = MeetingRuntime(
            MeetingRuntimeConfig(slug=slug, title=title, group_key=self._group_key,
                                 galene_port=self._galene.port, web_port=port),
            on_snapshot=self._on_snapshot,
        )
        self._runtime.start()
        assert self._runtime.core is not None
        stage_key = self._runtime.core.stage_key
        lan = f"http://{lan_address()}:{port}"
        self._update(slug=slug, lan_url=f"{lan}/j/{slug}", invitation_url=f"{lan}/j/{slug}",
                     stage_url=f"{lan}/stage/{slug}#{stage_key}")

        if hub is not None:
            self._start_tunnel(hub, port, slug, stage_key)
        self._start_receiver(slug)
        self._main(lambda: self._start_output(slug))
        self._update(state=MeetingState.LIVE)

    def _start_tunnel(self, hub: HubConnection, port: int, slug: str, stage_key: str) -> None:
        binary = find_helper(Helper.FRPC)
        if binary is None:
            self._warn("The hub tunnel (frpc) is not installed; the meeting is reachable on this network only.")
            self._update(tunnel="failed")
            return
        origin = hub.public_origin

        def on_state(state: TunnelState, detail: str) -> None:
            self._update(tunnel=state.value)
            if state is TunnelState.ONLINE:
                self._update(invitation_url=f"{origin}/j/{slug}", stage_url=f"{origin}/stage/{slug}#{stage_key}")

        self._tunnel = FrpTunnel(binary, self._data_dir / "frpc", hub, local_port=port, on_state=on_state)
        self._update(tunnel="connecting")
        self._tunnel.start()

    def _start_receiver(self, slug: str) -> None:
        binary = find_helper(Helper.BRIDGE)
        if binary is None or self._galene is None:
            self._warn("The meeting bridge is not installed; commenters will not be heard in the hall.")
            return
        token = sign_token(self._group_key, username=HALL_LISTENER_USERNAME, audience=self._audience(slug),
                           permissions=OBSERVE, ttl_seconds=_LONG_TOKEN_TTL)
        device = self._settings.remote_audio_device()
        command: list[str | Path] = [binary, "receive", "--ws", f"ws://127.0.0.1:{self._galene.port}/ws",
                                     "--group", slug, "--token", token, "--ignore-user", PROGRAM_USERNAME]
        if device.get("id"):
            command += ["--device", device["id"]]
        self._receiver = SupervisedProcess(
            "commenter-audio", command, max_restarts=20,
            on_state=lambda state, _detail: self._update(commenter_audio=state.value),
        )
        self._receiver.start(wait=False)

    def _audience(self, slug: str) -> str:
        return f"https://solin.invalid/group/{slug}/"

    def _start_output(self, slug: str) -> None:
        runtime = self._scene_runtime
        engine = getattr(runtime, "scene_engine", None) if runtime is not None else None
        bridge = find_helper(Helper.BRIDGE)
        if engine is None or bridge is None or self._galene is None:
            self._warn("The program cannot be sent to the meeting (scene engine or bridge unavailable).")
            self._update(output="failed")
            return
        from solin.core.scenes.engine import MeetingOutputEvent, MeetingOutputSettings
        from solin.core.scenes.recording import AudioDeviceSelection, AudioSelectionMode

        quality = self._settings.quality()
        video = quality.video
        feed = self._settings.hall_feed()
        try:
            mode = AudioSelectionMode(feed.get("mode") or "system_default")
            hall_feed = (AudioDeviceSelection(mode, feed["device_id"], feed["display_name"] or feed["device_id"])
                         if mode is AudioSelectionMode.DEVICE else AudioDeviceSelection(mode))
        except (ValueError, KeyError):
            hall_feed = AudioDeviceSelection()
        token = sign_token(self._group_key, username=PROGRAM_USERNAME, audience=self._audience(slug),
                           permissions=PRESENT, ttl_seconds=_LONG_TOKEN_TTL)
        settings = MeetingOutputSettings(
            bridge=str(bridge), whip_url=f"http://127.0.0.1:{self._galene.port}/group/{slug}/.whip", token=token,
            width=video[0] if video else 640, height=video[1] if video else 360,
            video_kbps=video[2] if video else 0, audio_only=quality is MeetingQuality.AUDIO, hall_feed=hall_feed,
            hall_feed_delay_ms=self._settings.hall_feed_delay_ms(),
        )

        def on_event(event: object) -> None:
            if isinstance(event, MeetingOutputEvent):
                self._update(output=event.state)

        if hasattr(runtime, "engine_event"):
            runtime.engine_event.connect(on_event)
            self._unsubscribe_engine = lambda: runtime.engine_event.disconnect(on_event)
        runtime.set_meeting_output_required(True)
        self._update(output="starting")
        future = engine.start_meeting_output(settings, request_id=f"meeting-{secrets.token_hex(4)}",
                                             deadline_ms=10_000)

        def done(f: Any) -> None:
            try:
                ack = f.result()
                if not getattr(ack, "applied", False):
                    self._update(output="failed")
                    self._warn(f"The program output did not start: {getattr(ack, 'error_message', '')}")
            except Exception as error:  # noqa: BLE001 - engine boundary
                self._update(output="failed")
                self._warn(f"The program output did not start: {error}")

        future.add_done_callback(done)

    def end_meeting(self) -> None:
        with self._lock:
            if self._info.state not in (MeetingState.STARTING, MeetingState.LIVE, MeetingState.FAILED):
                return
        self._update(state=MeetingState.ENDING)
        self._stop_output()
        threading.Thread(target=self._end_worker, name="solin-meeting-end", daemon=True).start()

    def _end_worker(self) -> None:
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(30)
        self._stop_services()
        self._update(state=MeetingState.IDLE, output="idle", tunnel="off", galene="stopped",
                     commenter_audio="stopped")

    def _stop_output(self) -> None:
        runtime = self._scene_runtime
        engine = getattr(runtime, "scene_engine", None) if runtime is not None else None
        if engine is not None:
            try:
                engine.stop_meeting_output(request_id=f"meeting-stop-{secrets.token_hex(4)}", deadline_ms=5_000)
            except Exception:  # noqa: BLE001 - engine boundary
                log.debug("stop_meeting_output failed", exc_info=True)
        if runtime is not None and hasattr(runtime, "set_meeting_output_required"):
            runtime.set_meeting_output_required(False)
        unsubscribe, self._unsubscribe_engine = self._unsubscribe_engine, None
        if unsubscribe is not None:
            try:
                unsubscribe()
            except (RuntimeError, TypeError):
                pass

    def _stop_services(self) -> None:
        for name in ("_runtime", "_receiver", "_tunnel", "_galene"):
            component = getattr(self, name)
            setattr(self, name, None)
            if component is None:
                continue
            try:
                component.stop()
            except Exception:  # noqa: BLE001 - keep stopping the rest
                log.warning("stopping %s failed", name, exc_info=True)

    def shutdown(self) -> None:
        """App shutdown: stop everything synchronously."""

        if self._info.state is MeetingState.IDLE:
            return
        self._stop_output()
        self._stop_services()
        with self._lock:
            self._info = MeetingInfo()

    # ── observers ─────────────────────────────────────────────────────────

    def _on_galene_state(self, state: ProcessState, detail: str) -> None:
        self._update(galene=state.value)
        if state is ProcessState.FAILED and self._info.state is MeetingState.LIVE:
            self._warn(f"The media server stopped: {detail}")

    def _on_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self.snapshot_changed.emit(snapshot)

    # ── operator actions ──────────────────────────────────────────────────

    def _command(self, name: str, *args: Any) -> None:
        runtime = self._runtime
        if runtime is None or not runtime.running:
            return
        future = runtime.command(name, *args)
        future.add_done_callback(lambda f: f.exception() and log.warning("meeting %s failed: %s", name, f.exception()))

    def admit(self, device: str) -> None:
        self._command("admit", device)

    def admit_all(self) -> None:
        self._command("admit_all")

    def remove(self, device: str) -> None:
        self._command("remove", device)

    def rename(self, device: str, name: str) -> None:
        self._command("rename", device, name)

    def call_on(self, device: str) -> None:
        self._command("call_on", device)

    def done(self, device: str) -> None:
        self._command("done", device)

    def lower_hand(self, device: str) -> None:
        self._command("lower_hand", device)

    def set_part_holder(self, device: str, value: bool) -> None:
        self._command("set_part_holder", device, bool(value))

    def lock_mics(self, locked: bool) -> None:
        self._command("lock_mics", bool(locked))

    def attendance_text(self) -> str:
        """The attendance as plain text (one line per admitted name, with the time)."""

        runtime = self._runtime
        if runtime is None or not runtime.running:
            return ""
        try:
            report = runtime.command("attendance_report").result(3.0)
        except Exception:  # noqa: BLE001 - runtime boundary
            return ""
        import datetime as _dt

        lines = []
        for row in report:
            when = _dt.datetime.fromtimestamp(float(row["admittedAt"])).strftime("%H:%M")
            lines.append(f"{when}\t{row['name']}")
        return f"{len(report)}\n" + "\n".join(lines) if report else ""
