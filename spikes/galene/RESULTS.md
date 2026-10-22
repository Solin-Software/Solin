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
