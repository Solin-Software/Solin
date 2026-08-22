# ADR-0001: Use a supervised native sidecar for Scenes

- **Status:** Accepted
- **Date:** 2026-08-22
- **Initial release stage:** Beta
- **Decision record:** [Discussion #12](https://github.com/Solin-Software/Solin/discussions/12)
- **Implementation:** [PR #11](https://github.com/Solin-Software/Solin/pull/11)

## Context

Solin needs authored Scenes, additional camera sources, transitions, media-window mirroring,
and a virtual camera without regressing the playback behavior already used during live
meetings.

Two implementations were evaluated:

1. [PR #9](https://github.com/Solin-Software/Solin/pull/9) replaces QtMultimedia with one
   in-process libobs engine that owns playback, composition, projection, and virtual-camera
   output. The evaluated state is commit
   [`dac0a8af`](https://github.com/Solin-Software/Solin/commit/dac0a8af).
2. [PR #11](https://github.com/Solin-Software/Solin/pull/11) retains QtMultimedia as the
   playback owner and adds a supervised C++ sidecar for sources, composition, presentation,
   and output publication. The decision snapshot is commit
   [`79a47ea2`](https://github.com/Solin-Software/Solin/commit/79a47ea2).

PR #9 offers a mature general-purpose compositor, fewer boundaries in its current media
path, and a stronger immediate Linux foundation. It also makes Scenes contingent on a full
playback migration. Real-runtime testing at the evaluated commit continued to expose
regressions and reliability gaps in established playback behavior, increasing operational
risk for live meetings. The review of PR #2 and Discussion #12 preserve the detailed evidence
and examples. This does not establish that libobs itself is unstable; it demonstrates that
replacing Solin's playback engine requires work and qualification beyond the Scenes feature.

PR #11 keeps the established playback, cache, reconnection, audio, metadata, and session
lifecycle contracts behind the existing Qt adapter. Native failures are limited to the
supervised composition/output process and can be recovered by restarting it and replaying
the latest validated state. Its current Windows implementation provides D3D11 composition,
native presentation, and a per-user DirectShow virtual camera for x86 and x64 consumers on
Windows 10 and Windows 11.

The decision compares the intended architectures, not only feature counts in their
prototypes. Neither alternative has completed the full low-end performance and reliability
qualification required for a stable release.

## Decision drivers

- Preserve the production playback contracts unless replacing them is necessary.
- Keep native source, compositor, and plugin failures outside the UI/playback process.
- Support Windows 10 as well as Windows 11 without elevation during normal use.
- Keep video queues bounded and make desired, pending, and applied scene state explicit.
- Allow the composition and transport paths to be optimized without changing playback,
  persistence, or UI contracts.
- Limit runtime, ABI, plugin, packaging, and licensing obligations to capabilities Solin
  currently needs.
- Retain a credible path to Linux and macOS without coupling platform publication APIs to the
  scene model.

## Decision

Solin will use the PR #11 architecture as the foundation for Scenes and virtual-camera
output.

- `QMediaPlayer` remains the single owner of user-projected media decoding, transport,
  trim, seek, speed, cache handoff, reconnection, and audio.
- The supervised native sidecar owns camera/RTSP acquisition, scene composition,
  transitions, native presentation, and Program output publication.
- Python/PySide6 remains the source of truth for persisted scene documents, automation,
  PTZ orchestration, supervision, and user-facing state.
- Windows uses the Solin-owned per-user DirectShow filter built for x86 and x64 consumers.
- Native Scenes initially ships as a Windows beta. Unsupported platforms retain their
  established behavior until their native backends are implemented and qualified.
- The existing OBS WebSocket integration remains independent from the native Scenes engine.

PR #9 is not selected for integration into `main`, but its work is not discarded.
Commit [`dac0a8af`](https://github.com/Solin-Software/Solin/commit/dac0a8af) records the
libobs state evaluated by this decision. The `scenes_and_virtual_camera` branch may continue
as an experimental alternative while an active owner keeps it synchronized with `main` and
validates real meeting workflows. If it becomes stale, a future experiment should start from
the then-current `main` and port only the still-relevant work in focused changes.

A composition-only libobs sidecar is not selected. In the current scope it would retain
libobs runtime, plugin, ABI, GPL, IPC, and packaging costs while giving up much of the unified
playback path that motivates PR #9. Libobs remains eligible for reconsideration when a
concrete requirement makes its broader capabilities material.

## Consequences

### Positive

- Scenes can evolve without replacing or requalifying the established playback engine.
- A compositor crash cannot directly take down playback or the main UI process.
- Engine restart and state replay are explicit, testable product contracts.
- The render and output implementation can evolve behind stable domain and protocol
  boundaries.
- DirectShow provides one Windows publication model across the supported Windows 10 and
  Windows 11 range.
- Solin retains flexibility over its eventual source-code license; the selected architecture
  does not impose GPL on the application.

### Costs and trade-offs

- Solin owns the scene model, IPC protocol, supervisor, compositor integration, native
  presenter, and virtual-camera lifecycle.
- The current Qt-decoded media ingress crosses shared memory and performs a D3D11 upload.
  Shared textures remain an optimization path, not a completed capability.
- Cross-process synchronization and recovery add complexity that an in-process engine does
  not have.
- Native Scenes is Windows-only initially; Linux `v4l2loopback` and macOS CoreMediaIO output
  remain future platform work.
- The narrower engine does not provide libobs recording, streaming, or third-party plugin
  capabilities.

## Beta and stable-release gates

The architectural decision does not declare the implementation stable. The beta must be
observable, reversible, and explicit in the UI. Stable release requires evidence for the
applicable budgets and scenarios in the implementation plan, including:

- P50/P95 CPU, frame-time, and latency measurements on the lowest supported hardware;
- a 30-minute soak without crash, deadlock, unbounded queues, or monotonic memory growth;
- a real sidecar/GStreamer/D3D11 crash and state replay while a camera consumer is active;
- installed x86 and x64 DirectShow qualification in supported OBS, Zoom, and Chrome versions
  on Windows 10 and Windows 11, including concurrent consumers and engine restart;
- qualification of native Raw/Program ownership transitions, device recovery, delayed first
  frames, rapid media changes, and multi-monitor operation;
- install, update, rollback, unregister, and uninstall verification for both filter
  architectures.

Keyed shared-texture ingress and editor egress remain preferred optimizations. They become
stable-release blockers only if the compatibility transport cannot meet the measured budgets
or required user experience.

## Reconsideration triggers

Create a superseding ADR before changing this decision. Reconsider libobs or another engine
when one or more of the following becomes true:

- recording, streaming, or a broad third-party source/plugin ecosystem becomes a committed
  product requirement;
- the native sidecar cannot meet its performance or reliability budgets after the planned
  shared-texture work;
- maintaining Solin's compositor and protocol costs more than adopting and qualifying a
  broader engine;
- the playback engine itself is intentionally replaced as a separate product decision with
  complete parity and migration criteria.

## References

- [Architecture bake-off discussion](https://github.com/Solin-Software/Solin/discussions/12)
- [PR #2: opt-in libobs playback and projection](https://github.com/Solin-Software/Solin/pull/2)
- [PR #9: unified libobs engine](https://github.com/Solin-Software/Solin/pull/9)
- [PR #11: native scene engine and DirectShow virtual camera](https://github.com/Solin-Software/Solin/pull/11)
- [Native scenes implementation plan](../native-scenes-engine-implementation-plan.md)
- [Native media performance methodology](../native-media-performance.md)
