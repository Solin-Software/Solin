# Hosted meetings on Galene: design and build plan

Solin hosts the hybrid congregation meeting itself, in place of Zoom.
- **Engine:** a [Galene](https://galene.org/) sidecar on the hall PC.
- **Internet access:** through a small self-hostable **hub**.
- **Out of scope for this engine:** end-to-end encryption and community relays (see `spikes/galene/COMPARISON.md`).

The spike behind every number here is on this branch: `spikes/galene/` (G1–G6) and `spikes/hosted-meetings/s5_hub/` (hub).

## Meeting rules (from the JW guidance)

- **Hall monitors:** remote commenters are never shown there; only remote part-holders may be.
- **Commenting:** no self-unmute during the meeting. The Solin operator calls on a commenter, and only the mic is opened.
- **Raised hands:** shown to the conductor on a read-only **stage page** on their own device.
- **Access:** lobby with admit; the meeting can be locked. The attendant may be remote, as an operator on another device.
- **Recording:** none.
- **Scope:** no phone dial-in, no breakout rooms.

## Architecture

```
                      ┌──────────────── hub (VPS, self-hostable) ────────────────┐
 attendee browser ──► │ Caddy: TLS for <hall>.<hub-domain>  ──►  frps (tunnel)    │
   (HTTPS + WSS)      │ coturn: TURN UDP/TCP, time-limited credentials           │
        │             └───────────────────────────────▲──────────────────────────┘
        │ media (UDP; direct via IPv6/srflx,          │ hall dials out (frpc)
        │ else relayed through the hub's TURN)        │
        ▼                                             │
 ┌──────────────────────────────── hall PC (Solin) ───┴──────────────────────────┐
 │ Meeting web server (aiohttp, one origin):                                     │
 │   /join/<meeting>  name → lobby (waits)  → admitted: signed Galene token      │
 │   /m/<meeting>     attendee client (program, raise hand, mic when called on,  │
 │                    auto-reconnect)                                            │
 │   /stage/<meeting> conductor's raised-hands list (read-only)                  │
 │   /ws              proxied to Galene's WebSocket                              │
 │ Galene sidecar (127.0.0.1): group per meeting, tokens only, no recording      │
 │ Operator client (Python, Galene `op`): admit, lock, call on/off, kick, hands  │
 │ libobs sidecar: "meeting" output — x264 (scenecut=0) + Opus, packets →        │
 │   solin-galene-bridge publish ──WHIP──► Galene                                │
 │ solin-galene-bridge receive ◄── Galene: commenter audio → hall output device, │
 │   part-holders' H.264 → loopback RTP slots → "Remote participant" sources    │
 │ Qt: Live meeting page (console), Meeting hosting settings                     │
 └───────────────────────────────────────────────────────────────────────────────┘
```

### Hall side (Solin)

**Galene supervisor** (`core/conference/galene/`)
- Locates the binary: env var, then `<app>/bin`, then `build/galene/<platform>`.
- Writes a per-run data directory:
  - `config.json` with `allowOrigin` set to the web server;
  - `ice-servers.json` with the hub's STUN/TURN and `hmac-sha1` credentials;
  - one group per meeting: `h264` + `opus`, `allow-recording: false`, an `authKeys` HS256 key generated per meeting, no password users.
- Starts Galene with `-insecure -http 127.0.0.1:<port> -turn ""`, health-checks it, and restarts it with backoff (like the libobs sidecar supervision).

**Tokens.**
- Short-lived JWTs (`sub` = display name, `aud` = group URL, `permissions`).
- Attendees get `[]` (observe), part-holders `["present", "message"]`, the operator client `["op", "present", "message"]`.

**Operator client.**
- An asyncio Galene protocol client (aiohttp WebSocket) with an `op` token. It tracks users and their data (raised hands), and sends `present`, `unpresent`, `kick`, `lock` and `unlock`.
- On top of it, a Qt-free `MeetingCore` holds the rules:
  - lobby queue, admit / admit all;
  - mic lock, with call-on opening the mic only;
  - hands queue in arrival order;
  - rename (issue a new token, then the client rejoins), remove (block new tokens for that device);
  - attendance.

**Meeting web server.**
- aiohttp on its own thread, like `core/remote_control/server.py`.
- Lobby state is held in memory and pushed to waiting browsers over a WebSocket.
- `/ws` is proxied to Galene, so attendees see a single origin: the hub tunnel forwards one port, and Galene's strict CSP and origin checks never get in the way.

**Program publisher.**
- A new output in the libobs sidecar, driven by the existing `set_output_enabled` / output-health pattern.
- Encoders: `obs_x264` (`scenecut=0`, `bframes=0`, CBR, keyint 2 s, `repeat_headers`) and Opus, on a `null_output`.
- The packet callback pipes frames, stamped with `sys_dts_usec`, to `solin-galene-bridge publish` over stdin.
- The audio comes from the meeting mix track: media + hall feed, no loopback captures.
- Quality setting: 360p (default, ~0.5 Mbps/viewer), 720p (~1.8 Mbps) or audio only.

**`solin-galene-bridge`** (Go, Pion; the spike's `whippub` + `hallrx`):
- `publish`: WHIP into Galene, capture-clock RTP timestamps and sender reports, NACK.
- `receive`: joins as the hall, decodes the commenter's Opus with PLC, writes PCM.
- The hall plays that PCM on the selected output device, the one wired to the mixer as the remote-audio channel (mix-minus, as with Zoom today).
- `receive --video-ports`: also forwards remote part-holders' H.264, undecoded, to two loopback RTP **video slots** (below).

**Remote part-holders' video** (`core/hosted_meeting/participant_video.py`):
- Two slots on fixed loopback ports (47110, 47112). The bridge gives each part-holder's camera track the first free slot (a returning user gets their old slot back), and rewrites it to one continuous RTP stream per slot: payload type 96, a fixed SSRC, continuous sequence numbers and timestamps. A new part-holder in a slot is just a new keyframe for the reader.
- Scenes show a slot with a **Remote participant** source (`SourceKind.REMOTE_PARTICIPANT`, slot 1 or 2): one shared libobs `ffmpeg_source` per slot, reading the slot's SDP file. The Scenes "+ Source" menu adds participant 1, participant 2, or both side by side. The operator makes a one-person and a two-person scene and routes them to the program and the hall projection like any scene.
- Keyframes: `ffmpeg_source` drops the first keyframe after it opens, so the bridge sends PLI at 0, 0.5, 1.5 and 4 s after a slot starts (or resumes after 2 s of silence), then every 10 s.
- Leaving: the bridge's `video … stopped` event goes through `HostedMeetingService` to the sidecar (`set_participant_video`), which stops the slot's source. The source clears instead of freezing on the last frame, and starts listening again a second later. The Live meeting page shows who holds each slot.
- Attendees see part-holders inside the program. Their page shows only the program's video and plays every other stream (commenters, part-holders) as audio. Part-holders' cameras go up under the label `participant`, which attendees request with no tracks, so they never download them separately.

**UI.**
- **Live meeting page:** status, attendance, lobby admit / admit all, lock mics, raised-hands queue (call on / done / lower), rename / remove, share invitation (link + QR).
- **Meeting hosting settings group:** host with Zoom or this computer, hall name, quality, remote-audio output device, hub address + hall key.

**Attendee web client** (vendored `protocol.js` + our page, from `spikes/galene/g2/web/viewer.js`):
- name entry → lobby → program;
- raise / lower hand;
- mic UI only while the operator has granted `present`, audio only;
- auto-reconnect with backoff, plus an immediate reconnect when visible again;
- media session, optional screen wake lock.

### Hub (VPS, run by the project or self-hosted)

Adapted from the S5 bundle (`spikes/hosted-meetings/s5_hub/`):
- **Caddy:** on-demand TLS for `<hall>.<hub-domain>`, gated by an `ask` service that approves only halls currently connected to frps.
- **frps:** each hall's `frpc` dials out and forwards one TCP port, the meeting web server. Raw TCP forwarding, not frp's HTTP vhost, which closed long WebSockets after ~60 s in S5. No `heartbeatTimeout` with `tcpMux`, which dropped tunnels every 90 s in S5.
- **coturn:** dual-stack UDP + TCP 3478 with `use-auth-secret`; Solin hands its credentials to Galene, which hands them to attendees.
- **Auth:** a hub token per hall. Phase 1 uses frps' shared token plus a per-hall subdomain reservation; registration can come later.
- **Statelessness:** the hub holds no meeting state; everything lives on the hall PC.
- **Deployment:** a `docker compose up` bundle with an IP-only test variant (no DNS).

Solin supervises `frpc` next to Galene and shows the hall's public URL once the tunnel is up.

## Build phases

1. **Hall core, working on the LAN**
   - Galene supervisor + tokens; operator client + `MeetingCore`.
   - Meeting web server with join, lobby, attendee and stage pages, plus the `/ws` proxy.
   - Bridge `publish` + the libobs meeting output.
   - Bridge `receive` → remote-audio device.
   - Settings group + Live meeting page.
   - Unit tests (core, tokens, protocol client, web server); live integration tests against a real Galene.
2. **Hub:** the compose bundle (Caddy + frps + coturn + ask) and its README; `frpc` supervision in Solin; hub TURN in Galene's ICE config; invitation link and QR; a test against a real VPS.
3. **Packaging:**
   - fetch/build scripts for Galene, frpc and the bridge (Linux, Windows, macOS; cgo libopus);
   - staging in `<app>/bin`;
   - CI.
4. **Polish:** mic sync offset setting (camera latency), screen wake lock option, audio retransmission / RED for lossy Wi-Fi, attendance export.

## Decisions taken from the spikes

- **Codec:** H.264 constrained baseline + Opus; x264 `scenecut=0`. All tested browsers play it (G2).
- **Lobby:** in Solin. Tokens are issued on admit, so nobody touches Galene before admission (G4).
- **Commenters:** audio-only is enforced by our client. The operator can `unpresent` or kick anyone sending video (G4).
- **Reconnect:** the client must reconnect on its own. Android drops background pages' connections about 17 s after the screen locks (reconnect section in `spikes/galene/RESULTS.md`).
- **Audio clock:** stamped by sample count, never by send time (G5).
- **TURN:** must be dual-stack. Media tries direct first; IPv6 often works directly (G6).

## Status (2026-10-23)

All four phases are built on `spike/galene`.

| Phase | What exists | Verified by |
|---|---|---|
| 1. Hall core | `core/hosted_meeting/`: tokens, Galene config + supervisor, operator client, `MeetingCore` rules, meeting runtime (web server, lobby, `/ws` proxy, stage feed), `HostedMeetingService`, frpc tunnel, settings + hub code. libobs sidecar: `libobs_meeting_output.py` + `start/stop_meeting_output` IPC. UI: "Hosted meetings" settings group, Live meeting console window opened from the quick-toolbar meeting button. Attendee and stage pages in `resources/hosted_meeting_web/` (en / pt / es). Bridge: `tools/solin-galene-bridge/`. | Unit tests (tokens, config, core). Live tests against real Galene: runtime journey, service lifecycle. Native libobs test of the meeting output. Sidecar IPC round trip. Console QML render test. |
| 2. Hub | `deploy/hub/` (Caddy + frps + coturn + ask, `add_hall.py` prints the connection code). | Local smoke test (`deploy/hub/scripts/smoke_local.sh`). A real GCP VM (below). An end-to-end meeting through the internet: headless Chromium joined through `https://hall-teste.<hub>/j/…`, was admitted, played the program, was called on (mic opened), was released, and saw "meeting ended". Galene's relay self-test against the hub's TURN. |
| 3. Packaging | `scripts/fetch_hosted_meeting_helpers.py` (Galene from source, frpc release with checksum), `scripts/build_galene_bridge.py`, `scripts/stage_hosted_meeting_helpers.py` (into `<app>/bin` + licences), Nuitka data dir for the web pages in Linux/Windows/macOS, CI: Go + libopus (apt / MSYS2 / Homebrew). | Staging run locally on Linux. CI and the Windows/macOS builds have not run yet (the branch must be pushed). |
| 4. Polish | Hall-feed delay setting (camera latency, `obs_source_set_sync_offset`), copy attendance, optional screen wake lock and auto-reconnect in the attendee app. | Unit and live tests. |

### Running it (development)

1. Build the helpers once:

   ```
   python scripts/fetch_hosted_meeting_helpers.py
   python scripts/build_galene_bridge.py
   ```

   This needs Go, git, gcc, pkg-config and libopus-dev.
2. Launch Solin from the worktree (`PYTHONPATH=<worktree>/src`).
3. In Settings → Integrations → Hosted meetings:
   - choose "This computer (Solin)";
   - set the meeting name;
   - optionally paste a hub connection code.
4. The quick toolbar's meeting button opens the Live meeting console → **Start meeting**. Share the invitation link; open the stage link on the conductor's device.

### Hub deployment

See `deploy/hub/README.md`. On a fresh VPS:

```
docker compose up -d
python3 scripts/add_hall.py <hall> --env hub.env
```

The second command prints the hall's connection code.

### Known limits and next steps

- **Android, screen locked:** audio stops about 17 s after locking, because the browser drops the page's connection. It resumes on unlock. The fix is an installed app or Galene keeping media alive for a grace period.
- **Lossy Wi-Fi:** Galene has no audio NACK or RED. The bridge sends Opus with in-band FEC, which helps only with isolated losses. A small Galene patch could add audio NACK.
- **Program quality:** a single quality layer. Galene's two-layer simulcast is not sent yet.
- **Remote part-holders' video:** at most two at a time, and the layout is chosen by the operator's scene (no automatic one/two-person switch). A third camera is not forwarded.
- **Not yet tested on real devices:** Safari/iOS and old Android.
- **Not yet built or run:** TURN over TLS (5349) on the hub; Windows/macOS builds; CI.
