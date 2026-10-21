"""S5: N simulated attendees forced through the hub's TURN relay, each subscribing to the hall program.

Credentials follow the TURN REST scheme used by coturn's `use-auth-secret`
(username = "<expiry>:<user>", password = base64(HMAC-SHA1(secret, username))).
Reports, per interval, how many attendees are receiving and the aggregate received audio/video rates.

    python relay_load.py --url wss://hall-a.hub.example.org --turn hub.example.org --secret ... --n 50
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time

from livekit import api, rtc


def turn_credentials(secret: str, user: str, ttl: int = 14400) -> tuple[str, str]:
    username = f"{int(time.time()) + ttl}:{user}"
    password = base64.b64encode(hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
    return username, password


QUALITY = {"low": rtc.VideoQuality.VIDEO_QUALITY_LOW, "medium": rtc.VideoQuality.VIDEO_QUALITY_MEDIUM,
           "high": rtc.VideoQuality.VIDEO_QUALITY_HIGH}


class Attendee:
    """Subscribes like a viewer but doesn't render: progress is read from WebRTC inbound stats,
    so one process can simulate many attendees without decoding cost dominating."""

    def __init__(self, idx: int, quality: str) -> None:
        self.idx = idx
        self.quality = quality
        self.room = rtc.Room()
        self.tracks: list = []
        self.reconnects = 0
        self.room.on("track_subscribed", self._on_track)
        self.room.on("reconnecting", lambda: setattr(self, "reconnects", self.reconnects + 1))

    def _on_track(self, track, pub, _rp) -> None:
        self.tracks.append(track)
        if track.kind == rtc.TrackKind.KIND_VIDEO:
            pub.set_video_quality(QUALITY[self.quality])

    async def counters(self) -> tuple[int, int, int]:
        """(video bytes, video frames received, audio bytes) so far."""
        vb = vf = ab = 0
        for t in self.tracks:
            try:
                for st in await t.get_stats():
                    if st.WhichOneof("stats") == "inbound_rtp":
                        r = st.inbound_rtp
                        if t.kind == rtc.TrackKind.KIND_VIDEO:
                            vb += r.inbound.bytes_received
                            vf += r.inbound.frames_received
                        else:
                            ab += r.inbound.bytes_received
            except Exception:  # noqa: BLE001 - track may be gone
                pass
        return vb, vf, ab


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="hall URL through the hub, e.g. wss://hall-a.hub.example.org")
    p.add_argument("--room", default="s5")
    p.add_argument("--api-key", default="devkey")
    p.add_argument("--api-secret", default="secret")
    p.add_argument("--direct", action="store_true", help="don't force TURN: normal ICE (signaling-only hub tests)")
    p.add_argument("--turn", default="", help="TURN host (required unless --direct)")
    p.add_argument("--turn-port", type=int, default=3478)
    p.add_argument("--turn-tcp", action="store_true", help="relay over TCP instead of UDP")
    p.add_argument("--secret", default="", help="coturn static-auth-secret")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--per-second", type=float, default=5)
    p.add_argument("--duration", type=float, default=180)
    p.add_argument("--interval", type=float, default=15)
    p.add_argument("--quality", choices=QUALITY, default="medium")
    p.add_argument("--identity-prefix", default="relay")
    p.add_argument("--out", default="")
    a = p.parse_args()

    out = open(a.out, "a") if a.out else None

    def log(rec: dict) -> None:
        line = json.dumps(rec)
        print(line, flush=True)
        if out:
            out.write(line + "\n")
            out.flush()

    transport = "tcp" if a.turn_tcp else "udp"
    attendees: list[Attendee] = []
    join_ms: list[float] = []
    failures = 0
    for i in range(a.n):
        ident = f"{a.identity_prefix}-{i}"
        if a.direct:
            cfg = None
        else:
            user, pw = turn_credentials(a.secret, ident)
            cfg = rtc.RtcConfiguration(
                ice_transport_type=rtc.IceTransportType.TRANSPORT_RELAY,
                ice_servers=[rtc.IceServer(urls=[f"turn:{a.turn}:{a.turn_port}?transport={transport}"],
                                           username=user, password=pw)])
        tok = (api.AccessToken(a.api_key, a.api_secret).with_identity(ident)
               .with_grants(api.VideoGrants(room_join=True, room=a.room, can_publish=False)).to_jwt())
        att = Attendee(i, a.quality)
        try:
            t0 = time.monotonic()
            opts = rtc.RoomOptions(rtc_config=cfg) if cfg else rtc.RoomOptions()
            await asyncio.wait_for(att.room.connect(a.url, tok, options=opts), 20)
            join_ms.append((time.monotonic() - t0) * 1000)
            attendees.append(att)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log({"event": "connect_failed", "i": i, "error": f"{type(exc).__name__}: {exc}"[:200]})
        await asyncio.sleep(1 / a.per_second)
    js = sorted(join_ms)
    log({"event": "start", "connected": len(attendees), "failed": failures,
         "transport": "direct" if a.direct else transport, "quality": a.quality,
         "join_ms_median": round(js[len(js) // 2]) if js else None,
         "join_ms_p90": round(js[int(len(js) * 0.9)]) if js else None})

    prev = {id(x): await x.counters() for x in attendees}
    start = time.monotonic()
    while time.monotonic() - start < a.duration:
        await asyncio.sleep(a.interval)
        recv_v = recv_a = 0
        v_kbps = a_kbps = fps = 0.0
        for x in attendees:
            cur = await x.counters()
            pv = prev[id(x)]
            dvb, dvf, dab = (c - q for c, q in zip(cur, pv))
            recv_v += dvf > 0
            recv_a += dab > 0
            v_kbps += dvb * 8 / a.interval / 1000
            a_kbps += dab * 8 / a.interval / 1000
            fps += dvf / a.interval
            prev[id(x)] = cur
        n = max(len(attendees), 1)
        log({"event": "stats", "t": round(time.monotonic() - start), "attendees": len(attendees),
             "receiving_video": recv_v, "receiving_audio": recv_a,
             "avg_video_kbps": round(v_kbps / n), "avg_audio_kbps": round(a_kbps / n, 1),
             "avg_video_fps": round(fps / n, 1), "total_mbps": round((v_kbps + a_kbps) / 1000, 1),
             "reconnects": sum(x.reconnects for x in attendees),
             "connected_now": sum(x.room.isconnected() for x in attendees)})
    # Don't let a slow teardown keep the process (and its load) alive after the measurement.
    await asyncio.gather(*(asyncio.wait_for(x.room.disconnect(), 5) for x in attendees), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
    os._exit(0)
