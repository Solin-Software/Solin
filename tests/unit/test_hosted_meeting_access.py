"""Meeting access rules: password, attendance count, stage/moderator approval, moderator power."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from solin.core.hosted_meeting.devices import DeviceRegistry, challenge_message
from solin.core.hosted_meeting.galene_client import GaleneUser
from solin.core.hosted_meeting.meeting_core import (
    RECONNECT_GRACE_S,
    JoinStatus,
    MeetingCore,
    StaffStatus,
)

SECRET = "s" * 32
STAFF_SECRET = "t" * 32
PASSWORD = "482913"


class Effects:
    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.staff_notes: list[tuple[str, dict[str, Any]]] = []
        self.actions: list[tuple[str, str]] = []
        self.devices: list[list[dict[str, Any]]] = []

    def notify(self, device_id, message):
        self.notes.append((device_id, message))

    def notify_staff(self, device_id, message):
        self.staff_notes.append((device_id, message))

    def present(self, galene_id):
        self.actions.append(("present", galene_id))

    def unpresent(self, galene_id):
        self.actions.append(("unpresent", galene_id))

    def kick(self, galene_id, reason):
        self.actions.append(("kick", galene_id))

    def changed(self):
        pass

    def devices_changed(self, devices):
        self.devices.append(devices)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def core(clock):
    meeting = MeetingCore(Effects(), lambda name, perms: f"token:{name}", clock=clock, password=PASSWORD)
    meeting.fx = meeting._effects  # type: ignore[attr-defined]
    return meeting


def join(core, device, name="Ana", count=1, password=PASSWORD):
    return core.request_join(device, SECRET, name, password=password, count=count)


def connect(core, device, galene_id):
    core.on_galene_user("add", GaleneUser(id=galene_id, username="x", data={"device": device}))


# ── password and count ───────────────────────────────────────────────────


def test_attendees_need_the_password(core):
    assert join(core, "d1", password="000000").reason == "password"
    assert join(core, "d1", password="").reason == "password"
    assert join(core, "d1", password=" 482913 ").status is JoinStatus.LOBBY


@pytest.mark.parametrize("count", [None, -1, 21, "x", True, 2.5])
def test_the_count_is_required_and_bounded(core, count):
    decision = join(core, "d1", count=count)
    if count == 2.5:  # a number is truncated, like the page's number field
        assert decision.status is JoinStatus.LOBBY
    else:
        assert decision.reason == "count required"


def test_already_counted_is_zero_and_the_count_never_changes(core):
    assert join(core, "d1", count=0).status is JoinStatus.LOBBY
    assert join(core, "d1", count=5).status is JoinStatus.LOBBY  # the page reloaded
    assert core.snapshot()["lobby"][0]["count"] == 0


def test_a_waiting_or_admitted_device_returns_without_the_password(core):
    join(core, "d1")
    assert core.request_join("d1", SECRET, "Ana").status is JoinStatus.LOBBY
    core.admit("d1")
    assert core.request_join("d1", SECRET, "Ana").status is JoinStatus.ADMITTED


def test_a_new_password_keeps_people_in(core):
    join(core, "d1")
    core.admit("d1")
    core.set_password("777777")
    assert core.request_join("d1", SECRET, "Ana").status is JoinStatus.ADMITTED
    assert join(core, "d2").reason == "password"
    assert join(core, "d2", password="777777").status is JoinStatus.LOBBY


def test_totals_count_people_and_keep_dropped_connections_for_a_while(core, clock):
    for device, count in (("d1", 3), ("d2", 1), ("d3", 0)):
        join(core, device, name=device, count=count)
        core.admit(device)
        connect(core, device, f"g-{device}")
    join(core, "d4", name="waiting", count=4)  # in the lobby: not counted
    assert core.totals() == {"people": 4, "devices": 3}
    core.on_galene_user("delete", GaleneUser(id="g-d1", username="x", data={}))
    clock.now += RECONNECT_GRACE_S - 1
    assert core.totals() == {"people": 4, "devices": 3}
    clock.now += 2
    assert core.totals() == {"people": 1, "devices": 2}


def test_the_official_count_reaches_moderators(core):
    join(core, "d1", count=2)
    core.admit("d1")
    registry_moderator(core, "m1")
    count = core.take_official_count("2026-11-18T21:00-03:00")
    assert count == {"people": 2, "devices": 1, "at": "2026-11-18T21:00-03:00"}
    assert ("m1", {"type": "official_count", **count}) in core.fx.staff_notes
    assert core.snapshot()["officialCount"] == count


def test_admitted_people_survive_a_restart(core, clock):
    join(core, "d1", count=3)
    core.admit("d1")
    rows = core.export_admitted()
    restarted = MeetingCore(Effects(), lambda name, perms: f"token:{name}", clock=clock, password=PASSWORD)
    assert restarted.restore_admitted(rows) == 1
    assert restarted.request_join("d1", SECRET, "Ana").status is JoinStatus.ADMITTED
    assert restarted.totals() == {"people": 3, "devices": 1}  # counted while they reconnect
    assert restarted.restore_admitted([{"device": "x", "secret": "short", "name": "X", "count": 1}]) == 0


# ── stage devices and moderators ─────────────────────────────────────────


def registry_moderator(core, device_id, *, network="198.51.100.0/24"):
    """A moderator that logged in with a secret, was approved and is online."""

    assert core.staff_hello(device_id, "moderator", "", network).status is StaffStatus.LOGIN
    assert core.staff_login(device_id, "moderator", "Irmão Lima", PASSWORD, key_kind="secret",
                            key=STAFF_SECRET, network=network).status is StaffStatus.PENDING
    core.staff_socket(device_id, True)
    assert core.approve_staff(device_id)
    return device_id


def test_staff_need_the_password_and_the_operators_approval(core):
    assert core.staff_hello("s1", "stage", "", "n").status is StaffStatus.LOGIN
    assert core.staff_login("s1", "stage", "Tablet", "bad", key_kind="secret", key=STAFF_SECRET,
                            network="n").reason == "password"
    assert core.staff_login("s1", "stage", "Tablet", PASSWORD, key_kind="secret", key=STAFF_SECRET,
                            network="n").status is StaffStatus.PENDING
    core.staff_socket("s1", True)
    assert core.snapshot()["staffRequests"] == [{"device": "s1", "name": "Tablet", "role": "stage"}]
    assert core.staff_role("s1") is None  # nothing before approval
    assert core.approve_staff("s1")
    assert core.fx.staff_notes[-1] == ("s1", {"type": "ready"})
    assert core.staff_role("s1").value == "stage"
    assert core.snapshot()["staff"] == [{"device": "s1", "name": "Tablet", "role": "stage", "networkChanged": False}]
    assert core.fx.devices[-1][0]["device_id"] == "s1"


def test_a_remembered_device_skips_password_and_approval(core, clock):
    registry_moderator(core, "m1")
    devices = DeviceRegistry.from_json(core.devices.to_json())
    later = MeetingCore(Effects(), lambda n, p: "t", clock=clock, password="other", devices=devices)
    assert later.staff_hello("m1", "moderator", STAFF_SECRET, "198.51.100.0/24").status is StaffStatus.READY
    assert later.staff_hello("m1", "moderator", "wrong" * 8, "x").status is StaffStatus.LOGIN
    assert later.staff_hello("m1", "stage", STAFF_SECRET, "x").status is StaffStatus.LOGIN  # other role


def test_a_moderator_on_a_new_network_is_flagged(core, clock):
    registry_moderator(core, "m1", network="198.51.100.0/24")
    core.staff_socket("m1", False)
    assert core.staff_hello("m1", "moderator", STAFF_SECRET, "203.0.113.0/24").status is StaffStatus.READY
    core.staff_socket("m1", True)
    assert core.snapshot()["staff"][0]["networkChanged"] is True


def test_refused_and_removed_devices(core):
    core.staff_login("s1", "stage", "Tablet", PASSWORD, key_kind="secret", key=STAFF_SECRET, network="n")
    assert core.refuse_staff("s1")
    assert core.fx.staff_notes[-1] == ("s1", {"type": "refused"})
    registry_moderator(core, "m1")
    assert core.remove_device("m1")
    assert core.fx.staff_notes[-1] == ("m1", {"type": "removed"})
    assert core.devices.get("m1", core._clock()) is None
    assert core.moderator_command("m1", "admit_all", []) is False


def test_new_link_forgets_every_device(core):
    registry_moderator(core, "m1")
    core.forget_all_devices()
    assert core.devices.to_json() == [] and core.staff == {}
    assert ("m1", {"type": "removed"}) in core.fx.staff_notes


def test_moderators_have_the_operators_power_over_people_only(core):
    registry_moderator(core, "m1")
    join(core, "d1")
    assert core.moderator_command("m1", "admit", ["d1"])
    connect(core, "d1", "g1")
    assert core.moderator_command("m1", "call_on", ["d1"])
    assert core.moderator_command("m1", "done", ["d1"])
    assert core.moderator_command("m1", "lock_mics", [False]) and core.mics_locked is False
    assert core.moderator_command("m1", "remove", ["d1"])
    for forbidden in ("end", "rename", "set_part_holder", "approve_staff", "forget_all_devices", "take_official_count"):
        assert core.moderator_command("m1", forbidden, ["d1"]) is False
    assert core.ended is False


def test_stage_devices_cannot_moderate(core):
    core.staff_login("s1", "stage", "Tablet", PASSWORD, key_kind="secret", key=STAFF_SECRET, network="n")
    core.approve_staff("s1")
    join(core, "d1")
    assert core.moderator_command("s1", "admit", ["d1"]) is False


def _signature(private_key: ec.EllipticCurvePrivateKey, challenge: str) -> str:
    """What WebCrypto produces: raw r||s."""

    der = private_key.sign(challenge_message(challenge), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


def _spki(private_key: ec.EllipticCurvePrivateKey) -> str:
    raw = private_key.public_key().public_bytes(serialization.Encoding.DER,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(raw).decode()


def test_a_moderator_device_key_must_sign_every_visit(core, clock):
    key = ec.generate_private_key(ec.SECP256R1())
    assert core.staff_login("m1", "moderator", "Lima", PASSWORD, key_kind="ecdsa", key=_spki(key),
                            network="n").status is StaffStatus.PENDING
    core.staff_socket("m1", True)
    assert core.approve_staff("m1")
    challenge = core.fx.staff_notes[-1][1]["challenge"]
    assert core.staff_role("m1") is None  # approved, but the key is not proven yet
    assert core.staff_proof("m1", _signature(key, challenge)).status is StaffStatus.READY
    assert core.staff_role("m1").value == "moderator"

    # Next visit: a fresh challenge; another device's key (a copied identity) fails.
    core.staff_socket("m1", False)
    decision = core.staff_hello("m1", "moderator", "", "n")
    assert decision.status is StaffStatus.CHALLENGE
    impostor = ec.generate_private_key(ec.SECP256R1())
    assert core.staff_proof("m1", _signature(impostor, decision.challenge)).status is StaffStatus.LOGIN
    decision = core.staff_hello("m1", "moderator", "", "n")
    assert core.staff_proof("m1", _signature(key, "a-different-challenge")).status is StaffStatus.LOGIN
    decision = core.staff_hello("m1", "moderator", "", "n")
    assert core.staff_proof("m1", _signature(key, decision.challenge)).status is StaffStatus.READY


def test_bad_keys_are_refused(core):
    rsa_like = base64.b64encode(b"not a key").decode()
    assert core.staff_login("m1", "moderator", "Lima", PASSWORD, key_kind="ecdsa", key=rsa_like,
                            network="n").reason == "invalid key"
    assert core.staff_login("m1", "moderator", "Lima", PASSWORD, key_kind="secret", key="short",
                            network="n").reason == "invalid key"
