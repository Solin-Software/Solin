# OBS runtime corrections

The Windows build replaces only the pinned pylibobs `obs.dll`, built from the
same verified OBS source version. Original plugins, graphics modules and runtime
dependencies remain in place. Linux and macOS retain their distributed cores.

```text
python scripts/build_obs_runtime.py
```

Source PCM arrives in packets at a real-time cadence. Starting an empty WASAPI
endpoint leaves no margin for scheduler jitter: the device can insert silence
between otherwise continuous packets.

The ordered patch manifest separates core-only build configuration from the
WASAPI correction. The endpoint starts after its first nonempty packet is
submitted. Its reserve uses the existing 75 ms A/V latency allowance minus one
endpoint period, limited by available capacity. This leaves scheduling headroom
for small and large source packets without changing active media samples or
timestamps. EOF drains its queued tail. An empty
endpoint after an expired submission is rearmed before new audio is submitted.
The reserve does not compensate for persistent clock drift or arbitrary stalls.

Monitoring and output muting are independent: muting removes a source from
outputs while monitoring continues at its configured volume.

CTest exercises the patched submission functions with a simulated device clock,
including delivery gaps, capacity, failures, reset and output mute independence.
The candidate is qualified in a separate process before installation; its binary and source/patch hashes are
recorded in `obs-runtime-manifest.json`. Packaging rejects a core that differs
from its receipt or the current patch series. Source provenance and license
headers are retained.

The Windows monitoring integration test checks the rendered waveform through
isolated process loopback. It requires an audio endpoint and Windows build 20348+.

When the correction becomes available in the distributed runtime, qualify that
version against the same contracts, then remove this build, its patch series,
Windows CI steps and receipt requirement together.
