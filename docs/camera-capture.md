# Camera discovery and capture

Camera inventory, supported modes, and capture health are separate contracts.
Finding a camera or reading its capabilities does not establish that the device
can currently deliver video. All capture runs through the supervised libobs
process. Scene framing and layer transforms are independent of capture modes.

## Metadata

On Windows and macOS, discovery reads the DirectShow and AVFoundation plugins'
dynamic properties. Temporary settings run the dependent property callbacks;
enumeration does not create another capture source. A small native boundary
owns the properties and settings that the Python binding does not expose.
The plugin contracts target the OBS version pinned by the project.

On Linux, metadata uses nonblocking V4L2 ioctls and skips metadata-only nodes.
This avoids opening those nodes through the capture plugin's enumeration path.
Frame sizes and frame intervals retain their discrete, continuous, or stepwise
constraints. Only formats supported by the V4L2 capture plugin are offered.

Modes correlate pixel format, resolution, and rational FPS. A resolution range
does not imply any FPS: custom sizes require their own native interval query.
Enumeration limits fail explicitly rather than silently truncating a device.
Inventory failures, individual capability failures, and an empty inventory are
reported separately. Device labels do not claim that capture has been verified.

The dialog loads capabilities asynchronously when opened and reuses successful
results. Explicit refresh, inventory replacement, and process restart invalidate
the cache. Concurrent requests share native work. Custom-size queries are cached
with the inventory and discard stale completions. Native metadata work runs on a
worker outside foreground IPC handling and the Qt UI thread.

## Mode selection

`LocalCameraConfig` stores either Automatic or one exact tuple of format,
resolution, and rational FPS. Dependent dialog controls offer valid combinations;
custom values must satisfy the reported range and steps. Missing devices and
saved modes remain identified instead of selecting a replacement device.

Automatic considers advertised modes up to 1920×1080 and 30 FPS. It prioritizes
fluid video, grouping 30 and 30000/1001 FPS in the target tier, then image area,
canvas aspect ratio, and the backend's ordering. If metadata offers no eligible
mode, Automatic makes an explicit attempt using the plugin's native preference;
that attempt does not promise Full HD.

One runtime source is shared per physical device. Conflicting configurations
are rejected before document persistence or graph mutation. Resolution, native
format, and rational FPS all participate in change detection. Returning to
Automatic explicitly resets all application-controlled native mode settings.

| Backend | Exact-mode settings |
| --- | --- |
| DirectShow | Custom resolution, native format enum, frame interval in 100 ns units |
| AVFoundation | Serialized resolution, native format code, rational frame rate |
| V4L2 | Native fourcc, packed resolution, packed rational frame interval |

## Capture confirmation

Creating or updating a source starts an observation generation. A passive async
video filter observes frame dimensions and timestamps while returning the same
frame to the pipeline. It never consumes frames from the renderer. Deferred
updates wait for the native update signal and discard the pre-update frame.
Only subsequent fresh frames with the requested dimensions confirm readiness.

No frames, persistent dimension mismatch, failed updates, and observation
failures produce contextual health events. The timeout counts active capture
time; hidden sources do not time out for being inactive. Health events carry a
configuration fingerprint and document revision so old results cannot overwrite
the current configuration. A rejected manual mode is not silently substituted.

## Validation

Unit and contract tests cover the platform settings, dependent capability
queries, interval constraints, Automatic policy, cache lifecycle, shared source
reuse, conflicts, and stale events. QML integration tests exercise dependent
controls, refresh, missing selections, and asynchronous custom-size errors.
`tests/integration/test_native_camera_capture.py` runs synthetic native sources
in isolated processes to check passive observation, rendered pixels, and
deferred updates without requiring camera hardware.

Hardware qualification must additionally compare advertised modes, native
settings, and received dimensions on Windows, macOS, and Linux. For a reported
field-of-view problem, compare the camera image with direct capture in the same
mode. A log showing 640×480 alone does not establish the cause of visual zoom.
Record cold and cached discovery P50/P95 on each qualification machine, together
with device and plugin versions; cached dialog opens should not repeat native
enumeration.
