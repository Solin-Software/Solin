from __future__ import annotations

from typing import Any

import pytest

from solin.core.hosted_meeting.galene_client import GaleneUser
from solin.core.hosted_meeting.meeting_core import JoinStatus, MeetingCore

SECRET = "s" * 32


class Effects:
    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.actions: list[tuple[str, str]] = []
        self.changes = 0

    def notify(self, device_id: str, message: dict[str, Any]) -> None:
        self.notes.append((device_id, message))

    def present(self, galene_id: str) -> None:
        self.actions.append(("present", galene_id))

    def unpresent(self, galene_id: str) -> None:
        self.actions.append(("unpresent", galene_id))

    def kick(self, galene_id: str, reason: str) -> None:
        self.actions.append(("kick", galene_id))

    def changed(self) -> None:
        self.changes += 1


@pytest.fixture
def core():
    effects = Effects()
    tick = iter(range(1, 10_000))
    meeting = MeetingCore(effects, lambda name, perms: f"token:{name}:{','.join(perms)}", clock=lambda: float(next(tick)))
    meeting.fx = effects  # type: ignore[attr-defined]
    return meeting


def admit(core: MeetingCore, device: str, name: str = "Ana") -> None:
    assert core.request_join(device, SECRET, name).status is JoinStatus.LOBBY
    assert core.admit(device)


def connect(core: MeetingCore, device: str, galene_id: str, *, present: bool = False, **data: Any) -> GaleneUser:
    user = GaleneUser(id=galene_id, username="x", permissions=("present",) if present else (), data={"device": device, **data})
    core.on_galene_user("add", user)
    return user


def test_join_waits_in_lobby_until_admitted(core):
    assert core.request_join("d1", SECRET, "  Ana   Souza ").status is JoinStatus.LOBBY
    assert core.snapshot()["lobby"][0]["name"] == "Ana Souza"
    assert core.token_for("d1", SECRET).status is JoinStatus.LOBBY
    assert core.admit("d1")
    assert core.fx.notes[-1] == ("d1", {"type": "admitted", "token": "token:Ana Souza:", "name": "Ana Souza"})
    assert core.token_for("d1", SECRET).token == "token:Ana Souza:"


def test_admitted_device_is_readmitted_on_return(core):
    admit(core, "d1")
    decision = core.request_join("d1", SECRET, "Ana")
    assert decision.status is JoinStatus.ADMITTED and decision.token


def test_wrong_secret_and_bad_input_are_refused(core):
    admit(core, "d1")
    assert core.request_join("d1", "x" * 32, "Eve").status is JoinStatus.REFUSED
    assert core.token_for("d1", "x" * 32).status is JoinStatus.REFUSED
    assert core.request_join("d2", "short", "Eve").status is JoinStatus.REFUSED
    assert core.request_join("d3", SECRET, "   ").status is JoinStatus.REFUSED


def test_admit_all(core):
    for device in ("a", "b", "c"):
        core.request_join(device, SECRET, device)
    assert core.admit_all() == 3
    assert core.snapshot()["lobby"] == [] and len(core.snapshot()["attendees"]) == 3


def test_locked_mics_unpresent_a_client_that_sneaks_present(core):
    admit(core, "d1")
    connect(core, "d1", "g1", present=True)
    assert ("unpresent", "g1") in core.fx.actions


def test_call_on_opens_one_microphone_at_a_time(core):
    admit(core, "d1", "Ana")
    admit(core, "d2", "Bia")
    connect(core, "d1", "g1", raisehand=True)
    connect(core, "d2", "g2", raisehand=True)
    assert [h["name"] for h in core.snapshot()["hands"]] == ["Ana", "Bia"]
    assert core.call_on("d1")
    assert ("present", "g1") in core.fx.actions
    assert core.snapshot()["live"] == "d1"
    assert [h["name"] for h in core.snapshot()["hands"]] == ["Bia"]  # calling on lowers the hand
    assert core.call_on("d2")
    assert ("unpresent", "g1") in core.fx.actions and ("present", "g2") in core.fx.actions
    assert core.done("d2")
    assert core.fx.actions[-1] == ("unpresent", "g2")
    assert core.snapshot()["live"] == ""


def test_cannot_call_on_someone_not_connected(core):
    admit(core, "d1")
    assert not core.call_on("d1")


def test_unlocked_mics_let_everyone_present(core):
    admit(core, "d1")
    connect(core, "d1", "g1")
    core.lock_mics(False)
    assert ("present", "g1") in core.fx.actions
    admit(core, "d2")
    connect(core, "d2", "g2")
    assert ("present", "g2") in core.fx.actions
    core.lock_mics(True)
    assert ("unpresent", "g1") in core.fx.actions and ("unpresent", "g2") in core.fx.actions


def test_part_holder_keeps_present_when_locked(core):
    admit(core, "d1")
    connect(core, "d1", "g1")
    assert core.set_part_holder("d1", True)
    assert ("present", "g1") in core.fx.actions
    assert core.token_for("d1", SECRET).token.endswith("present,message")
    core.lock_mics(False)
    core.lock_mics(True)
    assert ("unpresent", "g1") not in core.fx.actions


def test_remove_kicks_and_refuses_new_tokens(core):
    admit(core, "d1")
    connect(core, "d1", "g1")
    assert core.remove("d1")
    assert ("kick", "g1") in core.fx.actions
    assert core.token_for("d1", SECRET).status is JoinStatus.REFUSED
    assert core.request_join("d1", SECRET, "Ana").status is JoinStatus.REFUSED
    connect(core, "d1", "g9")  # an old token used again
    assert ("kick", "g9") in core.fx.actions


def test_rename_sends_a_token_in_the_new_name(core):
    admit(core, "d1", "Ana")
    assert core.rename("d1", "Ana e Bia Souza")
    assert core.fx.notes[-1] == ("d1", {"type": "renamed", "name": "Ana e Bia Souza", "token": "token:Ana e Bia Souza:"})


def test_hand_and_disconnect_tracking(core):
    admit(core, "d1")
    user = connect(core, "d1", "g1", raisehand=True)
    assert core.snapshot()["hands"]
    core.lower_hand("d1")
    assert core.fx.notes[-1] == ("d1", {"type": "hand_lowered"})
    core.call_on("d1")
    core.on_galene_user("delete", user)
    snapshot = core.snapshot()
    assert snapshot["attendees"][0]["connected"] is False and snapshot["live"] == ""
    assert snapshot["attendance"] == 1


def test_end_notifies_everyone_and_blocks_joins(core):
    admit(core, "d1")
    core.request_join("d2", SECRET, "Bia")
    core.end()
    assert {note[0] for note in core.fx.notes if note[1] == {"type": "ended"}} == {"d1", "d2"}
    assert core.request_join("d3", SECRET, "Caio").status is JoinStatus.ENDED
    assert core.token_for("d1", SECRET).status is JoinStatus.ENDED


def test_stage_view(core):
    admit(core, "d1", "Ana")
    admit(core, "d2", "Bia")
    connect(core, "d1", "g1", raisehand=True)
    connect(core, "d2", "g2")
    core.call_on("d2")
    assert core.stage_view() == {"hands": ["Ana"], "live": "Bia", "ended": False}


def test_attendance_report_lists_everyone_admitted(core):
    admit(core, "d1", "Ana")
    admit(core, "d2", "Bia")
    core.request_join("d3", SECRET, "Caio")  # never admitted
    core.remove("d2")
    report = core.attendance_report()
    assert [row["name"] for row in report] == ["Ana", "Bia"]
    assert [row["removed"] for row in report] == [False, True]
