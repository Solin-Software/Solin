# Camera discovery and capture

Camera inventory, supported modes, and capture health are separate contracts.
Finding a camera or reading its capabilities does not establish that the device
can currently deliver video. All capture runs through the supervised libobs
process. Scene framing and layer transforms are independent of capture modes.

## Connections and scene layers

A camera connection belongs to the profile's shared resource catalog. It owns
the connection name, local device or network address, capture settings, and
credential reference. Collections store camera references and materialize their
current definitions from that catalog. Reusing a connection in another scene or
collection does not copy its settings or open a separate capture connection.

Each occurrence is a `SceneLayer` with its own name, framing, visibility, and
transform. **Rename…** changes only that layer's name in its scene.
**Configure camera…** edits the shared connection; every scene using it receives
the updated settings. A layer alias does not rename the shared connection.

Adding a layer or changing its name requires a unique name within that scene.
Comparisons trim surrounding whitespace and apply Unicode case folding.
Loading preserves legacy duplicate layer names, and accepting an unchanged
name in **Rename…** preserves those existing duplicates.

The add-camera dialog offers saved connections and a **New connection…** option
for network cameras. Selecting a local device reuses its existing connection
when possible. Reusing a connection asks for the name in this scene and shows
the saved settings; changing those settings is an explicit configuration flow.
Saved devices remain identifiable when disconnected. An empty inventory does
not offer a substitute automatic device: Automatic selects a mode for a device.

**Find cameras…** in the network camera's connection list searches the local
networks for ONVIF cameras with a WS-Discovery probe sent from every IPv4
interface that can multicast. Cameras that answer join the list beside the
saved connections, marked when a saved connection already uses their address.
Choosing one asks the camera, through its ONVIF device service, for the RTSP
address of each media profile and for its PTZ service; the first stream fills
the RTSP address, and a camera with a movable profile also gets ONVIF PTZ
control. Many cameras answer anonymously; one that refuses asks for its login,
which is then sent with **Connect**. In the ONVIF PTZ settings, **Find PTZ
settings…** reads the PTZ service and profile token from a found camera or from
the host of the current RTSP address.

The multicast probe only reaches the local network segment, so a camera on
another subnet, or behind a switch that drops multicast, does not answer it.
The address field below **Find cameras…** sends the probe directly to an
address (`192.168.0.100`), a range (`192.168.0.1-254`), or a network of up to
1024 addresses (`192.168.0.0/24`), which crosses routers; its finds join the
list. A single address that ignores the probe is still found when an ONVIF
device service answers on port 80, 8899, 2020, 8000, or 8080. A sweep spreads
its probes over several sockets because probes to absent hosts hold send-buffer
space while ARP waits. Replies are corrected for common firmware
quirks: an advertised address the camera did not answer from is replaced by the
address it did, and unescaped `&` characters in stream addresses are accepted.
A login a camera embeds in a stream address is removed; logins are stored in
the system keyring like any other camera credential.

Scene references have their own searchable chooser and layer name. The chooser
excludes the current scene and explains when another scene would introduce a
circular reference. The document graph validates the proposed layer before it
is committed. Failed validation leaves the dialog open with its entered values.
Dialog submissions also validate their scene and connection context; a stale
dialog asks the operator to reopen it instead of applying settings elsewhere.

Local device comparisons use `local_camera_device_key`. Windows ignores the
DirectShow friendly-name prefix and identifier case; macOS and Linux preserve
identifier case. An empty identifier does not identify a physical device.
Legacy aliases with different selectors or incompatible settings remain
separate records. Equivalent settings can reuse a stable existing identity,
while incompatible matches block the affected mutation with a contextual
conflict. Loading a collection preserves these aliases and their settings.

## Metadata

On Windows, discovery reads the DirectShow plugin's dynamic properties.
Temporary settings run the dependent property callbacks;
enumeration does not create another capture source. A small native boundary
owns the properties and settings that the Python binding does not expose.
The plugin contracts target the OBS version pinned by the project.

On macOS, metadata reads AVFoundation devices, format descriptions, and exact
frame durations without creating a capture session or requesting permission.
The modern capture plugin requires a source instance for its dynamic properties;
metadata therefore does not use those properties. Native format identity is
stored separately from pixel subtype and passed as `supported_format`. Camera
access is requested on the GUI thread when the operator submits a local camera;
startup, discovery, and recovery never prompt. App bundles declare camera and
microphone usage descriptions before signing. Source execution uses the
[macOS development host](building.md#macos) for its permission identity.

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
results. Device changes, explicit refresh, inventory replacement, and process
restart invalidate the cache. Concurrent requests share native work. Custom-size
queries are cached with the inventory and discard stale completions. A dispatch
worker runs outside foreground IPC handling and the Qt UI thread; native
enumeration runs in a disposable helper process with no capture sources or
graphics/audio outputs. Windows initializes COM on the helper's querying thread.
Each DirectShow inventory scan reuses one properties session, with fresh settings
for each device.

Native device notifications start when the scene engine becomes ready and remain
owned by the application controller until it closes. Platform adapters report
changes without running inventory queries or opening capture sources. The
controller marshals callbacks to its Qt thread, invalidates metadata, and groups
event bursts before refreshing an open local-camera dialog. Closed dialogs defer
enumeration until their next request. A change during discovery makes that scan
obsolete; its result is discarded and one subsequent scan obtains the current
inventory. Custom-size queries wait for current metadata. Existing device
selections, modes, and scene settings remain unchanged.

Notifications independently request capture reconciliation even when no dialog
has been opened. The controller sends a topology revision to the scene process;
it does not restart capture or change scene settings itself.

Windows uses device-interface notifications, macOS uses native camera
notifications, and Linux uses libudev events for video devices. If notifications
cannot start, the failure is logged and each subsequent dialog opening requests
a fresh inventory. Discovery failures can also be retried by reopening the
dialog; they do not trigger an automatic retry loop.

The helper has a 15-second budget covering startup, queries, and teardown, with
bounded forced cleanup. The caller's 20-second deadline also allows routing and
cleanup. Expiry or shutdown kills and reaps the helper; a later request starts a
fresh process. Requests that expire while queued do not enter native code.
Stage timings and bounded diagnostic tails identify unfinished native operations.
Discovery failures remain contextual camera errors. Camera requests neither
suppress compositor heartbeats nor determine the engine's availability.

## Mode selection

`LocalCameraConfig` stores either Automatic or one exact tuple of format,
resolution, and rational FPS. Dependent dialog controls offer valid combinations;
custom values must satisfy the reported range and steps. Missing devices and
saved modes remain identified instead of selecting a replacement device.

Automatic reads completed cached capabilities without starting or waiting for
discovery during scene preparation. It considers advertised modes up to
1920×1080 and nominal 30 FPS (with a 0.1% allowance for native clock
quantization). It prioritizes
fluid video, grouping 30 and 30000/1001 FPS in the target tier, then image area,
canvas aspect ratio, and the backend's ordering. If metadata offers no eligible
mode, including a cold cache at startup, Automatic makes an explicit attempt
using the plugin's native preference; that attempt does not promise Full HD.
Completing or refreshing discovery does not reconfigure an existing capture.

Resolution, pixel subtype, native format identity, and rational FPS all participate
in change detection.
Returning to Automatic explicitly resets all application-controlled native mode
settings. Connection mutations validate device conflicts before persistence or
graph mutation; historical conflicts remain available for correction.

| Backend | Exact-mode settings |
| --- | --- |
| DirectShow | Custom resolution, native format enum, frame interval in 100 ns units |
| AVFoundation | Native supported-format identity and rational frame rate |
| V4L2 | Native fourcc, packed resolution, packed rational frame interval |

## Capture lifetime

The runtime creates capture sources on first use, not during discovery or when
opening a configuration dialog. Local cameras share one native source per
physical device key; network cameras share one source per connection ID.
Consumers borrow the runtime-owned source. Mode changes update the existing
local source, and address changes update the existing network source.

Once instantiated, these sources stay cached for the runtime's lifetime.
Hiding a layer, changing scenes, or removing a scene occurrence does not release
the cached source. Runtime shutdown releases the capture probes and sources;
a restarted process creates them again on demand. There is no per-connection
keep-active preference. Native plugin activity and capture-health observation
remain distinct from the lifetime of the retained source.

Physical device changes start a bounded recovery cycle for instantiated local
cameras. Inventory work shares the isolated metadata catalog and never blocks
foreground IPC or waits for frames. Enumeration errors do not imply absence;
late enumeration can be retried. Obsolete topology results are discarded.

Windows recovery reapplies the source's last effective settings to the existing
DirectShow input, including a previously resolved Automatic mode. The runtime
checks the capture generation atomically before updating it. AVFoundation and
V4L2 handle physical reconnection natively; Solin does not issue competing
restarts on those platforms. A healthy source producing fresh frames is left
untouched. Mode rejection and observation failures are not connectivity retries.

Each physical-change cycle permits at most three metadata attempts and three
capture attempts, with 1- and 3-second backoff after failures and a 30-second
overall budget. Capture retries wait for the normal frame-health failure; hidden
sources keep their existing active-time observation semantics. Changed source
configurations and shutdown cancel affected recovery. There is no permanent
retry loop or recovery policy for stalls without a physical device notification.
The persisted native identifier must still identify the reconnected device;
recovery never selects another device by display name.

## Capture confirmation

Creating or updating a source starts an observation generation. A passive async
video filter observes frame dimensions and timestamps while returning the same
frame to the pipeline. It never consumes frames from the renderer. Deferred
updates wait for the native update signal and discard the pre-update frame.
Only subsequent fresh frames with the requested dimensions confirm readiness.
An internal frame sequence distinguishes continuing capture from a retained
ready state during a rapid unplug/replug. A recovery attempt retains the source
warning until fresh frames confirm readiness, including across scene hydration.

No frames, persistent dimension mismatch, failed updates, and observation
failures produce contextual health events. The timeout counts active capture
time; hidden sources do not time out for being inactive. Health events carry a
configuration fingerprint and document revision so old results cannot overwrite
the current configuration. A rejected manual mode is not silently substituted.

## Persistence and migration

| Contract | Current version |
| --- | --- |
| Scene document schema | 14 |
| Shared scene resource schema | 3 |
| Scene IPC protocol | 12 |

Scene schema 12 and resource schema 1 accept a missing or boolean `keep_active`
field in legacy camera configurations and remove it during migration. Other
legacy values and unknown configuration fields are rejected. Current local and
network camera configurations forbid the removed field. Raw inline camera
definitions are validated before catalog materialization can replace them, so
shared resources cannot conceal malformed collection data.

The resource repository atomically normalizes an older on-disk schema even when
its typed value equals the value being saved. Scene document primary and backup
reads apply the same validation and migration rules. Unsupported future versions
are reported without rewriting their original files, including during recovery.
IPC requires the current protocol version rather than migrating wire messages.

## Validation

Unit and contract tests cover the platform settings, dependent capability
queries, interval constraints, Automatic policy, cache lifecycle, shared source
reuse, conflicts, and stale events. QML integration tests exercise dependent
controls, refresh, missing selections, asynchronous custom-size errors, shared
connection reuse, and layer naming. Migration tests cover strict legacy parsing,
current-field rejection, resource normalization, and primary/backup recovery.
`tests/integration/test_native_camera_capture.py` runs synthetic native sources
in isolated processes to check passive observation, rendered pixels, and
deferred updates without requiring camera hardware.
The Windows synthetic recovery scenario also exercises an initially silent
source, same-source reactivation, native update acknowledgement, and new frames.

Device-notification tests cover platform filtering and teardown, event bursts,
changes during discovery, deferred refresh while hidden, and recovery on reopening
after notification or discovery failure. QML tests exercise arrival and removal
without invoking a refresh control and preserve the operator's draft settings.

Hardware qualification must additionally compare advertised modes, native
settings, and received dimensions on Windows, macOS, and Linux. For a reported
field-of-view problem, compare the camera image with direct capture in the same
mode. A log showing 640×480 alone does not establish the cause of visual zoom.
Record cold and cached discovery P50/P95 on each qualification machine, together
with device and plugin versions; cached dialog opens should not repeat native
enumeration.
