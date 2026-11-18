"""HostedMeetingService: one hosted meeting from start to end, for the Qt UI.

Links (:mod:`.links`): every meeting is reached at ``<origin>/<code>`` with a password;
the stage page is ``/<code>/s`` and the moderator page ``/<code>/m``. In "same link"
mode one permanent code serves every meeting; in "per meeting" mode each scheduled
meeting has its own code and password, prepared four weeks ahead from the
congregation's meeting days. With a meeting server (:mod:`.hub_client`), codes come
from the server: a new link first gets a local code, which works on this network right
away, and is then exchanged for the server's code and its owner secret; the server also
learns each code's start time (its waiting page counts down) and which codes are retired.
Without a server, or until it answers, links use this computer's LAN address.

Start (on a worker thread, so the UI never blocks):

1. Galene (with TURN credentials the server issues for this meeting) and a fresh group for this
   meeting (the code, a per-meeting HS256 key);
2. the meeting runtime: web server + operator client + rules (password, approved
   stage/moderator devices, and the people admitted before Solin restarted, if this
   meeting was already running);
3. the tunnel to the server (frpc), named after the code and authorized by its secret,
   giving the meeting its public HTTPS address — without a server (or a server code) the
   meeting is reachable on the LAN only;
4. the commenter-audio player (``solin-galene-bridge receive`` → remote-audio device),
   which also forwards remote part-holders' video to the scenes' video slots
   (``participant_video``); its slot events reach the scene engine so a slot whose
   part-holder left is cleared;
5. the program output in the libobs sidecar (``start_meeting_output``), on the Qt thread.

A scheduled meeting opens by itself when its window opens (30 minutes before the
start; ``auto_start``), and its official attendance count is taken at 0.75 of the
meeting. End reverses the start. Operator actions are forwarded to the meeting rules.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from solin.core.meetings.schedule_settings import MeetingScheduleSettingsStore

from .binaries import Helper, find_helper
from .galene_config import TurnServer
from .galene_server import GaleneServer, GaleneUnavailable
from .hub_client import CodeState, HubClient, HubError, TunnelInfo, new_secret
from .devices import DeviceRegistry
from .links import UNSCHEDULED, LinkMode, LinksState, ScheduledMeeting, clean_password
from .participant_video import bridge_ports_argument, valid_slot
from .processes import ProcessState, SupervisedProcess
from .runtime import MeetingRuntime, MeetingRuntimeConfig
from .settings import HostedMeetingSettingsStore, HubServer, MeetingProvider, MeetingQuality
from .tokens import OBSERVE, PRESENT, new_group_key, sign_token
from .tunnel import FrpTunnel, TunnelState

log = logging.getLogger(__name__)

PROGRAM_USERNAME = "Solin program"
HALL_LISTENER_USERNAME = "Solin hall"
_LONG_TOKEN_TTL = 24 * 3600
_AUTO_START_TICK_MS = 30_000
_HUB_SYNC_DELAY_MS = 300   # changes come in bursts (a dialog saves, then the list refreshes)
# Solin restarted after the count time: give people a moment to reconnect first.
_LATE_COUNT_DELAY = timedelta(minutes=2)
# The people admitted before a restart are let back in only within this time.
_ADMITTED_MAX_AGE_S = 4 * 3600


class MeetingState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    LIVE = "live"
    ENDING = "ending"
    FAILED = "failed"


@dataclass(slots=True)
class MeetingInfo:
    state: MeetingState = MeetingState.IDLE
    slug: str = ""                 # the code in use (or offered next)
    password: str = ""
    invitation_url: str = ""
    lan_url: str = ""
    stage_url: str = ""
    moderator_url: str = ""
    link_scope: str = "lan"        # lan (this network only) / hub (from anywhere) / pending (server code coming)
    server: str = ""               # the meeting server's address ("" = none)
    link_mode: str = LinkMode.SAME.value
    meeting_id: str = ""           # the scheduled meeting this link belongs to
    starts_at: str = ""            # ISO; empty for an unscheduled meeting
    count_at: str = ""             # ISO; when the official count is taken
    official_count: dict[str, Any] | None = None
    auto_started: bool = False
    upcoming: list[dict[str, Any]] = field(default_factory=list)
    low_on_codes: bool = False
    devices: list[dict[str, Any]] = field(default_factory=list)
    hub_error: str = ""            # why the server has not answered ("" = it has)
    output: str = "idle"           # program output: idle/starting/live/reconnecting/failed
    tunnel: str = "off"            # off/connecting/online/failed
    galene: str = "stopped"
    commenter_audio: str = "stopped"
    participant_video: dict[int, str] = field(default_factory=dict)  # slot → part-holder name
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value, "slug": self.slug, "code": self.slug, "password": self.password,
            "invitationUrl": self.invitation_url, "lanUrl": self.lan_url, "stageUrl": self.stage_url,
            "moderatorUrl": self.moderator_url, "linkScope": self.link_scope, "linkMode": self.link_mode,
            "meetingId": self.meeting_id, "startsAt": self.starts_at, "countAt": self.count_at,
            "officialCount": dict(self.official_count) if self.official_count else None,
            "autoStarted": self.auto_started, "upcoming": [dict(m) for m in self.upcoming],
            "lowOnCodes": self.low_on_codes, "devices": [dict(d) for d in self.devices],
            "hubError": self.hub_error, "server": self.server,
            "output": self.output, "tunnel": self.tunnel, "galene": self.galene,
            "commenterAudio": self.commenter_audio,
            "participantVideo": {str(slot): user for slot, user in sorted(self.participant_video.items())},
            "error": self.error, "warnings": list(self.warnings),
        }


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
    auto_started = Signal(object)       # dict (the scheduled meeting that opened by itself)
    official_count_taken = Signal(object)   # dict {"people", "devices", "at"}
    _call_on_main = Signal(object)

    def __init__(
        self,
        settings: HostedMeetingSettingsStore,
        data_dir: Path,
        *,
        scene_runtime: Any = None,
        auto_start: bool = False,
        clock: Callable[[], datetime] | None = None,
        hub_client: Callable[[HubServer], HubClient] = HubClient,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._data_dir = Path(data_dir) / "hosted_meeting"
        self._scene_runtime = scene_runtime
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.RLock()
        self._galene: GaleneServer | None = None
        self._runtime: MeetingRuntime | None = None
        self._tunnel: FrpTunnel | None = None
        self._receiver: SupervisedProcess | None = None
        self._group_key = b""
        self._snapshot: dict[str, Any] = {}
        self._worker: threading.Thread | None = None
        self._unsubscribe_engine: Callable[[], None] | None = None
        self._meeting: ScheduledMeeting | None = None   # the scheduled meeting running now
        self._code = ""
        self._password = ""
        self._handled: set[str] = set()   # meetings opened (by hand or automatically) or ended
        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.timeout.connect(self._take_official_count)
        self._call_on_main.connect(self._run_on_main, Qt.ConnectionType.QueuedConnection)
        # Talking to the meeting server: one exchange at a time; the last state it accepted.
        self._hub_client = hub_client
        self._hub_synced: tuple[str, list[CodeState]] | None = None
        self._hub_busy = False
        self._hub_again = False
        self._hub_timer = QTimer(self)
        self._hub_timer.setSingleShot(True)
        self._hub_timer.setInterval(_HUB_SYNC_DELAY_MS)
        self._hub_timer.timeout.connect(self._sync_hub_codes)
        self._info = MeetingInfo()
        self.refresh_links()
        self._auto_timer: QTimer | None = None
        if auto_start:
            self._auto_timer = QTimer(self)
            self._auto_timer.setInterval(_AUTO_START_TICK_MS)
            self._auto_timer.timeout.connect(self.check_schedule)
            self._auto_timer.start()
            QTimer.singleShot(0, self.check_schedule)

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

    def _active(self) -> bool:
        return self.state in (MeetingState.STARTING, MeetingState.LIVE, MeetingState.ENDING)

    # ── links ─────────────────────────────────────────────────────────────

    def _origin(self, *, use_hub: bool = True) -> tuple[str, str]:
        """(public origin, LAN origin)."""

        settings = self._settings
        hub = settings.hub() if use_hub else None
        lan = f"http://{lan_address()}:{settings.web_port()}"
        return (hub.public_origin if hub else lan), lan

    def _schedule(self) -> Any:
        return MeetingScheduleSettingsStore(self._settings.settings).load()

    def _prepared_links(self) -> LinksState:
        """The stored links, topped up to four weeks ahead (and saved if that changed them)."""

        mode = self._settings.link_mode()
        state = self._settings.links()
        updated = state.top_up(self._schedule(), self._clock(), with_links=mode is LinkMode.PER_MEETING)
        if mode is LinkMode.SAME:
            updated = updated.with_permanent()
        if updated != state:
            self._settings.set_links(updated)
        return updated

    def _link_fields(self, code: str, password: str, *, use_hub: bool = True) -> dict[str, Any]:
        """The links for a code: on the server once it issued the code, on this network until then."""

        hub = self._settings.hub()
        public, lan = self._origin(use_hub=use_hub)
        issued = use_hub and hub is not None and bool(code) and self._settings.links().issued(code)
        origin = public if issued else lan
        scope = "hub" if issued else "pending" if use_hub and hub is not None and code else "lan"
        server = hub.domain if hub is not None else ""
        if not code:
            return {"slug": "", "password": "", "invitation_url": "", "lan_url": "", "stage_url": "",
                    "moderator_url": "", "link_scope": scope, "server": server}
        return {"slug": code, "password": password, "invitation_url": f"{origin}/{code}", "lan_url": f"{lan}/{code}",
                "stage_url": f"{origin}/{code}/s", "moderator_url": f"{origin}/{code}/m",
                "link_scope": scope, "server": server}

    def _idle_fields(self) -> dict[str, Any]:
        """What the console shows before a meeting: the link to share, the meetings ahead."""

        state = self._prepared_links()
        mode = self._settings.link_mode()
        now = self._clock()
        origin, _lan = self._origin()
        upcoming = []
        for meeting in state.upcoming(now):
            code = meeting.code if mode is LinkMode.PER_MEETING else (state.permanent.code if state.permanent else "")
            password = meeting.password if mode is LinkMode.PER_MEETING else (
                state.permanent.password if state.permanent else "")
            upcoming.append({"id": meeting.id, "kind": meeting.kind, "startsAt": meeting.starts_at.isoformat(),
                             "code": code, "password": password,
                             "url": (f"{origin if state.issued(code) else self._origin(use_hub=False)[1]}/{code}"
                                     if code else ""),
                             "cancelled": meeting.cancelled, "moved": meeting.moved,
                             "open": meeting.in_window(now), "running": meeting.id == self._info.meeting_id
                             and self._active()})
        if mode is LinkMode.SAME and state.permanent is not None:
            code, password = state.permanent.code, state.permanent.password
        else:
            following = next((m for m in state.upcoming(now) if not m.cancelled and m.code), None)
            code, password = (following.code, following.password) if following else ("", "")
        fields = self._link_fields(code, password)
        fields.update(link_mode=mode.value, upcoming=upcoming,
                      low_on_codes=mode is LinkMode.PER_MEETING and state.low_on_codes(self._schedule(), now),
                      devices=self._device_rows())
        return fields

    def _device_rows(self) -> list[dict[str, Any]]:
        registry = DeviceRegistry.from_json(self._settings.devices())
        return [{"device": d.device_id, "name": d.name, "role": d.role.value, "lastSeen": d.last_seen}
                for d in registry.all(time.time())]

    def refresh_links(self) -> None:
        """Show the current links after a settings or schedule change; a running meeting keeps its own."""

        fields = self._idle_fields()
        if self._active():
            # Keep the running meeting's link; refresh only the list of meetings ahead.
            fields = {key: fields[key] for key in ("upcoming", "low_on_codes", "devices", "link_mode")}
        self._update(**fields)
        self._hub_timer.start()

    # ── the meeting server ────────────────────────────────────────────────

    def _active_codes(self, state: LinksState) -> dict[str, tuple[str, str]]:
        """The codes that lead to a meeting now: {code: (start, kind)}.

        The server's waiting page counts down to the start and names the kind of meeting.
        """

        now = self._clock()
        active: dict[str, tuple[str, str]] = {}
        if self._settings.link_mode() is LinkMode.SAME:
            if state.permanent is not None:
                following = next((m for m in state.upcoming(now) if not m.cancelled), None)
                active[state.permanent.code] = (following.starts_at.isoformat(), following.kind) if following else ("", "")
        else:
            for meeting in state.meetings:
                if meeting.code:
                    active[meeting.code] = (meeting.starts_at.isoformat() if meeting.scheduled else "", meeting.kind)
        if self._active() and self._code:
            active.setdefault(self._code, ("", self._running_kind(state)))
        return active

    def _running_kind(self, state: LinksState) -> str:
        return next((m.kind for m in state.meetings if m.id == self._info.meeting_id), "")

    def _sync_hub_codes(self) -> None:
        """Exchange local codes for the server's, then keep the server's view of our codes current.

        One exchange at a time, on a worker thread; results are applied on the Qt thread.
        The running meeting keeps its code even if it is still a local one.
        """

        hub = self._settings.hub()
        if hub is None:
            if self._info.hub_error:
                self._update(hub_error="")
            return
        if self._hub_busy:
            self._hub_again = True
            return
        state = self._settings.links()
        active = self._active_codes(state)
        running = self._code if self._active() else ""
        pending = [code for code in active if not state.issued(code) and code != running]
        title = self._settings.hall_name()
        states = []
        for code, secret in sorted(state.secrets.items()):
            starts_at, kind = active.get(code, ("", ""))
            states.append(CodeState(code, secret, starts_at, code in active, title=title, kind=kind))
        if not pending and self._hub_synced == (hub.domain, states):
            return
        self._hub_busy = True
        client = self._hub_client(hub)

        def work() -> None:
            issued: list[tuple[str, str, str]] = []
            rejected: set[str] = set()
            error = ""
            try:
                for local in pending:
                    secret = new_secret()
                    starts_at, kind = active[local]
                    issued.append((local, client.create_code(secret, starts_at, title=title, kind=kind), secret))
                if not pending:
                    rejected = client.sync_codes(states)
            except HubError as failure:
                error = str(failure)
            self._main(lambda: self._hub_done(hub.domain, states, issued, rejected, error, synced=not pending))

        threading.Thread(target=work, name="solin-hub-codes", daemon=True).start()

    def _hub_done(self, domain: str, states: list[CodeState], issued: list[tuple[str, str, str]],
                  rejected: set[str], error: str, *, synced: bool) -> None:
        self._hub_busy = False
        before = self._settings.links()
        state = before
        for local, code, secret in issued:
            if local in state.codes_in_use() - set(state.retired):   # not renewed meanwhile
                state = state.issue(local, code, secret)
        if rejected:
            # The server does not know these as ours (e.g. it lost its registry): they get new codes.
            log.info("meeting server: %d code(s) not known as ours; getting new ones", len(rejected))
            state = state.forget_secrets(rejected)
        if state != before:
            self._settings.set_links(state)
        if synced and not error and not rejected:
            self._hub_synced = (domain, states)
        self._update(hub_error=error)
        if state != before or self._hub_again:
            self._hub_again = False
            self.refresh_links()

    def _issue_for_start(self, hub: HubServer) -> str:
        """Worker thread, opening a meeting: the code's secret, getting a server code first if needed.

        Returns "" when the server cannot be reached: the meeting then runs on this network only.
        """

        state = self._settings.links()
        if state.issued(self._code):
            return state.secrets[self._code]
        secret = new_secret()
        try:
            code = self._hub_client(hub).create_code(secret, self._info.starts_at, title=self._settings.hall_name(),
                                                     kind=self._running_kind(state))
        except HubError as error:
            self._warn(f"No public link: {error}. The meeting is reachable on this network only.")
            return ""
        local, self._code = self._code, code
        applied = threading.Event()

        def apply() -> None:
            self._settings.set_links(self._settings.links().issue(local, code, secret))
            applied.set()

        self._main(apply)
        applied.wait(5)
        self._update(**self._link_fields(code, self._password))
        return secret

    def _changing_links(self) -> bool:
        return self.state not in (MeetingState.IDLE, MeetingState.FAILED)

    def new_link(self, meeting_id: str = "") -> bool:
        """Replace a link (and its password); every approved stage/moderator device is forgotten.

        Same-link mode: the permanent link, never during a meeting. Per-meeting mode: one
        meeting's link; the running meeting's link only after it has ended.
        """

        now = self._clock()
        state = self._prepared_links()
        if self._settings.link_mode() is LinkMode.SAME:
            if self._changing_links():
                return False
            state = state.new_permanent(now)
        else:
            if state.find(meeting_id) is None or (self._active() and meeting_id == self._info.meeting_id):
                return False
            state = state.renew(meeting_id, now)
        self._settings.set_links(state)
        self._forget_devices()
        self.refresh_links()
        return True

    def renew_upcoming(self) -> bool:
        """Per-meeting mode: new links for every meeting ahead (skipping the running one)."""

        if self._settings.link_mode() is not LinkMode.PER_MEETING:
            return False
        skip = self._info.meeting_id if self._active() else ""
        self._settings.set_links(self._prepared_links().renew_upcoming(self._clock(), skip=skip))
        self._forget_devices()
        self.refresh_links()
        return True

    def set_password(self, password: str, meeting_id: str = "") -> bool:
        """Change only the password (the code and approved devices stay)."""

        cleaned = clean_password(password)
        if cleaned is None:
            return False
        state = self._prepared_links()
        if self._settings.link_mode() is LinkMode.SAME:
            state = state.set_permanent_password(cleaned)
            running = self._active()
        else:
            if state.find(meeting_id) is None:
                return False
            state = state.set_meeting_password(meeting_id, cleaned)
            running = self._active() and meeting_id == self._info.meeting_id
        self._settings.set_links(state)
        if running:
            self._password = cleaned
            self._command("set_password", cleaned)
            self._update(password=cleaned)
        self.refresh_links()
        return True

    def move_meeting(self, meeting_id: str, starts_at: datetime) -> bool:
        if starts_at.tzinfo is None or (self._active() and meeting_id == self._info.meeting_id):
            return False
        state = self._prepared_links()
        if state.find(meeting_id) is None:
            return False
        self._settings.set_links(state.move(meeting_id, starts_at))
        self.refresh_links()
        return True

    def set_meeting_cancelled(self, meeting_id: str, cancelled: bool) -> bool:
        if self._active() and meeting_id == self._info.meeting_id:
            return False
        state = self._prepared_links()
        if state.find(meeting_id) is None:
            return False
        self._settings.set_links(state.set_cancelled(meeting_id, cancelled))
        self.refresh_links()
        return True

    def add_meeting(self, starts_at: datetime) -> str:
        """A one-off meeting (a special talk, the Memorial)."""

        if starts_at.tzinfo is None:
            return ""
        per_meeting = self._settings.link_mode() is LinkMode.PER_MEETING
        state, meeting_id = self._prepared_links().add_meeting(starts_at, with_link=per_meeting)
        self._settings.set_links(state)
        self.refresh_links()
        return meeting_id

    def remove_meeting(self, meeting_id: str) -> bool:
        """Remove a one-off meeting (weekly meetings are cancelled instead)."""

        state = self._prepared_links()
        meeting = state.find(meeting_id)
        if meeting is None or meeting.kind in ("midweek", "weekend") or (
                self._active() and meeting_id == self._info.meeting_id):
            return False
        self._settings.set_links(state.remove_meeting(meeting_id, self._clock()))
        self.refresh_links()
        return True

    def _forget_devices(self) -> None:
        runtime = self._runtime
        if runtime is not None and runtime.running:
            self._command("forget_all_devices")
        self._settings.set_devices([])

    # ── stage and moderator devices ───────────────────────────────────────

    def approve_staff(self, device: str) -> None:
        self._command("approve_staff", device)

    def refuse_staff(self, device: str) -> None:
        self._command("refuse_staff", device)

    def remove_device(self, device: str) -> None:
        runtime = self._runtime
        if runtime is not None and runtime.running:
            self._command("remove_device", device)
            return
        registry = DeviceRegistry.from_json(self._settings.devices())
        if registry.remove(device):
            self._settings.set_devices(registry.to_json())
            self._update(devices=self._device_rows())

    def _on_devices(self, devices: list[dict[str, Any]]) -> None:
        def save() -> None:
            self._settings.set_devices(devices)
            self._update(devices=self._device_rows())

        self._main(save)

    # ── the schedule ──────────────────────────────────────────────────────

    def check_schedule(self) -> None:
        """Every 30 s: keep four weeks of links ready; open a scheduled meeting when its window opens."""

        if self._settings.provider() is not MeetingProvider.SOLIN:
            return
        if not self._active():
            self.refresh_links()
        else:
            self._hub_timer.start()
        if self._active() or not self.available():
            return
        meeting = self._prepared_links().in_window(self._clock())
        if meeting is None or not meeting.scheduled or meeting.id in self._handled:
            return
        if self.start_meeting(auto=True):
            self.auto_started.emit({"id": meeting.id, "kind": meeting.kind,
                                    "startsAt": meeting.starts_at.isoformat()})

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

    def _choose_meeting(self) -> tuple[ScheduledMeeting | None, str, str]:
        """The scheduled meeting open now (if any) and the code and password to use."""

        now = self._clock()
        state = self._prepared_links()
        meeting = state.in_window(now)
        if self._settings.link_mode() is LinkMode.SAME:
            assert state.permanent is not None
            return meeting, state.permanent.code, state.permanent.password
        if meeting is not None and meeting.code:
            return meeting, meeting.code, meeting.password
        # Nothing scheduled now: an unscheduled meeting with its own link.
        state, meeting_id = state.add_meeting(now, with_link=True, kind=UNSCHEDULED)
        self._settings.set_links(state)
        created = state.find(meeting_id)
        assert created is not None
        return created, created.code, created.password

    def start_meeting(self, *, auto: bool = False) -> bool:
        with self._lock:
            if self._active():
                return False
        meeting, code, password = self._choose_meeting()
        self._meeting, self._code, self._password = meeting, code, password
        if meeting is not None:
            self._handled.add(meeting.id)
        scheduled = meeting is not None and meeting.scheduled
        with self._lock:
            self._info = MeetingInfo(
                state=MeetingState.STARTING, **self._link_fields(code, password),
                meeting_id=meeting.id if meeting else "", auto_started=auto,
                starts_at=meeting.starts_at.isoformat() if scheduled and meeting else "",
                count_at=meeting.count_at.isoformat() if scheduled and meeting else "",
            )
        self.refresh_links()
        self.info_changed.emit(self.info)
        if scheduled and meeting is not None:
            self._schedule_count(meeting)
        self._worker = threading.Thread(target=self._start_worker, name="solin-meeting-start", daemon=True)
        self._worker.start()
        return True

    def _schedule_count(self, meeting: ScheduledMeeting) -> None:
        now = self._clock()
        count_at = meeting.count_at
        if count_at <= now:
            if now >= meeting.closes_at:
                return
            count_at = now + _LATE_COUNT_DELAY
        self._count_timer.start(max(0, int((count_at - now).total_seconds() * 1000)))

    def _take_official_count(self) -> None:
        runtime = self._runtime
        if runtime is None or not runtime.running or self.state is not MeetingState.LIVE:
            return
        future = runtime.command("take_official_count", self._clock().isoformat(timespec="minutes"))

        def done(f: Any) -> None:
            if f.exception() is not None:
                log.warning("official count failed: %s", f.exception())
                return
            result = f.result()

            def publish() -> None:
                self._update(official_count=result)
                self.official_count_taken.emit(result)

            self._main(publish)

        future.add_done_callback(done)

    def _start_worker(self) -> None:
        try:
            self._start_services()
        except Exception as error:  # noqa: BLE001 - reported to the operator
            log.exception("could not start the hosted meeting")
            self._stop_services()
            self._update(state=MeetingState.FAILED, error=str(error))

    def _admitted_file(self, code: str) -> Path:
        return self._data_dir / f"admitted-{code}.json"

    def _load_admitted(self, code: str) -> tuple[dict[str, Any], ...]:
        path = self._admitted_file(code)
        try:
            if time.time() - path.stat().st_mtime > _ADMITTED_MAX_AGE_S:
                return ()
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ()
        return tuple(row for row in data if isinstance(row, dict)) if isinstance(data, list) else ()

    def _save_admitted(self, code: str, rows: list[dict[str, Any]]) -> None:
        path = self._admitted_file(code)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            log.debug("could not save the admitted attendees", exc_info=True)

    def _forget_admitted(self) -> None:
        try:
            for path in self._data_dir.glob("admitted-*.json"):
                path.unlink()
        except OSError:
            pass

    def _start_services(self) -> None:
        settings = self._settings
        hub = settings.hub()
        secret = self._issue_for_start(hub) if hub is not None else ""
        turn: TurnServer | None = None
        tunnel: TunnelInfo | None = None
        if hub is not None and secret:
            client = self._hub_client(hub)
            try:
                tunnel = client.tunnel_info()
                credentials = client.turn_credentials(self._code, secret)
                if credentials is not None:
                    turn = TurnServer(urls=credentials.urls, secret=credentials.credential,
                                      username=credentials.username, credential_type="")
            except HubError as error:
                self._warn(f"No public link: {error}. The meeting is reachable on this network only.")
                tunnel = None
        self._galene = GaleneServer(self._data_dir / "galene", on_state=self._on_galene_state)
        try:
            self._galene.start(turn=turn)
        except GaleneUnavailable as error:
            raise RuntimeError(str(error)) from error
        title = settings.hall_name() or "Meeting"
        slug = self._code
        self._group_key = new_group_key()
        self._galene.layout.clear_groups()
        self._galene.layout.write_group(slug, self._group_key, display_name=title)

        port = settings.web_port()
        self._runtime = MeetingRuntime(
            MeetingRuntimeConfig(slug=slug, title=title, group_key=self._group_key,
                                 galene_port=self._galene.port, web_port=port, password=self._password,
                                 starts_at=self._info.starts_at, devices=tuple(settings.devices()),
                                 admitted=self._load_admitted(slug)),
            on_snapshot=self._on_snapshot,
            on_devices=self._on_devices,
            on_admitted=lambda rows: self._save_admitted(slug, rows),
        )
        self._runtime.start()

        if hub is not None and tunnel is not None:
            self._start_tunnel(hub, tunnel, secret, port)
        self._start_receiver(slug)
        self._main(lambda: self._start_output(slug))
        self._update(state=MeetingState.LIVE)

    def _start_tunnel(self, hub: HubServer, tunnel: TunnelInfo, secret: str, port: int) -> None:
        binary = find_helper(Helper.FRPC)
        if binary is None:
            self._warn("The hub tunnel (frpc) is not installed; the meeting is reachable on this network only.")
            self._update(tunnel="failed", **self._link_fields(self._code, self._password, use_hub=False))
            return

        def on_state(state: TunnelState, detail: str) -> None:
            self._update(tunnel=state.value)

        self._tunnel = FrpTunnel(binary, self._data_dir / "frpc", hub, tunnel, code=self._code, secret=secret,
                                 local_port=port, on_state=on_state)
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
                                     "--group", slug, "--token", token, "--ignore-user", PROGRAM_USERNAME,
                                     "--video-ports", bridge_ports_argument()]
        if device.get("id"):
            command += ["--device", device["id"]]

        def on_state(state: ProcessState, _detail: str) -> None:
            self._update(commenter_audio=state.value)
            if state is not ProcessState.RUNNING:
                self._main(self._clear_participant_video)  # the slots' streams stopped with it

        self._receiver = SupervisedProcess(
            "commenter-audio", command, max_restarts=20, on_state=on_state, on_line=self._on_receiver_line,
        )
        self._receiver.start(wait=False)

    def _on_receiver_line(self, line: str) -> None:
        """Bridge stdout events (supervisor thread); only video slot changes matter here."""
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict) or event.get("event") != "video":
            return
        slot, state = event.get("slot"), event.get("state")
        if not valid_slot(slot) or state not in ("started", "stopped"):
            return
        active = state == "started"
        with self._lock:
            slots = dict(self._info.participant_video)
        if active:
            slots[slot] = str(event.get("user") or "")
        else:
            slots.pop(slot, None)
        self._update(participant_video=slots)
        self._main(lambda: self._send_participant_video(slot, active))

    def _send_participant_video(self, slot: int, active: bool) -> None:
        runtime = self._scene_runtime
        engine = getattr(runtime, "scene_engine", None) if runtime is not None else None
        if engine is None or not hasattr(engine, "set_participant_video"):
            return
        try:
            engine.set_participant_video(slot, active, request_id=f"meeting-video-{secrets.token_hex(4)}",
                                         deadline_ms=5_000)
        except Exception:  # noqa: BLE001 - engine boundary
            log.debug("set_participant_video failed", exc_info=True)

    def _clear_participant_video(self) -> None:
        with self._lock:
            slots = list(self._info.participant_video)
        if not slots:
            return
        self._update(participant_video={})
        for slot in slots:
            self._send_participant_video(slot, False)

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
        """The operator's End meeting: attendees see "ended", and the meeting stays closed."""

        with self._lock:
            if self._info.state not in (MeetingState.STARTING, MeetingState.LIVE, MeetingState.FAILED):
                return
        self._count_timer.stop()
        self._update(state=MeetingState.ENDING)
        self._stop_output()
        threading.Thread(target=self._end_worker, name="solin-meeting-end", daemon=True).start()

    def _end_worker(self) -> None:
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(30)
        self._stop_services()
        self._forget_admitted()
        self._meeting = None
        self._update(state=MeetingState.IDLE, output="idle", tunnel="off", galene="stopped",
                     commenter_audio="stopped", official_count=None, auto_started=False, meeting_id="",
                     starts_at="", count_at="")
        self._main(self.refresh_links)

    def _stop_output(self) -> None:
        self._clear_participant_video()
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
        """App shutdown: stop everything synchronously.

        Not an "end": the admitted attendees are kept, so if Solin comes back during the
        meeting they go straight back in, and their pages only show "reconnecting".
        """

        self._count_timer.stop()
        self._hub_timer.stop()
        if self._auto_timer is not None:
            self._auto_timer.stop()
        if self._info.state is MeetingState.IDLE:
            return
        self._stop_output()
        runtime = self._runtime
        if runtime is not None:
            runtime.keep_attendees_waiting = True
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
        """The people watching now, to paste where the attendance is recorded."""

        official = self._info.official_count
        totals = official or self._snapshot
        if not totals or "people" not in totals:
            return ""
        return str(int(totals["people"]))
