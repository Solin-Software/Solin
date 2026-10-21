# Spike results

## S1: libobs → LiveKit publish path

### Finding: path B covers hardware encoding too

`livekit` Python SDK 1.1.21 exposes `video_encoder` backends (`SOFTWARE`, `HARDWARE`, `VAAPI`, `NVENC`, `VIDEOTOOLBOX`, `PRE_ENCODED`) and an `EncodedVideoSource`. So path B (raw frames → Python SDK) gets **GPU encoding with simulcast and E2EE** without a Go helper. Path C is now only needed if we want libobs' own encoders; it is also possible in Python (`EncodedVideoSource`), but pre-encoded publishing is single-layer (`simulcast=False`).

### Setup (run 1, 2026-10-20)

- Machine: i7-13700T, Intel UHD 770 (VAAPI, iHD driver), Ubuntu 26.04, LiveKit server 1.13.10 (`--dev`, Docker, localhost).
- Publisher pinned to **8 threads (4 P-cores + HT)** with `taskset -c 0-7` to emulate a smaller hall PC.
- libobs 1080p30 canvas with a looping 1080p test clip (`testsrc2` + timecode + 1 kHz beeps); raw callback asks libobs for **I420 1280×720**; stereo 48 kHz PCM from the main mix.
- Simulcast 3 layers, top layer max 1.7 Mbps; dynacast **off** (all layers always encoded = worst case).
- 3 subscribers (`lk load-test`), 60 s per config, first 10 s sample discarded.
- Scripts: `s1_publish/publish_b.py`, `s1_publish/run_matrix.sh`.

### Results

| Config | CPU (cores) | Δ vs baseline | Layers (WxH@fps) | Send | Drops |
|---|---|---|---|---|---|
| Baseline (libobs program only) | 0.36 | — | — | — | — |
| VP8 software | 0.90 | +0.54 | 720p@30, 360p@20, 180p@15 | 2.3 Mbps | 0 |
| H.264 software (OpenH264) | 0.71 | +0.35 | same | 2.3 Mbps | 0 |
| **H.264 VAAPI** | **0.62** | **+0.26** | same | 2.1 Mbps | 0 |
| VP8 software + E2EE | 0.96 | +0.60 | same | 2.3 Mbps | 0 |
| **H.264 VAAPI + E2EE** | **0.67** | **+0.31** | same | 2.1 Mbps | 0 |

- libobs held 30.0 fps in every config; raw-callback cost ~0.7 ms/frame; no audio or video drops.
- Subscribers received ~1.4 Mbps video + 39 kbps audio each, 0% packet loss.
- E2EE adds ~0.05 cores.
- Encoder in use confirmed from stats: `SimulcastEncoderAdapter (VAAPI H264 Encoder ×3)`.

### Interpretation

- Every config is far below the 1.5-core budget, including software VP8 with E2EE.
- ~~Preferred: H.264 via VAAPI~~ — see the VAAPI leak finding below; software H.264/VP8 + simulcast + E2EE is the current choice.
- Caveat: the test clip compresses easily; real camera content costs software encoders more (hardware encoders are largely insensitive). A slower/older CPU (e.g. 10th-gen i5) may need ~1.5× these deltas — still within budget.

### Run 2: glass-to-glass latency and A/V sync (webcam, 31 min)

Setup: Brio 105 webcam (MJPEG 1080p30) + its mic as libobs sources → H.264 VAAPI simulcast → local LiveKit → Chrome probe page (`s1_publish/web/index.html`, `s1_publish/probe_server.py`). The page flashes + beeps every 2 s; the webcam/mic capture it; the page detects the flash/beep in the received (hidden, muted) stream. 865 of 930 stimuli matched (93%).

| Metric | Median | p5 | p95 |
|---|---|---|---|
| Video glass-to-glass (screen → camera → libobs → encode → LiveKit → decode → display) | **263 ms** | 228 | 288 |
| Video until received by the browser (before render) | 247 ms | 207 | 273 |
| Audio glass-to-glass (speaker → mic → libobs → Opus → LiveKit → browser) | **213 ms** | 199 | 225 |
| A/V sync (audio − video; negative = audio early) | **−48 ms** | −81 | −12 |

Per 5-minute window the medians stayed within 252–266 ms (video), 204–221 ms (audio), −42 to −59 ms (sync): **no drift over 30 minutes**. Audio ~50 ms early is inside the usual lip-sync tolerance (noticeable only beyond ~45–90 ms early); a small audio delay could centre it.

Latency excludes the internet hop (local server); expect roughly +RTT/2 plus jitter-buffer growth for real attendees.

### Finding: memory leak in the LiveKit SDK's VAAPI encoder

The 35-minute webcam run showed publisher RSS growing **344 → 962 MB (~18 MB/min)** with flat CPU and no drops. Isolation runs (`s1_publish/run_leak.sh`, clip source, 1 subscriber):

| Config | RSS growth |
|---|---|
| libobs only (clip or webcam) | flat |
| Raw callbacks + frame conversion, no LiveKit | flat |
| Audio-only publish | flat |
| Video publish, **H.264 VAAPI**, simulcast | **+18–19 MB/min** |
| Video publish, **H.264 VAAPI**, no simulcast | **+9.7 MB/min** |
| Video publish, H.264 software (OpenH264), simulcast | +0.3 MB/min |
| Video publish, VP8 software, simulcast | +0.7 MB/min |
| Video publish, VP8 software, no simulcast | +1.3 MB/min |

- Python allocations (tracemalloc) don't grow: the leak is native.
- With VAAPI the growth tracks the encoded output (~0.75–1.1× of the bytes produced), i.e. the VAAPI encoder path in the SDK's libwebrtc appears to retain encoded/bitstream buffers. Software encoders are essentially flat.
- `livekit` 1.1.21 (latest at the time); no matching public report found.

**Decision for now: software encoding** (OpenH264 H.264 or VP8) — it costs only +0.35–0.55 cores at 720p with 3 layers. Revisit hardware encoding when the SDK is fixed, or via path C (libobs' own hardware encoder → `EncodedVideoSource`, single layer). Worth reporting upstream with a minimal reproducer.

### Still open for S1

- ~~Confirm software encoding stays flat over 2 h~~ — done, see below.
- Re-run the 2 h soak on an idle machine to check the audio drops (see below).
- Windows hardware encoders (Media Foundation/NVENC/QSV) may not share the VAAPI leak — test separately.
- Real camera content (noise, motion) for software encoders.
- Visual quality at 360p/720p.
- Windows: Media Foundation / NVENC / QSV backends via the SDK; test on a real hall PC.
- Older/weaker CPU (actual hall PC).

### Run 3: 2-hour soak, software H.264 (OpenH264), simulcast, clip source, 1 subscriber

- RSS **439 → 326 MB** over 120 min (flat/declining): software encoding has no leak.
- CPU 0.5–0.7 cores, libobs 30.0 fps throughout, layers 720p@30 / 360p@20 / 180p@15, 0 video drops.
- **Audio: 1231 dropped 1024-sample chunks (~26 s)**, all between ~70 and ~85 min, when heavy profiling jobs (memray analysis, FFmpeg, extra publishers) were running on the same machine. Likely cause: the spike's audio hand-off (50-chunk queue drained through the shared default thread pool) stalls under load. To be re-run on an idle machine; the product design should use a dedicated audio thread / larger buffer.

### Upstream report (posted: livekit/python-sdks#882)

`upstream/livekit-python-sdks-vaapi.md`: VAAPI leak (~4.5 KB per encoded frame, frame-proportional; controls: software flat, FFmpeg `h264_vaapi` flat; memray/smaps/malloc_trim checks) and a SIGBUS in `vaEndPicture` with `max_bitrate=400000` (3/3, core-dump stack). Reproducer: `s1_publish/leak_repro.py`.

### Upstream fix (proposed: livekit/rust-sdks#1552)

Root cause: `webrtc-sys/src/vaapi/vaapi_h264_encoder_wrapper.cpp` created picture/slice parameter buffers every frame (plus sequence, rate-control and packed-header buffers on IDR) with `vaCreateBuffer()` and never destroyed them. The PR tracks them per frame and destroys them after `vaEndPicture()` and in `Destroy()`.

Verified with `livekit-ffi` built from the commit pinned by python `livekit` 1.1.21 (`5a656c4`) plus the patch, loaded via `LIVEKIT_LIB_PATH` (build: `~/Projetos/upstream/rust-sdks-pinned/target/release/liblivekit_ffi.so`):

| Library | Layers | RSS growth (5 min) |
|---|---|---|
| Official 1.1.21 | 1 | +8.4 MB/min |
| Patched | 1 | flat (±0.1 MB) |
| Patched | 3 | flat |

`max_bitrate=400_000`: no SIGBUS with the patch in two short runs, but VAAPI still sends 0 bytes (software sends ~390 kbps) — separate, unfixed.

Until a release ships the fix, the spike can use VAAPI only with the patched library; the product default stays software encoding.


## S9: moderation with LiveKit primitives

Scripts: `s9_moderation/moderation_test.py` (25 checks, Python clients + `livekit-api` operator), `s9_moderation/followups.py` (media-flow measurements). LiveKit server 1.13.10 (`--dev`), `livekit` 1.1.21.

**Result: everything we need works with stock LiveKit — no patches.** 23/25 event-level checks pass; the 2 "failures" are missing client *events*, while the server does enforce the change on the media (measured below).

| Need | Primitive | Verified behaviour |
|---|---|---|
| Lobby | Token with `can_subscribe=False, can_publish=False` | Hears nothing; publish attempts time out |
| Lobby invisible to others | `hidden=True` | Invisible to other participants, listed by the server API |
| Admit | `UpdateParticipant(permission)` | Starts receiving the program within ~1 s; `hidden=False` makes them visible |
| Meeting-live lock (no self-unmute) | `can_publish=False` | Self-publish refused; revoking it while speaking cuts the speaker's media immediately |
| Operator unmutes one commenter | `can_publish=True, can_publish_sources=[MICROPHONE]` | Client auto-publishes on the permission event; others stay locked; camera refused |
| Stop video | Drop `CAMERA` from `can_publish_sources` | Camera unpublished |
| Raised hand | Participant sets own attribute `hand=<ts>` (`can_update_metadata`) | Hall sees the change; operator clears it via `UpdateParticipant(attributes={"hand": ""})`; other attributes (`attendance`) kept |
| Rename | `UpdateParticipant(name=…)` | Visible to everyone |
| Back to lobby | Lobby permissions again | Program audio stops immediately (100 → 0 frames/s); client re-subscribe attempts get nothing |
| Remove | `RemoveParticipant` | Disconnected |
| Reconnect (same `device_id` identity) | — | Replaces the old connection, no duplicate |

### Gotchas for the product design

- **`can_publish_sources=[]` means "all sources"**, not none. The lock must use `can_publish=False`.
- **Server mute is not a lock**: a server-muted participant can unmute themselves. Use permissions.
- **Clients are not always told**: after `can_subscribe`/`can_publish` are revoked the media stops, but the client may not get `track_unsubscribed`/`local_track_unpublished`. The attendee page must drive its UI from `participant_permissions_changed`.
- **Attributes are lost on reconnect** (server shows `{}` after a same-identity reconnect): the attendee page must re-send `attendance` (and hand state) on every connect — it already keeps them in `localStorage`.
- **Removed participants can rejoin with a still-valid token**: Solin's token issuer must refuse removed identities for that meeting and keep token TTLs short.
- A participant needs `can_update_metadata` to raise a hand; that also lets them change their own name/metadata (acceptable; operator can rename back).

## S2: hall capacity (50 and 100 attendees)

Scripts: `s2_capacity/run_s2.sh`, `s2_capacity/sample_server.py`, server config `s2_capacity/livekit.yaml` (Prometheus on :6789). Publisher = S1 path B (libobs clip → software H.264, 3-layer simulcast, top layer max 1.7 Mbps), pinned to 8 threads. Subscribers = `lk load-test` on the same machine; the subscriber `--layout` picks the layer they request ("speaker" = top 720p, "3x3" = 360p, "5x5" = 180p). 120 s samples per variant; egress from the server's `livekit_packet_bytes` counter (= what the hall would upload).

| Variant | Hall upload (server egress) | Per attendee | LiveKit CPU | LiveKit RSS | Publisher CPU | Loss / NACK |
|---|---|---|---|---|---|---|
| Audio only, 50 | **2.7 Mbps** | 48 kbps | 0.20 cores | 249 MB | 0.45 | 0 / 0 |
| 720p program, 50 | **88.5 Mbps** | 1.7 Mbps | 0.58 | 298 MB | 0.77 | 0 / 0 |
| 360p program, 50 | **25.2 Mbps** | 0.49 Mbps | 0.35 | 282 MB | 0.73 | 0 / 0 |
| 180p program, 50 | **10.2 Mbps** | 0.20 Mbps | 0.30 | 279 MB | 0.78 | 0 / 0 |
| 720p program + 1 commenter camera (medium), 50 | 77.1 Mbps | — | 0.56 | 302 MB | 0.74 | 0 / 0 |
| 720p program, 100 | **176.8 Mbps** | 1.7 Mbps | 0.93 | 487 MB | 0.68 | 0 / 0 |

- libobs held 30 fps and no frames were dropped in any variant.
- **The SFU is cheap**: under 0.6 cores for 50 attendees at 720p, under 1 core for 100. Server load scales linearly.
- **Upload is the limit, and 720p costs more than estimated**: 1.7 Mbps per attendee (estimate was 1.5), so 50 attendees at 720p need ~90 Mbps up. 360p fits the earlier estimate (~25 Mbps).
- In the camera variant, the "speaker" layout gave the commenter's camera the top slot, so the program was received at a lower layer; the total is therefore lower than program-only 720p. A real layout (program large, camera small) would add roughly +0.2–0.5 Mbps per attendee.
- Loopback only: no packet loss, NACKs or retransmissions. A real line adds retransmission overhead (typically a few %) and congestion control may lower layers.

**Implications**
- Hall-direct mode: default the attendees' video to **360p** (≈25 Mbps for 50); allow 720p only on lines with ≥100 Mbps upload. Audio-only fits any line (≈3 Mbps).
- Edge mode (community node / hub) remains the answer for halls with weak upload: the hall sends one copy (~2–3 Mbps), the node pays the fan-out above.
- Hall PC CPU budget for 50 attendees, all-in on this CPU: publisher ~0.75 + LiveKit ~0.6 = **~1.4 cores**.
- Consider lowering the top simulcast layer to ~1.2 Mbps: a wide stage shot doesn't need 1.7 Mbps.

## S7: end-to-end encryption

Scripts: `s7_e2ee/run_s7.sh`, page `s1_publish/web/e2ee.html` (served by `s1_publish/probe_server.py`). Publisher = S1 path B with `--e2ee-key` (software H.264, 3-layer simulcast, shared key). Headless Chromium (snap) and Firefox 158 using `livekit-client` 2.23.0 + its E2EE worker, key taken from the URL fragment. Each run: 12 s at the top layer, 12 s at the low layer, 12 s back at the top layer.

| Browser | Mode | Video | Simulcast switch | Audio | Encryption errors |
|---|---|---|---|---|---|
| Chromium | correct key | 1280×720 @ 28–29 fps, real content | 720p → 320×180 @ 15 fps → 720p | steady test tone | 0 |
| Firefox 158 | correct key | 1280×720 @ 24–29 fps, real content | 720p → 320×180 @ 15 fps → 720p | steady test tone | 0 |
| Both | no key (e.g. a node operator with a valid token) | ~0–1 fps, blank (luma σ = 0) | — | erratic noise only | reported |
| Both | wrong key | nothing decoded | — | silence | 12, reported |

- **E2EE works end to end** between the Python/libobs publisher and both browser engines, including simulcast layer switching.
- **A participant without the key gets nothing usable**: no picture; audio is undecryptable data rendered as noise. With a wrong key nothing decodes at all and the client reports encryption errors, which the attendee page can turn into a clear "wrong link" message.
- Publisher cost of E2EE measured in S1: ~+0.05 cores.
- Not yet tested: **Safari / iOS**, Android Chrome on a real phone, and the decrypt CPU cost on phones (planned in S8).

## S3: comment audio round trip and echo

Scripts: `s3_audio/hall.py` (hall side: libobs program audio from the hall mic, optional WebRTC APM echo cancellation/noise suppression from the `livekit` SDK, remote audio played via `pw-cat`), `s1_publish/web/s3.html` (remote commenter: publishes a synthetic 1 kHz beep every 2.5 s as its microphone and detects it coming back in the program), `s3_audio/run_s3.sh`, `s3_audio/analyze.py`, `s3_audio/cdp_close.py`. Local LiveKit server, headless Chromium as the remote.

**Setup notes.** The real speaker → Brio path was too weak against room noise to time reliably (beep ≈ 0.02 vs broadband ≈ 0.05 at the mic), and routing through PipeWire virtual devices (null sink, `pw-loopback`) carried only digital silence from this session. The hall path was therefore emulated in-process: the remote audio that is played is also mixed back into the hall feed after a fixed delay (40 ms, gain 0.5) — a worst case equivalent to a mixer without mix-minus — with the far-end reference and the echo clocked on the same 10 ms tick, as a sound card would. Sound-card I/O latency (typically 10–30 ms each way) and the room are not included.

| Phase (75 s each) | Beeps returned to the commenter | Round trip (commenter → hall → program → commenter) | Echo attenuation in the program (1 kHz band) | Returned echo level |
|---|---|---|---|---|
| No echo cancellation | 25 / 26 | **≈ 150 ms** (page median 125 ms + ~30 ms page timestamp bias; includes the 40 ms emulated path) | 0 dB | 0.18 |
| Echo cancellation | 5 / 28 (residual, mostly during convergence) | — | **≈ 61 dB** median after convergence | 0.014 |
| Echo cancellation + noise suppression | 10 / 28 | — | **≈ 66 dB** | 0.013 |

- Software round trip on a local server is ≈ 110 ms excluding the emulated path. Remote voice → hall arrival is a few ms plus the jitter buffer locally; add the real network one-way delay (typically 20–60 ms) in production. Comfortably inside conversational limits.
- **The SDK's echo canceller (`rtc.AudioProcessingModule`) is effective, but only with a well-aligned reference.** An earlier run where the reference was fed when the remote audio *arrived* (not when it was *played*) and the echo delay jittered by ±10 ms gave only 10–15 dB. Product requirement: feed `process_reverse_stream` from the same clock/device that plays the remote audio, and run the hall capture and the playout as one duplex audio path (or at least on the same device clock), with `set_stream_delay_ms` set to the measured device delay.
- Mixer wiring still matters: recommend a mix-minus send (the computer's own channel excluded from the feed back to the computer) as in the current Zoom wiring; the canceller then only handles acoustic pickup from stage mics.

**Still open:** validate on real hall hardware (mixer, stage mics, loudspeakers) where the acoustic echo path and sound-card latencies are real.

## S5: hub (local validation; Oracle run pending)

Bundle in `s5_hub/`: `hub/` (docker-compose: Caddy 2.11.7 with on-demand TLS gated by an `ask` service that only approves subdomains of halls connected to frps; frps 0.71.1; coturn 4.18.0 with TURN-REST secret), `hall/` (frpc with `/rtc`+`/twirp` → LiveKit, `/` → join page; LiveKit `turn_servers` with the shared secret), `tools/` (relay load generator forcing TURN, 2.5 h signaling soak, VM sampler), `local/` (overrides to run everything on one machine), `README.md` (Oracle setup steps). All images have arm64 builds (Oracle A1).

**Validated locally** (one machine, `hub.localhost`, plain HTTP):
- Routing: `hall-a.hub.localhost` → Caddy → frps → tunnel → frpc → hall LiveKit (LiveKit answers through the tunnel).
- Certificate gate: `ask` approves a connected hall's subdomain (200) and refuses unknown names (403).
- TURN: coturn allocates with time-limited credentials derived from the shared secret, both for the load generator's own credentials and for the ones the hall's LiveKit hands out.
- **End to end with relay forced**: an attendee allowed only relayed ICE candidates gets signaling through the tunnel and media through coturn — 30 fps video + continuous audio.
- 30 relay-only attendees (medium layer): all connected and receiving (~445 kbps video + ~99 kbps audio each, 16 Mbps total); **coturn ≈ 0.11 cores**, Caddy/frps ≈ 0.01. (A later load step was invalidated by a stuck earlier load process and an unrelated VM starting on the machine.)

Local caveats: relaying to a **loopback** peer did not complete ICE (LiveKit ignores loopback candidates) — the local test uses the LAN address instead; on the VM the peer is the hall's public/NATed address. A single Python process decoding video tops out at ~20 attendees, so the generator now reads WebRTC stats instead of decoding, and must run off the hub VM.

**Pending (needs the Oracle VM + DNS):** relay capacity on 2 OCPU, 2.5 h signaling soak through the tunnel, tunnel recovery after a hall network drop, idle-reclamation behaviour. Steps in `s5_hub/README.md`.

### S5 on a real cloud VM (Google Cloud e2-micro, São Paulo)

VM: e2-micro (2 shared vCPU, 1 GB RAM + 1 GB swap), Debian 13, `southamerica-east1` (not a free-tier region — paid, ≈ US$0.35–0.50/day, deleted after the spike). No DNS: **IP-only variant** (`hub/docker-compose.ip.yml`, `hub/frps.ip.toml`, `hall/frpc.ip.toml`) — frps tunnel on :443, the hall's LiveKit forwarded as raw TCP on :80, coturn on 3478/50000–50999. Hall = this PC behind a NAT that LiveKit could not validate as reachable (typical CGNAT/no port forward) — exactly the case the hub exists for.

**Two configuration bugs found and fixed:**
1. frp **HTTP vhost** proxy closed long-lived LiveKit signaling WebSockets after ~60 s (≈ 1 reconnect per attendee per minute). → Forward signaling as **raw TCP** (`type = "tcp"`), or raise frp's HTTP timeouts in the domain/TLS variant.
2. `transport.heartbeatTimeout = 90` on frps with `tcpMux` (frpc then sends no app heartbeats) **dropped the whole tunnel every 90 s** — all attendees reconnected at once. → Removed from both frps configs.

**Test 2 — 50 attendees joining through the hub** (media direct on the hall machine, so the hub carries signaling only), after both fixes:

| Metric | Result |
|---|---|
| Joined | 50 / 50 |
| Join time via the São Paulo hub | median **197 ms**, p90 265 ms |
| Reconnects in 5 min | **0** |
| frps CPU | ≤ 0.02 cores |
| Hub egress | ~0.9 Mbps during joins, then **0.13–0.2 Mbps steady** (≈ 3–4 kbps per attendee) |

→ **Signaling for a 2 h, 50-attendee meeting ≈ 120–150 MB of hub egress** (connection-quality/active-speaker updates to every participant) — 5–10× the earlier 10–30 MB estimate. A 1 GB/month free allowance covers only ~6–8 such meetings; production hubs need a plan with real bandwidth (cheap VPS, TBs/month).

**Test 6 — TURN over the public IP:** a relay-only attendee (signaling via the hub, media hall → coturn São Paulo → attendee, no direct path allowed) joined in 363 ms and received the low layer (15 fps, ~148 kbps) + audio (~99 kbps) for 2 min, 0 reconnects; hub egress ~5 MB.

**Tests 3+4 — 2.5 h soak with tunnel cuts** (`tools/run_soak.sh`: one attendee through the hub; hall frpc stopped for 10/30/60 s at minutes 20/40/60):

| Time | Event | Attendee |
|---|---|---|
| 14:53 | connected | stable (100 audio fr/s, 30 fps) |
| 15:07 | no cut — spontaneous blip (frpc saw nothing; likely the home line) | reconnecting → reconnected in **3 s** |
| 15:13 | **10 s cut** | reconnecting → reconnected **2 s after the tunnel returned** (≈ 12 s outage) |
| 15:33 | **30 s cut** | SDK **gave up after ~27 s** ("Connection refused" on every retry) → disconnected for good |
| 15:53 | 60 s cut | (attendee already gone) |

- Hub egress for the whole soak: **41 MB**.
- Why the 30 s cut is fatal: while the hall's frpc is down, frps closes the hall's public port, so resume attempts get an immediate *connection refused* and the SDK's resume budget (~25–30 s) runs out before the tunnel is back. Short blips (≤ ~15 s) are absorbed transparently.

**Implications**
- The attendee page needs its **own rejoin loop** on top of the SDK resume: on `disconnected`, keep retrying with backoff (same device identity, attendance re-sent from `localStorage`, "reconnecting…" banner) for as long as the meeting runs.
- On rejoin the participant starts again with the token's permissions (lobby) — Solin should **auto-admit device identities already admitted** in this meeting, so a hall-side blip doesn't strand everyone in the lobby.
- Hall-side: frpc reconnects within seconds once the line is back; consider a hub-side "hall reconnecting" response instead of a refused port.

## S6: edge-node failover

Scripts: `s6_failover/` (`failover.py` hall/attendee client with failover between an ordered list of edge nodes, keeping the device identity; `run_s6.sh` starts nodes A and B, the hall and 10 attendees — one process each, like independent browsers — then kills node A at t = 30 s; `analyze.py` timeline). Local machine, two LiveKit nodes, no network latency.

| Failure | Strategy | Hall publishing on B | Attendees on B | Attendees hear the program again |
|---|---|---|---|---|
| Node crash (`docker kill`: connections refused at once) | wait (switch on SDK `disconnected`) | 16.4 s | 21.8 s median (25.7 max) | **21.9 s** median (25.7 max) |
| Node crash | **eager** (switch on SDK `reconnecting`) | 0.18 s | 0.13 s | **0.38 s** |
| Silent failure (`docker pause`: no answer at all) | eager | 11.7 s | 11.0 s | **11.9 s** (SDK ping timeout) |
| Silent failure | **eager + HTTP health probe** (1 s interval, 2 misses) | 3.4 s | 2.7 s median (3.7 max) | **3.6 s** median (3.7 max) |
| Node crash | eager + probe | 0.14 s | 0.11 s | **0.34 s** |

- With eager switching plus a health probe, a dead edge node costs attendees **~0.3 s** (crash) to **~3.5 s** (silent failure) of program, locally. Real networks add signaling RTT + ICE/DTLS setup (~0.5–2 s).
- Measurement pitfalls fixed along the way: awaiting a goodbye from the dead node (2 s) and running 10 attendees in one Python process (connects serialised → ~2.9 s) both inflated the first results; audio still draining from the dead node was briefly counted as "recovered".

**Design implications**
- Attendee page: switch eagerly on `reconnecting`, plus a 1 s health probe of the current node; the meeting pointer carries primary + backup node and tokens for both (both nodes must accept the hall's meeting key).
- **Attendees must follow the hall, not switch on their own**: if only the attendee's own line blips, eagerly moving to node B would strand them in an empty room while the hall stays on A. The failover target should be "where the hall is now" — the hall publishes its current node into the (encrypted) meeting pointer, and an attendee that lands on a node without the hall goes back/checks the pointer.
- Probe false positives (one slow response) must not cause flapping: 2 consecutive misses worked here; tune with real networks.
