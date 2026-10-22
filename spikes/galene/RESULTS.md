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
- **The remaining gap to S1 is in the browser, before display** (~90 ms here vs ~16 ms in S1), together with audio arriving early. whippub stamps packets on arrival instead of carrying libobs' capture timestamps (pts), so the browser's lip-sync and jitter buffer work from send times. S1's LiveKit SDK path used capture timestamps. This is publisher work: drive RTP timestamps and sender reports from the packet pts.
- **Option 1 isn't a product candidate.** FFmpeg's capture and RTP timing gave the widest arrival spread (117–344 ms) and the largest A/V offset.

### Still open for G1

- Carry libobs pts into RTP timestamps and sender reports; re-measure A/V sync and video render delay.
- Hardware encoder in path D (libobs `ffmpeg_vaapi` / QSV) with `repeat_headers` or SPS/PPS prepended on keyframes.
- 2 h soak of path D (memory, audio drops).
- Windows: path A (pylibobs ships `obs-webrtc.dll`) and path D.
- Simulcast: Galene accepts two layers; path D currently sends one.
