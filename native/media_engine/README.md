# Solin media engine

This directory contains the isolated native process for Solin-owned scenes. The current
checkpoint provides the build and warning policy, supervised-process protocol, strict
scene hydration, Media Foundation camera discovery, and a generation-aware source registry.
The internal GStreamer substrate captures local cameras, decodes RTSP, generates solid
color, reconnects failed sources, and publishes one latest BGRA frame per source through a
shared D3D11 device with coherent invalidation and a system-memory fallback. Registry
budgets cap aggregate frame pixels, decoder sessions, runtime generations, and consumers.
Validated scene references compile into an immutable DAG. Hydration stages candidate source
generations and atomically commits both output buses only after preparation succeeds;
manual preparation tokens are cancellable, single-use, revision-bound, and bind the
validated Program transition before Take. The renderer supports atomic Cut, cross-dissolve,
and fade-through-black. Animated transitions use a temporary D3D11 compositor, retain the
source leases required by both graphs, follow the Program output clock, and release their
temporary resources after completion. The editor Preview always cuts immediately. The
renderer builds one bounded BGRA/D3D11 pipeline per output, shares leaf sources and nested
scene nodes inside each pipeline, and implements crop, contain/cover/stretch fit, mirroring,
rotation, placement, and opacity. Solin content enters through a
versioned, three-slot latest-frame video channel. Qt video stays in NV12 and is bulk-copied
once per plane without `QImage` conversion or Python row loops. Protocol v4 preserves the
source strides and offsets, and `GstVideoMeta` describes that layout to the upload path;
static and unsupported formats use the BGRA path.
Protocol v4 leases individual slots while D3D11 upload consumes them, so the normal Windows
path wraps the SHM payload directly in `appsrc` without a second owned-pixel copy. The Qt
producer takes the control mutex with a zero timeout, chooses another unleased slot, or drops
the frame; it never waits for the engine. Auto-reset frame events and a process-local source
activity signal wake the source, compositor, and output workers without 1–4 ms polling. The
system-memory fallback still copies before releasing its slot. Keyed D3D11 texture ingress
remains the preferred future cross-process zero-copy path. The virtual-camera render branch
converts on the shared D3D11 device, downloads a
tightly packed NV12 edge frame, and publishes it through a separate lock-free, three-slot
shared-memory channel. A per-user DirectShow source filter consumes that channel through a
private protocol-v3 broker. The broker derives its pipe from the current SID and session,
rejects remote clients, validates the client's token and session, and reveals only a
read-only file-backed transport locator. Each x86 or x64 consumer owns an independent output
sample while reading the same latest frame. An exact-size NV12 consumer is filled directly
from the validated stable SHM span; staging remains only for scaling, letterboxing, and YUY2
conversion. The filter adapts the producer's NV12 frame to
eight fixed NV12/YUY2 30 fps profiles and serves a branded standby frame whenever the
producer heartbeat is stale. The public virtual-camera capability is enabled only when both
per-user filter registrations, x64 COM activation/device enumeration, cross-process
transport, and the supported Windows version pass the runtime probe. On Windows, physical
media surfaces render through engine-owned child windows and `d3d11videosink`; frames remain
D3D11-backed and do not cross back through Python. Raw-media targets acquire the canonical
`solin.content.current` runtime directly, independently of editable scenes, and bypass scene
composition. Program-mirror targets consume the already-transitioned Program bus. Both routes
are latest-frame/event-driven, with a bounded low-rate deadline only for window health and
shutdown. The expanded in-app player remains in
the Qt process and forwards the original QtMultimedia `QVideoFrame` to a `QVideoWidget`, so
opening it does not start a native scene-compositor output or materialize a `QImage` per
frame. A fixed BGRA channel feeds the independent scene-editor Preview. That compatibility
path samples the newest composed frame at no more than 30 fps and wraps the receiving owned
bytes in an immutable `QImage` without another full-frame copy. A demand-driven
dynamic NV12/BGRA channel remains the bounded Program fallback for Qt-owned surfaces that
cannot host the native presenter. Dynamic frames cross Python without conversion and are
materialized only on the receiving Qt thread, so shutdown never waits on a Qt Multimedia
conversion in a Python worker. Keyed-texture ingress, the exact border/corner-radius shader
path, and image sources remain later phases in
`docs/native-scenes-engine-implementation-plan.md`.

The native process must not import, link, or dynamically load libobs. OBS WebSocket support
belongs to the Python application's independent external-integration layer.

## Windows development build

From the repository root, the supported one-command build installs the pinned development
runtime when needed, configures CMake, builds, and runs the native tests:

```text
python scripts/build_native_engine.py --configuration Release
```

The lower-level presets remain available from this directory:

```text
cmake --preset windows-msvc
cmake --build --preset windows-msvc-debug
ctest --preset windows-msvc-debug
```

Generated files remain under the repository's ignored `build/` directory.

## Pinned GStreamer development toolchain

The Windows bootstrap script downloads the official GStreamer 1.28.5 MSVC x86_64
installer, verifies its pinned SHA-256 digest, and installs either runtime, development, or
debug components into an explicit directory. From the repository root:

```text
powershell -ExecutionPolicy Bypass -File scripts/install_gstreamer_windows.ps1
cmake -S native/media_engine -B build/native/media-engine-gstreamer -G "Visual Studio 17 2022" -A x64 -DSOLIN_MEDIA_ENGINE_ENABLE_GSTREAMER=ON -DSOLIN_GSTREAMER_ROOT=build/dependencies/gstreamer/msvc_x86_64
```

The GStreamer option builds the discovery and source-runtime integration tests, including
an isolated RTSP server. A development launch of Solin automatically discovers the
configured executable under `build/native/media-engine-gstreamer` and supplies the pinned
private GStreamer runtime; no path environment variable is required. The Windows release
workflow stages the engine and a separate runtime-only GStreamer installation under
`native/media-engine`, preserves its redistribution notices, rejects development headers
and link libraries, and runs the staged engine self-test before ZIP and Inno Setup
packaging. Plugin-level pruning remains a measured optimization rather than a precondition
for shipping a complete private runtime.

## Process contract

Control messages use a four-byte unsigned big-endian payload length followed by a bounded
payload. The maximum accepted control frame is 8 MiB. Video frames never use this channel.
The semantic protocol is versioned and rejects unknown envelope fields, duplicate JSON
keys, invalid identities, stale sessions, mismatched process generations, and expired
deadlines. Local-camera discovery, immutable scene-graph hydration, transactional
preparation, cancellation, output state, hot native-window target updates, rendered Program
transitions, media-window egress,
and Windows virtual-camera publication are available. Protocol version 3 binds Cut,
Dissolve, or Fade through black to `prepare_scene`; `take_prepared` consumes the resulting
token without accepting replacement effect parameters. If an animated effect cannot be
prepared, the response reports a typed fallback and Take still converges to the prepared
destination with Cut. Rapid requests retain only the latest destination and use the latest
composed GPU frame, or the last stable origin before the first composed frame, to preserve
visual continuity. Composed frames stay in bounded D3D11 or shared-memory latest-frame
transports; they never traverse the JSON pipe. Hardware
composition is advertised only when the runtime D3D11 compositor probe succeeds; individual
layer features remain schema-bounded. Virtual-camera readiness is probe-driven: a missing or
invalid x86/x64 DirectShow registration, unsupported Windows build, or unavailable
cross-process transport keeps that capability false and exposes a stable diagnostic code.

The build fetches the pinned nlohmann/json 3.12.0 release and verifies its SHA-256 digest.
It is used only for low-frequency control messages, never for video frames.
