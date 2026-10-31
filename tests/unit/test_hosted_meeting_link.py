"""Each hall has one permanent link, available before any meeting starts."""

from __future__ import annotations

import base64
import json
import secrets

import pytest

from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.hosted_meeting.meeting_core import MeetingCore
from solin.core.hosted_meeting.service import HostedMeetingService, MeetingState
from solin.core.hosted_meeting.settings import HUB_CODE_PREFIX, HostedMeetingSettingsStore


@pytest.fixture
def store():
    settings = SettingsStore.for_namespace("SolinTests", f"hosted-meeting-link-{secrets.token_hex(4)}")
    store = HostedMeetingSettingsStore(settings)
    store.set_hall_name("Salão Central")
    yield store
    for key in settings.all_keys():
        settings.set_value(key, None)


def _hub_code() -> str:
    fields = {"domain": "hub.example", "frp_addr": "hub.example", "frp_port": 7000, "frp_token": "t",
              "hall": "hall-x", "hall_key": "k"}
    return HUB_CODE_PREFIX + base64.urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip("=")


def test_link_is_created_once_and_survives_a_hall_rename(store):
    slug, stage_key = store.meeting_link()
    assert slug.startswith("salao-central-")
    store.set_hall_name("Outro nome")
    assert store.meeting_link() == (slug, stage_key)


def test_new_link_replaces_both_parts(store):
    slug, stage_key = store.meeting_link()
    new_slug, new_key = store.new_meeting_link()
    assert new_slug != slug and new_key != stage_key
    assert store.meeting_link() == (new_slug, new_key)


@pytest.mark.parametrize("stored", ["not json", '{"slug": "../etc", "stage_key": "k"}', '{"slug": 5}', "[]"])
def test_a_damaged_link_is_replaced(store, stored):
    store.settings.set_value(SettingsKey.HOSTED_MEETING_LINK, stored)
    slug, stage_key = store.meeting_link()
    assert slug.startswith("salao-central-") and len(stage_key) >= 16
    assert store.meeting_link() == (slug, stage_key)


def test_links_are_shown_before_a_meeting_and_follow_the_hub(store, tmp_path):
    service = HostedMeetingService(store, tmp_path)
    slug, stage_key = store.meeting_link()
    info = service.info
    assert info["state"] == "idle" and info["linkScope"] == "lan"
    assert info["invitationUrl"].endswith(f":{store.web_port()}/j/{slug}")

    store.set_hub_code(_hub_code())
    service.refresh_links()
    info = service.info
    assert info["linkScope"] == "hub"
    assert info["invitationUrl"] == f"https://hall-x.hub.example/j/{slug}"
    assert info["stageUrl"] == f"https://hall-x.hub.example/stage/{slug}#{stage_key}"


def test_new_link_is_refused_during_a_meeting(store, tmp_path):
    service = HostedMeetingService(store, tmp_path)
    link = store.meeting_link()
    service._info.state = MeetingState.LIVE
    assert service.new_link() is False
    assert store.meeting_link() == link
    service._info.state = MeetingState.IDLE
    assert service.new_link() is True
    assert store.meeting_link() != link
    assert store.meeting_link()[0] in service.info["invitationUrl"]


def test_core_uses_the_permanent_stage_key():
    assert MeetingCore(None, None, stage_key="fixed-stage-key").stage_key == "fixed-stage-key"
    assert MeetingCore(None, None).stage_key
