# Upstream issue: https://github.com/livekit/python-sdks/issues/882

**Title:** VAAPI H.264 encoder: publisher RSS grows ~4.5 KB per encoded frame without bound; SIGBUS in `vaEndPicture` with low `max_bitrate`

## Summary

Publishing a video track with `video_encoder=ENCODER_BACKEND_VAAPI` makes the publisher's resident memory grow linearly for as long as frames are captured (~8.5 MB/min for one 30 fps layer, ~18 MB/min with 3-layer simulcast). The same pipeline with `ENCODER_BACKEND_SOFTWARE` stays flat, and FFmpeg's `h264_vaapi` with the same driver stays flat. Separately, VAAPI with `max_bitrate=400_000` crashes the process with SIGBUS inside `vaEndPicture` within ~20 s.

## Environment

- `livekit` (python) 1.1.21; `liblivekit_ffi.so` build-id `ca6b16e7849fd605cde702ef34c3969799e8b4af`, sha256 `05a58fc83b3515cd4e984a66c69c22e0aad09eadb6e972edde626488dafbf5ad`
- Python 3.13.13, Ubuntu 26.04, kernel 7.0.0-38-generic, glibc 2.43
- Intel i7-13700T, UHD Graphics 770 (RPL-S); `intel-media-va-driver-non-free` 26.1.2 (iHD), `libva2` 2.23.0 (VA-API 1.23)
- LiveKit server 1.13.10, `livekit/livekit-server --dev` (Docker, localhost)

## Reproducer

Self-contained script using only `livekit` + `numpy` + `psutil`: precomputed synthetic I420 frames pushed into a `VideoSource` at a fixed rate, one published track, no subscribers needed. Logs RSS and `bytes_sent` every minute. See `leak_repro.py` (to be attached).

```
python leak_repro.py --encoder vaapi --no-simulcast --duration 600   # grows
python leak_repro.py --encoder software --duration 600              # flat
python leak_repro.py --encoder vaapi --no-simulcast --max-bitrate 400000   # SIGBUS
```

## Results (10-minute runs unless noted, 1280×720 synthetic content)

| Encoder | Layers | Size | FPS | Max bitrate | RSS slope | Bytes sent | Runs |
|---|---|---|---|---|---|---|---|
| VAAPI | 3 (simulcast) | 1280×720 | 30 | 1.7 Mbps | **+18.1 / +18.3 MB/min** | 15.5 MB/min | 2 |
| Software (OpenH264) | 3 (simulcast) | 1280×720 | 30 | 1.7 Mbps | −0.1 / −4.0 MB/min (flat) | 17.2 MB/min | 2 |
| VAAPI | 1 | 1280×720 | 30 | 1.7 Mbps | **+8.4 MB/min** | 12.7 MB/min | 1 (+1 smaps run, +8 MB/min) |
| VAAPI | 1 | 640×360 | 30 | 1.7 Mbps | **+8.6 MB/min** | 12.7 MB/min | 1 |
| VAAPI | 1 | 1280×720 | 15 | 1.7 Mbps | **+4.0 MB/min** | 6.3 MB/min | 1 |
| VAAPI | 1 | 1280×720 | 30 | 0.4 Mbps | **SIGBUS after ~17 s** (0 bytes sent) | — | 3 |

- Growth is proportional to the **number of encoded frames** (~4.5 KB per frame per encoder instance): halving the frame rate halves it; changing resolution or the bytes per frame does not change it; 3 simulcast encoders ≈ 2.1× one encoder.
- In an application-level test (libobs-rendered frames, 1 subscriber) the same configuration grew 344 → 962 MB over 34 minutes, i.e. linear and unbounded over meeting-length sessions.

## What we checked

- **Not Python-side:** `tracemalloc` shows no growth.
- **Not a retained malloc allocation:** a `memray --native` capture of a 4-minute VAAPI run shows no allocation stack retained per frame (largest retained items are ~32 one-time VA buffer maps from `vaMapBuffer` / `vaCreateContext`).
- **Where it grows:** `/proc/<pid>/smaps` — the number of mappings stays constant (562); RSS grows ~4 MB per 30 s inside existing **anonymous** mappings; `[heap]` and file/driver mappings stay flat.
- **Not reclaimable fragmentation:** calling glibc `malloc_trim(0)` every minute returns only ~1.5 MB; growth continues.
- **Driver control:** FFmpeg `h264_vaapi` (same driver, 1280×720@30, 1.7 Mbps, 5 minutes) stays flat at ~56 MB RSS.

We have not identified the root cause; the controls point at the VAAPI encoder path in the SDK's WebRTC build rather than at the driver or at the Python bindings.

## SIGBUS stack (core dump, `eu-stack`)

```
#6  iHD_drv_video.so+0x4fd900
#7  iHD_drv_video.so+0x4f1b42
#8  iHD_drv_video.so+0x42953f
#9  iHD_drv_video.so+0x40beb6
#10 vaEndPicture (libva.so.2)
#11 liblivekit_ffi.so+0x1996e8d
#12 liblivekit_ffi.so+0x1994446
#13 liblivekit_ffi.so+0x113940d
#14 liblivekit_ffi.so+0x113bf47
#15 liblivekit_ffi.so+0x113891d
#16 liblivekit_ffi.so+0xe6450b
#17 liblivekit_ffi.so+0xe6363b
#18 liblivekit_ffi.so+0xe5ad53
#19 liblivekit_ffi.so+0xe62d52
#20 liblivekit_ffi.so+0xe62966
#21 liblivekit_ffi.so+0xe5920e
#22 liblivekit_ffi.so+0xc72765
#23 liblivekit_ffi.so+0xc6c435
#24 liblivekit_ffi.so+0xc6c33c
#25 start_thread (libc.so.6)
```

`bytes_sent` was still 0 at 15 s before the crash.
