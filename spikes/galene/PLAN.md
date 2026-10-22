# Spike: hall-hosted meetings on Galene

Throwaway experiments to see whether [Galene](https://galene.org/) can replace LiveKit for Solin-hosted hybrid meetings. Spike code lives under `spikes/galene/` and is never merged as product code. The LiveKit spike results in `spikes/hosted-meetings/RESULTS.md` are the baseline to beat.

## Scope

- **Galene runs on the hall PC** as a sidecar, like LiveKit in the main plan. Meetings are one-to-many: one program stream out, one commenter at a time back in.
- **Out of scope, by decision:** end-to-end encryption and community edge nodes. Galene supports neither.
- **Kept:** an optional hub that provides TLS/directory, the signaling tunnel for halls behind NAT/CGNAT and a dual-stack TURN relay (S4 showed dual-stack is required).
- **JW constraints still apply:** no self-unmute, the operator calls on commenters, commenters never on hall monitors, lobby/admit, no recording, raised hands shown on the conductor's device.

## Questions and pass criteria

| # | Question | Pass criteria |
|---|---|---|
| G1 | How does the program get from libobs into Galene? | Stable for 2 h, < 1.5 cores, A/V in sync, no memory growth, works on Linux and Windows |
| G2 | Which codec do all attendee devices play? | Chrome, Firefox (Ubuntu and Flatpak/snap builds), Safari iOS and an old Android all play the program |
| G3 | Can the hall PC serve 50 and 100 viewers? | Server CPU < 25% of the hall PC, hall upload close to bitrate × viewers, no freezes |
| G4 | Do Galene's permissions cover moderation? | Lobby, admit, lock, no self-unmute, operator unmute and revoke, hands, kick: all without patching Galene |
| G5 | Is comment audio good enough? | Mouth-to-ear < 400 ms; no echo (same bar as S3) |
| G6 | Can attendees reach a hall behind NAT/CGNAT? | The S4 cases pass, including IPv6-only 4G phone ↔ IPv4-only CGNAT hall |
| G7 | Can Solin drive Galene and ship it? | Groups, tokens and operator actions work from Python; Galene runs as a sidecar on Linux and Windows |
| G8 | Does a full meeting run cleanly? | 2.5 h with 50 viewers: no leaks, no drops, operator actions still work at the end |

**Go/no-go order:** G1 + G2 first. G4 and G6 next, because each has a likely blocker. Then G3, G5, G7, G8.

**Stop the spike early if:** no publish path stays under 1.5 cores; Safari iOS can't play any codec the publisher can send; a CGNAT hall can't be reached through TURN or the tunnel; or revoking `present` doesn't cut a live microphone.

## Setup

- Galene 1.2.1 (latest, 8 Sep 2026), built from source as a single static Go binary. Same i7-13700T machine as S1, publisher pinned to 8 threads.
- One group per meeting, e.g. `groups/hall.json`: `"codecs"` chosen in G2, `"allow-recording": false`, `"autolock": true`, `"max-clients": 120`.
- Reuse from the LiveKit spike: the libobs test clip, the flash/beep latency page (`s1_publish/web/`, adapted to the Galene JS client), the echo harness (`s3_audio/`), the connectivity scripts (`s4_connectivity/`) and the hub/tunnel setup (`s5_hub/`).

## G1: publish path libobs → Galene

Galene ingests over WHIP (`https://<host>/group/<name>/.whip`, bearer token from an invite or `galenectl create-token`). Galene forwards packets and never transcodes, so viewers get exactly what the publisher sends.

| Path | How it works | Open questions |
|---|---|---|
| **A. libobs WHIP output** (`obs-webrtc`) | libobs encodes, including hardware H.264, and publishes over WHIP itself | pylibobs ships the plugin on **Windows only**; Linux needs it built. Simulcast over WHIP? |
| **B. libobs raw frames → FFmpeg WHIP muxer** | The S1 raw callbacks pipe frames into FFmpeg 8, which encodes and sends WHIP | FFmpeg WHIP: H.264 + Opus only, single layer. Latency and A/V sync through a pipe |
| **C. libobs raw frames → Go helper** (`gclient`) | A Go sidecar speaks Galene's native protocol, so it can send VP8 and two simulcast layers | One more sidecar; we write the encoding glue |

Steps:
1. **Server sanity check:** OBS Studio's built-in WHIP output → Galene → browser. Rules out server-side problems before writing glue.
2. **Measure path A on Windows and path B on Linux.** Build C only if neither passes or simulcast turns out to matter in G3.
3. **For each path, measure** the S1 metrics: CPU, glass-to-glass latency, A/V sync over 30 min, memory over 2 h, plus layers received by the browser.

## G2: codec and device compatibility

- **Groups to test:** `["h264","opus"]` (WHIP paths A and B) and `["vp8","opus"]` (path C, Galene's default and its best-supported codec).
- **Devices:** Chrome desktop, Firefox Ubuntu deb + snap/Flatpak (older reports of broken H.264), Safari iOS, an old Android phone, Galene's own web client and our minimal page.
- **Per device, record:** plays yes/no, decode CPU, and whether audio continues with the phone screen off (an S8 overlap).

## G3: hall capacity, 50 and 100 viewers

- **Load generator:** a small Go program on `gclient` that joins N `observe` viewers and receives the program. It runs from another machine (a cloud VM, as in S4/S5), not the hall PC.
- **Variants:** audio only; 360p; 720p; with and without a low simulcast layer (path C only).
- **Measure:** Galene CPU and RAM, hall upload Mbps, packet loss and freezes on a real browser viewer while the load runs.
- **Compare with S2:** LiveKit at 360p default, 720p ≈ 1.7 Mbps per attendee.

## G4: moderation mapping

| Need | Galene mechanism to test | Watch for |
|---|---|---|
| Lobby (hears nothing, invisible) | No waiting room in Galene. The hub/Solin holds people on a lobby page and mints a signed token (`authKeys`) only on admit | Token lifetime; a re-join skips the lobby? |
| Admit | Token issued, client joins with `observe` | Time from admit to hearing the program |
| No self-unmute | Attendees have `observe` only; the client UI has no mic | A modified client can't publish (server refuses) |
| Operator unmutes one commenter | Operator `useraction present` on that user; the client publishes its mic on the permission change | Camera also allowed with `present`? Needs our client to send audio only |
| Operator ends the comment | `useraction unpresent` | **Does the server close the live upstream immediately?** If not, op `mute` plus close |
| Raised hands | User status via `setdata`, shown on the stage page | Hidden from other attendees? |
| Lock / passcode | Tokens, `autolock`, `lock`/`unlock` | Operator can be remote through the hub |
| Kick, rename | `useraction kick`; display name | Renames without rejoining? |
| No recording | `"allow-recording": false` | Operator can't override it |
| Commenters off hall monitors | Solin decides what the hall shows | Nothing to test in Galene |
| Who sees the attendee list | Galene sends `user` messages to every client | Can `observe` clients be kept from seeing other attendees? |

Script it like `s9_moderation/moderation_test.py`: an operator client plus test clients, with a pass/fail check per row.

## G5: comment audio round trip

- **Solin's receive path:** Solin must receive the commenter's audio to feed the hall mixer. Options: the G1 Go helper also subscribes and hands PCM to Solin, or a Python client on aiortc speaking the Galene protocol.
- **Reuse S3:** clap-test mouth-to-ear in both directions, plus echo cancellation with the reference clocked to playout.

## G6: connectivity

- **Signaling:** the hall's Galene behind the S5 tunnel (frp + Caddy on the hub), WebSocket at `wss://<hall>.hub/...`.
- **Media:** dual-stack coturn on the hub via `data/ice-servers.json`. Galene's built-in TURN is documented as IPv4 only, so turn it off with `-turn ""`.
- **Key question:** does the **Galene server itself** gather TURN relay candidates (needed when the hall is behind CGNAT), or only hand TURN to clients? If only clients, test whether client-side relay alone reaches a CGNAT hall.
- **Cases:** rerun the S4 phone tests: IPv6-only 4G phone ↔ IPv4-only CGNAT hall, home Wi-Fi, UDP blocked (TURN over TCP/443).

## G7: Solin integration and packaging

- **Control from Python:** the admin HTTP API for creating groups and tokens; a WebSocket client for operator actions (present/unpresent, mute, lock, kick).
- **Sidecar lifecycle:** start, health-check and stop Galene from Solin on Linux and Windows; ports, certificates, binary size.
- **Licence:** MIT, compatible with GPL-3.0+; record it.

## G8: full-length run

- 2.5 h, 50 synthetic viewers plus 2 real browsers, a comment every ~5 min (present → speak → unpresent), lock/unlock and one kick.
- **Measure:** Galene and publisher RSS and CPU over time, drops, and whether every operator action still works at the end.

## Needed from you

- The phone on 4G again (G2, G6) and an old Android phone (G2).
- A Windows PC for path A and the Windows sidecar check (G1, G7).
- A cloud VM for the load generator and the hub (G3, G6); the S4 Google Cloud project worked.

## Results comparison (fill in)

| | LiveKit (spike results) | Galene |
|---|---|---|
| Publish CPU, 720p | +0.35–0.55 cores (software, 3 layers) | |
| Glass-to-glass video | 263 ms median | |
| 50 viewers: server CPU / hall upload | | |
| Moderation without patches | Yes (S9) | |
| CGNAT hall + IPv6 phone | Yes, dual-stack TURN (S4) | |
| E2EE / community relays | Yes / yes | Out of scope |
| Extra processes on the hall PC | LiveKit server + publisher | Galene + publisher (+ Go helper?) |
