# ADR-0002: Use supervised libobs for media and Scenes

- **Status:** Accepted
- **Date:** 2026-10-02
- **Supersedes:** [ADR-0001](0001-use-a-supervised-native-sidecar-for-scenes.md)
- **Decision history:** [Discussion #12](https://github.com/Solin-Software/Solin/discussions/12)
- **Implementation:** [PR #64](https://github.com/Solin-Software/Solin/pull/64)
- **Cross-platform stabilization:** [PR #65](https://github.com/Solin-Software/Solin/pull/65)

## Context

ADR-0001 selected a supervised C++ sidecar for scene composition while keeping
QtMultimedia as the playback owner. That split reduced migration risk at the time,
but it left Solin with two media architectures: Qt owned user media playback while
the native GStreamer sidecar owned scene composition and native outputs.

As Scenes, projection, recording, cameras, remote streams, preview egress, and
virtual-camera output grew, the split increased the number of boundaries that had
to agree on source lifecycle, timing, frame transport, recovery, and platform
behavior. It also kept substantial Windows-specific GStreamer and D3D11 machinery
on the primary architecture while macOS and Linux needed separate delivery paths.

The libobs work originally evaluated before ADR-0001 was later revisited with a
different process model and a substantially more complete implementation. The
August evaluation in Discussion #12 covered an in-process unified libobs engine at
`dac0a8af`. At that point the architectural objection was not that libobs was
intrinsically unstable; it was that shipping Scenes would also require replacing
and requalifying the established playback path, while native media failures shared
the application process.

That qualification requirement was already visible in the review of PR #2. The
libobs path was deliberately kept opt-in: regressions in the established Qt path
were blocking, while libobs-specific readiness/seek, recovery, deterministic
fallback, fail-closed teardown, and lifecycle gaps were accepted only as tracked
follow-ups that had to be addressed before making the engine the default. In other
words, adopting libobs required a staged, safe playback migration rather than a
backend switch based only on feature coverage.

Beginning with the September rewrite of the experimental branch, libobs moved
behind the same supervised process boundary proven by the native engine. Playback,
source ownership, recovery, projection, recording, preview/thumbnail egress, and
virtual-camera integration were then implemented and exercised through that
sidecar. By October 1, Discussion #12 recorded that most issues raised during the
earlier bake-off had been resolved, while also preserving the remaining playback
limitations rather than treating them as solved.

The migration therefore addressed both dimensions of the earlier decision: the
native failure boundary moved out of the UI process, and the application-level
playback contracts were migrated incrementally and qualified before libobs became
the production default. The sidecar alone would not have been sufficient grounds
to supersede ADR-0001.

The application keeps ownership of persisted state, orchestration, and user-facing
behavior while the sidecar owns the native media runtime. This preserves crash
isolation and state replay without preserving the former split between playback and
composition.

The
conditions changed: the libobs proposal acquired the process isolation ADR-0001
required, the playback migration became an implemented and tested product path
rather than a prerequisite left to complete, and Solin's scope expanded into
recording and cross-platform native outputs where a unified media runtime has more
value. At that scope, libobs is also the stronger long-term ownership boundary:
continuing the purpose-built GStreamer/D3D11 engine would require Solin to maintain
an expanding compositor, source lifecycle, recovery model, and per-platform media
backends that libobs already provides as a cohesive runtime. The remaining
libobs-specific costs and playback limitations are accepted below as trade-offs of
the new architecture.

## Decision drivers

- Use one media runtime for playback, scene composition, projection, recording,
  cameras, streams, and native outputs.
- Keep native media and graphics failures outside the Qt UI process.
- Preserve the existing scene document, IPC, supervision, and state-replay
  contracts where they remain useful.
- Reduce duplicated decode, presentation, transport, and lifecycle code.
- Prefer qualified, reusable libobs media/composition primitives over expanding a
  parallel Solin-owned GStreamer/D3D11 engine across platforms.
- Make Windows, macOS, and Linux follow the same media architecture, with
  platform-specific output adapters only where the operating system requires them.
- Keep queues and cross-process frame transport bounded and observable.
- Package and qualify the exact libobs runtime used by release artifacts instead of
  relying on an unspecified host installation.
- Keep application state and product behavior independent from libobs-specific
  persistence formats.

## Decision

Solin uses a supervised libobs sidecar as its primary media and scene engine.

- libobs owns user media decoding and transport, scene composition, transitions,
  camera and RTSP sources, projection rendering, recording, and the native media
  outputs supported by each platform.
- The sidecar is supervised through the existing scene-engine process contract.
  Heartbeats, restart, IPC validation, and state replay remain application-level
  responsibilities rather than being delegated to the media library.
- Python/PySide6 remains the source of truth for persisted scene documents,
  playlists, automation, PTZ orchestration, application state, and user-facing
  controls.
- QtMultimedia is not a production playback engine. UI code may render static
  images or application-owned frames, but media files are not decoded through a
  parallel Qt playback pipeline.
- Release builds package and validate the pylibobs/libobs runtime and the plugins
  required by Solin. Missing native components or a failed packaged-runtime smoke
  test are build failures.
- Platform publication remains isolated behind platform adapters. Windows retains
  the Solin DirectShow virtual-camera components required by its consumers; Linux
  uses the available libobs/v4l2loopback path; macOS support follows the
  capabilities qualified by its packaged runtime.
- The former Windows C++/GStreamer scene engine remains selectable with
  `SOLIN_SCENE_ENGINE=native` as an escape hatch while it is still shipped. It is
  not a second product architecture for new media features. New media behavior
  targets libobs unless a separate ADR explicitly changes that decision.

## Consequences

### Positive

- Playback and composition share one source lifecycle and timing model.
- Media is decoded once in the engine that also composes and publishes it, reducing
  redundant frame conversion and fan-out paths.
- The UI process keeps the failure isolation, supervision, and replay properties
  that motivated ADR-0001.
- The same architectural boundary applies on Windows, macOS, and Linux, reducing
  platform drift in the application layer.
- Recording, scene sources, projection, and virtual-camera routing can share libobs
  primitives instead of growing separate media implementations.
- Solin maintains the product contracts, supervision, and platform adapters rather
  than also owning a growing general-purpose compositor/media engine.

### Costs and trade-offs

- Solin now depends on the libobs ABI, plugin set, graphics backends, and packaging
  behavior on every supported platform.
- Release qualification must cover the packaged native runtime, not just Python
  tests, because loader paths, plugins, graphics initialization, and native library
  identity are part of the product.

## Operational requirements

Changes to the media engine must preserve the following properties:

- bounded queues and explicit back-pressure for frame transport;
- deterministic sidecar teardown and restart without leaking native resources;
- replay of the latest validated scene/output state after a sidecar restart;
- packaged-runtime smoke coverage on every supported release platform;
- platform-specific integration tests where behavior cannot be established on a
  different operating system;
- P50/P95 measurements for performance-sensitive frame, presentation, and output
  paths when those paths change materially;
- no silent fallback to a second playback engine when libobs is unavailable.

Closing foreground transport silences audio immediately and removes its media
input through the native source settings. This unloads the decoder and closes the
file or network input while retaining the last native video texture for transition
origins and borrowed previews, including thumbnails. Retired media uses an empty
local input with end clearing disabled, so teardown cannot erase the picture or
schedule network reconnection. The sidecar collects retired sources on its control
heartbeat after their scene and showing references disappear; shutdown releases all
remaining references. Closing media never restores cached pixels from an earlier
application presentation.

Every visual producer publishes only at Take, after preparation and revalidation
of its requested presentation epoch. Native media opens its decoder privately;
opening transport never retargets a live Content layer. Preparation declares the
producer and does not wait for a native decoder queued behind it on the ordered
IPC stream. Take resolves the exact decoder epoch and primes a native video frame
on the GPU within its deadline; cached dimensions alone do not prove readiness.
Paused opens request a native seek to the initial position so the decoder
publishes its first frame without starting playback. Transport retains its own
clock and does not depend on an output selecting a Content scene. Audio-only opens
do not supersede preparations for application-owned visual content.
Cancellation or unavailable content leaves the live picture intact. Every new
presentation must reconcile its epoch even when the scene ID stays the same or
hydration is pending. Replaying the current media restarts its existing decoder.

BGRA ingress allocates one native source per presentation epoch and reuses it for
subsequent frames of that epoch. A new image cannot overwrite
the outgoing presentation's texture before its transition. The scene graph keeps
its committed source across hydration; new ingress sources attach only at an
accepted Take. Both native media and BGRA use the same Content presentation
transaction. Content scenes and their referencing ancestors get new composition
instances for a new presentation; shared cameras and other native sources are
reused. All compositions are private libobs scenes owned by the graph. Public
canvas scenes retain an additional native reference in OBS 32, so releasing only
the graph reference would keep their items and decoders alive. This preserves an
outgoing composition while another output or preview receives new content. The
control heartbeat releases superseded compositions and
sources after borrowed scene references and native showing references retire,
without cleanup timers.

### Session-owned idle screen

`idle_screen` sources refer to the session's shared idle content, independently of
the foreground Content presentation. The initial Default scene contains one
full-canvas Idle screen layer. Its content defaults to the styled annual text;
choosing an idle image or video updates every scene using this source. Videos
loop silently at normal speed. Removing the selected media restores the annual
text. Each layer retains its own placement, crop, visibility, and ordering.
Changing idle media does not select a scene or override customized default scenes
or manual routing. The annual-text settings and timer presentations remain
independent consumers of the annual text.

One dedicated idle owner holds a stable private libobs scene shared by all idle
layers, including nested scenes. Its committed producer is an annual-text PNG,
an image, or a video decoder separate from foreground media and background music.
The private scene uses the output canvas; its producer fits centrally with its
aspect ratio preserved and no automatic crop. Layer transforms and crop remain
outside this shared surface. Additional scenes and outputs share the same
committed decoder and playback clock.

Updates prepare a replacement privately and validate an uploaded image or a
usable native video frame before changing the visible content. The new item,
its transform, and removal of the old item commit under the native scene lock,
without rebuilding the scene graph or restarting cameras. Video input opens only
after mute, zero volume, disabled audio mixers, and disabled monitoring are set.
Rejected media, expired preparation, and superseded requests leave the committed
content intact. Observers detach before their source references are released.

The session remains the source of truth for the selected media. An explicit
`IdleScreenState` carries `revision`, `media_path`, `yeartext_image_path`, and
`yeartext_revision` through hydration and acknowledged idle updates, independently
of Content presentation epochs. The PNG path and revision are supplied directly;
the engine does not resolve them from an environment variable. Updating annual
text while selected media is playing refreshes the fallback state without
replacing or restarting that media. Sidecar recovery and profile hydration replay
the latest idle state. If the selected file is unavailable during recovery, the
owner tries the annual-text PNG within the same deadline and reports the failure
without changing the session's choice. If the fallback cannot be prepared either,
the idle surface is transparent. Removing the media uses the latest annual-text
state. Foreground Content stays transparent during idle.

Idle preparation runs on one worker with one pending request. Newer valid choices
supersede older preparation and pending requests, keeping heartbeat and foreground
commands responsive while decoding the first frame. Only an accepted current
revision updates the session and interface. Shutdown cancels and joins preparation
before releasing native owners. Scene rebuilds and profile changes wait for the
current idle transaction before replaying its confirmed state.

Idle video playback is driven by live outputs whose effective source tree
contains idle content, including visible nested scenes and transition origins.
Without a live consumer, the shared video is paused at its prepared initial frame;
editor and Program previews and scene thumbnails display that static frame.
The first live consumer starts playback from this frame. Readiness of the initial
frame must be confirmed before releasing idle content to that output, so its first
appearance cannot expose a stale frame from an earlier playback position.
Additional live monitors, virtual-camera output, or recording share the current
loop without restarting while any live consumer remains. Previews and thumbnails
also show this shared playback while it is live. Losing the last live consumer
resets the video to its initial frame and holds it paused, even if previews remain
open or no consumers remain.

The native video source does not expose a seek-completion acknowledgement, so a
seek or playback-clock value alone cannot prove that its displayed texture is the
initial frame. Off live, the private idle scene displays its confirmed, immutable
initial frame while the old decoder is retired and a fresh inactive decoder is
prepared. Only a decoder with a confirmed initial frame can replace this surface
when live playback starts. There is one steady-state idle decoder shared by all
consumers; this frame remains inside libobs, without a Qt poster producer or a
Content-channel publication. If preparing the next decoder fails, the cached
initial frame remains visible and source health reports the restart failure;
this transport failure does not select the annual-text fallback or change the
session's selected media.

Transition demand includes both visible branches until video reaches its
destination. An outgoing idle branch remains a live consumer until it is no
longer visible; the reset after losing the last live consumer must wait for that
transition to finish.
Once video completes, it inspects the native active destination and releases the
temporary reference directly. Projection transitions may retain their old audio
branch without mixing it; that retained reference does not establish visibility.

The sidecar derives consumer visibility from effective egress `active` state,
virtual-camera and recorder `active` state, and window `render_targets`,
distinguishing previews from live outputs. Selecting Program or
enabling aggregate rendering alone does not establish a live consumer; projection
can require Program rendering without publishing Program itself. Graph hydration
and rebuilds suspend demand reconciliation so transient graph changes do not
create artificial pause or restart boundaries.

Scene schema 11 migrates every persisted `yeartext` source to `idle_screen`,
preserving source and layer identities and scene references. Migrated source
names become Idle screen; layers matching the previous source name follow the
rename, while custom layer names remain intact. The workspace localizes the
generic label when loading a collection. New sources use `solin.idle.current`;
migrated sources retain their previous IDs.

The native escape-hatch backend understands the schema and reports idle sources
as unsupported. It does not create an alternate decoder for this feature.

## Reconsideration triggers

Create a superseding ADR before replacing this architecture. Reconsider the
decision if one or more of the following becomes true:

- libobs cannot meet required playback, latency, stability, or platform-delivery
  targets after measured and bounded remediation;
- maintaining the pylibobs/libobs integration becomes materially more expensive
  than a qualified alternative;
- a required platform capability cannot be implemented behind the current sidecar
  and output-adapter boundaries;
- the process-isolation model itself becomes a measured performance or reliability
  limitation.

## References

- [Architecture bake-off and later libobs re-evaluation](https://github.com/Solin-Software/Solin/discussions/12)
- [ADR-0001: Use a supervised native sidecar for Scenes](0001-use-a-supervised-native-sidecar-for-scenes.md)
- [PR #2: opt-in libobs playback and projection](https://github.com/Solin-Software/Solin/pull/2)
- [PR #9: original unified libobs engine proposal](https://github.com/Solin-Software/Solin/pull/9)
- [PR #11: native scene engine and DirectShow virtual camera](https://github.com/Solin-Software/Solin/pull/11)
- [PR #64: replace the default native backend with supervised libobs](https://github.com/Solin-Software/Solin/pull/64)
- [PR #65: centralize release distribution and stabilize cross-platform runtime](https://github.com/Solin-Software/Solin/pull/65)
- [Building and platform requirements](../building.md)
- [Native Scenes implementation plan](../native-scenes-engine-implementation-plan.md)
