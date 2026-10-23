"""S9: can LiveKit's built-in primitives cover hybrid-meeting moderation?

Spike code. Simulated attendees (Python rtc clients) join a local dev server;
an "operator" uses the server API (livekit-api) the way Solin would. Each check
prints PASS/FAIL plus what was observed.

    docker run --rm --network host livekit/livekit-server --dev --bind 0.0.0.0
    .venv/bin/python s9_moderation/moderation_test.py
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import numpy as np
from livekit import api, rtc

URL, HTTP_URL, KEY, SECRET = "ws://127.0.0.1:7880", "http://127.0.0.1:7880", "devkey", "secret"
ROOM = f"s9-{int(time.time())}"
MIC = api.TrackSource.MICROPHONE
CAMERA = api.TrackSource.CAMERA

LOBBY = dict(can_subscribe=False, can_publish=False, can_publish_data=False, can_update_metadata=True)
ADMITTED_LOCKED = dict(can_subscribe=True, can_publish=False, can_publish_data=True, can_update_metadata=True)
SPEAKING = dict(can_subscribe=True, can_publish=True, can_publish_sources=[MIC], can_publish_data=True,
                can_update_metadata=True)

results: list[dict] = []


def check(name: str, ok: bool, observed: str) -> None:
    results.append({"check": name, "ok": ok, "observed": observed})
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {observed}", flush=True)


async def until(pred, timeout: float = 5.0, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()


def token(identity: str, name: str, *, hidden: bool = False, **grants) -> str:
    return (
        api.AccessToken(KEY, SECRET).with_identity(identity).with_name(name)
        .with_grants(api.VideoGrants(room_join=True, room=ROOM, hidden=hidden,
                                     can_update_own_metadata=grants.pop("can_update_own_metadata", True), **grants))
        .to_jwt()
    )


@dataclass
class Client:
    identity: str
    room: rtc.Room = field(default_factory=rtc.Room)
    events: list[tuple[str, str]] = field(default_factory=list)
    mic_track: rtc.LocalAudioTrack | None = None
    mic_pub_sid: str = ""
    auto_publish_mic: bool = True

    def __post_init__(self) -> None:
        r = self.room
        r.on("track_subscribed", lambda t, p, rp: self.events.append(("subscribed", f"{rp.identity}:{t.kind}")))
        r.on("track_unsubscribed", lambda t, p, rp: self.events.append(("unsubscribed", rp.identity)))
        r.on("track_published", lambda p, rp: self.events.append(("remote_published", rp.identity)))
        r.on("track_unpublished", lambda p, rp: self.events.append(("remote_unpublished", rp.identity)))
        r.on("track_muted", lambda rp, p: self.events.append(("remote_muted", rp.identity)))
        r.on("track_unmuted", lambda rp, p: self.events.append(("remote_unmuted", rp.identity)))
        r.on("local_track_unpublished", self._on_local_unpublished)
        r.on("participant_connected", lambda rp: self.events.append(("joined", rp.identity)))
        r.on("participant_disconnected", lambda rp: self.events.append(("left", rp.identity)))
        r.on("participant_name_changed", lambda p, old, new: self.events.append(("name", f"{p.identity}={new}")))
        r.on("participant_attributes_changed",
             lambda changed, p: self.events.append(("attrs", f"{p.identity}:{json.dumps(dict(changed))}")))
        r.on("disconnected", lambda reason: self.events.append(("disconnected", str(reason))))
        r.on("participant_permissions_changed", self._on_permissions)

    def _on_local_unpublished(self, publication) -> None:
        self.events.append(("local_unpublished", publication.sid))
        if publication.sid == self.mic_pub_sid:
            self.mic_pub_sid = ""   # the app republishes when the mic is granted again

    def saw(self, kind: str, contains: str = "") -> bool:
        return any(k == kind and contains in v for k, v in self.events)

    def _on_permissions(self, participant, perm) -> None:
        if participant.identity != self.identity:
            return
        self.events.append(("permissions", f"publish={perm.can_publish} sources={list(perm.can_publish_sources)} "
                                           f"subscribe={perm.can_subscribe} hidden={perm.hidden}"))
        mic_allowed = perm.can_publish and (not perm.can_publish_sources or MIC in perm.can_publish_sources)
        if self.auto_publish_mic and mic_allowed and not self.mic_pub_sid:
            asyncio.ensure_future(self.publish_mic())

    async def connect(self, tok: str) -> None:
        await self.room.connect(URL, tok, options=rtc.RoomOptions(auto_subscribe=True))

    async def publish_mic(self, timeout: float = 5.0) -> str:
        """Publish a sine 'microphone'. Returns '' on success or the error text."""
        src = rtc.AudioSource(48000, 1)
        self.mic_track = rtc.LocalAudioTrack.create_audio_track("mic", src)
        try:
            pub = await asyncio.wait_for(self.room.local_participant.publish_track(
                self.mic_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)), timeout)
        except Exception as exc:  # noqa: BLE001 - we want to observe the failure mode
            return f"{type(exc).__name__}: {exc}" or type(exc).__name__
        self.mic_pub_sid = pub.sid
        tone = (np.sin(np.arange(480) / 48000 * 2 * np.pi * 440) * 8000).astype(np.int16).tobytes()

        async def feed():
            while self.mic_pub_sid == pub.sid:
                await src.capture_frame(rtc.AudioFrame(tone, 48000, 1, 480))
        asyncio.ensure_future(feed())
        return ""

    async def publish_camera(self) -> str:
        src = rtc.VideoSource(320, 240)
        track = rtc.LocalVideoTrack.create_video_track("cam", src)
        try:
            await asyncio.wait_for(self.room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA)), 5)
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"
        frame = rtc.VideoFrame(320, 240, rtc.VideoBufferType.I420, bytes(320 * 240 * 3 // 2))

        async def feed():
            while self.room.isconnected():
                src.capture_frame(frame)
                await asyncio.sleep(1 / 15)
        asyncio.ensure_future(feed())
        return ""

    def remote(self, identity: str):
        return self.room.remote_participants.get(identity)


async def main() -> None:
    lk = api.LiveKitAPI(HTTP_URL, KEY, SECRET)
    room_svc = lk.room

    async def set_perm(identity: str, **perm) -> None:
        await room_svc.update_participant(api.UpdateParticipantRequest(
            room=ROOM, identity=identity, permission=api.ParticipantPermission(**perm)))

    async def participant_info(identity: str):
        return await room_svc.get_participant(api.RoomParticipantIdentity(room=ROOM, identity=identity))

    # --- hall publishes the program (audio only is enough here)
    hall = Client("hall", auto_publish_mic=False)
    await hall.connect(token("hall", "Kingdom Hall"))
    assert await hall.publish_mic() == ""

    # --- T1 lobby: can't hear, can't publish
    a = Client("att-a")
    await a.connect(token("att-a", "Silva", can_subscribe=False, can_publish=False, can_publish_data=False))
    await asyncio.sleep(2)
    check("T1 lobby: no program audio", not a.saw("subscribed"),
          f"subscribed events={[e for e in a.events if e[0] == 'subscribed']}")
    err = await a.publish_mic(timeout=4)
    check("T1 lobby: publishing refused", err != "", f"publish attempt -> {err or 'SUCCEEDED'}")
    check("T1 lobby (not hidden): visible to hall", a.identity in hall.room.remote_participants,
          "hall sees att-a" if a.identity in hall.room.remote_participants else "hall does not see att-a")

    # --- T1b hidden lobby
    h = Client("att-hidden")
    await h.connect(token("att-hidden", "Hidden", hidden=True, can_subscribe=False, can_publish=False,
                          can_publish_data=False))
    await asyncio.sleep(2)
    seen = h.identity in hall.room.remote_participants
    listed = [p.identity for p in (await room_svc.list_participants(api.ListParticipantsRequest(room=ROOM))).participants]
    check("T1b hidden lobby: invisible to others, visible to server API", not seen and h.identity in listed,
          f"hall sees={seen}, server list has it={h.identity in listed}")

    # --- T2 admit into the locked meeting: hears program, still can't publish
    await set_perm(a.identity, **ADMITTED_LOCKED)
    ok = await until(lambda: a.saw("subscribed", "hall:"), 6)
    check("T2 admit: hears program after permission upgrade", ok,
          f"events={[e for e in a.events if e[0] in ('permissions', 'subscribed')]}")
    await set_perm(h.identity, **ADMITTED_LOCKED, hidden=False)
    ok = await until(lambda: h.identity in hall.room.remote_participants, 5)
    check("T2b admit hidden lobby: becomes visible after hidden=False", ok,
          f"hall sees att-hidden={h.identity in hall.room.remote_participants}")
    err = await a.publish_mic(timeout=4)
    check("T2 locked: self-publish refused", err != "", f"publish attempt -> {err or 'SUCCEEDED'}")

    # --- T3 'empty sources' semantics: can_publish=True with sources=[] means ALL sources
    b = Client("att-b", auto_publish_mic=False)
    await b.connect(token("att-b", "Souza", can_subscribe=True, can_publish=False, can_publish_data=True))
    await set_perm(b.identity, can_subscribe=True, can_publish=True, can_publish_sources=[], can_update_metadata=True)
    await asyncio.sleep(1)
    err = await b.publish_mic(timeout=4)
    check("T3 note: can_publish=True + sources=[] allows publishing (empty = unrestricted)", err == "",
          f"publish -> {err or 'succeeded'}")
    await set_perm(b.identity, **ADMITTED_LOCKED)
    ok = await until(lambda: hall.saw("remote_unpublished", "att-b"), 5)
    check("T3 revoking can_publish unpublishes an existing track", ok,
          f"hall events for att-b={[e for e in hall.events if 'att-b' in e[1]]}")

    # --- T4 operator 'unmutes' one commenter by granting MIC; others stay locked
    a.mic_pub_sid = ""
    await set_perm(a.identity, **SPEAKING)
    ok = await until(lambda: hall.saw("subscribed", "att-a:"), 6)
    check("T4 grant MIC to A: hall receives A's audio (client auto-publishes on grant)", ok,
          f"hall events for att-a={[e for e in hall.events if 'att-a' in e[1]][-3:]}")
    b.mic_pub_sid = ""
    err = await b.publish_mic(timeout=4)
    check("T4 B still locked while A speaks", err != "", f"B publish -> {err or 'SUCCEEDED'}")
    pub = next((p for p in a.room.local_participant.track_publications.values()), None)
    err_cam = await a.publish_camera()
    check("T4 MIC-only grant refuses camera", err_cam != "", f"A camera publish -> {err_cam or 'SUCCEEDED'}")

    # --- T5 server mute vs self-unmute (why the lock must be permission-based)
    if pub is not None:
        await room_svc.mute_published_track(api.MuteRoomTrackRequest(
            room=ROOM, identity=a.identity, track_sid=pub.sid, muted=True))
        muted = await until(lambda: hall.saw("remote_muted", "att-a"), 5)
        a.mic_track.unmute()
        await asyncio.sleep(2)
        self_unmuted = hall.saw("remote_unmuted", "att-a")
        check("T5 server mute reaches others", muted, f"hall saw mute={muted}")
        check("T5 note: a server-muted participant CAN self-unmute (mute alone is not a lock)", self_unmuted,
              f"hall saw unmute after client unmute={self_unmuted}")

    # --- T6 relock A: track removed
    hall_unpub_before = sum(1 for e in hall.events if e == ("remote_unpublished", "att-a"))
    await set_perm(a.identity, **ADMITTED_LOCKED)
    ok = await until(lambda: sum(1 for e in hall.events if e == ("remote_unpublished", "att-a")) > hall_unpub_before, 5)
    check("T6 relock: A's mic unpublished by the server", ok, f"hall unpublished events for A={ok}")

    # --- T7 raised hand via own attributes; operator lowers it
    ts = str(int(time.time() * 1000))
    await b.room.local_participant.set_attributes({"hand": ts, "attendance": "3"})
    ok = await until(lambda: hall.saw("attrs", f'att-b:{{"hand": "{ts}"') or hall.saw("attrs", ts), 4)
    check("T7 raise hand: hall receives attribute change", ok,
          f"hall attrs events={[e for e in hall.events if e[0] == 'attrs'][-2:]}")
    await room_svc.update_participant(api.UpdateParticipantRequest(room=ROOM, identity=b.identity,
                                                                   attributes={"hand": ""}))
    ok = await until(lambda: b.room.local_participant.attributes.get("hand", "") == "", 4)
    info = await participant_info(b.identity)
    check("T7 operator lowers hand (attribute cleared), attendance kept", ok and info.attributes.get("attendance") == "3",
          f"B attrs now={dict(b.room.local_participant.attributes)}, server={dict(info.attributes)}")
    ok_lobby_hand = False
    try:
        await asyncio.wait_for(h.room.local_participant.set_attributes({"hand": ts}), 4)
        ok_lobby_hand = await until(lambda: hall.saw("attrs", "att-hidden"), 3)
    except Exception:  # noqa: BLE001
        ok_lobby_hand = False
    check("T7b participant with can_update_metadata can set own attributes", ok_lobby_hand, f"seen by hall={ok_lobby_hand}")

    # --- T8 rename
    await room_svc.update_participant(api.UpdateParticipantRequest(room=ROOM, identity=b.identity,
                                                                   name="Souza family (3)"))
    ok = await until(lambda: hall.saw("name", "att-b=Souza family (3)"), 4)
    check("T8 operator rename visible to others", ok, f"hall name events={[e for e in hall.events if e[0] == 'name']}")

    # --- T9 stop video: grant camera, then revoke camera only
    await set_perm(b.identity, can_subscribe=True, can_publish=True, can_publish_sources=[MIC, CAMERA],
                   can_publish_data=True, can_update_metadata=True)
    await asyncio.sleep(1)
    err = await b.publish_camera()
    got_cam = await until(lambda: hall.saw("subscribed", "att-b:"), 5)
    before = sum(1 for e in hall.events if e == ("remote_unpublished", "att-b"))
    await set_perm(b.identity, **SPEAKING)
    ok = await until(lambda: sum(1 for e in hall.events if e == ("remote_unpublished", "att-b")) > before, 5)
    check("T9 stop video: revoking CAMERA unpublishes the camera", got_cam and ok and err == "",
          f"camera published={err == ''} hall got it={got_cam} unpublished after revoke={ok}")

    # --- T10 back to lobby: stops hearing the program
    unsub_before = sum(1 for e in a.events if e[0] == "unsubscribed")
    await set_perm(a.identity, **LOBBY)
    ok = await until(lambda: sum(1 for e in a.events if e[0] == "unsubscribed") > unsub_before, 5)
    check("T10 move back to lobby: program unsubscribed", ok,
          f"A unsubscribed events={[e for e in a.events if e[0] == 'unsubscribed']}")

    # --- T11 reconnect with the same identity keeps one participant; are attributes kept?
    await b.room.local_participant.set_attributes({"attendance": "4"})
    await asyncio.sleep(1)
    b2 = Client("att-b", auto_publish_mic=False)
    await b2.connect(token("att-b", "Souza", can_subscribe=True, can_publish=False, can_publish_data=True))
    await asyncio.sleep(2)
    listed = [p.identity for p in (await room_svc.list_participants(api.ListParticipantsRequest(room=ROOM))).participants]
    info = await participant_info("att-b")
    check("T11 same identity reconnect replaces the old connection (no duplicate)", listed.count("att-b") == 1,
          f"att-b entries={listed.count('att-b')}, old conn events={[e for e in b.events if e[0] == 'disconnected']}")
    check("T11 note: attributes after reconnect", True,
          f"server attributes now={dict(info.attributes)} (client must re-send attendance)")

    # --- T12 remove; can the same token rejoin?
    await room_svc.remove_participant(api.RoomParticipantIdentity(room=ROOM, identity="att-b"))
    ok = await until(lambda: b2.saw("disconnected"), 5)
    check("T12 remove disconnects the participant", ok, f"b2 events={[e for e in b2.events if e[0] == 'disconnected']}")
    b3 = Client("att-b", auto_publish_mic=False)
    try:
        await asyncio.wait_for(b3.connect(token("att-b", "Souza", can_subscribe=True, can_publish=False)), 6)
        rejoined = True
    except Exception:  # noqa: BLE001
        rejoined = False
    check("T12 note: a removed identity can rejoin with a valid token", rejoined,
          "rejoined -> Solin must stop issuing tokens to removed identities (and keep token TTLs short)"
          if rejoined else "rejoin refused")

    for c in (hall, a, h, b, b2, b3):
        try:
            await c.room.disconnect()
        except Exception:  # noqa: BLE001 - spike teardown
            continue
    await lk.aclose()
    passed = sum(r["ok"] for r in results)
    print(f"\n{passed}/{len(results)} checks passed", flush=True)
    with open("results/s9_moderation.json", "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
