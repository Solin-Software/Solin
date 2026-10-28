# Media playback module

`solin-media-source` registers `solin_media_source`, using the OBS 32.1.2 media
source and media-playback implementation. It links the installed pylibobs libobs
and matching FFmpeg libraries; it does not replace upstream modules or libraries.

```text
python scripts/build_media_playback.py
```

The builder requires Git, CMake 3.25+ and a native C compiler: MSVC x64 on Windows,
Apple Clang on macOS, or the Ubuntu 24.04 toolchain on Linux. Supported targets
are Windows/Linux x86_64 and macOS x86_64/arm64. Linux additionally requires
`libavdevice-dev`, `libavfilter-dev`, `libsimde-dev` and the media-extraction development packages.
Windows/macOS reuse the checksum-pinned OBS dependencies SDK from the extraction
builder. macOS imports and rpaths resolve inside the private `Frameworks` tree.

The source archive is SHA-256 verified on every build. Ordered unified diffs in
[`patches/series.json`](patches/series.json) are the source of truth for downstream
changes. Each entry records its purpose, upstream status and prerequisites. Git
rejects context mismatches before the patched source tree is published. Original
upstream license headers and `COPYING` are preserved.

To retire a correction, first adopt and qualify the upstream version containing
it, remove its series entry and diff, and update any dependent patches. Keep the
behavioral regression tests. Once all generic changes are upstream, switch the
application back to `ffmpeg_source` and remove this module, its build/packaging
requirements and receipt checks together. A source identifier change alone does
not establish that a runtime contains the corrections.

Initialization/decode failures report `ERROR` without an end-of-media signal.
Terminal I/O errors and a final packet marked corrupt remain failures when the
demuxer returns EOF. Explicit stop/destruction takes precedence over cancellation;
Play and Restart rebuild a failed decoder on the OBS source owner thread.
Non-looping completion retains the decoder and duration without rewinding or
preloading; explicit Play/Restart resets it and rebases the playback clock.
Looping continues to reset immediately.

Speed updates keep the input and decoder open. The worker applies the latest
requested speed at an audio boundary, retains queued audio, and uses original
media timestamps with scaled presentation intervals. Non-unity audio passes
through FFmpeg `atempo` at its original sample rate; unity playback bypasses the
filter. Graph creation, lookahead bounds and drain are isolated in `rate_tempo.c`;
the worker schedules its queued output through one native audio path.
Queued output is due at audio sample precision, retaining its original timestamp;
sub-sample rescaling differences do not defer it across video wake-ups.

The native range remains the upstream 1–200%; Solin settings and controls use
50–200%. Video source settings are acknowledged on the next libobs video tick.
The private source rejects `full_decode`, which Solin does not request. Upstream
`ffmpeg_source` remains available for other OBS consumers, including stingers.

Finite input starvation publishes `BUFFERING` from FFmpeg's interrupt callback.
Read service time shares the remaining presentation interval, bounded below by
decoder cadence rather than gaps between interleaved events; only its excess
suspends the clock. Decode/filter work and late worker wake-ups do not accumulate
gaps in audio timestamps. Internal filter-feed events do not set the read budget.
The UI notification debounce is independent of clock accounting. Accepted seeks
and clock resets discard pending read accounting. Pause/stop/error/completion take
precedence over buffering callbacks. Live inputs retain their existing timing policy.

Installation qualifies a candidate in a separate process before publishing
`obs-plugins/solin-media-source.{dll,so,dylib}` and its matching data directory.
`media-playback-manifest.json` records the relative plugin path, plugin SHA-256,
upstream version, source archive SHA-256 and each applied patch with its SHA-256.
Packaging requires the current patch receipts. A failed load or ABI
check does not install the candidate; there is no decoder fallback.

CTest exercises the patched upstream seek function with controlled decoder and
FFmpeg boundaries, including rejected seeks, EOF reset and nonzero stream indices.
The tempo contract runs the real FFmpeg graph with short packed/planar audio,
unspecified channel ordering, repeated drains and rate changes from 1% to 200%.
It also exercises the upstream format initialization and interrupt callback to
verify finite/live input cancellation while retaining remote-only timeout options.
Terminal contracts cover failure/EOF mapping, masked I/O errors, corrupt final
packets, cancellation, state-transition races and immediate Play/Restart after
failure. Native integration tests cover local/HTTP transport, output pitch,
audio continuity, A/V synchronization, non-unity EOF drain and finite starvation:

```text
python -m pytest tests/integration/test_native_media_startup.py tests/integration/test_native_media_rate.py tests/integration/test_native_media_buffering.py tests/integration/test_native_media_cancellation.py tests/integration/test_native_audio_monitoring.py
```
