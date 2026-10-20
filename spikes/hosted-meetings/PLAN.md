# Spike: Solin-hosted hybrid meetings

Throwaway experiments to de-risk Solin hosting hybrid meetings itself (LiveKit sidecar + own hub + community edge nodes) before any product code. Spike code lives under `spikes/hosted-meetings/` and is never merged as product code.

Context: [Solin-Software/Solin discussion #138](https://github.com/Solin-Software/Solin/discussions/138).

## Questions and pass criteria

| # | Question | Pass criteria |
|---|---|---|
| S1 | How does the program get from libobs into LiveKit? | Stable, < 1.5 cores CPU, A/V in sync, simulcast, **compatible with end-to-end encryption** |
| S2 | Can a typical hall PC and line serve 50 attendees directly? | CPU < 50%, hall upload close to the estimate, no freezes |
| S3 | Is remote comment audio good enough? | Mouth-to-ear < 400 ms, no echo in the hall or remotely |
| S4 | Which connection path does each hall/attendee combination use? | Matrix filled in with real results |
| S5 | What can one Oracle ARM VM carry as hub? | CPU and bandwidth per relayed attendee measured; signaling survives 2.5 h |
| S6 | Edge node + failover: how long does recovery take? | < 10 s, attendance not asked again |
| S7 | Does end-to-end encryption work with our publisher and browsers? | Yes for the chosen S1 path, plus Chrome, Firefox, Safari (iOS) |
| S8 | Does the browser client work on attendees' devices? | Phones keep audio with the screen off; old Android works |
| S9 | Do LiveKit's built-in controls cover moderation? | Lobby, admit, lock, hands, rename work without patching LiveKit |

## S1: libobs → LiveKit publish path (key decision)

Finding: pylibobs bundles the `obs-webrtc` plugin (WHIP output) on **Windows only**, not on Linux.

| Path | How it works | Advantages | Disadvantages |
|---|---|---|---|
| **A. WHIP output** | libobs `obs-webrtc` sends hardware H.264 + Opus to LiveKit Ingress (transcoding off) | Hardware encoding, low CPU | Extra Ingress service + Redis; plugin missing on Linux; simulcast uncertain; **Ingress sees the media → no E2EE** |
| **B. Raw frames → Python SDK** | libobs raw video/audio frames → `livekit` Python realtime SDK encodes | Simplest; simulcast built in; **E2EE supported**; no extra service | Software encoding: ~1.5–3 cores at 720p |
| **C. Encoded frames → Go publisher** | libobs encodes 3 layers in hardware; a Go helper (LiveKit Go SDK) publishes them | Hardware encoding, simulcast, no Ingress | New Go sidecar; E2EE support in Go SDK uncertain |

E2EE is the default on community nodes, so **A is effectively ruled out for edge mode**. Measure **B first**; prototype **C** only if B's CPU is too high on a 4–6-core hall PC.

Measure per path: CPU/GPU, glass-to-glass latency, A/V sync over 30 min, simulcast layers produced, quality at 360p and 720p.

## S2: hall capacity with 50 attendees

- LiveKit on the hall-like PC; Solin publishes via the S1 path.
- `lk load-test` with 50 subscribers from **another machine** (ideally an Oracle VM).
- Measure: hall CPU/RAM, upload Mbps, packet loss, simulcast layer distribution, effect of adaptive stream (part of subscribers paused).
- Variants: audio only; 360p cap; adaptive up to 720p; plus 1 remote camera.

## S3: comment audio round trip

- Remote browser → LiveKit → Solin → hall mixer; hall mix → remote attendees.
- Measure: mouth-to-ear latency both ways (clap test, recorded); echo with the mixer feeding Solin; Opus settings (mono/stereo, bitrate, noise suppression).
- Check: remote audio gets a clean, separate output device toward the mixer (mirrors today's Zoom wiring).

## S4: connectivity matrix

Halls:
1. Public IP + forwarded ports
2. NAT, no forwarding
3. CGNAT (emulated with a phone 4G hotspot)
4. IPv6 only

Attendees:
1. Home Wi-Fi
2. 4G
3. UDP blocked (firewall rule)
4. IPv6

Record per combination: path chosen (direct / ICE-TCP / TURN / edge), time to connect, success.

## S5: hub prototype on Oracle

- Oracle A1 VM in the region closest to the halls: Caddy (wildcard TLS), frp (reverse tunnel), coturn, edge LiveKit.
- Measure: CPU and outbound Mbps per relayed attendee and per edge viewer (50 and 150 simulated); **2.5 h signaling** through the tunnel; tunnel behaviour on a brief hall line drop.
- Bonus: idle-reclamation flags; Pay-As-You-Go upgrade behaviour.

## S6: edge node + failover

- Second LiveKit "community node" on home fibre (volunteer) or the Oracle VM.
- Steps: CGNAT hall publishes to the node → attendees connect → **kill the node** → Solin republishes to the backup, clients reconnect.
- Measure: time to recover audio/video; hands and attendance preserved (prototype join page with localStorage + device ID); what the operator sees.

## S7: end-to-end encryption

- LiveKit E2EE with the key in the link fragment, S1 publisher, browser clients.
- Verify: a node operator joining with a valid token can't decode anything; CPU cost on phones; simulcast still works; Safari iOS support.

## S8: attendee devices

- Desktop Chrome/Firefox, iOS Safari, Android Chrome, including a cheap/old Android phone.
- Checks: audio continues with the screen locked (iOS risk); adaptive stream pauses video in background; battery and data over 1 h; lobby/attendance stepper usability with an older person.

## S9: moderation via LiveKit primitives (`livekit-api`)

- Lobby-only token (can't publish, can't hear), upgraded on admit.
- Meeting-live lock: allowed publish sources = none, server-side mute, attempt client unmute.
- Raised-hand attribute with timestamp, ordered in Solin; auto-lower on re-mute.
- Rename, remove, move back to lobby.
- Pass: none of this needs a patched LiveKit.

## Resources

| Item | Notes |
|---|---|
| Hall-like PC | 4–6 cores, Intel Quick Sync, wired. Ideally a real Kingdom Hall media PC. |
| Second machine / VM | Load-test client |
| Oracle Cloud account | Hub prototype + remote load generator |
| Phone with 4G hotspot | CGNAT emulation |
| 3–5 volunteers' devices | S3, S8; one home fibre connection for S6 |
| Domain | Cheap test domain or a subdomain |

## Order and effort (rough)

| Week | Work |
|---|---|
| 1 | **S1** (B, then C if needed) and **S9** — decide the architecture; stop and review |
| 2 | **S2, S3, S7** on the chosen path |
| 3 | **S5, S4, S6** |
| 3–4 | **S8** with real devices and people; report |

**Go/no-go after week 1:** if neither B nor C fits a modest hall PC with E2EE, rethink (e.g. encoding on the edge node/hub).

## Deliverables

- Local report per question: measurements, chosen publish path, connectivity matrix.
- Updated plan with measured bandwidth/capacity numbers.
- Spike code kept for reference, never merged as product code.
