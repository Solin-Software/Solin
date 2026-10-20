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

### Upstream report (draft, not posted)

`upstream/livekit-python-sdks-vaapi.md`: VAAPI leak (~4.5 KB per encoded frame, frame-proportional; controls: software flat, FFmpeg `h264_vaapi` flat; memray/smaps/malloc_trim checks) and a SIGBUS in `vaEndPicture` with `max_bitrate=400000` (3/3, core-dump stack). Reproducer: `s1_publish/leak_repro.py`.
