# Media playback module

`solin-media-source` registers `solin_media_source`, using the OBS 32.1.2 media
source and media-playback implementation. It links the installed pylibobs libobs
and matching FFmpeg libraries; it does not replace upstream modules or libraries.

```text
python scripts/build_media_playback.py
```

The builder requires CMake 3.25+ and a native C compiler: MSVC x64 on Windows,
Apple Clang on macOS, or the Ubuntu 24.04 toolchain on Linux. Supported targets
are Windows/Linux x86_64 and macOS x86_64/arm64. Linux additionally requires
`libavdevice-dev`, `libsimde-dev` and the media-extraction development packages.
Windows/macOS reuse the checksum-pinned OBS dependencies SDK from the extraction
builder. macOS imports and rpaths resolve inside the private `Frameworks` tree.

The source archive is SHA-256 verified on every build. Exact audited changes
reject attached-picture streams as playback video, reset demux EOF after a
successful seek, preserve queued frames after a rejected seek, select the timed
playback stream for seeking, enable stop/destruction cancellation for finite
inputs, and give the source its own identifier. Original upstream
license headers and `COPYING` are preserved. A source change that invalidates an
audited transformation fails the build.

Initialization/decode failures report `ERROR` without an end-of-media signal.
Terminal I/O errors and a final packet marked corrupt remain failures when the
demuxer returns EOF. Explicit stop/destruction takes precedence over cancellation;
Play and Restart rebuild a failed decoder on the OBS source owner thread.
Non-looping completion retains the decoder and duration without rewinding or
preloading; explicit Play/Restart resets it and rebases the playback clock.
Looping continues to reset immediately.

Installation qualifies a candidate in a separate process before publishing
`obs-plugins/solin-media-source.{dll,so,dylib}` and its matching data directory.
`media-playback-manifest.json` records the relative plugin path, plugin SHA-256,
upstream version, source archive SHA-256 and applied fixes. A failed load or ABI
check does not install the candidate; there is no decoder fallback.

CTest exercises the patched upstream seek function with controlled decoder and
FFmpeg boundaries, including rejected seeks, EOF reset and nonzero stream indices.
It also exercises the upstream format initialization and interrupt callback to
verify finite/live input cancellation while retaining remote-only timeout options.
Terminal contracts cover failure/EOF mapping, masked I/O errors, corrupt final
packets, cancellation and immediate Play/Restart after failure.
After installation, run `python -m pytest tests/integration/test_native_media_cancellation.py`
to verify bounded close/destruction against a real stalled HTTP origin in a subprocess.
