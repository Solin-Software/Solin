"""The links the console offers before and during a meeting, in both link modes."""

from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.hosted_meeting.devices import ApprovedDevice, KeyKind, StaffRole
from solin.core.hosted_meeting.links import LinkMode, normalize_code
from solin.core.hosted_meeting.service import HostedMeetingService, MeetingState
from solin.core.hosted_meeting.settings import HUB_CODE_PREFIX, HostedMeetingSettingsStore

TZ = timezone(timedelta(hours=-3))
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=TZ)   # Monday


@pytest.fixture
def store():
    settings = SettingsStore.for_namespace("SolinTests", f"hosted-meeting-link-{secrets.token_hex(4)}")
    store = HostedMeetingSettingsStore(settings)
    store.set_hall_name("Salão Central")
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


def _hub_code() -> str:
    fields = {"domain": "meet.solin.run", "frp_addr": "meet.solin.run", "frp_port": 7000, "frp_token": "t",
              "hall": "hall-x", "hall_key": "k"}
    return HUB_CODE_PREFIX + base64.urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip("=")


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

    store.set_hub_code(_hub_code())
    service.refresh_links()
    assert service.info["invitationUrl"] == f"https://hall-x.meet.solin.run/{code}"
    assert HostedMeetingService(store, tmp_path, clock=clock).info["code"] == code  # stored


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
