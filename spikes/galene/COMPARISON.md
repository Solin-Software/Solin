# Galene vs LiveKit for Solin-hosted meetings

Summary of the two spikes. Both were measured on the same machine (i7-13700T, home line), with the same libobs program, test clip and Brio rig wherever possible.
- **LiveKit** results: `spikes/hosted-meetings/RESULTS.md` (S1–S9).
- **Galene** results: `spikes/galene/RESULTS.md` (G1–G6, branch `spike/galene`).

## Bottom line

- **Both work for a hall-hosted meeting.** Galene passed every test it was given; LiveKit had already passed the same ground.
- **Galene is lighter and simpler. LiveKit has more features and more support behind it.**
- **The choice comes down to two features we chose to drop for Galene:**
  - **end-to-end encryption;**
  - **community relays.** The relays also limit how far a hall on a weak line can scale.
- **Recommendation: stay with LiveKit as the main engine**, unless we're willing to give up those two features for good. If we are, Galene is the better fit.

## Side by side

| | LiveKit | Galene |
|---|---|---|
| **Server footprint, 50 / 100 viewers at 720p** | 0.58 / 0.93 cores, 298 / 487 MB | **0.26 / 0.57 cores, 47 / 74 MB** |
| **Hall upload, 50 viewers** (720p / 360p / audio) | 88.5 / 25.2 / 2.7 Mbps | 89.2 / 26.1 / 3.2 Mbps (same: set by bitrate) |
| **Hall PC budget, 50 viewers at 720p** | ~1.4 cores | **~1.1 cores** |
| **Publishing from libobs** | Python SDK directly (raw frames → SDK encodes, 3-layer simulcast). VAAPI encoder leaked memory; software encoding used, upstream fix proposed | Needs our own Go helper (`whippub`, ~400 lines, Pion + libopus via cgo). libobs encodes video; helper encodes Opus with FEC. **Single layer**: simulcast not built yet |
| **Receiving comments in the hall** | Python SDK | Our own Go helper (`hallrx`) |
| **Video latency, camera → screen** (local) | 263 ms (A/V sync −48 ms) | 317–333 ms (sync −97 to −121 ms; ~−100 ms of that is the camera, inside libobs) |
| **Comment round trip** (local, incl. 40 ms emulated echo) | ≈150 ms (biased page timing) | **96 ms** |
| **Echo cancellation** | 61–66 dB | 60–69 dB (same canceller, in the hall) |
| **No self-unmute, operator unmutes, instant cut** | ✅ | ✅ (two permission names: `observe` / `present`) |
| **Commenter limited to the mic** | ✅ server-side (`can_publish_sources`) | ❌ client convention only |
| **Lobby** | ✅ in the server (hidden participants) | ✅ via the hub (signed token issued on admit) |
| **Rename, hide attendees from each other** | ✅ | ❌ (rename = new token + rejoin; everyone sees the list) |
| **Lock, kick, no recording, raised hands** | ✅ | ✅ |
| **End-to-end encryption** | ✅ Chromium + Firefox | ❌ not supported |
| **Community edge nodes / relays** | ✅ measured; failover 0.3–3.5 s | ❌ no cascading |
| **CGNAT hall, IPv6-only 4G phone** | ✅ via dual-stack TURN or edge node | ✅ direct over IPv6 when the hall has it; ✅ via dual-stack TURN (UDP and TCP) |
| **Devices** | Chromium, Firefox (E2EE test) | Chromium, Firefox, Android Chrome (H.264). iOS / old Android untested in both |
| **Reconnect** | SDK resume (~25–30 s budget) + our own rejoin loop needed (S5) | Nothing built in: our own loop (done in the viewer: back ~0.2 s after unlock) |
| **Phone screen locked** | not tested | audio stops ~17 s after locking (Android drops the page's connection) |
| **Deployment** | One Go binary; Redis only for multi-node | One 13 MB Go binary, JSON config |
| **Project** | Company-backed, large community, SDKs for every platform | NLnet-funded, essentially one main developer, small community; MIT |

## What each choice commits us to

**LiveKit**
- **Keeps:** E2EE, community relays with failover, server-side audio-only commenters, rename and hidden lobby; the Python SDK end to end.
- **Still to do:**
  - the attendee page's rejoin loop and auto-admit on rejoin (S5);
  - VAAPI (until the upstream fix ships) or software encoding;
  - the screen-lock behaviour, not yet measured.

**Galene**
- **Gives up:** E2EE and community relays. A hall's upload then caps its audience: ~90 Mbps for 50 viewers at 720p. Weak lines must go 360p (~26 Mbps) or audio-only (~3 Mbps). There's no relay to take the fan-out.
- **We build and maintain:**
  - the Go publisher and receiver helpers (cgo libopus, so packaging per platform, Windows included);
  - two-layer simulcast in the publisher;
  - the lobby and token issuing in the hub;
  - our own reconnect;
  - our own web client conventions (audio-only commenters, no attendee list).
- **Gains:** about half the server CPU and a sixth of the memory, a much smaller and more readable server, and simpler moderation.

## Open in both

- **Listening with the screen off on Android:** wake lock, a server grace period, or an installed app with a foreground service.
- **Audio on lossy Wi-Fi.** Opus FEC was tried on Galene and helped little against burst loss. Audio retransmission or RED is the next step, not measured on either.
- **Safari on iOS and old Android devices.**
- **Real hall hardware:** mixer, stage mics and loudspeakers for the echo path; the actual hall PC and line.

## Recommendation

**Keep LiveKit as the main engine.**

Galene's advantages are real but not decisive:
- Both servers fit comfortably within the hall PC budget (1.1 vs 1.4 cores).
- Their latency is in the same range.

What Galene lacks are structural features:
- End-to-end encryption.
- Relays that let a hall on a weak line serve many attendees.

Neither can be added without writing an SFU ourselves, which is what we set out to avoid.

**Choose Galene instead if we decide, as a product decision, that:**
- meetings are always hall-hosted;
- the server operator (the congregation itself) is trusted, so E2EE isn't needed;
- weak-line halls will use 360p or audio-only rather than relays.

In that world it's the leaner, simpler engine, and the spike code (`whippub`, `hallrx`, the viewer with reconnect, the hub-token lobby) is a working starting point.
