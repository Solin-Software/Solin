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
benchmark. Consumer tests share a broker endpoint and use a CTest resource lock.
