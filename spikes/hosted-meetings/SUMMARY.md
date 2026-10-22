# Solin-hosted meetings: spike summary

Branch `spike/solin-hosted-meetings`. Details and raw numbers in `RESULTS.md`; plan in `PLAN.md`; original proposal in Solin-Software/Solin discussion #138.

## Verdict: go

Solin can host hybrid congregation meetings itself — the hall PC publishes the program, LiveKit forwards it, stock LiveKit permissions cover moderation, end-to-end encryption works in the browsers that matter, and attendees recover from failures. Nothing found blocks the design. Several findings **change** it (below).

## What was proven

| Area | Result |
|---|---|
| **Publish path** (S1) | libobs raw frames → LiveKit Python SDK. Software H.264, 3 simulcast layers, E2EE: **+0.3–0.6 cores** on top of libobs. Glass-to-glass **263 ms** video, **213 ms** audio, A/V offset −48 ms, no drift over 30 min. 2 h soak: memory flat. |
| **Hall capacity** (S2) | LiveKit itself is cheap (< 0.6 cores for 50 attendees at 720p, < 1 core for 100). **Upload is the limit**: 50 attendees ≈ **25 Mbps at 360p**, ≈ 90 Mbps at 720p, ≈ 3 Mbps audio-only. |
| **Comment audio** (S3) | Round trip ≈ 150 ms locally. The SDK's echo canceller removes **≈ 61–66 dB** — but only when its reference is clocked with playout (10–15 dB otherwise). |
| **E2EE** (S7) | Chromium and Firefox decode with the key, 720p↔180p layer switching works; without the key: blank/noise; wrong key: nothing + errors. |
| **Moderation** (S9) | Stock LiveKit covers lobby, admit, meeting lock, operator unmute, stop video, hands, rename, back-to-lobby, remove. Media is cut immediately on permission changes. |
| **Hub** (S5) | Caddy/frp/coturn bundle works on a real cloud VM: 50 attendees through the tunnel, 0 reconnects, join ≈ 200 ms, frps ≤ 0.02 cores; TURN relay works on a public IP; 2.5 h soak OK. |
| **Edge failover** (S6) | Eager switch + 1 s health probe: attendees hear the program again **0.3 s** after a node crash, **≈ 3.6 s** after a silent failure (vs 22 s / 12 s with SDK defaults). |
| **Connectivity** (S4) | IPv6-only mobile attendee ↔ IPv4-only CGNAT hall: **direct fails**; **dual-stack edge node** (134 ms RTT) and **dual-stack TURN** (78 ms) both work. |
| **Real phone** (S8) | Android Chrome: join, video/audio, hands, remembered name/attendance OK; recovery after unlock ≈ 11 s. |

## Design changes the spike forces

1. **Hub TURN and community edge nodes must be dual-stack (IPv4 + IPv6).** Mobile carriers can be IPv6-only (hotspot clients get no IPv4 at all); many halls are IPv4-only behind CGNAT. Halls with IPv6 should advertise it.
2. **The hub needs real bandwidth, not a free tier.** Signaling alone is ≈ 120–150 MB of hub egress per 2 h, 50-attendee meeting (status updates to every participant). Plan a small paid VPS with TBs/month; free 1 GB tiers cover less than one hall.
3. **Attendees on phones with the screen off need a thin app.** Android Chrome freezes a locked page after ~15–30 s even with Media Session and audio playing. Web page = screen-on viewing (with wake lock); app (Android foreground service / iOS background audio) = pocket listening.
4. **Own reconnection logic in the attendee client**, not just the SDK's:
   - rejoin loop with backoff after the SDK gives up (it does after ~25–30 s);
   - stale-session check when the page becomes visible (no frames in 3 s → drop and rejoin);
   - Solin **auto-readmits** device identities already admitted to the meeting (otherwise a hall blip strands everyone in the lobby);
   - re-send attendance/hand on every connect (attributes are lost on reconnect).
5. **Failover: attendees follow the hall.** Eager switching is fast, but an attendee whose own line blips must not land alone on the backup node: the hall publishes its current node into the encrypted meeting pointer and attendees go where the hall is.
6. **Defaults for bandwidth:** 360p for attendees in hall-direct mode (720p only with ≥ 100 Mbps upload); an **audio-only** option (≈ 0.1 vs 0.55 Mbps on mobile data); top simulcast layer ≈ 1.2 Mbps is enough for a wide stage shot.
7. **Audio path in the hall:** remote audio playout and hall capture on one clock/duplex path so the echo canceller gets an aligned reference; keep mix-minus wiring at the mixer.
8. **Encoder:** software H.264 for now. The SDK's VAAPI encoder leaks ≈ 4.5 KB per frame — reported as livekit/python-sdks#882, fix proposed in livekit/rust-sdks#1552; revisit hardware encoding once released (and test Windows encoders separately).

## Operational pitfalls found (now documented in the configs)

- frp: forward LiveKit signaling as **raw TCP** (the HTTP vhost proxy closed WebSockets after ~60 s) and **no `heartbeatTimeout` with `tcpMux`** (dropped the whole tunnel every 90 s).
- `can_publish_sources=[]` means *all* sources: the meeting lock must use `can_publish=false`. Server mute is not a lock (participants can self-unmute).
- A removed participant can rejoin with a still-valid token: the token issuer must refuse removed identities and keep TTLs short.
- Debian's `coturn` package starts its own service on :3478 — disable it or configure it.

## Not yet covered

| Item | Why it matters |
|---|---|
| iPhone / Safari (join, E2EE, screen lock) | iOS is the most restrictive platform for background audio and WebRTC |
| 1 h battery + data run on a phone | Confirms the ≈ 10 %/h and 245 MB/h indications |
| Usability with an older attendee | The join flow (name, attendance, big button) is meant for them |
| Real hall hardware (mixer, stage mics, loudspeakers) | Echo canceller and latency with real acoustics and sound cards |
| Windows hall PC (encoders, libobs sidecar) | Most halls run Windows |
| UDP-blocked attendee; hall on mobile ↔ attendee elsewhere | Needs TURN over TCP/TLS 443 and a second device |
| Relay capacity at scale on the production hub/edge | Dozens of relayed attendees per node |

## Suggested next steps

1. Fold the design changes above into the plan in discussion #138 (when you choose to publish).
2. Phase 1 product work can start on the proven parts: LiveKit sidecar + libobs publisher, moderation console, attendee web page with the reconnection/attendance logic.
3. In parallel, close the gaps that can change the design: iPhone, Windows hall PC, a real hall test.
4. Prototype the thin mobile app (Android first) once the web page is stable.
