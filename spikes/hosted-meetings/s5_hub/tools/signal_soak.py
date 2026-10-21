"""S5: keep one attendee connected through the hub tunnel for a whole meeting and log every
signaling interruption (reconnecting / reconnected / disconnected) plus periodic media checks.

    python signal_soak.py --url wss://hall-a.hub.example.org --hours 2.5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from livekit import api, rtc


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--room", default="s5")
    p.add_argument("--hours", type=float, default=2.5)
    p.add_argument("--interval", type=float, default=60)
    p.add_argument("--out", default="")
    a = p.parse_args()
    out = open(a.out, "a") if a.out else None

    def log(rec: dict) -> None:
        line = json.dumps({"wall": time.strftime("%H:%M:%S"), **rec})
        print(line, flush=True)
        if out:
            out.write(line + "\n")
            out.flush()

    room = rtc.Room()
    frames = {"audio": 0, "video": 0}

    def on_track(track, _pub, _rp) -> None:
        kind = "video" if track.kind == rtc.TrackKind.KIND_VIDEO else "audio"
        stream = rtc.VideoStream(track) if kind == "video" else rtc.AudioStream(track)

        async def drain():
            async for _ in stream:
                frames[kind] += 1
        asyncio.ensure_future(drain())

    room.on("track_subscribed", on_track)
    for ev in ("reconnecting", "reconnected"):
        room.on(ev, lambda ev=ev: log({"event": ev}))
    room.on("disconnected", lambda reason: log({"event": "disconnected", "reason": str(reason)}))

    tok = (api.AccessToken("devkey", "secret").with_identity(f"soak-{int(time.time())}")
           .with_grants(api.VideoGrants(room_join=True, room=a.room, can_publish=False)).with_ttl(
               __import__("datetime").timedelta(hours=a.hours + 1)).to_jwt())
    await room.connect(a.url, tok)
    log({"event": "connected"})
    start = time.monotonic()
    prev = dict(frames)
    while time.monotonic() - start < a.hours * 3600:
        await asyncio.sleep(a.interval)
        log({"event": "tick", "minutes": round((time.monotonic() - start) / 60, 1), "connected": room.isconnected(),
             "audio_fps": round((frames["audio"] - prev["audio"]) / a.interval, 1),
             "video_fps": round((frames["video"] - prev["video"]) / a.interval, 1)})
        prev = dict(frames)
    await room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
