# Media extraction helper

`solin-media-extract` uses only the public shared FFmpeg libraries (`avformat`,
`avcodec`, `avutil`, `swscale`). It has no Qt, OBS, GPU or FFmpeg command-line
dependency. Public structures and function types come from the builder's SDK;
there are no private symbols or manually reconstructed ABI layouts.

## Build interface

Supply `SOLIN_FFMPEG_ROOT`, containing `include/libavformat/avformat.h` and the
other FFmpeg headers. Linux can omit the root to discover the matching development
headers using `pkg-config`; `/usr` roots also support the architecture-specific
include directory. No import libraries are required: the helper resolves the
shared library APIs dynamically.

```sh
cmake -S native/media_extract -B build/native/media-extract \
  -DSOLIN_FFMPEG_ROOT=/path/to/ffmpeg-sdk -DCMAKE_BUILD_TYPE=Release
cmake --build build/native/media-extract --config Release --parallel
ctest --test-dir build/native/media-extract -C Release --output-on-failure
```

Windows produces `build/native/media-extract/Release/solin-media-extract.exe`;
single-configuration Unix generators produce
`build/native/media-extract/solin-media-extract`. Tests are enabled by default;
set `SOLIN_MEDIA_EXTRACT_BUILD_TESTS=OFF` to omit them. Windows uses a static C++
runtime and a Unicode `wmain`; Unix arguments must be UTF-8.

All targets use the FFmpeg 8 ABI. The minimum public versions are defined in
[`src/ffmpeg_api.hpp`](src/ffmpeg_api.hpp):

| Library | Minimum public version | Required major |
| --- | --- | --- |
| `avformat` | 62.3.100 | 62 |
| `avcodec` | 62.11.100 | 62 |
| `avutil` | 60.8.100 | 60 |
| `swscale` | 9.1.100 | 9 |

Compilation rejects a different major or older SDK. Runtime validation happens
before allocating FFmpeg objects and requires those majors and versions at least
as new as the SDK used to compile. The packaged libraries are loaded using
absolute paths below the executable directory, without searching `PATH` or the
working directory. `SOLIN_FFMPEG_RUNTIME_SUBDIR` is a CMake setting, default `.`;
the macOS builder can use `Frameworks`. The builder is
responsible for placing the libraries and their dependencies there.

## Protocol 1

```sh
solin-media-extract --source 'media.mp4' --image-policy frame \
  --width 640 --height 360 --title 1 --duration 1
solin-media-extract --version
```

`--source` is required for extraction. Defaults are image policy `none`, width
640, height 360, title 1 and duration 1. Width and height can be smaller positive
integers, but cannot exceed 640 and 360. Duplicate, unknown and invalid options
are permanent errors. `--version` must be used alone.

Stdout is binary on Windows. Every response begins with exactly one UTF-8 JSON
line terminated by a single LF. Successful extraction returns:

```json
{"protocol":1,"status":"ok","title":"","duration_ms":0,"has_video":false,"image":null}
```

When present, `image` contains integer `width`, `height`, `stride`, `bytes` and
`kind` (`cover` or `frame`). Exactly `bytes` raw BGRA bytes follow the newline,
without another delimiter. Stride is `width * 4`, and bytes is `stride * height`.
The image contains its aspect-preserving dimensions rather than letterboxing.
Display matrix rotation and reflection, sample aspect ratio and color range are
applied. Maximum payload is 921,600 bytes. Headers stay below 64 KiB; titles are
normalized to valid UTF-8 and capped at 8,192 UTF-8 bytes before JSON escaping.
Disabled title/duration requests return `""`/0 while preserving `has_video`.

`--version` returns `protocol: 1`, `status: "ok"` and an `abi` object keyed by
library name. Each entry contains `compiled`, `runtime` and `minimum` full packed
version integers, `major`, and the actual loaded `library` path. It has no binary
payload. Errors return `status: "error"` with `error.kind` (`transient`,
`permanent`, `format`), a stable `error.code` and bounded `error.message`, then
exit 2. Successful output exits 0; stdout write failure exits 3. FFmpeg diagnostics
go exclusively to stderr and are capped at 8 KiB per process.

HTTP 401, 403 and 404 return permanent failures. HTTP 429 and FFmpeg's generic
remaining-4XX error return transient failures so the application can apply its
bounded retry policy. That generic error does not retain the exact status, so
responses such as 410 share the transient classification. The helper neither
reopens the source to inspect a status nor adds internal reconnection retries.

## Extraction behavior

One format context opens the source once. Container title, duration and stream
classification are read without opening an explicit extraction decoder. FFmpeg
may internally decode during stream probing; probe codec options request one
thread. Only the selected image stream gets an extraction decoder, with one CPU
thread and software-only pixel format selection.

`cover` uses the first attached picture, then the first PNG/MJPEG/BMP/GIF/WebP/
TIFF/PPM stream only when audio or moving video also exists. Missing artwork is a
successful null image. It never substitutes a moving-video thumbnail. This
codec classification preserves the metadata contract, including its treatment
of MJPEG streams. `frame` prefers that same cover; otherwise it selects moving
video or an isolated image/GIF stream. `has_video` describes moving video using
the same classification, so an isolated GIF can return a frame with
`has_video: false`. When a cover cannot be decoded, `frame` can fall back to a
separate moving-video candidate; `cover` still returns the typed format failure.

Frame extraction targets 5% of known duration, or one second when unknown,
relative to container start. Targets within the first second decode forward;
later targets seek and decode forward from a preceding keyframe. Selection uses
presentation timestamps; successful seeks flush the decoder. If the
target is unavailable, it uses a start frame cached before seeking within the same
format context. This also preserves fallback after a destructive failed seek.
Nonseekable sources read forward, retaining that decoded start frame for
fallback. HTTP Range support is provided by libavformat; sources are never
staged as complete downloads.

A monotonic interrupt callback bounds probing to 15 seconds and image extraction
to 20 seconds, with a 35-second total budget. SIGINT/SIGTERM request cancellation.
Deadlines are also checked around decoding and conversion. RAII releases format,
decoder, dictionary, packet, frame and scaler resources on all exits. Decoded
images are limited to 16,384 per axis and 33,554,432 pixels; packets passed to the
extraction decoder are limited to 64 MiB. Invalid dimensions, corrupt/unsupported
images and singular/perspective display matrices return format errors.

The native tests cover strict argument parsing, UTF-8 normalization and JSON
escaping, binary payload invariants, all quarter-turn/reflection permutations,
aspect-preserving containment and deterministic phase/total/cancellation
deadlines. Real-media and HTTP integration tests live in the parent test suite.
