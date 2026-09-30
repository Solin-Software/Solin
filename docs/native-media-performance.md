# Native media performance measurements

This guide records measurements for the former QtMultimedia-to-native Program
route. Its harness, `scripts/benchmark_native_media_route.py`, targets that
removed playback adapter and cannot qualify the current libobs playback path.
The historical harness generates a reproducible
video fixture, records the exact scenario, samples explicit process IDs, and writes raw
one-second buckets plus P50/P95/P99 summaries.

The report is evidence, not a synthetic acceptance test. It does not contain a built-in CPU
threshold because CPU depends on the recorded hardware, decode path, outputs, and consumer.
The release budgets and comparison rules remain in
[`native-scenes-engine-implementation-plan.md`](native-scenes-engine-implementation-plan.md#performance-and-reliability-budgets).

## What the harness measures

For every supplied PID, each bucket records:

- process CPU time consumed during the actual bucket duration;
- `cpu_one_core_percent`, where 100% means one fully occupied logical processor;
- `cpu_host_percent`, the same value divided by the recorded logical processor count;
- resident memory at the end of the bucket, when the platform exposes it.

The aggregate is the sum of only the supplied processes. A normal virtual-camera run should
sample Solin, `solin-media-engine`, and the camera consumer separately. This prevents sidecar
spikes from being hidden inside application totals and prevents unrelated processes with the
same executable name from entering the result.

The harness does **not** measure GPU-engine utilization, frame latency, DWM, driver CPU, or a
process whose PID was omitted. A `hardware` decode label records the configuration being
qualified; it does not prove which decoder Qt selected. Use ETW/GPUView and the engine frame
telemetry gate for those questions.

## Generate the pinned fixture

Install the repository-pinned GStreamer development runtime as described in
[`building.md`](building.md), then generate the fixture and its manifest:

```powershell
python scripts/benchmark_native_media_route.py fixture `
  --output build/benchmarks/native-media-route/fixture-720p30.mp4 `
  --manifest build/benchmarks/native-media-route/fixture-720p30.json
```

The generator reads the GStreamer version and installer digest from
`scripts/install_gstreamer_windows.ps1`, rejects a different reported version, and records the
pinned installer digest, actual `gst-launch` SHA-256, and complete pipeline. The input is a
deterministic 1280x720, 30 fps,
180-second H.264/MP4 stream generated with a single-threaded OpenH264 encoder. The manifest
records the final asset SHA-256, dimensions, frame count, codec, and duration. Generated
fixtures and reports stay under ignored `build/` output and are not committed.

The fixture has no audio. It is intended to isolate the video path; it cannot qualify audio
mixing, audio device selection, or A/V synchronization.

## Record the exact scenario

Create a configuration before running Solin. For the documented 720p30, two-presenter, virtual
camera budget, an OBS x64 NV12 example is:

```powershell
python scripts/benchmark_native_media_route.py configure `
  --fixture-manifest build/benchmarks/native-media-route/fixture-720p30.json `
  --output build/benchmarks/native-media-route/two-presenters-vcam.json `
  --native-presenters 2 `
  --editor-preview closed `
  --virtual-camera on `
  --consumer-application "OBS Studio" `
  --consumer-architecture x64 `
  --consumer-profile "NV12 1920x1080 30 fps" `
  --decode-path automatic `
  --ingress-transport d3d11_shared_texture
```

Use the real consumer name, architecture, negotiated profile, presenter count, Preview state,
decode path, and content-ingress transport. The command rejects placeholder consumer metadata.
Create separate configurations instead of changing outputs during a measurement.

Then:

1. Start the Release build being qualified.
2. Open the generated fixture through the normal Solin playback workflow without repeat.
3. Select the intended Program scene and enable only the outputs in the configuration.
4. Open the configured virtual-camera consumer and verify that it receives moving video.
5. Confirm that the log entry `Native content ingress transport selected` matches the
   configuration. A fallback run is a separate measurement, not an accelerated-run sample.
6. Allow source preparation, shader compilation, and consumer negotiation to settle.
7. Record the exact PIDs from Task Manager or `Get-Process`.

Start the recording early enough that warmup plus measurement ends before the fixture reaches
EOF. Looping is rejected by the steady-state configuration because a seek/replay seam is a
different workload and can create a legitimate transient CPU spike.

Attach using explicit roles. A 120-bucket run provides more useful tail evidence than the
minimum 60 buckets:

```powershell
python scripts/benchmark_native_media_route.py record `
  --configuration build/benchmarks/native-media-route/two-presenters-vcam.json `
  --process solin=1234 `
  --process media_engine=5678 `
  --process camera_consumer=9012 `
  --warmup-seconds 10 `
  --duration-seconds 120 `
  --output build/benchmarks/native-media-route/result.json
```

Replace every example PID. The harness fails if a PID is missing, exits, changes executable,
or cannot be sampled. It requires at least 60 one-second measurement buckets because a P99
from a shorter run would overstate its statistical meaning.

## Report and comparison contract

Every report includes:

- full source commit, branch, and dirty-worktree flag;
- OS, CPU model, logical processor count, memory, and best-effort Windows GPU/driver inventory;
- complete fixture and scenario configuration plus their SHA-256 provenance;
- warmup, target bucket duration, actual bucket durations, and CPU normalization rules;
- raw buckets and P50/P95/P99/min/max per process and for the sampled-process aggregate;
- explicit interpretation limits.

Compare two reports only when all of the following match:

- hardware, power mode, display topology, and driver versions;
- fixture SHA-256, playback/decode configuration, and content-ingress transport;
- presenter, Preview, virtual-camera, consumer, and negotiated-profile configuration;
- Release configuration, warmup, duration, and process-role coverage.

Keep raw reports. Do not quote only an average, discard the first run without recording why,
mix Debug and Release builds, or compare Task Manager snapshots taken at different phases.
If the working tree is dirty, the report says so; record the corresponding patch with the
result or repeat from a clean commit.

Run at least these distinct configurations rather than enabling everything in one sample:

- playback only;
- playback plus one and two native presenters;
- virtual camera disabled, enabled without a consumer, and enabled with its consumer sampled;
- editor Preview closed/open;
- steady state and a separate transition/reconnect trace.

The steady-state report must not be used to explain a startup, transition, reconnect, or
device-loss spike. Those are separate workloads and need event-correlated ETW/GStreamer/D3D11
instrumentation.
