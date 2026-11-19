"""The links the console offers before and during a meeting, in both link modes."""

from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from PySide6.QtCore import QCoreApplication

from solin.core.hosted_meeting.devices import ApprovedDevice, KeyKind, StaffRole
from solin.core.hosted_meeting.hub_client import HubError
from solin.core.hosted_meeting.links import LinkMode, normalize_code
from solin.core.hosted_meeting.service import HostedMeetingService, MeetingState
from solin.core.hosted_meeting.settings import SERVER_OVERRIDE, HostedMeetingSettingsStore

TZ = timezone(timedelta(hours=-3))
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=TZ)   # Monday


@pytest.fixture
def store(monkeypatch):
    settings = SettingsStore.for_namespace("SolinTests", f"hosted-meeting-link-{secrets.token_hex(4)}")
    store = HostedMeetingSettingsStore(settings)
    store.set_hall_name("Salão Central")
    monkeypatch.setenv(SERVER_OVERRIDE, "")   # each test that wants a server sets one, with a FakeHub
    settings.set_value(SettingsKey.MEETING_MIDWEEK_DAY, 2)
    settings.set_value(SettingsKey.MEETING_MIDWEEK_TIME, "19:30")
    settings.set_value(SettingsKey.MEETING_WEEKEND_DAY, 6)
    settings.set_value(SettingsKey.MEETING_WEEKEND_TIME, "09:00")
    yield store
    for key in settings.all_keys():
        settings.set_value(key, None)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock()


class FakeHub:
    """Stands in for the meeting server: issues codes, records syncs, fails when told."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []    # (code, owner secret)
        self.synced: list[list] = []
        self.reject: set[str] = set()
        self.down = ""
        self._codes = iter(f"q{n:05d}".translate(str.maketrans("01", "xz")) for n in range(1, 99999))

    def __call__(self, server):
        return self

    def create_code(self, secret: str, starts_at: str = "", **listing) -> str:
        if self.down:
            raise HubError(self.down)
        code = next(self._codes)
        self.created.append((code, secret))
        return code

    def sync_codes(self, codes) -> set[str]:
        if self.down:
            raise HubError(self.down)
        self.synced.append(list(codes))
        return {c.code for c in codes} & self.reject


def _pump(until=lambda: False, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not until():
        QCoreApplication.processEvents()
        time.sleep(0.01)


def _device() -> dict:
    return ApprovedDevice("m1", StaffRole.MODERATOR, "Lima", KeyKind.SECRET, "h", approved_at=4e9).to_json()


def test_same_link_mode_offers_one_permanent_link(store, tmp_path, clock):
    service = HostedMeetingService(store, tmp_path, clock=clock)
    info = service.info
    code = info["code"]
    assert normalize_code(code) == code and info["password"].isdigit() and len(info["password"]) == 6
    assert info["linkScope"] == "lan" and info["invitationUrl"].endswith(f":{store.web_port()}/{code}")
    assert info["stageUrl"].endswith(f"/{code}/s") and info["moderatorUrl"].endswith(f"/{code}/m")
    assert [m["kind"] for m in info["upcoming"][:2]] == ["midweek", "weekend"]
    assert all(m["code"] == code for m in info["upcoming"])
    assert HostedMeetingService(store, tmp_path, clock=clock).info["code"] == code  # stored


def test_the_server_issues_the_codes(store, tmp_path, clock, monkeypatch):
    hub = FakeHub()
    monkeypatch.setenv(SERVER_OVERRIDE, "meet.solin.run")
    service = HostedMeetingService(store, tmp_path, clock=clock, hub_client=hub)
    local = service.info["code"]
    assert service.info["linkScope"] == "pending"
    assert service.info["invitationUrl"].endswith(f":{store.web_port()}/{local}")   # works here meanwhile
    _pump(lambda: service.info["linkScope"] == "hub")
    code, secret = hub.created[0]
    info = service.info
    assert info["code"] == code != local and info["server"] == "meet.solin.run"
    assert info["invitationUrl"] == f"https://meet.solin.run/{code}"
    assert info["stageUrl"] == f"https://meet.solin.run/{code}/s"
    assert store.links().secrets == {code: secret}
    # Then the server learns the start time its waiting page counts down to.
    _pump(lambda: hub.synced)
    assert [(c.code, c.secret, c.starts_at, c.active) for c in hub.synced[0]] == \
           [(code, secret, "2026-11-18T19:30:00-03:00", True)]
    # Nothing changed: nothing is sent again.
    service.refresh_links()
    _pump(seconds=0.5)
    assert len(hub.created) == 1 and len(hub.synced) == 1


def test_new_link_retires_the_old_code_on_the_server(store, tmp_path, clock, monkeypatch):
    hub = FakeHub()
    monkeypatch.setenv(SERVER_OVERRIDE, "meet.solin.run")
    service = HostedMeetingService(store, tmp_path, clock=clock, hub_client=hub)
    _pump(lambda: hub.synced)
    old = service.info["code"]
    assert service.new_link() is True
    _pump(lambda: len(hub.created) == 2 and len(hub.synced) == 2)
    new = service.info["code"]
    assert new == hub.created[1][0] and new != old
    states = {c.code: c.active for c in hub.synced[1]}
    assert states == {old: False, new: True}


def test_per_meeting_codes_come_from_the_server(store, tmp_path, clock, monkeypatch):
    hub = FakeHub()
    monkeypatch.setenv(SERVER_OVERRIDE, "meet.solin.run")
    store.set_link_mode(LinkMode.PER_MEETING)
    store.set_hall_name("Central")
    service = HostedMeetingService(store, tmp_path, clock=clock, hub_client=hub)
    _pump(lambda: hub.synced)
    upcoming = service.info["upcoming"]
    assert len(hub.created) == 8
    assert [m["code"] for m in upcoming] == [code for code, _secret in hub.created]
    assert all(m["url"] == f"https://meet.solin.run/{m['code']}" for m in upcoming)
    starts = {c.code: c.starts_at for c in hub.synced[0]}
    assert starts[upcoming[1]["code"]] == upcoming[1]["startsAt"]
    # The waiting page names the congregation and the kind of meeting.
    listed = {c.code: (c.title, c.kind) for c in hub.synced[0]}
    assert listed[upcoming[0]["code"]] == ("Central", upcoming[0]["kind"]) and upcoming[0]["kind"] in ("midweek", "weekend")


def test_a_server_that_cannot_be_reached_is_shown_and_retried(store, tmp_path, clock, monkeypatch):
    hub = FakeHub()
    hub.down = "meet.solin.run cannot be reached (timed out)"
    monkeypatch.setenv(SERVER_OVERRIDE, "meet.solin.run")
    service = HostedMeetingService(store, tmp_path, clock=clock, hub_client=hub)
    _pump(lambda: service.info["hubError"])
    assert "cannot be reached" in service.info["hubError"] and service.info["linkScope"] == "pending"
    hub.down = ""
    service.refresh_links()   # what the 30 s schedule tick does
    _pump(lambda: service.info["linkScope"] == "hub" and not service.info["hubError"])
    assert service.info["hubError"] == ""


def test_codes_the_server_does_not_know_as_ours_are_replaced(store, tmp_path, clock, monkeypatch):
    hub = FakeHub()
    monkeypatch.setenv(SERVER_OVERRIDE, "meet.solin.run")
    service = HostedMeetingService(store, tmp_path, clock=clock, hub_client=hub)
    _pump(lambda: hub.synced and not service._hub_busy)
    lost = service.info["code"]
    hub.reject = {lost}               # e.g. the server lost its registry
    service._hub_synced = None        # force a new sync
    service.refresh_links()
    _pump(lambda: service.info["code"] != lost and service.info["linkScope"] == "hub")
    assert service.info["code"] == hub.created[-1][0] and lost not in store.links().secrets


def test_without_a_server_nothing_is_sent(store, tmp_path, clock):
    hub = FakeHub()
    HostedMeetingService(store, tmp_path, clock=clock, hub_client=hub)
    _pump(seconds=0.5)
    assert hub.created == [] and hub.synced == []


def test_per_meeting_mode_prepares_four_weeks(store, tmp_path, clock):
    store.set_link_mode(LinkMode.PER_MEETING)
    service = HostedMeetingService(store, tmp_path, clock=clock)
    upcoming = service.info["upcoming"]
    assert len(upcoming) == 8 and len({m["code"] for m in upcoming}) == 8
    assert service.info["code"] == upcoming[0]["code"]   # the next meeting's link
    assert service.info["lowOnCodes"] is False


def test_new_link_replaces_code_and_password_and_forgets_devices(store, tmp_path, clock):
    store.set_devices([_device()])
    service = HostedMeetingService(store, tmp_path, clock=clock)
    before = service.info
    assert before["devices"][0]["name"] == "Lima"
    service._info.state = MeetingState.LIVE
    assert service.new_link() is False
    service._info.state = MeetingState.IDLE
    assert service.new_link() is True
    after = service.info
    assert after["code"] != before["code"] and after["devices"] == [] and store.devices() == []
    assert before["code"] in store.links().retired


def test_changing_only_the_password_keeps_code_and_devices(store, tmp_path, clock):
    store.set_devices([_device()])
    service = HostedMeetingService(store, tmp_path, clock=clock)
    code = service.info["code"]
    assert service.set_password("x") is False
    assert service.set_password("rio2026") is True
    assert service.info["code"] == code and service.info["password"] == "rio2026" and store.devices()


def test_per_meeting_renewals(store, tmp_path, clock):
    store.set_link_mode(LinkMode.PER_MEETING)
    service = HostedMeetingService(store, tmp_path, clock=clock)
    first, second = service.info["upcoming"][:2]
    assert service.new_link(first["id"])
    assert service.info["upcoming"][0]["code"] != first["code"]
    # During a meeting its own link stays; the others may be renewed.
    service._info.state, service._info.meeting_id = MeetingState.LIVE, second["id"]
    assert service.new_link(second["id"]) is False
    assert service.renew_upcoming()
    codes = {m["id"]: m["code"] for m in service.info["upcoming"]}
    assert codes[second["id"]] == second["code"] and codes[first["id"]] != first["code"]


def test_schedule_edits(store, tmp_path, clock):
    service = HostedMeetingService(store, tmp_path, clock=clock)
    first = service.info["upcoming"][0]
    assert service.set_meeting_cancelled(first["id"], True)
    assert service.info["upcoming"][0]["cancelled"] is True
    meeting_id = service.add_meeting(NOW + timedelta(days=1, hours=8))
    assert any(m["id"] == meeting_id and m["kind"] == "extra" for m in service.info["upcoming"])
    assert service.move_meeting(meeting_id, NOW + timedelta(days=1, hours=9))
    assert service.remove_meeting(meeting_id)
    assert service.remove_meeting(first["id"]) is False   # weekly meetings are cancelled, not removed


def test_a_scheduled_meeting_opens_by_itself_once(store, tmp_path, clock, monkeypatch):
    from solin.core.hosted_meeting.settings import MeetingProvider

    store.set_provider(MeetingProvider.SOLIN)
    service = HostedMeetingService(store, tmp_path, clock=clock)
    started: list[bool] = []
    monkeypatch.setattr(HostedMeetingService, "available", staticmethod(lambda: True))
    monkeypatch.setattr(service, "start_meeting", lambda auto=False: started.append(auto) or True)
    service.check_schedule()
    assert started == []                                    # Monday noon: nothing open
    clock.now = datetime(2026, 11, 18, 19, 0, tzinfo=TZ)    # Wednesday, window opens
    service.check_schedule()
    assert started == [True]
    service._handled.add("midweek:2026-11-18")
    service.check_schedule()
    assert started == [True]                                # not reopened after it was handled


def test_unscheduled_meeting_in_per_meeting_mode_gets_its_own_link(store, tmp_path, clock):
    store.set_link_mode(LinkMode.PER_MEETING)
    service = HostedMeetingService(store, tmp_path, clock=clock)
    meeting, code, password = service._choose_meeting()
    assert meeting.kind == "unscheduled" and code and password
    assert code not in {m["code"] for m in service.info["upcoming"] if m["kind"] != "unscheduled"}


def test_an_upcoming_meeting_can_be_opened_early_with_its_own_link(store, tmp_path, clock, monkeypatch):
    store.set_link_mode(LinkMode.PER_MEETING)
    service = HostedMeetingService(store, tmp_path, clock=clock)
    upcoming = [m for m in service.info["upcoming"] if not m["cancelled"]]
    later = upcoming[1]                                      # not today's: days ahead
    meeting, code, password = service._choose_meeting(later["id"])
    assert (meeting.id, code, password) == (later["id"], later["code"], later["password"])
    assert len(service.info["upcoming"]) == len(upcoming)    # no unscheduled meeting added
    # Unknown or cancelled meetings are not opened.
    workers: list[str] = []
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: workers.append(k.get("name")))
    assert service.start_meeting("extra:nope") is False
    assert service.set_meeting_cancelled(later["id"], True)
    assert service.start_meeting(later["id"]) is False
    assert workers == []
