# Galene spike results

## G1: libobs → Galene publish path

### Setup (2026-10-21/22)

- Galene 1.2.1 built from source (`CGO_ENABLED=0`, 13 MB static binary), localhost, group with `"codecs": ["h264", "opus"]`, built-in TURN off.
- Machine and rig as S1 run 2: i7-13700T, Brio 105 webcam (MJPEG 1080p30) and its mic pointed at the screen, flash/beep probe page in Chromium full screen (`g1/web/probe.html`, the S1 page with Galene's `protocol.js` instead of LiveKit). 30 min per run unless noted.
- Publisher pinned to 8 threads (`taskset -c 0-7`) for path D, as in S1.
- Viewer: one browser (the probe page). Galene CPU/RAM from `pidstat` every 10 s.

### Finding: the planned WHIP routes need custom builds on Linux

| Route | Status on this machine |
|---|---|
| OBS Studio's WHIP output (sanity check) | Snap OBS 32.2 has no `obs-webrtc` plugin (`Service 'whip_custom' not found`) |
| Path A: libobs `obs-webrtc` | pylibobs' Linux build has `ENABLE_WEBRTC=OFF`; needs libdatachannel, which Ubuntu doesn't package |
| Path B: FFmpeg WHIP muxer | Ubuntu's FFmpeg 8.0.1 is built without it (`Unknown format 'whip'`) |

So the spike added a small WHIP publisher, `g1/whippub` (Go, Pion v4.2.20, ~200 lines). It runs in two modes:

- **RTP mode (option 1):** forwards FFmpeg's RTP.
- **`-stdin` mode (path D):** takes encoded frames from pylibobs' packet callback.

Pion's default interceptors give it NACK, sender reports and TWCC.

### Sanity check

FFmpeg (test clip, x264 720p30 1.7 Mbps + Opus) → whippub → Galene → Galene's stock web client: 1280×720, ~30 fps, audio track present. **Galene accepts WHIP H.264 + Opus and plays it in Chromium with no configuration beyond the codec list.**

### Results

| | Option 1: FFmpeg → whippub | Path D: libobs x264 → whippub | Path D, `scenecut=0` (10 min) | S1 reference: libobs → LiveKit SDK (VAAPI) |
|---|---|---|---|---|
| Video glass-to-glass, median (p5–p95) | 417 ms (383–450) | 383 ms (350–450) | **317 ms (283–350)** | 263 ms (228–288) |
| Video until received by the browser | 235 ms (117–344) | 269 ms (231–320) | **226 ms (190–269)** | 247 ms (207–273) |
| Audio glass-to-glass | 176 ms | 210 ms | 228 ms | 213 ms |
| A/V sync (negative = audio early) | −237 ms | −174 ms | **−97 ms** | −48 ms |
| Matched stimuli | 901 / 908 | 884 / 911 | 311 / 364 | 865 / 930 |
| Publisher CPU | 0.55 cores (FFmpeg incl. MJPEG decode) | 0.47 cores (whole libobs process) | 0.58 cores | 0.36 baseline + 0.26–0.35 |
| whippub CPU / RSS | 0.01 cores / 15 MB | 0.02 cores / 16 MB | — | — |
| **Galene CPU / RSS, 1 viewer** | **0.02 cores / 12–18 MB** | **0.02 cores / 11–17 MB** | — | — |
| Video fps / lagged frames | — | 30.0 / 0 | 30.0 / 0 | 30.0 / 0 |

Per 5-minute window, option 1 stayed at 408–433 ms video and 171–192 ms audio. Path D (scene cut on) stayed at 367–417 ms video and 201–236 ms audio; video and audio both rose ~30 ms in the last 5 minutes. No drops and no growth in publisher memory (RSS fell from 220 to 154 MB).

### Finding: x264 scene cuts fired ~5 times per second

With the webcam filming the flashing probe page, OBS's x264 marked **49 keyframes per 10 s**; the clip source gave the expected 5. `scenecut=0` brings the webcam back to one keyframe every 2 s. It also cut video latency by ~65 ms and halved the A/V offset. The product publisher should set `scenecut=0` (or a long `min-keyint`), because hall content (slides, camera cuts) changes abruptly too.

### Interpretation

- **Galene costs almost nothing here:** 0.02 cores and under 20 MB with one viewer. Frames reach the browser as fast as through LiveKit: 226 ms vs 247 ms, though with different encoders.
- **Path D works** with stock pylibobs: libobs' own x264 and Opus encoders → `null_output` → `add_packet_callback` → whippub. It needs no plugin rebuild and no Python-side encoding, and could use libobs' hardware encoders (to be tested).
- **The remaining A/V offset comes from capture, not from Galene or the transport.** See the timestamp follow-up below.
- **Option 1 isn't a product candidate.** FFmpeg's capture and RTP timing gave the widest arrival spread (117–344 ms) and the largest A/V offset.

### Follow-up: timestamps and where the A/V offset comes from (2026-10-22)

whippub's `-stdin` mode now:
- sets RTP timestamps from libobs packet pts (Python sends pts in place of a fixed duration);
- by default sends its own sender reports that map them to libobs' capture clock (`-sr pts`); `-sr send` keeps Pion's send-time reports for comparison;
- reads incoming RTCP, so NACK retransmission works. Before, nothing read it, so the NACK responder never saw requests.

Galene keeps the publisher's timing: each downstream sender report is computed from the upstream one (`remoteRTP + (now − remoteNTP)`).

**Capture offset in libobs itself** (`g1/raw_check.py`): raw-callback timestamps of the probe's flash and beep, 40 s, Brio 105 video + mic.

| Check | Audio − video in libobs timestamps (median, range) |
|---|---|
| Before the A/B run | −101 ms (−144 to −53) |
| After the first A/B run | −104 ms (−138 to −71) |
| After the send-time rerun | −75 ms (−110 to +55) |

**The Brio's video reaches libobs ~75–105 ms later than its audio.** This exists before any encoder or network, and sets the floor for browser-side A/V sync.

**A/B of the sender-report clock** (5 min each, `g1/run_sr_ab.sh`, same rig, nobody using the PC):

| | `-sr pts` (capture clock) | `-sr send` (Pion, send time) |
|---|---|---|
| Video glass-to-glass | **333 ms** (300–367) | 350 ms (283–417) |
| Video until received | 221 ms (196–259) | 230 ms (186–284) |
| Audio glass-to-glass | **213 ms (205–222)** | 234 ms (218–319) |
| A/V sync | −121 ms (−157 to −83) | −115 ms (−171 to −49) |
| Matched stimuli | 141 / 149 | 139 / 149 |

- Both modes keep sync within ~15–45 ms of the capture offset, so **Galene and the WebRTC hop preserve A/V sync**.
- Capture-clock reports give tighter, lower audio latency and ~17 ms lower video latency. Keep `-sr pts`.
- **Product fix for lip sync:** delay the mic in libobs by the camera's latency (OBS's per-source "Sync Offset", `obs_source_set_sync_offset`). With the Brio that's ~100 ms. Devices differ, so make it a setting, or measure it with this flash/beep method.
- An earlier 10-minute pts run (video 517 ms, sync −308 ms) is discarded: the PC was in use, the full-screen probe was covered and Chrome throttled it.
- Rig note: reopening the Brio straight after another libobs process released it sometimes stalls the webcam (`v4l2-input: select timed out`) and invalidates a run.

### Still open for G1

- Verify the mic sync offset closes the A/V gap end to end.
- Hardware encoder in path D (libobs `ffmpeg_vaapi` / QSV) with `repeat_headers` or SPS/PPS prepended on keyframes.
- 2 h soak of path D (memory, audio drops).
- Windows: path A (pylibobs ships `obs-webrtc.dll`) and path D.
- Simulcast: Galene accepts two layers; path D currently sends one.

## G2: codec and device compatibility (partial, 2026-10-22)

### Setup

- **Publisher:** path D with the test clip into H.264 + Opus groups.
- **Viewer:** `g2/web/viewer.html` plays the program and posts `getStats()` every 5 s to `/log`: codec, fps, freezes, packets lost, audio concealment, visibility.
- **Access:** phones reached it through two Cloudflare quick tunnels, one for the page and one for Galene's WebSocket, with the tunnel origin in Galene's `allowOrigin`. Media went direct over the home LAN; there was no TURN, so 4G is out of scope here.
- **Firefox:** ran headless with a fresh profile and was closed via WebDriver BiDi (`g2/bidi_close.py`).

### Results (H.264 constrained baseline 720p30 + Opus)

| Device / browser | Plays | Notes |
|---|---|---|
| Chromium desktop (built-in browser) | ✅ 30 fps | 0 freezes, 0 lost, audio OK |
| Firefox 158 snap, fresh profile, headless | ✅ 30 fps | H.264 decodes without OpenH264 being downloaded first; 0 freezes, 0 lost |
| Chrome 155, Android 10 phone (home Wi-Fi) | ✅ 29.4–30.2 fps | see the Wi-Fi loss and screen-lock findings below |
| Old Android, Safari iOS | not tested | skipped for now |

**So far no device needed VP8.** H.264 from libobs plays on every browser tested.

### Finding: audio suffers from Wi-Fi loss that video recovers from

- On the phone, video showed 0 lost packets after retransmission (NACK), while audio lost ~3% and concealed 3–9%. Galene and browsers don't retransmit Opus.
- **Opus in-band FEC:** libobs' `ffmpeg_opus` only exposes a bitrate. So `--audio pcm` (now the default) sends libobs' raw mix to whippub, which encodes with libopus (`gopkg.in/hraban/opus.v2`, cgo, build tag `nolibopusfile`) with in-band FEC and a 10% loss target.
  - Video is then stamped with the packet's `sys_dts_usec`, the same system clock as the raw audio; the output rebases `dts_usec`.
  - Per-packet TOC counts confirm the effect: with FEC every packet is **hybrid** (FEC-capable); without it the encoder picks **CELT**, which can't carry FEC. So FEC also changes the coding mode at 64 kbps.
  - Galene already advertises `useinbandfec=1` to receivers.
- **Phone A/B, one run each (inconclusive):**
  - FEC run: 0.7% audio loss, 0.7% concealed, 0 video loss.
  - No-FEC run: ~10% audio loss, 10–20% concealed, 3,925 video packets lost, 13 freezes.
  - The Wi-Fi was far worse during the second run, so the difference is mostly the network.
  - Within the FEC run, concealed audio after warm-up (~0.47 s) roughly equals lost audio (~0.5 s): FEC recovered little, which fits burst loss.
- **Next options for bursts:** audio NACK or RED (redundant earlier packets); support in Galene and mobile browsers not yet checked. A controlled-loss test (netem) needs root, which the spike didn't have.

### Finding: locking an Android phone drops the session

- Chrome on Android 10, screen locked: ~10 s later Galene logged `websocket: close 1006 (abnormal closure)`. The downstream closed and the page never recovered after unlock.
- **Any viewer client needs automatic reconnection**, and likely something that keeps the page alive in the background (a playing media session, Wake Lock).
- This overlaps S8. LiveKit's SDK should get the same screen-lock test before comparing.

### Still open for G2

- Old Android and Safari iOS.
- Screen-lock test with auto-reconnect in the viewer.
- Audio NACK / RED support in Galene and phone browsers.

## G4: moderation with Galene's permissions (2026-10-22)

Script: `g4/modtest` (Go, ~400 lines). It runs scripted operator, attendee and viewer clients over Galene's WebSocket protocol, with Pion media (Opus silence frames for the commenter). Groups:
- `g4`: an `operator` user with `op`, and `wildcard-user` with `observe`;
- `g4t`: tokens only, `authKeys` with an HS256 key and no users.

Galene 1.2.1, localhost.

**20 / 23 checks pass. The 3 failures are Galene limitations, confirmed in the source.**

| Need | Galene mechanism | Result |
|---|---|---|
| No self-unmute | Attendees have `observe` (no permissions); offers get `abort` + "not authorised" | ✅ refused; nothing reaches other clients |
| Operator calls on a commenter | `useraction present` → client gets `joined/change` with `present`, then publishes | ✅ permission event immediate; **247 ms** from grant to the commenter's audio at another client |
| Operator ends the comment | `useraction unpresent` → server deletes the user's up-streams (`delUpConn`) | ✅ others get `close` at once; media stops within the 100 ms check window; commenter gets `abort` |
| Commenter limited to the mic | — | ❌ `present` also accepts video and screen share; enforce audio-only in our client and let the operator `unpresent` |
| Raised hands | `useraction setdata {raisehand: true}` on oneself, pushed as `user/change` | ✅ reaches the operator immediately; **also visible to every attendee** |
| No recording | `"allow-recording": false` | ✅ `groupaction record` refused ("not authorised") |
| Lock once the meeting starts | `groupaction lock` | ✅ new attendees get `fail` with the lock message; an operator (e.g. remote attendant) still joins; unlock admits again |
| Kick | `useraction kick` | ✅ immediate |
| Rename | — | ❌ no such action ("unknown user action"); the username is fixed at join |
| Attendee list private | — | ❌ every client receives every user's name and data (`user add/change`) |
| **Lobby** (hears nothing, invisible) | No waiting room. **The hub keeps people on its own lobby page and mints a signed token only on admit** | ✅ token-only group refuses password joins; a hub-minted JWT (`sub` = display name, `aud` = group URL, `permissions: []`, `exp`) joins with that name; expired and wrong-group tokens refused |
| Part-holder | Token with `permissions: ["present", "message"]` | ✅ joins with `present` |

### Interpretation

- **The core meeting rules are enforced by the server**: no self-unmute, operator-controlled unmute, an immediate cut, lock, kick, no recording. They are as strong as LiveKit's (S9), and simpler: two permission names in place of per-source grants.
- **The lobby lives on the hub, not in Galene.** That fits the plan (the hub already does directory/TLS/signaling):
  - Attendees wait on a hub page and never touch Galene until admitted.
  - The operator admits them from Solin; the hub mints a short-lived token with the attendee's display name.
  - Attendance is the list of tokens issued.
- **Rename** becomes "issue a new token and rejoin". This costs a reconnect (about 1 s), which is acceptable because the attendant renames before admitting.
- **Audio-only commenters and the private attendee list are client-side conventions.** Our web client never offers video and never shows the attendee list or other people's hands. A modified client could do both. The operator sees any video stream and can `unpresent` or kick. The list exposes only display names the hub chose, comparable to Zoom's participant list.
- Galene can't hide users from each other. If that matters, the options are a small Galene patch (don't push `user` messages to `observe` clients) or separate subgroups.

## G6: reaching a hall behind CGNAT (2026-10-22)

### Setup

- **Hall:** this PC on the home line (IPv4 behind CGNAT, public `181.232.152.160`) running Galene, publishing the test clip with path D.
- **Relay:** a temporary dual-stack coturn on GCP (`e2-small`, São Paulo, custom `IPV4_IPV6` subnet, `use-auth-secret`, UDP + TCP 3478, relay ports 49160–49260). Script in `g6/coturn_startup.sh`. **Deleted after the test**, with its network and firewall rules.
- **Galene:** `data/ice-servers.json` with STUN plus the TURN URLs (IPv4 and IPv6, UDP and TCP), `credentialType: "hmac-sha1"`. Galene mints time-limited TURN credentials for every client.
- **Viewer:** the G2 page now reports the selected ICE pair. `relay=1` forces `iceTransportPolicy: relay`; `relay=tcp` also keeps only TCP TURN URLs, emulating a UDP-blocked network.
- **Signaling:** Cloudflare quick tunnels, as in S4/G2.

### Finding: Galene's own connections use the configured ICE servers

`rtpconn.go` creates every server-side PeerConnection with `ice.ICEConfiguration()`, the same STUN/TURN list handed to clients. So the hall gathers its server-reflexive address via STUN and can also take its own relay. Galene's startup self-test confirmed it: `Relay test successful in 752 ms, RTT = 19 ms`.

### Results (phone: Chrome 155, Android 10, 4G carrier = IPv6-only)

| Case | Selected path | Video | Loss / freezes | RTT |
|---|---|---|---|---|
| 4G phone, no forcing | **direct IPv6** host ↔ host (`2804:214:…` ↔ hall `2804:1354:…`) | 28–30 fps | 0 lost; 1 freeze after ~45 s | 49–67 ms |
| 4G phone, relay forced | `relay/udp` IPv4 on the VM ↔ hall's CGNAT address (prflx) | 30 fps, 65 s | 0 lost, 0 freezes, 0.05% audio concealed | 44–58 ms |
| Desktop, TCP-only relay (UDP-blocked emulation) | `relay/tcp` to the VM, UDP onward to the hall | 30 fps | 0 lost, 0 freezes | 40 ms |

- **The home line now has native IPv6.** S4, two days earlier, found none. Galene advertised it, so an IPv6-only 4G phone reached the hall directly with no relay. Halls with IPv6 need no relay for IPv6 attendees.
- **The dual-stack TURN fallback works with Galene exactly as with LiveKit in S4.** The IPv6-only phone got an IPv4 relay address, and the relay reached the IPv4 CGNAT hall.
- **TURN over TCP works.** TLS on 443, for networks that only allow web traffic, needs a certificate on coturn and isn't tested.
- In the unforced run the WebSocket closed after ~48 s (`close 1006`) with the page still visible. Cause unknown: Android, the carrier, or the user switching away. The forced-relay run (65 s) didn't drop. Together with G2's screen-lock finding, **the client must reconnect automatically**.

### Still open for G6

- TURN over TLS on 443.
- Hall on mobile (IPv6-only hall) ↔ IPv4-only attendee.
- Cause of the 4G WebSocket drop.

## G3: hall capacity, 50 and 100 viewers (2026-10-22)

Method as S2 (loopback, one machine). Script: `g3/run_g3.sh`; viewers from `g3/loadgen` (Go/Pion: N listen-only clients, counts received bytes and RTP sequence gaps).
- **Cores:** publisher 0–7, Galene 8–15, loadgen 16–23.
- **Publisher:** path D, test clip, single layer (x264 CBR), Opus with FEC.
- **Sampling:** 120 s per variant, first 10 s interval dropped. Received total = what the hall would upload.

| Variant | Hall upload | Per viewer | Galene CPU | Galene RSS | Publisher CPU (libobs + whippub) | Lost packets |
|---|---|---|---|---|---|---|
| Audio only, 50 | **3.2 Mbps** | 64 kbps | **0.08** | 45 MB | 0.82 + 0.06 | 0 / 275 k |
| 720p (1.7 Mbps), 50 | **89.2 Mbps** | 1.78 Mbps | **0.26** | 47 MB | 0.73 + 0.05 | 0 / 2.1 M |
| 720p, 100 | **178.3 Mbps** | 1.78 Mbps | **0.57** | 74 MB | 0.90 + 0.06 | 0 / 4.3 M |
| 360p (450 kbps), 50 | **26.1 Mbps** | 0.52 Mbps | **0.18** | 73 MB | 0.55 + 0.05 | 0 / 1.1 M |
| 360p, 100 | **51.6 Mbps** | 0.52 Mbps | **0.34** | 76 MB | 0.53 + 0.05 | 0 / 2.3 M |

- **Joining:** 50 viewers joined and received media in 2.6 s; 100 in 5.1 s (50 ms ramp).
- **Publisher health:** libobs held 30 fps with 0 lagged frames throughout.

**Compared with LiveKit (S2, same machine and method)**

| | LiveKit | Galene |
|---|---|---|
| Server CPU, 720p, 50 / 100 viewers | 0.58 / 0.93 cores | **0.26 / 0.57 cores** |
| Server memory, 50 / 100 viewers | 298 / 487 MB | **47 / 74 MB** |
| Hall upload, 720p, 50 viewers | 88.5 Mbps | 89.2 Mbps (same bitrate) |

- **Galene needs about half the CPU and a sixth of the memory of LiveKit for the same audience.** The hall-PC budget for 50 viewers at 720p becomes publisher ~0.8 + Galene ~0.3 ≈ **1.1 cores** (S2: ~1.4).
- **Upload is still the limit, and it's set by the bitrate, not the SFU:** ~90 Mbps for 50 at 720p, ~26 Mbps at 360p, ~3 Mbps audio-only. S2's conclusions carry over: default to 360p for hall-direct mode, 720p only on fast lines. Without community relays (out of scope for the Galene variant), weak lines must lower quality or offer audio-only.
- **Caveats:**
  - Loopback has no loss, so no retransmission overhead and no congestion response.
  - One layer only: viewers on weak links can't drop to a lower layer unless the publisher sends Galene's two-layer simulcast (not yet implemented in whippub).
  - The publisher's libobs CPU includes decoding the clip.
