# Native virtual camera

This component provides the Windows DirectShow virtual-camera filter. Solin's
libobs sidecar produces NV12 frames and publishes the ring location through a
per-user named-pipe broker; the filter reads that ring and adapts frames to the
consumer's negotiated format. This component does not decode media or compose
scenes.

The filter is built for both x64 and x86 consumers. Its COM identity, broker
protocol, frame layout, and registration behavior are independent of the scene
engine implementation. Dependencies are pinned by digest and linked statically;
see [THIRD_PARTY.md](THIRD_PARTY.md).

Build and test both Windows architectures with:

```text
python scripts/build_virtual_camera.py --configuration Release
```

Portable frame-layout and broker-protocol tests can also run on Linux or macOS:

```text
cmake -S native/virtual_camera -B build/native/virtual-camera-tests -DCMAKE_BUILD_TYPE=Release
cmake --build build/native/virtual-camera-tests --parallel
ctest --test-dir build/native/virtual-camera-tests --output-on-failure
```

On Windows the suite additionally exercises frame transport, the DirectShow
consumer, backpressure, cadence, frame adaptation, and the frame-adapter
benchmark. A separate test filter uses process-local broker endpoints without
registering COM classes or connecting to an installed application's producer.

Windows CI opts into production DLL qualification with `--qualify-production`.
The harness uses `--production-broker`, also used by installer smoke tests, to
reserve the current user's broker endpoint before loading the filter. It fails
if another producer owns that endpoint and does not change COM registration.
Close Solin and other production-filter tests before opting in locally. The
ownership integration tests additionally require `SOLIN_TEST_PRODUCTION_CAMERA=1`.
Quality runs the functional suite; platform builds also enforce the `performance`
benchmark budget.

Output readiness requires published frames, not just an enabled setting or a
running broker. The sidecar observes publication progress and recreates stalled
outputs with bounded backoff. The consumer independently checks both heartbeat
and frame-sequence progress, shows standby for stale frames, and reconnects when
the producer generation changes. Unchanged pixels remain valid as long as new
frames are published.
