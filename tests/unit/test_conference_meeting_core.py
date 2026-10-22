from __future__ import annotations

import pytest

from solin.core.conference.meeting_core import (
    ClearHand,
    MeetingCore,
    MeetingPolicy,
    ParticipantStatus,
    RemoveParticipant,
    SetName,
    SetPermission,
    lobby_permission,
)
from solin.core.conference.room_api import ParticipantInfo, Permission, TrackInfo, TrackSource

LISTENER = Permission(can_subscribe=True, can_update_metadata=True)
MICROPHONE = Permission(can_subscribe=True, can_publish=True,
                        can_publish_sources=(TrackSource.MICROPHONE,), can_update_metadata=True)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _p(identity, permission=None, *, name="", joined=0, live=False, **attributes):
    tracks = (TrackInfo("TR", "AUDIO", "MICROPHONE", False),) if live else ()
    return ParticipantInfo(identity=identity, name=name or identity, joined_at_ms=joined,
                           permission=permission or lobby_permission(),
                           attributes=attributes, tracks=tracks)


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def core(clock):
    return MeetingCore(clock=clock)


def test_new_attendees_wait_in_the_lobby_and_others_are_ignored(core):
    assert core.observe([_p("att:a"), _p("hall", Permission()), _p("stranger", Permission())]) == []
    (waiting,) = core.participants()
    assert waiting.status is ParticipantStatus.LOBBY


def test_an_attendee_with_wrong_permissions_is_put_back_in_the_lobby(core):
    assert core.observe([_p("att:a", MICROPHONE)]) == [SetPermission("att:a", lobby_permission())]


def test_admitting_grants_listening_and_the_microphone_while_unlocked(core):
    core.observe([_p("att:a"), _p("att:b")])
    assert core.admit("att:a") == [SetPermission("att:a", MICROPHONE)]
    assert core.admit_all() == [SetPermission("att:b", MICROPHONE)]


def test_locking_takes_every_microphone_and_calling_on_gives_one_back(core, clock):
    core.observe([_p("att:a"), _p("att:b")])
    core.admit_all()
    assert set(core.set_locked(True)) == {SetPermission("att:a", LISTENER), SetPermission("att:b", LISTENER)}
    clock.now += 10
    core.observe([_p("att:a", LISTENER), _p("att:b", LISTENER)])
    assert core.call_on("att:a") == [SetPermission("att:a", MICROPHONE)]
    assert core.participants()[0].status is ParticipantStatus.SPEAKING
    assert core.call_on("att:nobody") == []


def test_a_rejoining_admitted_device_is_readmitted_automatically(core):
    core.observe([_p("att:a")])
    core.admit("att:a")
    core.observe([])  # the hall's network blinked; everyone dropped
    assert core.observe([_p("att:a")]) == [SetPermission("att:a", MICROPHONE)]


def test_unchanged_actions_are_not_resent_until_the_server_had_time(core, clock):
    core.observe([_p("att:a")])
    assert core.admit("att:a")
    assert core.observe([_p("att:a")]) == []  # still in flight
    clock.now += 5
    assert core.observe([_p("att:a")]) == [SetPermission("att:a", MICROPHONE)]


def test_hands_queue_in_the_order_solin_saw_them(core):
    core.observe([_p("att:a"), _p("att:b"), _p("att:c")])
    core.admit_all()
    core.observe([_p("att:a", MICROPHONE), _p("att:b", MICROPHONE, hand="9"), _p("att:c", MICROPHONE)])
    core.observe([_p("att:a", MICROPHONE, hand="1"), _p("att:b", MICROPHONE, hand="9"),
                  _p("att:c", MICROPHONE)])
    assert [p.identity for p in core.hands()] == ["att:b", "att:a"]
    # Raising again (new value) goes to the back of the queue.
    core.observe([_p("att:a", MICROPHONE, hand="1"), _p("att:b", MICROPHONE, hand="10"),
                  _p("att:c", MICROPHONE)])
    assert [p.identity for p in core.hands()] == ["att:a", "att:b"]


def test_lobby_hands_are_not_in_the_queue(core):
    core.observe([_p("att:a", hand="1")])
    assert core.hands() == []


def test_finishing_a_comment_mutes_and_lowers_the_hand(core, clock):
    core.observe([_p("att:a")])
    core.admit("att:a")
    core.set_locked(True)
    core.call_on("att:a")
    clock.now += 10
    core.observe([_p("att:a", MICROPHONE, hand="5", live=True)])
    assert core.participants()[0].microphone_live
    assert core.finish("att:a") == [ClearHand("att:a"), SetPermission("att:a", LISTENER)]
    assert core.hands() == []


def test_lower_hand_only_when_raised(core):
    core.observe([_p("att:a", hand="1")])
    assert core.lower_hand("att:a") == [ClearHand("att:a")]
    assert core.lower_hand("att:a") == []


def test_back_to_lobby_drops_the_readmit(core, clock):
    core.observe([_p("att:a")])
    core.admit("att:a")
    core.observe([_p("att:a", MICROPHONE)])
    assert core.to_lobby("att:a") == [SetPermission("att:a", lobby_permission())]
    core.observe([])
    assert core.observe([_p("att:a")]) == []


def test_removed_identities_are_put_out_and_refused(core):
    core.observe([_p("att:a")])
    assert core.may_join("att:a") and not core.may_join("hall")
    assert core.remove("att:a") == [RemoveParticipant("att:a")]
    assert not core.may_join("att:a")
    assert core.participants() == []
    assert core.admit("att:a") == []
    assert core.remove("hall") == []


def test_renames_are_kept_against_self_renames(core, clock):
    core.observe([_p("att:a", name="anon")])
    assert core.rename("att:a", "  Ann B.  ") == [SetName("att:a", "Ann B.")]
    clock.now += 10
    assert core.observe([_p("att:a", name="anon again")]) == [SetName("att:a", "Ann B.")]
    assert core.participants()[0].name == "Ann B."


def test_attendance_sums_admitted_devices_and_clamps_bad_values(core):
    core.observe([_p("att:a", attendance="3"), _p("att:b", attendance="x"),
                  _p("att:c", attendance="999"), _p("att:d", attendance="4")])
    for identity in ("att:a", "att:b", "att:c"):
        core.admit(identity)
    assert core.attendance() == 3 + 1 + 50


def test_unlocking_ends_called_on_comments(core, clock):
    core.observe([_p("att:a")])
    core.admit("att:a")
    core.set_locked(True)
    core.call_on("att:a")
    core.set_locked(False)
    assert core.participants()[0].status is ParticipantStatus.ADMITTED


def test_attendee_video_is_a_policy_and_only_while_unlocked(clock):
    core = MeetingCore(policy=MeetingPolicy(attendee_video=True), clock=clock)
    core.observe([_p("att:a")])
    (action,) = core.admit("att:a")
    assert action.permission.can_publish_sources == (TrackSource.CAMERA, TrackSource.MICROPHONE)
    clock.now += 10
    core.set_locked(True)
    core.call_on("att:a")
    clock.now += 10
    (action,) = core.observe([_p("att:a")])
    assert action.permission.can_publish_sources == (TrackSource.MICROPHONE,)
